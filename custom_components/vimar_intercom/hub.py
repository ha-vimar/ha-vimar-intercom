"""Vimar Intercom Hub — manages SIP + media lifecycle."""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

from . import sip_client as sip
from . import media_handler as media
from . import const as C
from . import runtime as R
from .hub_messages import SIP_ID_NAMES, HubMessagesMixin, sip_id_name  # noqa: F401

_LOGGER = logging.getLogger(__name__)

STREAM_HANGUP_DELAY = 30
# Una chiamata a cui si è risposto aprendo la vista finisce quando la vista si
# chiude: nell'app Casa non c'è un altro modo per riagganciare. Pochi secondi
# bastano a coprire una riapertura immediata.
ANSWERED_HANGUP_DELAY = 2
# Quanto si concede all'ffmpeg di HomeKit per farsi vedere, prima di
# concludere che quella sessione non è mai partita.
HOMEKIT_WATCH_START = 8.0
# Quanto si aspetta che un riaggancio in corso finisca, prima di
# aprire una vista nuova. Sopra il tempo di una transazione BYE.
HANGUP_SETTLE = 6.0
# Ogni quanto si tiene viva la connessione verso il relay. Il rinnovo della
# registrazione non passa da qui: lo decide la durata concessa dal registrar.
KEEPALIVE_INTERVAL = 60

def sip_uri(target) -> str:
    """URI SIP per un target dell'impianto, rifiutando i newline.

    Un CR/LF dentro `target` spezza la request line e permette di iniettare
    header nel messaggio SIP. I chiamanti non autenticati sono gia' filtrati in
    `__init__` (`validate.sip_target`); questa e' la seconda rete, per i percorsi
    autenticati e per chiunque aggiunga un chiamante domani.
    """
    t = str(target)
    if "\r" in t or "\n" in t:
        raise ValueError(f"target SIP non valido: {t!r}")
    return f"sip:{t}@{R.SIP_DOMAIN}"


def _uri_to_id(uri: str | None) -> str | None:
    """'sip:55001@dominio' → '55001'."""
    if not uri:
        return None
    u = uri.replace("sip:", "").replace("sips:", "")
    return u.split("@")[0].split(";")[0] or None


MAX_CALL_DURATION = 300  # 5 minutes — auto-hangup safety net

# Interni interrogati con un OPTIONS all'avvio per farsi identificare dal
# citofono quando il modello non è ancora noto (OPTIONS è innocuo: è lo stesso
# messaggio già usato come keepalive).
MODEL_PROBE_TARGETS = ("55001", "55002", "60001")


class VimarIntercomHub(HubMessagesMixin):
    """Orchestrates SIP registration, calls, door control, and media."""

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._ring_callbacks: list[Callable] = []
        self._state_callbacks: list[Callable] = []
        self._call_end_callbacks: list[Callable] = []
        self._ring_end_callbacks: list[Callable] = []
        # Salva nell'entry ciò che l'hub impara dall'impianto (vedi __init__).
        self._persist: Callable[[dict], None] | None = None
        # L'ultima targa che ha suonato: è una targa video che risponde.
        self._last_ring_panel: str | None = None
        self._model_callbacks: list[Callable] = []
        self._ws_broadcast_fn: Callable | None = None
        self._stream_viewers = 0
        self._hanging_up = False
        # I processi ffmpeg della sessione HomeKit in corso, per poter chiudere
        # quelli e soltanto quelli.
        self._homekit_pids: set[int] = set()
        self._hangup_task: asyncio.Task | None = None
        self._call_timeout_task: asyncio.Task | None = None
        self._keyframe_task: asyncio.Task | None = None
        self._auto_called = False
        # Chiamata dal citofono a cui si è risposto aprendo una vista.
        self._answered_call = False
        self._auto_call_target: str | None = None

        # ─── Statistiche / stato esteso (esposte da sensor.py) ───────────
        self.stats: dict = {
            "last_ring_time": None,        # datetime UTC ultimo squillo
            "last_caller_id": None,        # es. "55001"
            "last_caller_uri": None,       # es. "sip:55001@dominio"
            "ring_count": 0,               # squilli dall'avvio
            "missed_count": 0,             # squilli non risposti da HA
            "last_call_start": None,
            "last_call_end": None,
            "last_call_duration": None,    # secondi
            "last_call_direction": None,   # "in" | "out"
            "call_count": 0,               # chiamate attive dall'avvio
            "last_door_time": None,
            "last_door_target": None,
            "last_door_result": None,
            "door_count": 0,
            "last_register_time": None,
            "register_failures": 0,
            "last_command_time": None,
            "last_command_body": None,
            "last_command_target": None,
            "last_command_result": None,
            "last_error": None,
            "last_error_time": None,
            "voicemail": None,             # stato segreteria annunciato dal Tab (True/False)
            "dnd": None,                   # stato Non disturbare annunciato dal Tab
            "vm_level": None,              # spazio segreteria, es. "0/100"
            "rubrica_ver": None,           # versione (md5) della rubrica del Tab
            "vm_ver": None,                # versione (md5) del db videomessaggi
            "init_status": {},             # ultimo GET_INIT_STATUS_REPLY grezzo {PARAM: VALUE}
            "last_message_in": None,       # ultimo SIP MESSAGE ricevuto dal citofono
            "last_message_in_time": None,
            # ─── Eventi in ingresso (PROTOCOL.md §4) ─────────────────────────
            "last_missed_call": None,      # dict {sip_id, ts, name}
            "missed_call_count": 0,        # MISSED_CALL ricevuti dall'avvio
            "new_videomessage": False,     # ON su VM;VIDEO_MESSAGE_CHANGE;NEW
            "last_videomessage": None,     # ultimo change grezzo
            "last_fuoriporta": None,       # dict {sip_id, msg}
            "last_call_info": None,        # dict {sip_id, reason, media_type, video_src}
            "started_at": datetime.now(timezone.utc),
        }
        self._call_started_mono: float | None = None
        self._ring_answered = False
        # Callback per emettere eventi bus HA (registrati da __init__.py).
        # Evita di iniettare hass nell'hub, coerente con ring/state callbacks.
        self._event_callbacks: list[Callable] = []
        self._init_status_sent = False

    # ─── helpers stato esteso ────────────────────────────────────────────
    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def _touch(self):
        """Notifica le entità HA che le statistiche sono cambiate."""
        for cb in self._state_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("State callback error")

    @property
    def calling(self) -> bool:
        return sip.calling

    @property
    def local_ip(self) -> str | None:
        return sip.MY_IP

    @property
    def transport(self) -> str:
        return "udp-local" if R.USE_LOCAL_UDP else "tls-cloud"

    @property
    def proxy(self) -> str:
        return R.LOCAL_PROXY if R.USE_LOCAL_UDP else R.SIP_PROXY

    @property
    def sip_user(self) -> str:
        return R.SIP_USER

    @property
    def sip_domain(self) -> str:
        return R.SIP_DOMAIN

    @property
    def voicemail(self) -> bool | None:
        """Stato segreteria annunciato dal Tab (None finché sconosciuto)."""
        return self.stats.get("voicemail")

    @property
    def dnd(self) -> bool | None:
        """Stato Non disturbare annunciato dal Tab (None finché sconosciuto)."""
        return self.stats.get("dnd")

    @property
    def status(self) -> str:
        """Stato sintetico: offline / ringing / in_call / calling / idle."""
        if not sip.registered:
            return "offline"
        if sip.pending_incoming["active"]:
            return "ringing"
        if sip.in_call:
            return "in_call"
        if sip.calling:
            return "calling"
        return "idle"

    def set_ws_broadcast(self, fn: Callable):
        self._ws_broadcast_fn = fn

    @property
    def registered(self) -> bool:
        return sip.registered

    @property
    def in_call(self) -> bool:
        return sip.in_call

    @property
    def video_available(self) -> bool:
        """Se da questo impianto ci si può aspettare del video.

        Alcuni posti esterni sono solo citofoni: microfono e altoparlante, e
        nessuna telecamera. In quel caso la telecamera in Home Assistant non
        deve fingere di esistere.
        """
        if not getattr(R, "VIDEO_ENABLED", True):
            return False
        if sip.in_call:
            return media.has_video
        return True

    @property
    def devices(self) -> list[dict]:
        """I dispositivi dell'impianto visti finora sul canale SIP."""
        return sip.DEVICES.snapshot()

    @property
    def devices_summary(self) -> list[str]:
        return sip.DEVICES.describe(SIP_ID_NAMES)

    @property
    def is_ringing(self) -> bool:
        return sip.pending_incoming["active"]

    def register_ring_callback(self, callback: Callable) -> None:
        self._ring_callbacks.append(callback)

    def unregister_ring_callback(self, callback: Callable) -> None:
        if callback in self._ring_callbacks:
            self._ring_callbacks.remove(callback)

    def register_state_callback(self, callback: Callable) -> None:
        """Register a callback for SIP state changes (registered, in_call)."""
        self._state_callbacks.append(callback)

    def unregister_state_callback(self, callback: Callable) -> None:
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

    def register_call_end_callback(self, callback: Callable) -> None:
        """Registra un callback() chiamato quando una chiamata finisce."""
        self._call_end_callbacks.append(callback)

    def unregister_call_end_callback(self, callback: Callable) -> None:
        if callback in self._call_end_callbacks:
            self._call_end_callbacks.remove(callback)

    def set_persist_callback(self, callback: Callable[[dict], None]) -> None:
        """Chi salva nell'entry i valori imparati dall'impianto."""
        self._persist = callback

    def register_ring_end_callback(self, callback: Callable) -> None:
        """Registra un callback() chiamato quando uno squillo finisce senza risposta."""
        self._ring_end_callbacks.append(callback)

    def unregister_ring_end_callback(self, callback: Callable) -> None:
        if callback in self._ring_end_callbacks:
            self._ring_end_callbacks.remove(callback)

    def register_event_callback(self, callback: Callable) -> None:
        """Registra un callback(event_type: str, data: dict) per gli eventi
        in ingresso da esporre sul bus HA (missed_call, videomessage, ...)."""
        self._event_callbacks.append(callback)

    def unregister_event_callback(self, callback: Callable) -> None:
        if callback in self._event_callbacks:
            self._event_callbacks.remove(callback)

    def _fire_event(self, event_type: str, data: dict) -> None:
        """Propaga un evento in ingresso ai callback registrati (bus HA)."""
        for cb in self._event_callbacks:
            try:
                cb(event_type, data)
            except Exception:
                _LOGGER.exception("Event callback error (%s)", event_type)

    @property
    def detected_model(self) -> str:
        """Modello rilevato via SIP (stringa vuota se ancora sconosciuto)."""
        return R.DETECTED_MODEL

    def register_model_callback(self, callback: Callable) -> None:
        """Callback(model, fw, user_agent, priority) sul rilevamento modello."""
        self._model_callbacks.append(callback)

    def unregister_model_callback(self, callback: Callable) -> None:
        if callback in self._model_callbacks:
            self._model_callbacks.remove(callback)

    def _on_model_detected(self, model: str, fw: str, ua: str, priority: int):
        """Chiamata da sip_client quando un peer SIP rivela il modello."""
        for cb in self._model_callbacks:
            try:
                cb(model, fw, ua, priority)
            except Exception:
                _LOGGER.exception("Model callback error")

    async def _probe_model(self):
        """Interroga gli interni con un OPTIONS finché qualcuno si identifica."""
        await asyncio.sleep(3)
        for target in MODEL_PROBE_TARGETS:
            if R.DETECTED_MODEL:
                break
            uri = sip_uri(target)
            try:
                await sip.do_options(target=uri)
            except Exception as e:
                _LOGGER.debug("Model probe %s fallito: %s", target, e)
            await asyncio.sleep(0.5)

        if R.DETECTED_MODEL:
            _LOGGER.info("Modello citofono: %s", R.DETECTED_MODEL)
        else:
            _LOGGER.info(
                "Modello non rilevato — nessun peer SIP si è identificato. "
                "User-Agent visti finora: %s", sorted(sip._seen_uas) or "nessuno")

    def _on_sip_state_change(self):
        """Called by sip_client when registered/in_call changes."""
        for cb in self._state_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("State callback error")
        # Notify WS clients of state change
        if self._ws_broadcast_fn:
            task = asyncio.create_task(self._ws_broadcast_fn({
                "type": "state",
                "registered": sip.registered,
                "in_call": sip.in_call,
            }))
            task.add_done_callback(
                lambda t: _LOGGER.error("WS state broadcast error: %s", t.exception())
                if not t.cancelled() and t.exception() else None
            )

    async def stream_opened(self, target: str | None = None):
        self._stream_viewers += 1
        _LOGGER.info("Stream opened (%d viewers, target=%s)", self._stream_viewers, target)

        if self._hangup_task:
            self._hangup_task.cancel()
            self._hangup_task = None

        # Se un riaggancio è in corso, quella chiamata sta morendo: attaccarsi
        # a lei significa vedersela chiudere sotto un attimo dopo. Si aspetta
        # che sia finita e se ne fa una nuova.
        waited = 0.0
        while getattr(self, "_hanging_up", False) and waited < HANGUP_SETTLE:
            await asyncio.sleep(0.05)
            waited += 0.05
        if waited:
            _LOGGER.info("Riaggancio in corso: attesi %.1fs prima di richiamare", waited)

        if sip.in_call or sip.calling:
            return

        # Se una chiamata sta già squillando, aprire il video significa
        # rispondere a quella: la targa ci sta chiamando e piazzarle un secondo
        # INVITE è inutile: risponde 404/486 perché è occupata nella chiamata in
        # corso, e intanto lo squillo resta senza risposta finché non scade.
        if getattr(sip, "pending_incoming", {}).get("active"):
            _LOGGER.info("Stream opened while ringing — answering the incoming call")
            self._spawn(self._answer_incoming())
            return

        # Qui c'era un rifiuto ad autochiamare quando esisteva anche un solo
        # WebSocket audio aperto, perché l'app manda la chiamata per conto suo.
        # Ma quel controllo guardava lo stato GLOBALE, non chi stava chiedendo:
        # bastava che l'app fosse aperta su un telefono qualsiasi perché la
        # telecamera in HomeKit non chiamasse più nessuno, aspettasse quindici
        # secondi e rispondesse 503 — cioè "Nessuna risposta" nell'app Casa,
        # senza una riga di errore da nessuna parte. Chi apre il video vuole
        # vedere: la chiamata si fa. Il doppio INVITE resta escluso dal
        # controllo su in_call/calling qui sopra.
        if sip.registered:
            self._auto_called = True
            self._auto_call_target = target
            # Fire auto-call as background task — don't block the HTTP response
            self._spawn(self._do_auto_call(target))

    async def _answer_incoming(self):
        """Risponde alla chiamata in arrivo invece di piazzarne una nuova."""
        try:
            ok, msg = await sip.do_answer_incoming()
            if ok:
                # Trattata come l'autoaccensione: alla chiusura dello stream la
                # chiamata viene chiusa dallo stesso percorso di hangup.
                self._auto_called = True
                self._answered_call = True
                # Ha risposto qualcuno: senza questo la chiamata veniva poi
                # contata fra quelle perse.
                self._ring_answered = True
            else:
                _LOGGER.error("Answering the incoming call failed: %s", msg)
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("Answer error: %s", e)

    async def _do_auto_call(self, target: str | None):
        """Background auto-call when video stream opens without active call."""
        try:
            # Il dominio SIP è un valore di runtime (dipende dal trasporto in
            # uso), non una costante: C.SIP_DOMAIN non esiste e sollevava
            # AttributeError, impedendo ogni autoaccensione — quindi niente
            # video e niente audio, che viaggiano sulla stessa chiamata.
            if target:
                uri = sip_uri(target)
                ok, msg = await sip.do_call(target=uri)
            else:
                # Autoaccensione: chiama la TARGA VIDEO, non il PICG
                # (il PICG risponde 488 Not Acceptable Here).
                uri = sip_uri(R.CAMERA_TARGET)
                ok, msg = await sip.do_call(target=uri)
                alt = None if ok else self._camera_fallback(msg)
                if alt:
                    _LOGGER.warning(
                        "La targa video %s non risponde (%s): provo %s, la targa "
                        "che ha suonato", R.CAMERA_TARGET, msg, alt)
                    ok, msg = await sip.do_call(target=sip_uri(alt))
                    if ok:
                        R.CAMERA_TARGET = alt
                        if self._persist:
                            self._persist({"learned_camera_target": alt})
                        _LOGGER.info("Targa video imparata dall'impianto: %s", alt)
            if not ok:
                _LOGGER.error("Auto-call failed: %s", msg)
                self._auto_called = False
            elif self._stream_viewers == 0 and not self._hangup_pending():
                # L'INVITE può concludersi dopo che lo spettatore ha già
                # rinunciato (lui aspetta 15 s, la chiamata fino a 45): senza
                # questo la chiamata resterebbe aperta senza nessuno a guardare.
                _LOGGER.info("Auto-call connected with no viewers left — hanging up")
                self._schedule_hangup()
        except Exception as e:
            _LOGGER.error("Auto-call error: %s", e)
            self._auto_called = False

    def _camera_fallback(self, result: str) -> str | None:
        """La targa da provare quando quella di default non esiste.

        Il default (55100) è la targa video dell'impianto di riferimento; su
        un altro impianto risponde 404 e l'autoaccensione non parte mai. La
        targa che ha suonato l'ultima volta invece esiste di sicuro e manda
        video. Solo se la targa non l'ha scelta nessuno: una scelta esplicita
        non si tocca.
        """
        if R.CAMERA_TARGET_CONFIGURED:
            return None
        code = (result or "").split(" ", 1)[0]
        if code not in ("404", "480", "488", "604"):
            return None
        alt = self._last_ring_panel
        return alt if alt and alt != R.CAMERA_TARGET else None

    async def stream_closed(self):
        self._stream_viewers = max(0, self._stream_viewers - 1)
        _LOGGER.info("Stream viewer disconnected (%d remaining)", self._stream_viewers)

        if self._stream_viewers == 0 and self._auto_called and sip.in_call:
            self._schedule_hangup()

    async def _delayed_hangup(self):
        try:
            # Una chiamata dal citofono (dalla targa o dal monitor di casa) a
            # cui si è risposto: 30 s di attesa la tenevano viva, e ogni vista
            # riaperta nel frattempo ci si riattaccava. La chiamata del monitor
            # non ha video, quindi per un minuto buono la strada non si vedeva.
            delay = (ANSWERED_HANGUP_DELAY if getattr(self, "_answered_call", False)
                     else STREAM_HANGUP_DELAY)
            await asyncio.sleep(delay)
            if self._stream_viewers == 0 and self._auto_called and sip.in_call:
                _LOGGER.info("No viewers, hanging up auto-call")
                # Da qui il riaggancio va fino in fondo: una vista che si apre
                # adesso cancella questo task, e prima interrompeva il BYE a
                # metà — in_call restava vero, il media acceso, e la vista
                # nuova si attaccava a una chiamata morta per cinque minuti.
                # La vista nuova aspetta _hanging_up e poi richiama.
                # Il flag lo spegne la fine del BYE, non la fine di questo
                # task: se una vista nuova lo cancella, il BYE continua e lei
                # deve continuare ad aspettarlo.
                self._hanging_up = True
                bye = asyncio.ensure_future(sip.do_hangup())
                bye.add_done_callback(lambda _t: setattr(self, "_hanging_up", False))
                await asyncio.shield(bye)
            if self._stream_viewers == 0:
                # Anche se la chiamata era già finita per conto suo: il flag
                # deve morire con lei, non sopravviverle.
                self._auto_called = False
        except asyncio.CancelledError:
            pass

    def _start_call_timeout(self):
        """Start max call duration timer."""
        self._cancel_call_timeout()
        self._call_timeout_task = asyncio.create_task(self._call_timeout())

    def _cancel_call_timeout(self):
        if self._call_timeout_task:
            self._call_timeout_task.cancel()
            self._call_timeout_task = None

    async def _call_timeout(self):
        try:
            await asyncio.sleep(MAX_CALL_DURATION)
            if sip.in_call:
                _LOGGER.info("Max call duration (%ds) reached, hanging up", MAX_CALL_DURATION)
                await sip.do_hangup()
                self._auto_called = False
        except asyncio.CancelledError:
            pass

    def _hangup_pending(self) -> bool:
        """Se c'è davvero un riaggancio in attesa.

        Un task concluso resta un oggetto, quindi "if self._hangup_task"
        continuava a essere vero per sempre dopo il primo riaggancio: la rete
        di sicurezza che quel controllo proteggeva non è più scattata.
        """
        return self._hangup_task is not None and not self._hangup_task.done()

    def _schedule_hangup(self):
        if self._hangup_pending():
            self._hangup_task.cancel()
        self._hangup_task = self._spawn(self._delayed_hangup())
        return self._hangup_task

    def start_homekit_watch(self, session):
        """Un guardiano per OGNI vista, non uno solo per tutte.

        Era uno solo, e l'apertura di una seconda vista annullava quello della
        prima: il suo finally rilasciava la sessione della prima vista — che da
        lì non riceveva più l'audio della strada — e quando la seconda si
        chiudeva, il suo guardiano credeva di essere l'ultimo e riagganciava.
        Visto il 26 settembre: si risponde dall'iPhone, la compagna apre la
        stessa chiamata, la chiude, e in strada la chiamata cade.

        Ognuno ora guarda soltanto il proprio ffmpeg, e si riaggancia solo quando
        si chiude l'ultima vista della chiamata.
        """
        tasks = self.__dict__.setdefault("_homekit_watch_tasks", set())
        task = self._spawn(self.watch_homekit_session(session))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def watch_homekit_session(self, session=None):
        """Riaggancia quando si chiude l'ultima vista HomeKit della chiamata.

        Senza, la chiamata resta in piedi fino al limite del citofono: una
        trentina di secondi per l'autoaccensione, ma DUE MINUTI per una
        chiamata che arriva dalla strada. In quei due minuti la luce del posto
        esterno resta accesa e nessun altro può suonare.

        Non ci sono eventi da cui accorgersene per la telecamera di Home
        Assistant — legge un SDP, non una richiesta HTTP che finisce — ma il suo
        ffmpeg vive esattamente quanto la vista: basta guardarlo comparire e
        sparire. L'SDP è unico per sessione, quindi l'ffmpeg che lo legge è
        per forza quello di questa vista.
        """
        # La chiamata di questa vista. Se alla fine ce n'è un'altra, la vista
        # era della vecchia, e riagganciare chiuderebbe quella di qualcun altro.
        call_id = (getattr(sip, "call_state", None) or {}).get("call_id")
        sdp = session["sdp"] if session else None
        mine: set[int] = set()
        loop = asyncio.get_running_loop()

        async def _pids():
            # Legge /proc: fuori dall'event loop, ogni pochi decimi di secondo
            # per ogni vista aperta sarebbe un fermo continuo.
            return await loop.run_in_executor(None, media.homekit_ffmpeg_pids, sdp)

        try:
            for _ in range(int(HOMEKIT_WATCH_START / 0.2)):
                await asyncio.sleep(0.2)
                mine = set(await _pids())
                if mine:
                    self._homekit_pids = set(mine)
                    break
            if not mine:
                # La vista non è mai partita (chiusa mentre la chiamata si
                # apriva). Conta come chiusa: senza, la chiamata restava su
                # senza nessuno a guardare fino al limite di sicurezza.
                _LOGGER.debug("Nessun ffmpeg per la sessione %s", sdp)
            while mine and await _pids():
                await asyncio.sleep(0.4)
                if not sip.in_call:
                    return
        finally:
            if session:
                media.release_homekit_session(session["id"])
        still_open = media.homekit_session_count()
        if still_open:
            _LOGGER.info("Vista HomeKit chiusa, ne restano %d aperte: la chiamata "
                         "resta", still_open)
            return
        if call_id and call_id != (getattr(sip, "call_state", None) or {}).get("call_id"):
            _LOGGER.debug("La vista chiusa era di una chiamata precedente: non "
                          "riaggancio")
            return
        if sip.in_call and self._auto_called:
            _LOGGER.info("Ultima vista HomeKit chiusa — riaggancio invece di "
                         "aspettare che sia il citofono a farlo")
            # Da qui fino a chiamata davvero finita c'è una finestra in cui
            # sip.in_call è ancora vero ma la chiamata è spacciata. Chi apre
            # una vista in quel momento ci si attacca e se la vede morire
            # sotto: è il "riapro subito e non parte più".
            self._hanging_up = True
            try:
                await sip.do_hangup()
            finally:
                self._hanging_up = False
            self._auto_called = False

    def spawn(self, coro):
        """Versione pubblica di _spawn, per le piattaforme."""
        return self._spawn(coro)

    def _spawn(self, coro):
        """Lancia un task tenendone un riferimento.

        asyncio conserva solo riferimenti deboli: un task lasciato andare può
        essere raccolto dal garbage collector a metà esecuzione.
        """
        task = asyncio.create_task(coro)
        tasks = getattr(self, "_bg_tasks", None)
        if tasks is None:
            tasks = self._bg_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def request_keyframe(self):
        """Chiede subito un keyframe: senza IDR il decoder non mostra nulla."""
        if sip.in_call:
            try:
                await sip.send_keyframe_request()
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("Keyframe request failed: %s", e)

    def _start_keyframe_loop(self):
        """Send periodic keyframe requests during calls for video recovery."""
        self._cancel_keyframe_loop()
        self._keyframe_task = asyncio.create_task(self._keyframe_loop())

    def _cancel_keyframe_loop(self):
        if self._keyframe_task:
            self._keyframe_task.cancel()
            self._keyframe_task = None

    async def _keyframe_loop(self):
        """Keyframe burst at start, then slow periodic refresh.

        Il burst serve a ottenere SPS/PPS+IDR appena parte il video. Dopo,
        se il video sta effettivamente arrivando (pkt_count cresce) rallentiamo
        molto: un INFO ogni 2s spammava il proxy (e i 407) senza utilità.
        """
        def _video_flowing():
            vp = media.video_proto
            return bool(vp and vp.pkt_count > 0)

        try:
            # Immediate first request — no delay
            if sip.in_call:
                await sip.send_keyframe_request()
            # Rapid burst: 8 requests at 150ms intervals to grab the first IDR
            for _ in range(8):
                await asyncio.sleep(0.15)
                if not sip.in_call:
                    return
                if _video_flowing():
                    break
                await sip.send_keyframe_request()
            # Nemmeno via RTCP si chiede più nulla: misurato sul campo, la
            # targa non onora PLI, FIR moderno né FIR legacy.
            # Niente refresh periodico: la targa non onora picture_fast_update
            # e manda i keyframe sulla sua cadenza (circa tre secondi), che non
            # è configurabile. Il vecchio ciclo ogni 5 s costava una transazione
            # SIP con sfida 407 e ritentativo, attraverso il relay cloud, per
            # tutta la durata della chiamata, e non anticipava un fotogramma.
            # Il burst iniziale resta: costa poco e copre il caso in cui una
            # targa diversa si comporti diversamente a chiamata fredda.
        except asyncio.CancelledError:
            pass

    async def async_start(self):
        if self._running:
            return

        sip.init(self._handle_broadcast)
        sip.set_state_callback(self._on_sip_state_change)
        sip.set_model_callback(self._on_model_detected)
        media.init(self._handle_broadcast)

        sip.MY_IP = sip.get_local_ip()
        sip.incoming_requests = asyncio.Queue()
        _LOGGER.info("Local IP: %s", sip.MY_IP)

        await media.setup_transports()
        _LOGGER.info("RTP transports ready")

        await sip.connect()

        self._tasks.append(asyncio.create_task(sip.reader_task()))
        self._tasks.append(asyncio.create_task(sip.request_processor()))
        self._tasks.append(asyncio.create_task(self._auto_startup()))
        self._tasks.append(asyncio.create_task(self._keepalive_loop()))
        # Every transport needs renewal: a cloud binding expires just like a local one.
        self._tasks.append(asyncio.create_task(sip.register_refresh_task()))
        self._running = True

    async def async_stop(self):
        self._running = False
        if sip.in_call or sip.calling:
            # Scaricare l'integrazione a chiamata aperta lasciava la targa
            # accesa fino al suo timeout: prima si chiude la chiamata.
            try:
                await asyncio.wait_for(sip.do_hangup(), timeout=3)
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("Riaggancio allo scaricamento non riuscito: %s", e)
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        if self._hangup_task:
            self._hangup_task.cancel()
        self._cancel_call_timeout()
        self._cancel_keyframe_loop()
        await media.stop_media()
        media.close_transports()
        if sip.writer:
            try:
                sip.writer.close()
            except Exception:
                pass
        if sip._udp_sock:
            try:
                sip._udp_sock.close()
            except Exception:
                pass
        sip.reset_state()
        _LOGGER.info("Hub stopped")

    async def async_call(self, target: str | None = None) -> tuple[bool, str]:
        self._auto_called = False
        if target:
            uri = sip_uri(target)
            return await sip.do_call(target=uri)
        return await sip.do_call()

    async def async_answer(self) -> tuple[bool, str]:
        ok, msg = await sip.do_answer_incoming()
        if ok:
            self._ring_answered = True
            self.stats["last_call_direction"] = "in"
        self._touch()
        return ok, msg

    async def async_decline(self):
        await sip.do_decline_incoming()
        self._touch()


    async def async_hangup(self):
        self._auto_called = False
        self._cancel_call_timeout()
        await sip.do_hangup()

    async def async_door(self, target: str | None = None, command: str | None = None) -> tuple[bool, str]:
        """Open door via SIP MESSAGE to targa (PE) address.

        From Tab5S rubrica ACTUATOR_LIST:
          55001 (targa master)  → OPEN_2F = Portone Esterno
          55002 (targa interna) → OPEN_2F = Portone Interno
        The targa forwards the command to its local relay.
        No active call required.
        """
        if target:
            uri = sip_uri(target)
            body = command or C.DOOR_COMMAND
        else:
            uri = R.DOOR_ESTERNO
            body = C.DOOR_COMMAND

        _LOGGER.info("Door command: uri=%s body=%s registered=%s", uri, body, sip.registered)

        ok, msg = await sip.do_system_message(
            uri, body, extra_headers={"Panda": "command"})

        self.stats["last_door_time"] = self._now()
        self.stats["last_door_target"] = target or R.DOOR_TARGET
        self.stats["last_door_result"] = msg
        if ok:
            self.stats["door_count"] += 1
        self._touch()

        if ok:
            _LOGGER.info("Door open OK: %s", msg)
            return ok, msg


        # Retry once after re-registration — handles stale connection
        _LOGGER.warning("Door command failed (%s), retrying after re-register...", msg)
        try:
            reg_ok = await sip.do_register()
            if reg_ok:
                ok2, msg2 = await sip.do_system_message(
                    uri, body, extra_headers={"Panda": "command"})
                self.stats["last_door_result"] = msg2
                if ok2:
                    self.stats["door_count"] += 1
                    self._touch()
                    _LOGGER.info("Door open OK on retry: %s", msg2)
                    return ok2, msg2
                self._touch()
                _LOGGER.error("Door retry also failed: %s", msg2)
                return ok2, msg2
            else:
                _LOGGER.error("Re-registration failed, cannot retry door")
                return False, "Re-registrazione fallita"
        except Exception as e:
            _LOGGER.error("Door retry error: %s", e)
            return False, str(e)

    async def async_send_command(
        self,
        body: str,
        target: str | None = None,
        header_name: str | None = "Panda",
        header_value: str | None = "command",
    ) -> tuple[bool, str]:
        """Invia un SIP MESSAGE arbitrario al citofono (per test / comandi non ancora mappati).

        target può essere un ID (es. "55001") oppure un URI sip: completo;
        senza, l'SGA configurato.
        """
        target = target or R.SGA_TARGET
        if target.startswith("sip:"):
            # Un URI intero finisce tale e quale nella request line: niente
            # a capo, spazi o parentesi angolari, che la spezzerebbero.
            if any(c in target for c in "\r\n <>"):
                return False, "target non valido"
            uri = target
        else:
            uri = sip_uri(target)
        # Header solo se nome e valore ci sono entrambi: fino alla 1.0.6 un
        # header_value vuoto dal servizio diventava None e partiva «Panda: None».
        # CR/LF vengono rifiutati: finirebbero dentro il messaggio SIP come
        # righe di header aggiuntive.
        name = (header_name or "").strip()
        value = (header_value or "").strip()
        if any(c in name + value for c in "\r\n"):
            return False, "header_name/header_value non possono contenere a capo"
        headers = {name: value} if name and value else None
        _LOGGER.info("Custom command: uri=%s body=%r headers=%s", uri, body, headers)
        try:
            ok, msg = await sip.do_system_message(uri, body, extra_headers=headers)
        except Exception as e:  # noqa: BLE001
            ok, msg = False, str(e)
        self.stats["last_command_time"] = self._now()
        self.stats["last_command_body"] = body
        self.stats["last_command_target"] = target
        self.stats["last_command_result"] = msg
        self._touch()
        return ok, msg

    async def async_probe(self, target: str) -> tuple[bool, str]:
        uri = sip_uri(target)
        return await sip.do_options(target=uri)

    async def async_scan(self, start: int, end: int) -> list[dict]:
        results = []
        for addr in range(start, end + 1):
            uri = sip_uri(addr)
            try:
                ok, msg = await sip.do_options(target=uri)
                results.append({"addr": addr, "ok": ok, "msg": msg})
            except Exception as e:
                results.append({"addr": addr, "ok": False, "msg": str(e)})
            await asyncio.sleep(0.3)
        return results

    async def _handle_broadcast(self, msg_type, msg):
        _LOGGER.debug("[%s] %s", msg_type, msg)
        self._update_stats(msg_type, msg)

        if msg_type in ("ring", "ring_ended", "call_started", "call_ended", "registered", "error"):
            # Don't broadcast "ring" to WS clients if we initiated the call
            if msg_type == "ring" and (sip.in_call or sip.calling):
                pass  # Will be handled below (suppress + decline)
            elif self._ws_broadcast_fn:
                try:
                    payload = {
                        "type": msg_type, "msg": msg,
                        "registered": sip.registered, "in_call": sip.in_call,
                    }
                    # Include caller URI so clients can identify which panel is ringing
                    if msg_type == "ring" and sip.pending_incoming.get("caller_uri"):
                        payload["caller_uri"] = sip.pending_incoming["caller_uri"]
                    await self._ws_broadcast_fn(payload)
                except Exception:
                    _LOGGER.exception("WS broadcast error")

        if msg_type == "call_started":
            self._start_call_timeout()
            self._start_keyframe_loop()
        elif msg_type == "ring_ended":
            for cb in list(getattr(self, "_ring_end_callbacks", ())):
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Ring-end callback error")
        elif msg_type == "call_ended" and (sip.in_call or sip.calling):
            # La fine di una chiamata precedente, arrivata quando ne è già
            # partita un'altra: toccare timer, viste e ffmpeg adesso vorrebbe
            # dire rompere quella nuova.
            _LOGGER.debug("call_ended in ritardo: c'è già un'altra chiamata, ignorato")
        elif msg_type == "call_ended":
            self._cancel_call_timeout()
            self._cancel_keyframe_loop()
            for cb in list(getattr(self, "_call_end_callbacks", ())):
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Call-end callback error")
            # Il conteggio degli spettatori vale solo durante una chiamata, e
            # va azzerato con lei. HomeKit adesso legge l'RTP da un SDP invece
            # che da /av, quindi non c'è più una richiesta HTTP che finendo
            # segnali l'uscita: senza questo il contatore cresceva a ogni
            # apertura e non tornava più a zero — visto arrivare a 14 — e con
            # lui non sarebbe mai scattato il riaggancio per assenza di
            # spettatori.
            # Solo se non c'è già una chiamata nuova in corso. Questo
            # call_ended può arrivare con secondi di ritardo — la chiusura di
            # ffmpeg ne costava tre — e nel frattempo qualcuno può aver
            # riaperto la vista. Azzerare qui i contatori della chiamata NUOVA
            # la lasciava senza spettatori e senza _auto_called: partiva il
            # "Auto-call connected with no viewers left", e poi chiudendo la
            # vista non si riagganciava più nulla, perché il guardiano
            # pretende _auto_called. Il posto esterno restava occupato per
            # tutti i trenta secondi. Visto nei registri alle 18:30:23.
            self._stream_viewers = 0
            self._auto_called = False
            self._answered_call = False
            # Chi guardava da HomeKit deve accorgersene. Il suo ffmpeg legge
            # un SDP, e un ingresso RTP non finisce da solo: senza pacchetti
            # resta acceso, si tiene le porte — così la vista successiva non
            # parte più — e intanto sullo schermo resta l'ultimo fotogramma,
            # come se il collegamento fosse ancora vivo mentre l'audio è già
            # morto. Chiuderlo fa capire a Home Assistant che è finita.
            # Difensivo: alcune prove costruiscono un hub parziale, e questo
            # percorso deve restare innocuo anche lì.
            media.stop_homekit_ffmpeg(getattr(self, "_homekit_pids", None) or None)
            self._homekit_pids = set()

        if msg_type == "ring":
            # Quando siamo noi a chiamare (tap per vedere / autoaccensione) il
            # Tab ci rimanda un INVITE: quello va zittito, non è una chiamata
            # al campanello ma l'eco della nostra. Deve però valere solo se
            # siamo davvero occupati: con il solo _auto_called, e nessuna
            # chiamata in corso, si finiva per rifiutare con 603 chiamate
            # legittime — cioè chiunque suonasse dopo una nostra autoaccensione.
            busy = sip.in_call or sip.calling
            if busy:
                _LOGGER.info(
                    "Suppressing ring — we initiated this call (auto_called=%s, in_call=%s, calling=%s)",
                    self._auto_called, sip.in_call, sip.calling)
                self._spawn(sip.do_decline_incoming())
                return
            if self._auto_called:
                _LOGGER.info(
                    "Ring arrivato con autoaccensione segnata ma nessuna chiamata attiva: "
                    "trattato come squillo vero")
                self._auto_called = False

            for cb in self._ring_callbacks:
                try:
                    cb()
                except Exception:
                    _LOGGER.exception("Ring callback error")

    def _update_stats(self, msg_type: str, msg):
        """Aggiorna le statistiche in base agli eventi SIP."""
        st = self.stats
        now = self._now()
        try:
            if msg_type == "ring":
                # Squillo reale solo se non l'abbiamo originato noi
                if not (sip.in_call or sip.calling):
                    caller = sip.pending_incoming.get("caller_uri") or ""
                    st["last_ring_time"] = now
                    st["last_caller_uri"] = caller or None
                    st["last_caller_id"] = _uri_to_id(caller)
                    if (st["last_caller_id"] or "").isdigit():
                        self._last_ring_panel = st["last_caller_id"]
                    st["ring_count"] += 1
                    self._ring_answered = False
            elif msg_type == "ring_ended":
                if not self._ring_answered and st["last_ring_time"]:
                    st["missed_count"] += 1
            elif msg_type == "call_started":
                self._call_started_mono = time.monotonic()
                st["last_call_start"] = now
                st["call_count"] += 1
                if not self._ring_answered:
                    st["last_call_direction"] = "out"
            elif msg_type == "call_ended" and not (sip.in_call or sip.calling):
                st["last_call_end"] = now
                if self._call_started_mono is not None:
                    st["last_call_duration"] = round(time.monotonic() - self._call_started_mono, 1)
                    self._call_started_mono = None
                self._ring_answered = False
            elif msg_type == "registered":
                st["last_register_time"] = now
            elif msg_type == "error":
                st["last_error"] = str(msg)[:200]
                st["last_error_time"] = now
            elif msg_type == "message":
                st["last_message_in"] = str(msg)[:200]
                st["last_message_in_time"] = now
                self._handle_incoming_message(str(msg))
        except Exception:
            _LOGGER.exception("stats update error")
        self._touch()

    # ─── Parsing dei SIP MESSAGE in ingresso (Panda: blue) ───────────────────
    # Qui si LEGGE soltanto: nessun comando in uscita. Parsing difensivo: alcuni
    # body sono JSON, altri delimitati da ';'. Se non combacia → debug, no crash.
    async def async_request_init_status(self):
        """Richiesta esplicita dello stato: usata anche per verificare i comandi."""
        return await self._request_init_status()

    async def _request_init_status(self):
        """Chiede lo stato iniziale al PICG (GET_INIT_STATUS, Panda: blue).

        La risposta arriva in modo asincrono come SIP MESSAGE
        (GET_INIT_STATUS_REPLY) → parsata in _handle_incoming_message.
        Funziona sia in UDP locale sia in cloud TLS.
        """
        try:
            ok, msg = await self.async_send_command(
                body=C.GET_INIT_STATUS,
                target=R.PICG_TARGET,
                header_name="Panda",
                header_value="blue",
            )
            # Solo se l'invio e' riuscito: altrimenti il ramo di retry del
            # keepalive (if not self._init_status_sent) non potrebbe mai
            # scattare e i sensori resterebbero a None fino al riavvio di HA.
            self._init_status_sent = bool(ok)
            _LOGGER.info("GET_INIT_STATUS → %s: ok=%s msg=%s", R.PICG_TARGET, ok, msg)
        except Exception:
            _LOGGER.exception("GET_INIT_STATUS invio fallito")

    async def _auto_startup(self):
        await asyncio.sleep(2)
        try:
            _LOGGER.info("Auto startup: registering SIP...")
            ok = await sip.do_register()
            _LOGGER.info("Auto startup: register result=%s", ok)
            if ok:
                self.stats["last_register_time"] = self._now()
            else:
                self.stats["register_failures"] += 1
            self._touch()
            if ok:
                # Stato iniziale (voicemail/dnd/rubrica_ver/vm_level) dal PICG.
                await self._request_init_status()
                # Identificazione del modello: passiva (header dei messaggi in
                # arrivo) + una sonda OPTIONS se non lo conosciamo ancora.
                self._tasks.append(asyncio.create_task(self._probe_model()))
            if ok and not R.USE_LOCAL_UDP:
                # connectProfiles è solo per la modalità cloud (push notifications)
                await asyncio.sleep(1)
                try:
                    ok2, msg2 = await sip.do_connect_profiles()
                    _LOGGER.info("connectProfiles: ok=%s msg=%s", ok2, msg2)
                except Exception as e:
                    _LOGGER.error("connectProfiles error: %s", e)
            elif not ok:
                _LOGGER.error("SIP registration failed")
        except Exception as e:
            _LOGGER.error("Auto startup error: %s", e, exc_info=True)

    async def _keepalive_loop(self):
        """Tiene viva la connessione e recupera se la registrazione cade.

        Prima questo ciclo rifaceva una REGISTER completa ogni 120 s, cioè
        rinnovava la registrazione per conto suo, in parallelo al rinnovo
        basato sulla durata concessa dal registrar: due meccanismi per la
        stessa cosa, con Call-ID e CSeq diversi. Qui resta solo ciò che la
        registrazione non copre — tenere viva la connessione verso il relay e
        rimettersi in piedi se lo stato si perde — mentre il rinnovo vero vive
        in sip_client.register_refresh_task().
        """
        while self._running:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            await self._keepalive_tick()

    async def _keepalive_tick(self):
        """Un giro di keepalive. Separato dal loop per poterlo testare."""
        try:
            if sip.registered:
                # Il rinnovo lo fa sip_client alla scadenza concessa dal
                # registrar; qui basta tenere vivo il collegamento col relay.
                await sip.send_keepalive()
                if not self._init_status_sent:
                    await self._request_init_status()
                self._touch()
                return
            else:
                # Fino alla 1.0.5 questo ramo non esisteva: la guardia era
                # `if sip.registered`, quindi persa la registrazione il loop
                # girava a vuoto per sempre. In UDP locale — il default —
                # non c'era nessun altro percorso di recupero: il citofono
                # restava scollegato fino al riavvio di Home Assistant.
                _LOGGER.warning("Registrazione SIP assente: provo a recuperarla")
                ok = await sip.reconnect()
                if ok:
                    # Tornati su dopo un'interruzione: mentre eravamo
                    # scollegati lo stato del Tab puo' essere cambiato
                    # (segreteria, DND, versione rubrica). Rifacciamo la
                    # domanda invece di restare con i valori di prima.
                    self._init_status_sent = False
                    _LOGGER.info("Registrazione SIP recuperata")

            if ok:
                self.stats["last_register_time"] = self._now()
                # Se lo stato iniziale non è mai stato ottenuto (primo
                # invio fallito / reconnect dopo offline), riprova ora.
                if not self._init_status_sent:
                    await self._request_init_status()
            else:
                self.stats["register_failures"] += 1
            self._touch()
        except Exception as e:
            _LOGGER.error("Keepalive error: %s", e)
