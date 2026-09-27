"""Camera platform for Vimar Intercom."""

from __future__ import annotations

import asyncio
import base64

import logging
from datetime import timedelta

from aiohttp import web

from homeassistant.components.camera import Camera
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import media_handler as media
from .const import DOMAIN
from .device import device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([VimarIntercomCamera(hub, entry.entry_id, hass)])


# Immagine di attesa 320x240, usata quando non c'è un fotogramma vero da dare.
IDLE_IMAGE = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjI4LjEwMgD/2wBDAAgYGBwYHCEhISEhISckJygoKCcnJycoKCgrKyszMzMrKysoKCsrMDAzMzc5NzQ0MzQ5OTw8PEhIRUVUVFdnZ3z/xABLAAEBAAAAAAAAAAAAAAAAAAAABwEBAAAAAAAAAAAAAAAAAAAAABABAAAAAAAAAAAAAAAAAAAAABEBAAAAAAAAAAAAAAAAAAAAAP/AABEIAPABQAMBIgACEQADEQD/2gAMAwEAAhEDEQA/AI8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/2Q=="
)


# Quanto si aspetta che la chiamata sia viva prima di rinunciare.
CALL_WAIT = 12.0
# E quanto, poi, che la targa mandi i parametri video. Ne manda in fretta:
# misurato, SPS, PPS e un keyframe completo arrivano circa quattro decimi
# dopo la risposta alla chiamata. Se non arrivano, si va in solo audio
# invece di far aspettare chi sta alla porta.
PARAMS_WAIT = 2.0


async def prepare_homekit_source(hub, hass) -> tuple[str | None, dict | None]:
    """Sorgente per l'ffmpeg che HomeKit lancia per conto suo.

    Gli si dà un SDP che descrive l'RTP, non un MPEG-TS via HTTP.

    Il giro lungo costava carissimo. Quell'ffmpeg, davanti a un flusso
    MPEG-TS, lo ANALIZZA prima di emettere qualunque cosa: cinque secondi
    pieni, pagati sempre, anche quando i codec sono noti — e li paga per
    TUTTE le tracce insieme, quindi l'audio restava fermo ad aspettare che
    il video fosse pronto. Chi suona alla porta non sentiva niente per
    sette secondi per colpa dell'immagine, non della voce.

    Con un SDP i codec sono dichiarati, non c'è niente da scoprire, e le
    due tracce tornano a essere due flussi RTP indipendenti — che è la
    forma in cui la targa ce li manda. Sparisce anche tutto ciò che il
    transito per MPEG-TS si portava dietro: marcatori a 33 bit che
    girano, due orologi RTP da ricucire, un mux seguito subito da un
    demux della stessa roba.

    Qui si fa partire anche la chiamata: questo metodo è ciò che Home
    Assistant chiama quando HomeKit chiede il flusso, e senza la GET su
    /av non ci sarebbe più nient'altro a farla partire.
    """
    media.open_timeline_start()
    await hub.stream_opened()

    waited = 0.0
    while not hub.in_call and waited < CALL_WAIT:
        await asyncio.sleep(0.02)
        waited += 0.02
    media.mark("chiamata attiva")
    if not hub.in_call:
        _LOGGER.warning("HomeKit ha chiesto il flusso ma la chiamata non è partita")
        # Lo spettatore contato all'inizio va restituito, o resta a carico
        # di una sessione che non esiste: la rete di sicurezza che chiude
        # la chiamata "senza spettatori" non scatterebbe più.
        await hub.stream_closed()
        return None, None

    # I parametri video arrivano col primo keyframe, che la targa manda
    # subito: aspettarli qui evita di dichiarare un video che ffmpeg non
    # saprebbe decodificare, ma senza bloccare l'audio più del necessario.
    waited = 0.0
    while not media.video_ready() and waited < PARAMS_WAIT:
        await asyncio.sleep(0.02)
        waited += 0.02

    # L'inoltro dell'RTP lo accende il nostro ffmpeg, che serve comunque
    # per /av, per la scheda telecamera e per le istantanee. Prima lo
    # accendeva la GET su /av di HomeKit, che adesso non arriva più.
    await media.start_av_ffmpeg()

    # Da qui in poi qualcuno deve accorgersi di quando la vista si chiude:
    # è l'unico modo per riagganciare subito invece di lasciare il posto
    # esterno occupato fino al suo timeout.
    if not media.video_ready():
        # Chiamata senza immagini — succede quando è il Tab a chiamare
        # Home Assistant. Qui la scorciatoia non si può prendere: Home
        # Assistant mette sempre "-map 0:v:0" nella riga di comando, e un
        # SDP di solo audio la fa fallire in partenza ("Stream map ''
        # matches no streams"), quindi non parte nemmeno l'audio. Si torna
        # al percorso lungo, che una traccia video nera se la genera.
        # Loopback, con la porta e lo schema veri di Home Assistant (8123 e
        # http non valgono dappertutto). Non l'internal_url: dietro un proxy
        # (add-on NGINX) la richiesta porterebbe X-Forwarded-For e l'endpoint,
        # che accetta solo chiamate dirette, la rifiuterebbe. In più l'URL è
        # firmato, perché /av vuole l'autenticazione.
        http = hass.http
        scheme = "https" if getattr(http, "ssl_certificate", None) else "http"
        base = f"{scheme}://127.0.0.1:{getattr(http, 'server_port', None) or 8123}"
        _LOGGER.info("HomeKit: chiamata senza video — passo da /av")
        # Su questa strada è la richiesta HTTP a contare lo spettatore, e
        # a scalarlo quando finisce. Il nostro va quindi restituito, o il
        # conto non tornerebbe mai a zero e la chiamata non si chiuderebbe
        # più da sola.
        await hub.stream_closed()
        # Anche qui vanno passati gli argomenti d'ingresso, o quell'ffmpeg
        # analizza il flusso con i suoi valori di default: misurati 23
        # secondi prima di emettere un fotogramma. Su questa strada il
        # video è quello nero generato da noi, quindi c'è dal primo
        # istante e un'analisi breve è sicura.
        signed = async_sign_path(
            hass, "/api/vimar_intercom/av", timedelta(minutes=1),
            use_content_user=True)
        return (
            "-analyzeduration 1000000 -probesize 500000 "
            f"-i {base}{signed}"
        ), None

    # Porte e SDP tutti suoi: finché li condivideva con la sessione
    # precedente, quella che moriva si portava dietro anche questa.
    session = media.reserve_homekit_session()
    if session is not None:
        await hass.async_add_executor_job(media._write_homekit_sdp_for, session)
    if session is None:
        _LOGGER.warning("Nessuna coppia di porte libera per HomeKit")
        await hub.stream_closed()
        return None, None
    hub.start_homekit_watch(session)
    # Appena il suo ffmpeg apre la porta gli si rigioca l'ultimo keyframe:
    # quello di apertura chiamata passa una settantina di millisecondi
    # prima che l'inoltro sia acceso, e senza di lui non c'è niente di
    # decodificabile fino al successivo, tre secondi dopo.
    hub.spawn(media.feed_keyframe_when_ready(session))
    path = session["sdp"]
    _LOGGER.info("HomeKit: sessione %s, SDP %s", session["id"], path)
    media.mark("SDP restituito a Home Assistant")
    return (
        "-protocol_whitelist file,udp,rtp "
        # Con un SDP che dichiara tutto — PCMU per l'audio, H.264 con
        # sprop-parameter-sets per il video — non resta niente da
        # scoprire, e il tempo di analisi è tempo morto: ffmpeg lo
        # aspetta tutto comunque. Misurato leggendo questo stesso SDP:
        # con 1000000 il primo audio esce dopo 3,00 s, con 300000 esce
        # dopo 0,00 s, e la geometria del video si trova lo stesso.
        "-fflags +genpts -analyzeduration 300000 -probesize 500000 "
        # Il relay perde pacchetti, e per ognuno ffmpeg fermava l'audio 100 ms
        # ad aspettarlo (misurato: 101 ms). Due-tre fermate al secondo e il
        # telefono allunga il suo buffer: ritardo. 40 ms coprono i pacchetti
        # solo in ritardo; quelli persi diventano un buco di 20 ms.
        "-max_delay 40000 "
        f"-i {path}"
    ), session


class VimarIntercomCamera(Camera):
    """Intercom camera — streams video from SIP/RTP pipeline.

    Uses MJPEG directly (no RTSP/WebRTC). When the stream is opened
    (e.g. from Apple Home), the hub auto-calls the intercom.
    """

    _attr_has_entity_name = False
    _attr_name = "Intercom"
    _attr_icon = "mdi:doorbell-video"

    def __init__(self, hub, entry_id: str, hass: HomeAssistant) -> None:
        super().__init__()
        self._hub = hub
        self._hass = hass
        self._attr_unique_id = f"{entry_id}_camera"
        self._attr_device_info = device_info(entry_id)

    # Niente proprietà "available": restituiva False ogni volta che non c'era
    # video in corso — cioè quasi sempre, e per tutta la durata di una chiamata
    # senza immagini. In accessory mode HomeKit non la guarda (il percorso
    # dello streaming controlla is_on), ma l'entità risultava non disponibile in
    # Home Assistant fra una chiamata e l'altra, e in un bridge avrebbe fatto
    # rispondere -70402 a ogni lettura. Un impianto senza video non deve creare
    # l'entità, non renderla perennemente spenta.

    @property
    def is_streaming(self) -> bool:
        """True when there's an active SIP call with video."""
        return self._hub.in_call and self._hub.video_available

    @property
    def is_on(self) -> bool:
        return True

    # Qui si dichiarava StreamType.MJPEG, ma l'MJPEG non esiste su questo
    # impianto: il video esce in H.264 e handle_async_mjpeg_stream non ha una
    # sorgente di fotogrammi da cui partire. Il risultato era una scheda
    # telecamera che prometteva un flusso e non mostrava mai nulla. Senza la
    # dichiarazione, Home Assistant ripiega sulle istantanee periodiche di
    # async_camera_image, che adesso sono immagini vere.

    async def stream_source(self) -> str | None:
        """Sorgente per l'ffmpeg di HomeKit: vedi prepare_homekit_source."""
        args, _session = await prepare_homekit_source(self._hub, self._hass)
        return args
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest cached JPEG frame (no auto-call).

        This is called by Apple Home for the thumbnail on the home screen.
        We only return whatever frame we already have — no SIP call triggered.
        The live stream (user taps camera) goes through stream_source/MJPEG view
        which triggers auto-call there.

        L'immagine vera la scrive ffmpeg durante le chiamate, un fotogramma al
        secondo; qui si legge l'ultima disponibile. Resta visibile anche a
        chiamata finita, ed è voluto: sulla scheda e dentro la notifica del
        campanello si vede chi c'era, non un rettangolo nero.

        Se non c'è ancora stata nessuna chiamata — o il file è sparito col
        riavvio — si ripiega sull'immagine di attesa. Restituire None faceva
        rispondere 500 a Home Assistant, e l'app Casa si ritrovava una scheda
        telecamera senza nulla da mostrare.
        """
        # Solo un JPEG intero: un file a zero byte (ffmpeg che l'ha aperto e
        # poi non è partito) non deve cancellare l'ultima immagine buona.
        snapshot = await self._hass.async_add_executor_job(media.read_snapshot)
        return snapshot or IDLE_IMAGE

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse | None:
        """Flusso per la scheda telecamera di Home Assistant.

        Restituire ``None`` qui fa rispondere **502** al proxy di Home
        Assistant, ed è quello che la scheda mostrava. Non c'è un vero MJPEG
        su questo impianto — il video esce in H.264 — ma un'istantanea al
        secondo è comunque una scheda che funziona, e non chiama nessuno:
        mostra l'ultima immagine disponibile, come la scheda dell'app Casa.
        """
        response = web.StreamResponse()
        response.content_type = "multipart/x-mixed-replace; boundary=frame"
        await response.prepare(request)
        try:
            while True:
                frame = await self.async_camera_image()
                if frame:
                    await response.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                        + frame + b"\r\n"
                    )
                await asyncio.sleep(1.0)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("Scheda telecamera chiusa: %s", e)
        return response
