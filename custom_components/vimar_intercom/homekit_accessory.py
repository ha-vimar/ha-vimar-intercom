"""Il citofono come accessorio HomeKit nostro, con l'audio nei due sensi.

Il ponte HomeKit di Home Assistant non sa rispondere al citofono. La sua
telecamera-campanello ha un Microphone (la voce dalla strada al telefono) e
uno Speaker — l'app Casa mostra infatti il pulsante per parlare — ma quando il
telefono manda il SetupEndpoints gli si rispondono le SUE STESSE porte come se
fossero nostre, e sul Pi nessuno le ascolta. Il proxy audio di Home Assistant
apre quella porta solo per trasmettere: quello che il telefono ci manda
arriva a un socket che non legge nessuno. Il corriere del 23 settembre si
sentiva benissimo; lui, di chi rispondeva, non sentiva niente.

Qui l'accessorio lo pubblichiamo noi, con pyhap — la stessa libreria che usa
Home Assistant, già installata — senza toccare nulla di Home Assistant:

- nel SetupEndpoints si dichiarano porte che ascoltiamo davvero;
- l'audio del telefono si decifra (SRTP, con la chiave che il telefono stesso
  ci ha dato), si decodifica e va al posto esterno per la stessa strada che
  usa già il microfono della scheda web (``media.send_audio``);
- campanello, video, voce e serratura del cancello stanno in un accessorio
  solo, così nella vista dal vivo della notifica ci sono insieme «Parla» e
  «Apri».

Il video e l'audio dalla strada al telefono restano quelli collaudati: stessa
preparazione della chiamata della telecamera di Home Assistant
(``prepare_homekit_source``), stesso SDP, stesso rigioco del keyframe.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import secrets
import shlex
import socket
import struct
import time
from uuid import UUID

from aiohttp import web
from pyhap import tlv
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import (
    SETUP_ADDR_INFO,
    SETUP_SRTP_PARAM,
    SETUP_STATUS,
    SETUP_TYPES,
    SRTP_CRYPTO_SUITES,
    VIDEO_CODEC_PARAM_LEVEL_TYPES,
    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES,
    Camera,
)
from pyhap.const import CATEGORY_VIDEO_DOOR_BELL
from pyhap.util import to_base64_str

from homeassistant.components import persistent_notification
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import media_handler as media
from .camera import IDLE_IMAGE, prepare_homekit_source
from .const import CONF_HOMEKIT_SMOOTH, DEFAULT_HOMEKIT_SMOOTH, DOMAIN
from .homekit_audio import AudioBridge, ffmpeg_binary, log_stderr
from .homekit_transcode import Transcoder
from .homekit_video import DirectVideo

_LOGGER = logging.getLogger(__name__)

ACCESSORY_NAME = "Citofono"
# Porta del server HAP. Fuori dall'intervallo che Home Assistant assegna ai
# suoi ponti (21063 e seguenti) e diversa da quella del vecchio accessorio
# telecamera (21077): quello va tolto, ma se restasse non si pestano i piedi.
HOMEKIT_PORT = 21099

# Il cancello apre la stessa porta della serratura dell'integrazione (lock.py):
# la targa configurata come door_target, con il comando di default.
# Il relè richiude da solo; in HomeKit si torna a «chiuso» dopo questo tempo.
GATE_RELOCK_SECONDS = 5.0
# LockCurrentState: 0 aperta, 1 chiusa, 3 sconosciuto. Il citofono non sa se il
# cancello è chiuso: manda solo l'impulso all'elettroserratura. Dire "chiuso"
# dopo cinque secondi era un'affermazione falsa, e iOS la notificava.
LOCK_UNSECURED, LOCK_SECURED, LOCK_UNKNOWN = 0, 1, 3


# Quanto si aspetta un keyframe intero in memoria prima di aprire il video
# diretto. La targa lo manda circa 0,45 s dopo la risposta; con il media in
# anticipo dello squillo c'è già.
KEYFRAME_WAIT = 2.0


# Risoluzioni offerte al telefono. Il video della targa è sempre 320x240 e lo
# si copia senza ricodificarlo: la lista serve solo perché iOS scelga qualcosa
# (l'orologio chiede 320x240, l'iPhone di solito di più). Stessa scelta del
# ponte di Home Assistant, che con questa pipeline ha sempre funzionato.
_RESOLUTIONS = [
    [320, 240, 15], [320, 240, 30], [480, 270, 30], [480, 360, 30],
    [640, 360, 30], [640, 480, 30], [1024, 576, 30], [1024, 768, 30],
    [1280, 720, 30], [1280, 960, 30], [1920, 1080, 30],
]

_PIN_NOTIFICATION = f"{DOMAIN}_homekit_pairing"
# Stato di HomeKit condiviso fra le entry (la vista del QR si registra una
# volta sola). Fuori da hass.data[DOMAIN], che contiene solo le entry: lì una
# chiave in più faceva trovare ai servizi un booleano al posto dell'hub.
_HK_DATA = f"{DOMAIN}_homekit"


# ─── L'accessorio ───────────────────────────────────────────────────


class VimarDoorbell(Camera):
    """Videocitofono HomeKit: video, voce nei due sensi, campanello, cancello."""

    category = CATEGORY_VIDEO_DOOR_BELL

    def __init__(self, driver, name: str, *, hass: HomeAssistant, hub,
                 stream_address: str, serial: str, smooth: bool = False) -> None:
        options = {
            "video": {
                "codec": {
                    "profiles": [VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["BASELINE"]],
                    "levels": [
                        VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_1"],
                        VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_2"],
                        VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE4_0"],
                    ],
                },
                "resolutions": _RESOLUTIONS,
            },
            "audio": {
                "codecs": [
                    {"type": "OPUS", "samplerate": 16},
                    {"type": "OPUS", "samplerate": 24},
                ],
            },
            "srtp": True,
            "address": stream_address,
            # Quante viste insieme (iPhone, Mac, orologio). Dal lato del
            # citofono le coppie di porte per sessione sono tre.
            "stream_count": 3,
        }
        super().__init__(options, driver, name)
        self._hass = hass
        self._hub = hub
        # Video ricodificato sul Pi (più fluido) invece che diretto (più veloce).
        self._smooth = smooth
        self._transcoder: Transcoder | None = None
        self._transcoder_lock = asyncio.Lock()
        self.set_info_service(
            manufacturer="Vimar", model="Videocitofono",
            serial_number=serial, firmware_revision="1.0")

        # Il campanello, servizio principale: è lui che fa di questo
        # accessorio un videocitofono, con la notifica e l'istantanea.
        serv_doorbell = self.add_preload_service("Doorbell")
        self.set_primary_service(serv_doorbell)
        self._char_ring = serv_doorbell.configure_char(
            "ProgrammableSwitchEvent", value=0)
        # Lo stesso squillo come pulsante: serve alle automazioni di Casa.
        serv_switch = self.add_preload_service("StatelessProgrammableSwitch")
        self._char_ring_switch = serv_switch.configure_char(
            "ProgrammableSwitchEvent", value=0, valid_values={"SinglePress": 0})

        # L'altoparlante del posto esterno: con questo l'app Casa mostra
        # «Parla». Il muto e il volume li chiede il telefono; la voce vera
        # arriva per l'RTP, vedi AudioBridge.
        serv_speaker = self.add_preload_service("Speaker", chars=["Volume"])
        serv_speaker.configure_char("Mute", value=False)
        serv_speaker.configure_char("Volume", value=100)

        # Il cancello, nello stesso accessorio: nella vista dal vivo si apre
        # senza cercarlo altrove.
        serv_lock = self.add_preload_service("LockMechanism", chars=["Name"])
        serv_lock.configure_char("Name", value="Cancello")
        self._char_lock_current = serv_lock.configure_char(
            "LockCurrentState", value=LOCK_UNKNOWN)
        self._char_lock_target = serv_lock.configure_char(
            "LockTargetState", value=1, setter_callback=self._set_lock_target)

    # ── campanello ──

    def ring(self) -> None:
        """Qualcuno ha suonato: la notifica sul telefono parte da qui."""
        self._char_ring.set_value(0)
        self._char_ring_switch.set_value(0)
        _LOGGER.info("HomeKit: squillo inoltrato al campanello")
        if self._smooth:
            # Il media in anticipo porta il video già adesso: il ricodificatore
            # si scalda mentre chi è in casa prende il telefono.
            self._hub.spawn(self._prewarm())

    async def _prewarm(self) -> None:
        waited = 0.0
        while not media.video_ready() and waited < 4.0:
            await asyncio.sleep(0.05)
            waited += 0.05
        if media.video_ready():
            await self._ensure_transcoder()

    # ── ricodifica ──

    async def _ensure_transcoder(self) -> Transcoder | None:
        """Il ricodificatore della chiamata in corso, avviato se serve."""
        async with self._transcoder_lock:
            if self._transcoder and self._transcoder.running:
                return self._transcoder
            if self._transcoder:
                # Quello di prima è morto: via il suo aggancio al video e i
                # suoi socket, prima di accenderne un altro.
                dead, self._transcoder = self._transcoder, None
                media.remove_video_sink(dead.feed)
                await dead.stop()
            sps, pps = media._parameter_sets()
            tc = Transcoder(sps, pps)
            ok = await tc.start(media.gop_for_direct_video,
                                on_ready=lambda: media.add_video_sink(tc.feed))
            if not ok:
                media.remove_video_sink(tc.feed)
                await tc.stop()
                _LOGGER.warning("HomeKit: ricodifica non partita, si usa il video diretto")
                return None
            self._transcoder = tc
            return tc

    async def _stop_transcoder(self) -> None:
        async with self._transcoder_lock:
            tc, self._transcoder = self._transcoder, None
            if tc:
                media.remove_video_sink(tc.feed)
                await tc.stop()

    # ── cancello ──

    def _set_lock_target(self, value: int) -> None:
        if value == 0:
            self._hub.spawn(self._open_gate())

    async def _open_gate(self) -> None:
        """Manda l'impulso di apertura. Si notifica "aperto"; poi si torna a
        "sconosciuto", non a "chiuso", e il bersaglio torna a "chiuso" perché
        il prossimo tocco sia di nuovo un'apertura."""
        ok, msg = await self._hub.async_door()
        if ok:
            _LOGGER.info("HomeKit: cancello aperto")
            self._char_lock_current.set_value(LOCK_UNSECURED)
            await asyncio.sleep(GATE_RELOCK_SECONDS)
        else:
            _LOGGER.error("HomeKit: apertura del cancello non riuscita: %s", msg)
        self._char_lock_target.set_value(LOCK_SECURED)
        self._char_lock_current.set_value(LOCK_UNKNOWN)

    # ── istantanea ──

    async def async_get_snapshot(self, image_size) -> bytes:
        return await self._hass.async_add_executor_job(_read_snapshot)

    # ── sessione di streaming ──

    def set_endpoints(self, value, stream_idx=None):
        """Come pyhap, ma con porte NOSTRE, su cui ascoltiamo davvero.

        pyhap rimanda al telefono le porte del telefono stesso come se fossero
        dell'accessorio, e non apre niente. Per il video andava bene per
        caso: ffmpeg si lega lo stesso numero con ``localrtpport``. Per
        l'audio no, ed è la ragione per cui nessuno sentiva chi rispondeva.
        """
        if stream_idx is None:
            stream_idx = 0
        objs = tlv.decode(value, from_base64=True)
        session_id = UUID(bytes=objs[SETUP_TYPES["SESSION_ID"]])

        addr = tlv.decode(objs[SETUP_TYPES["ADDRESS"]])
        address = addr[SETUP_ADDR_INFO["ADDRESS"]].decode("utf8")
        target_video_port = struct.unpack("<H", addr[SETUP_ADDR_INFO["VIDEO_RTP_PORT"]])[0]
        target_audio_port = struct.unpack("<H", addr[SETUP_ADDR_INFO["AUDIO_RTP_PORT"]])[0]

        video = tlv.decode(objs[SETUP_TYPES["VIDEO_SRTP_PARAM"]])
        audio = tlv.decode(objs[SETUP_TYPES["AUDIO_SRTP_PARAM"]])
        v_key = video[SETUP_SRTP_PARAM["MASTER_KEY"]]
        v_salt = video[SETUP_SRTP_PARAM["MASTER_SALT"]]
        a_key = audio[SETUP_SRTP_PARAM["MASTER_KEY"]]
        a_salt = audio[SETUP_SRTP_PARAM["MASTER_SALT"]]

        self._drop_stale_sessions()
        # L'audio: il socket lo apriamo subito, perché il telefono può
        # cominciare a mandare appena ha la risposta.
        a_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        a_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        a_sock.bind(("0.0.0.0", 0))  # noqa: S104
        a_sock.setblocking(False)
        local_a_port = a_sock.getsockname()[1]
        # Il video: il socket è nostro anche lui — ci passa il video diretto
        # e ci arriva l'RTCP del telefono. Se la chiamata non ha video, lo si
        # chiude e il numero lo riprende ffmpeg (localrtpport).
        v_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        v_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        v_sock.bind(("0.0.0.0", 0))  # noqa: S104
        v_sock.setblocking(False)
        local_v_port = v_sock.getsockname()[1]

        suite = SRTP_CRYPTO_SUITES["AES_CM_128_HMAC_SHA1_80"]
        video_srtp = tlv.encode(
            SETUP_SRTP_PARAM["CRYPTO"], suite,
            SETUP_SRTP_PARAM["MASTER_KEY"], v_key,
            SETUP_SRTP_PARAM["MASTER_SALT"], v_salt)
        audio_srtp = tlv.encode(
            SETUP_SRTP_PARAM["CRYPTO"], suite,
            SETUP_SRTP_PARAM["MASTER_KEY"], a_key,
            SETUP_SRTP_PARAM["MASTER_SALT"], a_salt)
        video_ssrc = int.from_bytes(os.urandom(3), "big")
        audio_ssrc = int.from_bytes(os.urandom(3), "big")

        res_address = tlv.encode(
            SETUP_ADDR_INFO["ADDRESS_VER"], self.stream_address_isv6,
            SETUP_ADDR_INFO["ADDRESS"], self.stream_address.encode("utf-8"),
            SETUP_ADDR_INFO["VIDEO_RTP_PORT"], struct.pack("<H", local_v_port),
            SETUP_ADDR_INFO["AUDIO_RTP_PORT"], struct.pack("<H", local_a_port))
        response = tlv.encode(
            SETUP_TYPES["SESSION_ID"], session_id.bytes,
            SETUP_TYPES["STATUS"], SETUP_STATUS["SUCCESS"],
            SETUP_TYPES["ADDRESS"], res_address,
            SETUP_TYPES["VIDEO_SRTP_PARAM"], video_srtp,
            SETUP_TYPES["AUDIO_SRTP_PARAM"], audio_srtp,
            SETUP_TYPES["VIDEO_SSRC"], struct.pack("<I", video_ssrc),
            SETUP_TYPES["AUDIO_SSRC"], struct.pack("<I", audio_ssrc),
            to_base64=True)

        self.sessions[session_id] = {
            "id": session_id,
            "stream_idx": stream_idx,
            "address": address,
            "v_port": target_video_port,
            "v_srtp_key": to_base64_str(v_key + v_salt),
            "v_ssrc": video_ssrc,
            "a_port": target_audio_port,
            "a_srtp_key": to_base64_str(a_key + a_salt),
            "a_ssrc": audio_ssrc,
            "local_v_port": local_v_port,
            "local_a_port": local_a_port,
            "a_sock": a_sock,
            "v_sock": v_sock,
            "created": time.monotonic(),
            # Avvio e chiusura della sessione si danno il turno: uno «stop»
            # che arriva mentre start_stream lavora aspetta che abbia finito,
            # e poi chiude anche quello che start_stream ha appena aperto.
            "lock": asyncio.Lock(),
        }
        _LOGGER.debug("HomeKit: sessione %s — telefono %s v%d a%d, nostre v%d a%d",
                      session_id, address, target_video_port, target_audio_port,
                      local_v_port, local_a_port)
        self._management[stream_idx].get_characteristic("SetupEndpoints").set_value(
            response)

    def _drop_stale_sessions(self) -> None:
        """Sessioni preparate e mai partite: il telefono ha cambiato idea."""
        now = time.monotonic()
        for sid, info in list(self.sessions.items()):
            if "proc" not in info and now - info.get("created", now) > 60:
                _close_quietly(info.get("a_sock"))
                _close_quietly(info.get("v_sock"))
                del self.sessions[sid]

    async def start_stream(self, session_info, stream_config) -> bool:
        async with session_info["lock"]:
            ok = await self._open_stream(session_info, stream_config)
        # Chiusa mentre partiva: per pyhap è partita e poi fermata. Con False
        # pyhap cancellava la sessione, e il suo «stop» già in corso la
        # cercava di nuovo (KeyError nel log di Home Assistant).
        return ok or bool(session_info.get("stopping"))

    async def _open_stream(self, session_info, stream_config) -> bool:
        """Apre video, audio e ffmpeg. Ogni pezzo va in session_info appena
        esiste, così la chiusura lo trova anche se arriva a metà."""
        args, media_session = await prepare_homekit_source(self._hub, self._hass)
        if session_info.get("stopping"):
            return False
        if not args:
            _close_quietly(session_info.get("a_sock"))
            _close_quietly(session_info.get("v_sock"))
            return False

        address = session_info["address"]
        v_port = session_info["v_port"]
        v_pt = _pt(stream_config.get("v_payload_type"), 99)
        # Video diretto quando la chiamata ha video. Altrimenti (chiamata
        # senza immagini, percorso /av) il video nero lo fa ancora ffmpeg.
        direct = media_session is not None and media.video_ready()
        video_mode = "via ffmpeg"
        if direct:
            video = DirectVideo(session_info["v_sock"], (address, v_port),
                                session_info["v_srtp_key"], session_info["v_ssrc"], v_pt)
            session_info["video"] = video
            await video.open()
            tc = await self._ensure_transcoder() if self._smooth else None
            if tc:
                waited = 0.0
                while not tc.gop.has_keyframe and waited < KEYFRAME_WAIT:
                    await asyncio.sleep(0.02)
                    waited += 0.02
            if tc and tc.gop.has_keyframe:
                # Niente await fino all'aggancio (vedi sotto).
                gop = list(tc.gop.packets)
                video.begin(gop, [])
                tc.add_sink(video.on_live)
                session_info["video_detach"] = lambda: tc.remove_sink(video.on_live)
                video_mode = "ricodificato"
                media.mark("video ricodificato al telefono", pacchetti=len(gop))
            else:
                waited = 0.0
                while not media.gop_has_keyframe() and waited < KEYFRAME_WAIT:
                    await asyncio.sleep(0.02)
                    waited += 0.02
                # Da qui fino all'aggancio niente await: il gruppo e il dal
                # vivo devono combaciare senza un pacchetto perso o doppio.
                prefix, gop = media.gop_for_direct_video()
                video.begin(gop, prefix)
                media.add_video_sink(video.on_live)
                session_info["video_detach"] = lambda: media.remove_video_sink(video.on_live)
                video_mode = "diretto"
                media.mark("video diretto al telefono", pacchetti=len(prefix) + len(gop))
        else:
            _close_quietly(session_info.pop("v_sock", None))

        rate = int(stream_config.get("a_sample_rate") or 16) * 1000
        bridge = AudioBridge(
            session_info["a_sock"],
            (session_info["address"], session_info["a_port"]),
            session_info["a_srtp_key"], rate, media.send_audio)
        session_info["bridge"] = bridge
        await bridge.start()
        if session_info.get("stopping"):
            return False

        a_pt = _pt(stream_config.get("a_payload_type"), 110)
        a_bitrate = int(stream_config.get("a_max_bitrate") or 24)
        a_ptime = int(stream_config.get("a_packet_time") or 20)
        video_out = [] if direct else [
            # Solo senza video diretto: il video nero del percorso /av.
            "-map", "0:v:0", "-an", "-c:v", "copy",
            "-payload_type", str(v_pt), "-ssrc", str(session_info["v_ssrc"]),
            "-f", "rtp",
            "-srtp_out_suite", "AES_CM_128_HMAC_SHA1_80",
            "-srtp_out_params", session_info["v_srtp_key"],
            f"srtp://{address}:{v_port}?rtcpport={v_port}"
            f"&localrtpport={session_info['local_v_port']}&pkt_size=1316",
        ]
        cmd = [
            ffmpeg_binary(), "-hide_banner", "-nostats", "-loglevel", "warning",
            *shlex.split(args),
            *video_out,
            # Audio dalla strada: Opus in chiaro al ponte, che lo cifra.
            "-map", "0:a:0", "-vn", "-c:a", "libopus", "-application", "lowdelay",
            "-ac", "1", "-ar", str(rate), "-b:a", f"{a_bitrate}k",
            "-frame_duration", str(a_ptime),
            "-payload_type", str(a_pt), "-ssrc", str(session_info["a_ssrc"]),
            # pkt_size generoso: un hub di casa (Apple TV, HomePod), che fa da
            # ponte quando il telefono è fuori casa, chiede pacchetti da 60 ms,
            # e un Opus da 60 ms supera i 176 byte di payload che lasciava il
            # 188 di Home Assistant. ffmpeg rifiutava il pacchetto e usciva
            # («Packet size 179 too large for max RTP payload size 176»), la
            # chiamata veniva chiusa e il video moriva dopo un secondo o due.
            "-f", "rtp", f"rtp://127.0.0.1:{bridge.encoder_port}?pkt_size=1316",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        except OSError as err:
            _LOGGER.error("HomeKit: ffmpeg non parte: %s", err)
            self._hub.spawn(self._close_session(session_info))
            return False
        session_info["proc"] = proc
        session_info["tasks"] = [
            asyncio.create_task(log_stderr(proc, "ffmpeg HomeKit")),
            asyncio.create_task(self._watch(session_info)),
        ]
        _LOGGER.info("HomeKit: flusso avviato verso %s (PID %d, video %s)", address,
                     proc.pid, video_mode)
        return True

    async def _watch(self, session_info) -> None:
        """Se ffmpeg muore da solo — chiamata chiusa dalla strada — lo si dice."""
        proc = session_info["proc"]
        await proc.wait()
        if session_info.get("stopping"):
            return
        _LOGGER.info("HomeKit: il flusso è finito (ffmpeg uscito con %s)", proc.returncode)
        await self._close_session(session_info)
        # La sessione resta: il telefono, avvisato qui, manderà il suo «stop»
        # e pyhap la cercherà. Toglierla adesso gli faceva scrivere un ERROR
        # nel log di Home Assistant per ogni vista finita da sola.
        if session_info["id"] in self.sessions:
            self.set_streaming_available(session_info["stream_idx"])

    # ── la targa chiude ──

    def on_call_ended(self) -> None:
        """La chiamata è finita: le viste aperte vanno chiuse, non congelate.

        Senza, quando la targa riagganciava l'app Casa restava sull'ultimo
        fotogramma con l'audio muto, finché non la si chiudeva a mano.
        """
        if self._hub.in_call or self._hub.calling:
            # È arrivato tardi: nel frattempo è partita un'altra chiamata, e
            # le viste aperte sono sue.
            return
        for info in list(self.sessions.values()):
            if "proc" in info and not info.get("stopping"):
                self._hub.spawn(self._end_from_panel(info))
        if self._transcoder:
            self._hub.spawn(self._stop_transcoder())

    def on_ring_ended(self) -> None:
        """Squillo finito senza risposta: il ricodificatore acceso allo
        squillo non serve più, e senza questo restava a tenersi un socket."""
        if self._transcoder and not (self._hub.in_call or self._hub.calling):
            self._hub.spawn(self._stop_transcoder())

    async def _end_from_panel(self, session_info) -> None:
        _LOGGER.info("HomeKit: la targa ha chiuso la chiamata — chiudo la vista")
        for part in (session_info.get("video"), session_info.get("bridge")):
            if part:
                part.send_bye()
        await self._close_session(session_info)
        if session_info["id"] in self.sessions:
            self.set_streaming_available(session_info["stream_idx"])

    async def stop_stream(self, session_info) -> None:
        await self._close_session(session_info)
        _LOGGER.info("HomeKit: vista chiusa")

    async def _close_session(self, session_info) -> None:
        """Chiude tutto della sessione, una volta sola, da chiunque arrivi.

        La chiedono in tre (lo «stop» del telefono, la targa che riaggancia,
        ffmpeg che esce da solo), a volte insieme. Prima ognuno chiudeva per
        conto suo, e uno poteva cancellare l'altro a metà: restavano aperti i
        socket del ponte audio o il file SDP. Ora la chiusura è un task solo,
        protetto dalle cancellazioni di chi l'aspetta.
        """
        session_info["stopping"] = True
        task = session_info.get("closing")
        if task is None:
            session_info["closing_by"] = asyncio.current_task()
            task = session_info["closing"] = asyncio.ensure_future(
                self._do_close(session_info))
        await asyncio.shield(task)

    async def _do_close(self, session_info) -> None:
        lock = session_info.get("lock")
        if lock is None:
            lock = session_info["lock"] = asyncio.Lock()
        async with lock:          # se start_stream sta lavorando, finisce prima
            await _kill(session_info.get("proc"))
            for task in session_info.get("tasks", []):
                # Non chi ha chiesto questa chiusura (il guardiano, quando
                # ffmpeg esce da solo): dopo deve ancora dire al telefono che
                # lo slot è libero.
                if task is session_info.get("closing_by"):
                    continue
                task.cancel()
            bridge = session_info.pop("bridge", None)
            if bridge:
                await bridge.stop()
            _close_quietly(session_info.pop("a_sock", None))
            await self._stop_video(session_info)

    async def _stop_video(self, session_info) -> None:
        video = session_info.pop("video", None)
        if video:
            detach = session_info.pop("video_detach", None)
            if detach:
                detach()
            await video.stop()
        _close_quietly(session_info.pop("v_sock", None))

    async def reconfigure_stream(self, session_info, stream_config) -> bool:
        # Il video è copiato: bitrate e risoluzione non sono nostri da cambiare.
        return True


async def _kill(proc) -> None:
    """Ferma ffmpeg davvero. Due SIGTERM: uno solo, a inizializzazione finita,
    ffmpeg lo ignora e resta fermo nella poll dell'UDP (misurato in
    media_handler: tre secondi tondi a ogni chiusura)."""
    if not proc or proc.returncode is not None:
        return
    proc.terminate()
    await asyncio.sleep(0.1)
    if proc.returncode is None:
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 2.0)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        await proc.wait()


def _pt(value, default: int) -> int:
    if isinstance(value, (bytes, bytearray)) and value:
        return value[0]
    if isinstance(value, int):
        return value
    return default


def _close_quietly(sock) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _read_snapshot() -> bytes:
    # L'ultimo fotogramma buono, di qualunque età: è quello che si vuole
    # vedere sulla scheda di Casa. L'immagine di riposo solo se non ce n'è.
    return media.read_snapshot() or IDLE_IMAGE


# ─── Il server HAP e l'abbinamento ─────────────────────────────────


class _Driver(AccessoryDriver):
    """Il driver di pyhap, che avvisa quando l'abbinamento cambia."""

    def __init__(self, *, on_pairing_change, **kwargs) -> None:
        super().__init__(**kwargs)
        self._on_pairing_change = on_pairing_change

    def pair(self, client_username_bytes, client_public, client_permissions) -> bool:
        ok = super().pair(client_username_bytes, client_public, client_permissions)
        if ok:
            self._on_pairing_change(True)
        return ok

    def unpair(self, client_uuid) -> None:
        super().unpair(client_uuid)
        if not self.state.paired:
            self._on_pairing_change(False)


def _load_or_create_pin(path: str) -> str:
    """Il codice di abbinamento resta lo stesso fra un riavvio e l'altro."""
    try:
        with open(path, encoding="utf-8") as f:
            pin = json.load(f)["pincode"]
        os.chmod(path, 0o600)   # i file scritti prima erano leggibili da tutti
        return pin
    except (OSError, ValueError, KeyError):
        pass
    digits = f"{secrets.randbelow(10**8):08d}"
    # Apple rifiuta i codici banali.
    while len(set(digits)) == 1 or digits in ("12345678", "87654321"):
        digits = f"{secrets.randbelow(10**8):08d}"
    pin = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    # Chi ha il codice può abbinare il citofono: lo legge solo Home Assistant.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"pincode": pin}, f)
    return pin


class _PairingQRView(HomeAssistantView):
    """Il QR di abbinamento, per la notifica. Protetto da un gettone casuale."""

    url = "/api/vimar_intercom/homekit_qr"
    name = "api:vimar_intercom:homekit_qr"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        qr = self._hass.data.get(_HK_DATA, {}).get("qr")
        given = request.query.get("t", "").encode()
        if not qr or not secrets.compare_digest(given, qr["token"].encode()):
            return web.Response(status=404)
        return web.Response(body=qr["svg"], content_type="image/svg+xml")


def _qr_svg(uri: str) -> bytes:
    import pyqrcode  # noqa: PLC0415 — solo per l'abbinamento

    buf = io.BytesIO()
    pyqrcode.create(uri).svg(buf, scale=6, module_color="#000", background="#FFF")
    return buf.getvalue()


async def async_setup_homekit(hass: HomeAssistant, entry: ConfigEntry, hub):
    """Pubblica il videocitofono in HomeKit. Restituisce la funzione di arresto."""
    from homeassistant.components import network, zeroconf  # noqa: PLC0415

    storage = hass.config.path(".storage")
    state_path = os.path.join(storage, f"{DOMAIN}.{entry.entry_id}.homekit.state")
    pin_path = os.path.join(storage, f"{DOMAIN}.{entry.entry_id}.homekit.pin")
    pincode = await hass.async_add_executor_job(_load_or_create_pin, pin_path)
    address = await network.async_get_source_ip(hass)
    aiozc = await zeroconf.async_get_async_instance(hass)

    acc_holder: dict = {}

    def _pairing_changed(paired: bool) -> None:
        if paired:
            persistent_notification.async_dismiss(hass, _PIN_NOTIFICATION)
            # Abbinato: il QR non serve più, e il link non deve restare valido.
            hass.data.get(_HK_DATA, {}).pop("qr", None)
            _LOGGER.info("HomeKit: citofono abbinato")
        else:
            _show_pairing(hass, acc_holder["acc"], pincode)

    driver = _Driver(
        on_pairing_change=_pairing_changed,
        loop=hass.loop, address=address, port=HOMEKIT_PORT,
        persist_file=state_path, pincode=pincode.encode(),
        async_zeroconf_instance=aiozc)
    acc = VimarDoorbell(driver, ACCESSORY_NAME, hass=hass, hub=hub,
                        stream_address=address, serial=entry.entry_id[:8],
                        smooth=bool(entry.options.get(CONF_HOMEKIT_SMOOTH, DEFAULT_HOMEKIT_SMOOTH)))
    acc_holder["acc"] = acc
    await hass.async_add_executor_job(driver.add_accessory, acc)
    hub.register_ring_callback(acc.ring)
    hub.register_call_end_callback(acc.on_call_ended)
    hub.register_ring_end_callback(acc.on_ring_ended)
    await driver.async_start()
    _LOGGER.info("HomeKit: videocitofono pubblicato su %s:%d (%s, video %s)", address,
                 HOMEKIT_PORT, "abbinato" if driver.state.paired else "da abbinare",
                 "ricodificato" if acc._smooth else "diretto")

    hk_data = hass.data.setdefault(_HK_DATA, {})
    if not hk_data.get("qr_view"):
        hass.http.register_view(_PairingQRView(hass))
        hk_data["qr_view"] = True
    if not driver.state.paired:
        _show_pairing(hass, acc, pincode)

    async def _stop() -> None:
        hub.unregister_ring_callback(acc.ring)
        hub.unregister_call_end_callback(acc.on_call_ended)
        hub.unregister_ring_end_callback(acc.on_ring_ended)
        await acc._stop_transcoder()
        await driver.async_stop()

    return _stop


def _show_pairing(hass: HomeAssistant, acc: VimarDoorbell, pincode: str) -> None:
    token = secrets.token_urlsafe(16)
    try:
        svg = _qr_svg(acc.xhm_uri())
        hass.data.setdefault(_HK_DATA, {})["qr"] = {"token": token, "svg": svg}
        qr = f"\n\n![QR](/api/vimar_intercom/homekit_qr?t={token})"
    except Exception:  # noqa: BLE001
        _LOGGER.exception("HomeKit: QR di abbinamento non generato")
        qr = ""
    persistent_notification.async_create(
        hass,
        "Nell'app Casa: **Aggiungi accessorio**, poi inquadra il QR oppure "
        f"scegli *Altre opzioni* → **{ACCESSORY_NAME}** e inserisci il codice:"
        f"\n\n## {pincode}{qr}",
        title="Citofono: abbinamento HomeKit",
        notification_id=_PIN_NOTIFICATION,
    )
