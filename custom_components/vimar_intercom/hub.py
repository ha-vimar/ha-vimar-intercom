"""Vimar Intercom Hub — manages SIP + media lifecycle."""

import asyncio
import functools
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime

from . import away_tts, frame_grabber, log_redact, rest_client, validate, webhook
from . import const as C
from . import media_handler as media
from . import plant_state as S
from . import runtime as R
from . import sip_client as sip
from .plant_messages import SIP_ID_NAMES as SIP_ID_NAMES
from .plant_messages import PlantMessages
from .ring_media import RingMedia

_LOGGER = logging.getLogger(__name__)

STREAM_HANGUP_DELAY = 30
# Niente auto-call per 60 s dopo la fine di una chiamata (qualsiasi, anche
# rifiutata o annullata), di un auto-call fallito o di uno squillo, ma solo per
# le riaperture «di riflesso»: go2rtc e lo stream worker riaprono /av da soli
# appena lo stream finisce, con un back-off che cresce (1, 2, 4, 8 s...), e ogni
# riapertura richiamava la targa (486, poi un altro tentativo). Nel frattempo /av
# risponde subito 503. Riflesso = l'ultimo spettatore di /av è uscito dopo la
# fine della chiamata (era lì quando è finita, o è una riapertura già rifiutata):
# il tempo fra un tentativo e l'altro non lo distingue da una persona (#57).
# Chi apre la camera senza aver guardato /av alla fine (dashboard dopo la card,
# HomeKit) chiama anche entro il minuto.
AUTO_CALL_COOLDOWN = 60
# Tetto del clip dello squillo (video dell'anteprima e, se rispondiamo noi, della chiamata).
CLIP_MAX_S = 60
# The longest a view opening during an automatic hang-up waits for it.
HANGUP_SETTLE = 6.0
# do_hangup ends the call locally (in_call false, media off) and then waits up
# to 5 s for the answer to its BYE, which the cloud relay never sends. A view
# waits only this long after the local end, not for that answer.
HANGUP_LOCAL_SETTLE = 0.5
# Upper bound for the whole automatic hang-up, BYE answer included.
HANGUP_BYE_TIMEOUT = 8.0
# Local UDP only (#41, #44): a panel that has just ended a call can swallow the
# next INVITE in two ways, seen on a 40507: the Tab's proxy answers 100 Trying
# and the panel never sends its 180, or the panel rings (180) and never
# answers. Measured over 35 openings: an INVITE less than 1 s after the
# previous dialog ended was never answered (8 of 8), 2-3.5 s later about half
# the time, from 5 s on almost always; a real answer comes 3.2-5.0 s after the
# INVITE. So a call to the panel waits until LOCAL_UDP_SETTLE after the last
# dialog (a call that ended, or our own cancelled try), is given up after
# LOCAL_UDP_RING_TIMEOUT with a 100 and no 180 or after
# LOCAL_UDP_ANSWER_TIMEOUT without a final answer, and is tried again as long
# as it is wanted and LOCAL_UDP_CALL_BUDGET (inside /av's 25 s) leaves room.
# The cloud relay answers at once (100 Trying); there, nothing changes.
LOCAL_UDP_SETTLE = 5.0
LOCAL_UDP_ANSWER_TIMEOUT = 6.0      # the slowest real answer measured: 5.0 s
LOCAL_UDP_RING_TIMEOUT = 3.0
LOCAL_UDP_CALL_BUDGET = 21.0       # from the first INVITE
LOCAL_UDP_TAP_LIMIT = 24.0          # from the tap, settle included: inside /av's 25 s
LOCAL_UDP_MIN_TRY = 5.0             # a try needs room for an answer


def sip_uri(target) -> str:
    """URI SIP per un id dell'impianto. Chiamate, porta, pulsanti e attuatori
    passano tutti da qui: un id non numerico (CR/LF, `x@altro.dominio`) non
    arriva mai nella request line."""
    t = validate.sip_target(target)
    if t is None:
        raise ValueError(f"target SIP non valido: {target!r}")
    return f"sip:{t}@{R.SIP_DOMAIN}"


def _plant_uri(uri: str) -> bool:
    """True for sip:<digits>@<domain> on one of the plant's own domains."""
    domains = {d for d in (R.SIP_DOMAIN, R.LOCAL_DOMAIN, R.CLOUD_DOMAIN) if d}
    m = re.fullmatch(r"sip:(\d+)@([^\s]+)", uri or "")
    return bool(m) and m.group(2) in domains


def _command_uri(target: str) -> str | None:
    """The URI for a command target: a plant id, or a whole plant URI."""
    if str(target).startswith("sip:"):
        return target if _plant_uri(target) else None
    try:
        return sip_uri(target)
    except ValueError:
        return None


def _offers_video(sdp: str | None) -> bool:
    """True when an SDP offer has an m=video line with a non-zero port."""
    for m in re.finditer(r"^m=video[ \t]+(\d+)", sdp or "", re.M):
        if int(m.group(1)) != 0:
            return True
    return False


def _uri_to_id(uri: str | None) -> str | None:
    """'sip:55001@dominio' → '55001'."""
    if not uri:
        return None
    u = uri.replace("sip:", "").replace("sips:", "")
    return u.split("@")[0].split(";")[0] or None


MAX_CALL_DURATION = 300  # 5 minutes — auto-hangup safety net
DOOR_TLS_TIMEOUT = 20  # MESSAGE over the cloud: the relay answered after ~15.2 s in #14


def _message_timeout() -> int:
    """A command MESSAGE's wait: send_message's default 15 s on local UDP, DOOR_TLS_TIMEOUT over the cloud."""
    return 15 if R.USE_LOCAL_UDP else DOOR_TLS_TIMEOUT


# async_door's result keys, translated where shown (exceptions in strings.json, #128).
# "queued" and "unconfirmed" are not retried: the relay may still deliver them.
DOOR_OPENED = "opened"
DOOR_BUSY = "busy"
QUEUED = "queued"  # a 202: also the result of non-door commands
DOOR_QUEUED = QUEUED
DOOR_UNCONFIRMED = "unconfirmed"
DOOR_NOT_REGISTERED = "not_registered"
DOOR_TIMEOUT = "timeout"
DOOR_ERROR = "error"  # with the SIP code
DOOR_SEND_FAILED = "send_failed"

# Interni interrogati con un OPTIONS all'avvio per farsi identificare dal
# citofono quando il modello non è ancora noto (OPTIONS è innocuo: è lo stesso
# messaggio già usato come keepalive).
MODEL_PROBE_TARGETS = ("55001", "55002", "60001")


class VimarIntercomHub(PlantMessages, RingMedia):
    """Orchestrates SIP registration, calls, door control, and media."""

    _sim_ring: asyncio.Task | None = None  # simulate_ring in progress

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        # Fire-and-forget tasks (see _spawn): the event loop keeps only weak
        # references, so a task nobody holds can be collected half way.
        self._background: set[asyncio.Task] = set()
        self._running = False
        self._ring_callbacks: list[Callable] = []
        self._persist: Callable[[dict], None] | None = None
        self._last_ring_panel: str | None = None
        # Panels whose default door command was already logged (#58): once each.
        self._door_default_logged: set[str] = set()
        self._door_lock = asyncio.Lock()
        self._video_end_callbacks: list[Callable] = []  # video finito: la camera ferma lo stream di HA
        self._state_callbacks: list[Callable] = []
        self._model_callbacks: list[Callable] = []
        self._ws_broadcast_fn: Callable | None = None
        self._has_ws_clients: Callable | None = None
        # True from the start of an automatic hang-up until the call has ended
        # locally (plus HANGUP_LOCAL_SETTLE) or its BYE is done, whichever comes
        # first: a view opening meanwhile waits for it instead of joining a
        # dying call. _hangup_done is set when the flag drops.
        self._hanging_up = False
        self._hangup_done: asyncio.Event | None = None
        self._hangup_settle: asyncio.TimerHandle | None = None
        self._stream_viewers = 0
        self._hangup_task: asyncio.Task | None = None
        self._away_task: asyncio.Task | None = None
        # Registro squilli: aggiornamenti in ordine di richiesta (l'executor non lo garantisce).
        self._ring_log_lock = asyncio.Lock()
        self._auto_ended_at = -1e9  # monotonic: fine dell'ultima chiamata
        # When the last dialog on the line ended (local UDP: the panel needs a
        # few seconds before it answers the next INVITE, see LOCAL_UDP_SETTLE).
        self._dialog_ended_at = -1e9
        self._viewers_left_at = -1e9  # monotonic: l'ultimo spettatore di /av se n'è andato
        self._was_busy = False      # in_call or calling, all'ultimo cambio di stato
        self._photo_task: asyncio.Task | None = None
        self._ring_time: str | None = None  # chiave dell'ultimo squillo nel registro
        self._call_timeout_task: asyncio.Task | None = None
        self._keyframe_task: asyncio.Task | None = None
        self._keyframe_now: asyncio.Task | None = None
        media.request_keyframe = self._request_keyframe  # pacchetto video perso
        self._auto_called = False
        self._auto_gen = 0  # which auto-call is the current one (_do_auto_call)
        # A panel call waiting to be tried again (_call_panel): the line is
        # free for 2 s, but a call is coming, for /av and for a new view.
        self._panel_retry = False
        self._explicit_gen = 0  # which explicit call or hang-up is the latest

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
            "last_door_result": None,          # async_door's result key, e.g. "busy"
            "last_door_code": None,            # its SIP code, or None
            "last_door_command": None,         # body sent, e.g. "OPEN"
            "last_door_command_source": None,  # "phonebook" | "default" | "explicit"
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
            # Foto e clip dell'ultimo squillo (snapshot_dir): nome file, percorso, versione foto
            "last_photo": None, "last_photo_path": None, "last_photo_v": None,
            "last_clip": None, "last_clip_path": None,
            "started_at": datetime.now(UTC),
        }
        self._call_started_mono: float | None = None
        self._ring_answered = False
        # Segreteria di HA (switch Segreteria): spenta, _away_message non parte.
        # Niente VOICEMAIL;OFF da soli, solo su azione dell'utente.
        self._away_enabled = True
        self._ring_declined = False  # rifiutato da noi (603): non è uno squillo perso
        # Call-ID of the last real ring, and whether another device answered it (#164)
        self._ring_cid: str | None = None
        self._ring_elsewhere = False
        # HA answered the last ring (Rispondi or the away message). Unlike _ring_answered
        # it lasts until the next ring: a late C;<id>;ANSWERED must not rewrite the outcome.
        self._ring_taken = False
        # HA opened the door while the last ring was up: the 603 that silences the
        # house afterwards is not a decline.
        self._ring_opened = False
        self._was_ringing = False  # per il webhook di fine squillo, vedi _handle_broadcast
        # Callback per emettere eventi bus HA (registrati da __init__.py).
        # Evita di iniettare hass nell'hub, coerente con ring/state callbacks.
        self._event_callbacks: list[Callable] = []
        self._init_status_sent = False
        # SET_APT_PARAMS in attesa della risposta, per MSGID (PROTOCOL §3: l'unico
        # comando che correla la risposta per ID).
        self._apt_param_waiters: dict[str, asyncio.Future] = {}
        # Ricerca del PICG (servizio find_sga, issue #14): una scansione alla volta, e un
        # future che _handle_nicks_reply / la GET_INIT_STATUS_REPLY risolvono.
        self._scan_lock = asyncio.Lock()
        self._probe_waiter: asyncio.Future | None = None
        self._probe_kind: str | None = None   # "GET_NICKS" | "GET_INIT_STATUS"
        self._nicks_seq = 0   # quante GET_NICKS_REPLY sono arrivate
        self._init_seq = 0    # quante GET_INIT_STATUS_REPLY sono arrivate

    # ─── helpers stato esteso ────────────────────────────────────────────
    @staticmethod
    def _now():
        return datetime.now(UTC)

    def notify(self) -> None:
        """Rinfresca le entità (es. dopo una modifica delle opzioni fuori dall'hub)."""
        self._touch()

    def _touch(self):
        """Notifica le entità HA che le statistiche sono cambiate."""
        for cb in list(self._state_callbacks):  # un callback può togliersi (select.py)
            try:
                cb()
            except Exception:
                _LOGGER.exception("State callback error")

    @property
    def calling(self) -> bool:
        return sip.calling

    @property
    def devices(self) -> list[dict]:
        """The plant's devices seen so far on the SIP channel."""
        return sip.DEVICES.snapshot()

    @property
    def devices_public(self) -> list[dict]:
        """The device list as shown in Home Assistant: identifiers masked,
        no addresses."""
        return sip.DEVICES.snapshot(public=True)

    @property
    def devices_summary(self) -> list[str]:
        return sip.DEVICES.describe(SIP_ID_NAMES, public=True)

    def restore_devices(self, items) -> None:
        """The device list saved before a restart (see DeviceInventory.load)."""
        sip.DEVICES.load(items)

    @property
    def _busy_now(self) -> bool:
        """Una chiamata nostra in corso o in partenza."""
        return sip.in_call or sip.calling

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

    def ring_media(self) -> dict:
        """Foto e clip dell'ultimo squillo per i sensori: percorso su disco e URL (con
        l'autenticazione di HA, come la card) per le notifiche. La foto c'è ~1 s dopo
        lo squillo, il clip a squillo (o chiamata) finiti."""
        st = self.stats
        photo, clip = st.get("last_photo"), st.get("last_clip")
        return {
            "foto": st.get("last_photo_path"),
            "foto_url": f"/api/vimar_intercom/rings/{photo}?v={st.get('last_photo_v')}" if photo else None,
            "clip": st.get("last_clip_path"),
            "clip_url": f"/api/vimar_intercom/rings/{clip}" if clip else None,
        }

    @property
    def status(self) -> str:
        """Stato sintetico: offline / ringing / in_call / calling / idle."""
        # In chiamata prima dello squillo: un INVITE che arriva a chiamata in corso
        # (l'eco della nostra) non deve trasformare "Microfono" in "Rispondi".
        # "offline" solo dopo: una REGISTER di rinnovo fallita durante una chiamata
        # o uno squillo non deve far sparire la chiamata (la card chiuderebbe media e
        # microfono) mentre il media scorre ancora.
        if sip.in_call:
            return "in_call"
        if self.is_ringing:
            return "ringing"
        if sip.calling:
            return "calling"
        if not sip.registered:
            return "offline"
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
    def video_active(self) -> bool:
        """Il video arriva: chiamata attiva o anteprima dello squillo (early media)."""
        return sip.in_call or sip.early_media()

    @property
    def _call_in_view(self) -> bool:
        """A call of ours is up or starting (an auto-call too), or a ring is on.

        Shared by stream_opened and call_pending so the two cannot drift.
        """
        return bool(self._busy_now or sip.ringing() or self._auto_called or self._panel_retry)

    @property
    def call_coming(self) -> bool:
        """A call of ours is up or being placed (an auto-call too), or a ring
        is on. Not the open WebSockets that call_pending also counts."""
        return self._call_in_view

    @property
    def call_pending(self) -> bool:
        """Il video può ancora arrivare: chiamata attiva o in partenza (anche un
        auto-call), squillo, o l'app iOS collegata che chiamerà da sé. /av aspetta
        solo in questi casi, altrimenti 503 subito."""
        return bool(self._call_in_view
                    or (self._has_ws_clients and self._has_ws_clients()))

    @property
    def call_active(self) -> bool:
        """A call, an outgoing call or a ring is on: the SIP connection must not be dropped."""
        return self._busy_now or self.is_ringing

    @property
    def is_ringing(self) -> bool:
        """A ring is on: a real one, or the simulate_ring test ring."""
        return sip.ringing() or self._sim_ring is not None

    def state_message(self, **extra) -> dict:
        """/audio_ws "state", the same fields in every reply and broadcast. "ringing"
        lets a client joining mid-ring (an Echo through Scrypted) watch instead of
        calling over it."""
        return {"type": "state", "registered": self.registered, "in_call": self.in_call,
                "ringing": self.is_ringing, **extra}

    def _spawn(self, coro, name: str) -> asyncio.Task:
        """Run `coro` in the background, holding the task until it is done.

        asyncio keeps only a weak reference to a task: a bare create_task()
        can be garbage collected before it finishes. An exception is logged
        here, since nobody awaits the task.
        """
        task = asyncio.create_task(coro, name=f"vimar_intercom {name}")
        self._background.add(task)

        def _done(t: asyncio.Task) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                _LOGGER.error("%s failed: %s", name, t.exception(), exc_info=t.exception())

        task.add_done_callback(_done)
        return task

    def fire_ring_callbacks(self) -> None:
        """Evento doorbell → automazioni, e il webhook di inizio squillo."""
        if R.RING_WEBHOOK_URL:
            self._spawn(webhook.fire(R.RING_WEBHOOK_URL), "ring webhook")
        for cb in self._ring_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Ring callback error")

    def register_video_end_callback(self, callback: Callable) -> Callable[[], None]:
        """Chiamata o squillo finiti: non arriva più video. Restituisce l'annullamento."""
        self._video_end_callbacks.append(callback)
        return lambda: self._video_end_callbacks.remove(callback)

    def _video_ended(self) -> None:
        for cb in self._video_end_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Video callback error")

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
        return S.DETECTED_MODEL

    def register_model_callback(self, callback: Callable) -> None:
        """Callback(model, fw, user_agent, priority) sul rilevamento modello."""
        self._model_callbacks.append(callback)

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
            if S.DETECTED_MODEL:
                break
            uri = sip_uri(target)
            try:
                await sip.do_options(target=uri)
            except Exception as e:
                _LOGGER.debug("Model probe %s fallito: %s", target, e)
            await asyncio.sleep(0.5)

        if S.DETECTED_MODEL:
            _LOGGER.info("Modello citofono: %s", S.DETECTED_MODEL)
        else:
            _LOGGER.info(
                "Modello non rilevato — nessun peer SIP si è identificato. "
                "User-Agent visti finora: %s", sorted(sip._seen_uas) or "nessuno")

    def _on_sip_state_change(self):
        """Called by sip_client when registered/in_call changes."""
        # Fine di QUALSIASI chiamata (anche rifiutata, anche fatta da "Vedi esterno"),
        # nell'istante in cui in_call/calling scendono: prima del call_ended, che
        # arriva dopo stop_media, quando go2rtc ha già riaperto /av. Fino alla 1.0.9
        # la pausa valeva solo dopo un auto-call chiuso dalla targa: chiusa una
        # chiamata dalla card, go2rtc riapriva /av e l'hub richiamava da solo
        # (→ 486 dalla targa ancora occupata → un altro auto-call al retry).
        busy = self._busy_now
        if self._was_busy and not busy:
            self._auto_ended_at = self._dialog_ended_at = time.monotonic()
            self._video_ended()
        self._was_busy = busy
        if self._hanging_up and not busy and self._hangup_settle is None and not R.USE_LOCAL_UDP:
            # The hang-up has ended the call locally; its BYE may still wait
            # for an answer that never comes on the cloud. On local UDP the
            # panel does answer the BYE, and it is busy until it has (#41):
            # there the guard drops only when the BYE is done
            # (_hangup_finished), still within HANGUP_SETTLE for a view.
            try:
                self._hangup_settle = asyncio.get_running_loop().call_later(
                    HANGUP_LOCAL_SETTLE, self._end_hanging_up, self._hangup_done)
            except RuntimeError:
                self._end_hanging_up(self._hangup_done)
        self._touch()
        # Notify WS clients of state change
        if self._ws_broadcast_fn:
            self._spawn(self._ws_broadcast_fn(self.state_message()), "WS state broadcast")

    async def stream_opened(self, reflex_guard: bool = True) -> bool:
        """Uno spettatore apre /av. False = niente chiamata in vista, inutile aspettare.

        ``reflex_guard=False`` for a viewer that is always a person (a HomeKit
        view): the quick-reopen pause exists for go2rtc and the stream worker
        reopening /av on their own, and it refused a person reopening the view
        a few seconds after closing it (the Home app then waits 30 s)."""
        self._stream_viewers += 1
        _LOGGER.info("Stream opened (%d viewers)", self._stream_viewers)

        if self._hangup_task:
            self._hangup_task.cancel()
            self._hangup_task = None

        # A hang-up in progress means that call is dying: joining it means seeing
        # it close a moment later. Wait for it to end, then call again.
        if self._hanging_up and self._hangup_done is not None:
            started = time.monotonic()
            try:
                await asyncio.wait_for(self._hangup_done.wait(), HANGUP_SETTLE)
            except TimeoutError:
                pass
            _LOGGER.info("Hang-up in progress: waited %.1fs before calling again",
                         time.monotonic() - started)
            if self._stream_viewers == 0:
                # The viewer left while waiting: nobody to call for.
                _LOGGER.info("The viewer left during the hang-up: no call")
                return False

        # Chiamata in corso, in partenza o in arrivo (vedi call_pending): si aspetta il
        # video, senza chiamare né rispondere da soli (uno stream aperto ruberebbe lo
        # squillo al Tab). Altrimenti, nella pausa AUTO_CALL_COOLDOWN, una riapertura
        # di riflesso (l'ultimo spettatore è uscito dopo la fine) non chiama e riceve 503.
        # Not call_pending: an audio WebSocket open anywhere (a dashboard with the
        # card, a phone with the app) is global state, not this viewer's call,
        # and it used to stop every HomeKit view from calling: 15 s of waiting,
        # then "No response" in the Home app without a line in the log.
        if self._call_in_view:
            return True
        now = time.monotonic()
        if (reflex_guard and self._viewers_left_at >= self._auto_ended_at
                and now - self._auto_ended_at < AUTO_CALL_COOLDOWN):
            _LOGGER.info("Stream riaperto subito dopo la fine del precedente: "
                         "riconnessione, niente auto-call")
            return False

        if sip.registered:
            self._auto_called = True
            self._auto_gen += 1
            # Fire auto-call as background task — don't block the HTTP response
            self._spawn(self._do_auto_call(self._auto_gen), "auto-call")
            return True
        return False

    async def _do_auto_call(self, gen: int | None = None):
        """Background auto-call when video stream opens without active call.

        gen: the auto-call this is. A failure resets the auto-call flag only
        if no newer auto-call has started meanwhile: it is that one's flag.
        """
        try:
            # Default di do_call: R.INTERCOM, cioè la targa video (camera_target).
            ok, msg = await self._call_panel(None, lambda: self._view_still_waits(gen),
                                             silence_limit=R.VIEW_KEEPALIVE)
            alt = None if ok else self._camera_fallback(msg)
            if alt:
                _LOGGER.warning(
                    "Video panel %s did not answer (%s): trying %s, the panel "
                    "that last rang", R.CAMERA_TARGET, msg, alt)
                ok, msg = await self._call_panel(sip_uri(alt), lambda: self._view_still_waits(gen),
                                                 silence_limit=R.VIEW_KEEPALIVE)
                if ok:
                    self._learn_camera_target(alt)
        except Exception as e:  # noqa: BLE001
            ok, msg = False, str(e)
        current = gen is None or gen == self._auto_gen
        if (ok and current and self._stream_viewers == 0 and self._auto_called
                and self._busy_now and not self._hanging_up
                and (self._hangup_task is None or self._hangup_task.done())):
            # The viewer left between stream_opened and do_call raising
            # `calling`: stream_closed saw no call and scheduled nothing, so
            # the call would stay up with nobody watching until the 5 minute cap.
            _LOGGER.info("Auto-call connected with no viewers left: hanging up")
            self._hangup_task = asyncio.create_task(self._delayed_hangup())
        if not ok:
            # Anche un fallito che non è mai arrivato a `calling` (es. squillo in
            # corso): i retry di go2rtc non devono richiamare subito.
            _LOGGER.error("Auto-call failed: %s", msg)
            if gen is None or gen == self._auto_gen:
                self._auto_called = False
            self._auto_ended_at = time.monotonic()

    async def _call_panel(self, target, still_wanted, **kw) -> tuple[bool, str]:
        """Call a video panel (None: R.INTERCOM). On local UDP: let the panel
        settle after the last dialog, and try again a call it swallows (see
        LOCAL_UDP_SETTLE), while `still_wanted()`. The cloud call is
        unchanged: at once, 45 s, no retry."""
        if not R.USE_LOCAL_UDP:
            return await sip.do_call(target=target, **kw)
        tapped, started = time.monotonic(), None
        tries, last_try = 0, -1e9

        def time_left() -> float:
            now = time.monotonic()
            return min(LOCAL_UDP_CALL_BUDGET - (now - started), LOCAL_UDP_TAP_LIMIT - (now - tapped))
        ok, msg = False, "Not wanted any more"
        while True:
            if not await self._settle(still_wanted, last_try):
                return ok, msg
            if started is None:
                # The budget counts from the first INVITE, not from the tap: a
                # settle right after a hang-up (up to 5 s) left no room for a
                # second try, in the case that needs it (#44, 40507). The tap
                # limit keeps every try inside /av's 25 s: a call answered after
                # /av gave up would be one nobody watches.
                started = time.monotonic()
            left = time_left()
            tries += 1
            ok, msg = await sip.do_call(
                target=target, answer_timeout=max(1.0, min(LOCAL_UDP_ANSWER_TIMEOUT, left)),
                ring_timeout=LOCAL_UDP_RING_TIMEOUT, **kw)
            last_try = time.monotonic()
            if ok or not msg.startswith(sip.NO_ANSWER) or not still_wanted():
                return ok, msg
            left = time_left()
            if left < LOCAL_UDP_SETTLE + LOCAL_UDP_MIN_TRY:
                _LOGGER.warning("The panel did not answer (%s) after %d tries in %.0fs: giving up",
                                msg, tries, last_try - started)
                return ok, msg
            _LOGGER.warning("The panel did not answer (%s): trying again in %.0fs",
                            msg, LOCAL_UDP_SETTLE)

    async def _settle(self, still_wanted, last_try: float) -> bool:
        """Wait until LOCAL_UDP_SETTLE after the last dialog on the line, or our
        own last try, whichever is later. Meanwhile a call is coming: /av keeps
        waiting and a new view does not place its own. False: not wanted any
        more."""
        wait = LOCAL_UDP_SETTLE - (time.monotonic() - max(self._dialog_ended_at, last_try))
        if wait <= 0:
            return True
        _LOGGER.info("Panel call in %.1fs: the panel is settling after the last call", wait)
        self._panel_retry = True
        try:
            await asyncio.sleep(wait)
        finally:
            self._panel_retry = False
        return still_wanted()

    def _view_still_waits(self, gen: int | None) -> bool:
        """This auto-call is still wanted: the newest one, a viewer waiting,
        and nothing else started on the line meanwhile."""
        return ((gen is None or gen == self._auto_gen) and self._stream_viewers > 0
                and self._auto_called and not self._busy_now and not sip.ringing())

    def set_persist_callback(self, callback: Callable[[dict], None]) -> None:
        """Who saves the values learned from the plant into the entry."""
        self._persist = callback

    def _camera_fallback(self, result: str) -> str | None:
        """Another video panel to try when the configured one does not exist.

        The default panel id comes from the reference plant; on another plant
        the same call answers 404 and auto-call never starts. The panel that
        last rang certainly exists and sends video. Only when nobody chose the
        panel: an explicit choice is never replaced. A busy panel (486) is not
        a missing one, and neither is one temporarily unavailable (480).
        """
        if R.CAMERA_TARGET_CONFIGURED:
            return None
        code = (result or "").split(" ", 1)[0]
        if code not in ("404", "604"):
            return None
        alt = self._last_ring_panel
        return alt if alt and alt != R.CAMERA_TARGET else None

    def _learn_camera_target(self, panel: str) -> None:
        R.CAMERA_TARGET = panel
        R.INTERCOM = sip_uri(panel)
        _LOGGER.warning(
            "Video panel learned from the plant and saved: %s. To forget it, set "
            "the camera target in the integration options (that replaces "
            "learned_camera_target)", panel)
        if self._persist:
            self._persist({"learned_camera_target": panel})

    async def stream_closed(self):
        self._stream_viewers = max(0, self._stream_viewers - 1)
        _LOGGER.info("Stream viewer disconnected (%d remaining)", self._stream_viewers)
        if self._stream_viewers == 0:
            self._viewers_left_at = time.monotonic()

        # Anche mentre collega (calling): chi apre la camera e la richiude prima
        # della risposta (il cloud ci mette anche 15 s) lasciava la chiamata aperta
        # senza spettatori fino al tetto di 5 minuti. do_hangup allora annulla.
        if self._stream_viewers == 0 and self._auto_called and self._busy_now:
            self._hangup_task = asyncio.create_task(self._delayed_hangup())

    def should_hang_up_for_viewers(self, answered_for_them: bool = False) -> bool:
        """The last viewer is gone: does that end the call?

        True with no viewer left, on an auto-call still up, or on a call the
        caller answered for its viewers (``answered_for_them``). A HomeKit view
        uses it to hang up at once instead of after STREAM_HANGUP_DELAY."""
        if self._stream_viewers:
            return False
        return answered_for_them or bool(self._auto_called and self._busy_now)

    async def _delayed_hangup(self):
        try:
            await asyncio.sleep(STREAM_HANGUP_DELAY)
            if self._stream_viewers == 0 and self._auto_called and self._busy_now:
                _LOGGER.info("No viewers, hanging up auto-call")
                self._auto_called = False
                # From here the hang-up runs to the end. A view opening now
                # cancels this task, and that used to cut the BYE half way:
                # in_call stayed true, the panel stayed busy (486 on the next
                # call). The flag is cleared by the local end of the call (see
                # _on_sip_state_change) or by the end of the BYE, not of this
                # task, so the new view keeps waiting for it.
                done = self._begin_hanging_up()
                bye = asyncio.ensure_future(
                    asyncio.wait_for(sip.do_hangup(), HANGUP_BYE_TIMEOUT))
                bye.add_done_callback(functools.partial(self._hangup_finished, done))
                # Held until done: it outlives this task when a view cancels it.
                self._background.add(bye)
                bye.add_done_callback(self._background.discard)
                try:
                    await asyncio.shield(bye)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    pass  # logged by _hangup_finished
        except asyncio.CancelledError:
            pass

    def _begin_hanging_up(self) -> asyncio.Event:
        """Raise the hang-up guard; the Event returned identifies this guard."""
        if self._hangup_settle is not None:
            self._hangup_settle.cancel()
            self._hangup_settle = None
        self._hanging_up = True
        self._hangup_done = asyncio.Event()
        return self._hangup_done

    def _end_hanging_up(self, done: asyncio.Event | None = None) -> None:
        """Drop the hang-up guard `done` (the current one when None).

        A hang-up that finishes after a newer one started only wakes its own
        waiters: the newer guard stays up until its own call has ended.
        """
        if done is not None and done is not self._hangup_done:
            done.set()
            return
        if self._hangup_settle is not None:
            self._hangup_settle.cancel()
            self._hangup_settle = None
        self._hanging_up = False
        if self._hangup_done is not None:
            self._hangup_done.set()

    def _hangup_finished(self, done: asyncio.Event, task: asyncio.Future,
                         log: bool = True) -> None:
        """Done callback of a hang-up: log its failure, drop its own guard."""
        if log and not task.cancelled() and (exc := task.exception()) is not None:
            if isinstance(exc, asyncio.TimeoutError):
                _LOGGER.warning("Automatic hang-up: no end after %.0fs",
                                HANGUP_BYE_TIMEOUT)
            else:
                _LOGGER.warning("Automatic hang-up failed: %r", exc)
        self._end_hanging_up(done)

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
                self._auto_called = False
                await sip.do_hangup()
        except asyncio.CancelledError:
            pass

    def _request_keyframe(self):
        """Pacchetto video perso (media_handler): keyframe subito, non al prossimo IDR."""
        self._keyframe_now = asyncio.create_task(sip.send_keyframe_request())

    def _start_keyframe_loop(self):
        """Ask for a keyframe at call start (a short burst, see _keyframe_loop)."""
        self._cancel_keyframe_loop()
        self._keyframe_task = asyncio.create_task(self._keyframe_loop())

    def _cancel_keyframe_loop(self):
        if self._keyframe_task:
            self._keyframe_task.cancel()
            self._keyframe_task = None

    async def _keyframe_loop(self):
        """Keyframe burst at call start, and nothing after it.

        Il burst serve a ottenere SPS/PPS+IDR appena parte il video, e si ferma
        appena i pacchetti video arrivano.

        No periodic refresh afterwards: on the plants tested the panel ignores
        picture_fast_update and sends its keyframes on its own clock (about
        every 3 s on a Tab 7S Up 40517, not configurable), and each INFO is a
        transaction through the cloud relay for the whole call. A lost video
        packet still asks for a keyframe at once (media_handler._lost).
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
        except asyncio.CancelledError:
            pass

    async def async_start(self, sps_store=None):
        """sps_store: Store di HA con gli ultimi SPS/PPS della targa (media.restore_sps_pps)."""
        if self._running:
            return

        sip.init(self._handle_broadcast)
        sip.set_state_callback(self._on_sip_state_change)
        sip.set_model_callback(self._on_model_detected)
        media.init(self._handle_broadcast)

        # Blocking DNS lookup in cloud mode (see sip.connect): off the loop.
        sip.MY_IP = await asyncio.get_running_loop().run_in_executor(None, sip.get_local_ip)
        sip.incoming_requests = asyncio.Queue()
        _LOGGER.info("Local IP: %s", sip.MY_IP)

        await media.setup_transports()
        if sps_store:
            await media.restore_sps_pps(sps_store)
        _LOGGER.info("RTP transports ready")

        await sip.connect()

        self._tasks.append(asyncio.create_task(sip.reader_task()))
        self._tasks.append(asyncio.create_task(sip.request_processor()))
        self._tasks.append(asyncio.create_task(self._auto_startup()))
        self._tasks.append(asyncio.create_task(self._keepalive_loop()))
        self._running = True

    async def async_stop(self):
        self._running = False
        if self._busy_now:
            # Unloading mid-call left the panel on until its own timeout.
            # On the cloud this wait always runs to the 3 s timeout: the BYE
            # leaves and the call ends locally at once, but the relay never
            # answers the BYE, and do_hangup waits for that answer.
            try:
                await asyncio.wait_for(sip.do_hangup(), timeout=3)
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("Hang-up on unload failed: %s", e)
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        if self._hangup_task:
            self._hangup_task.cancel()
        # Webhooks, auto-call, a hang-up's BYE: nothing of this hub may run on
        # after unload (a stale auto-call would call from the unloaded hub).
        for t in list(self._background):
            t.cancel()
        if self._hangup_settle is not None:
            self._hangup_settle.cancel()
            self._hangup_settle = None
        sip.cancel_reconnect()  # non deve riconnettere un hub scaricato
        self._cancel_away()
        self._sim_ring = None  # its task went with _background; no end webhook, as for a real ring
        if self._photo_task:
            self._photo_task.cancel()
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
        self._explicit_gen += 1
        uri = sip_uri(target) if target else None
        if uri is None or uri == R.INTERCOM:
            # The video panel (the card's "view outside", the call buttons):
            # the same timeout and single retry as a view's auto-call (#44),
            # unless the user hangs up or calls again, or a ring starts, meanwhile.
            gen = self._explicit_gen
            return await self._call_panel(
                uri, lambda: gen == self._explicit_gen and not self._busy_now and not sip.ringing())
        # A flat or the switchboard: a person answers, it may ring for long.
        return await sip.do_call(target=uri)

    def claim_call(self) -> None:
        """Qualcuno parla (microfono sul WS audio): la chiamata è sua. Il messaggio
        di assenza non la chiude a fine file, un auto-call non viene riagganciato
        quando lo stream video si chiude."""
        self._cancel_away()
        self._auto_called = False
        media.claim_voice()

    async def async_answer(self) -> tuple[bool, str]:
        if self._stop_simulated_ring():
            return False, "Squillo di prova: niente da rispondere"
        if self._away_task and not self._away_task.done() and sip.in_call:
            # Sta suonando il messaggio di assenza: "Rispondi" prende la chiamata
            # (annullato, il messaggio non riaggancia).
            self._away_task.cancel()
            ok, msg = True, "Chiamata presa dal messaggio di assenza"
        elif sip.in_call:
            # Un INVITE a chiamata in corso (l'eco della nostra): rispondergli
            # sovrascriverebbe il dialogo attivo e la chiamata vera cadrebbe.
            ok, msg = False, "Già in chiamata"
        else:
            ok, msg = await sip.do_answer_incoming()
        if ok:
            self._ring_answered = self._ring_taken = True
            self.stats["last_call_direction"] = "in"
            self._log_outcome("answered")
        self._touch()
        return ok, msg

    @property
    def away_enabled(self) -> bool:
        return self._away_enabled

    def on_voicemail_on(self) -> None:
        """La segreteria del Tab è (o sta per essere) accesa, comunque lo si sia saputo
        (annuncio, GET_INIT_STATUS, comando dello switch): o quella del Tab o quella di HA."""
        self.set_away_enabled(False)

    def set_away_enabled(self, on: bool) -> None:
        """Accende/spegne il messaggio di assenza di HA; spento annulla anche quello in attesa."""
        self._away_enabled = on
        if not on:
            self._cancel_away()
        self._touch()

    async def async_decline(self) -> tuple[bool, str]:
        """Rifiuta lo squillo con 603, come l'app: il PBX smette di far suonare tutta la casa."""
        # Prima della chiamata: il ring_ended parte da dentro do_decline_incoming.
        if self._stop_simulated_ring():
            return True, "Squillo di prova chiuso"
        self._ring_declined = True
        declined = await sip.do_decline_incoming()
        self._ring_declined = bool(declined)
        if declined and not self._ring_opened:
            self._log_outcome("declined")
        self._touch()
        return (True, "Squillo rifiutato") if declined else (False, "Nessuna chiamata in arrivo")


    async def async_hangup(self) -> None:
        """Hang up (the Hang up button, the card, HomeKit).

        Goes through the same guard as the automatic hang-up: a view opening
        meanwhile waits for this call to end instead of joining it. The BYE
        runs in the background and this returns once the call has ended
        locally: the cloud never answers a BYE, and waiting for that answer
        held the caller for 5 s. A hang-up that fails before the call ends
        locally raises, as before.
        """
        self._auto_called = False  # chiusa da noi: niente riaggancio automatico
        self._explicit_gen += 1   # a panel call waiting for its retry is not retried
        self._cancel_call_timeout()
        ended = asyncio.Event()
        done = self._begin_hanging_up()
        bye = self._spawn(self._bye(ended.set), "hang-up")
        bye.add_done_callback(functools.partial(self._hangup_finished, done, log=False))
        local_end = asyncio.ensure_future(ended.wait())
        try:
            await asyncio.wait({bye, local_end}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            local_end.cancel()
        if bye.done() and not bye.cancelled() and bye.exception() is not None:
            raise bye.exception()

    async def _bye(self, on_local_end: Callable[[], None]) -> None:
        try:
            await asyncio.wait_for(sip.do_hangup(on_local_end=on_local_end),
                                   HANGUP_BYE_TIMEOUT)
        except TimeoutError:
            _LOGGER.warning("Hang-up: no end after %.0fs", HANGUP_BYE_TIMEOUT)

    async def async_door(self, target: str | None = None, command: str | None = None) -> tuple[bool, str, int | None]:
        """(ok, result key, SIP code or None).

        One door command at a time, from any caller (card, lock, button, HomeKit):
        a tap while one still waits for the relay (20 s or more) would send a second copy.
        The command runs shielded: a cancelled caller (an automation in restart mode)
        must not free the lock while the MESSAGE is still in flight."""
        if self._door_lock.locked():
            return False, DOOR_BUSY, None
        await self._door_lock.acquire()  # free, so taken at once: no await before it
        task = self._spawn(self._door(target, command), "door")
        task.add_done_callback(lambda _: self._door_lock.release())
        return await asyncio.shield(task)

    async def _door(self, target: str | None, command: str | None) -> tuple[bool, str, int | None]:
        """Open door via SIP MESSAGE to targa (PE) address.

        From Tab5S rubrica ACTUATOR_LIST:
          55001 (targa master)  → OPEN_2F = Portone Esterno
          55002 (targa interna) → OPEN_2F = Portone Interno
        The targa forwards the command to its local relay.
        No active call required.
        """
        # Senza target: la targa che apre la porta (R.DOOR_TARGET, dalla
        # rubrica), non l'SGA — su un 2FV2 l'SGA risponde 200 e non apre.
        uri = sip_uri(target) if target else R.DOOR_ESTERNO
        door_target = target or R.DOOR_TARGET
        # The body is the phonebook's MSG for that panel's door actuator (#58);
        # OPEN_2F only when the phonebook has none. An explicit command wins.
        if command:
            body, source = command, "explicit"
        else:
            body, source = R.door_command_for(door_target)
            if source == "default" and door_target not in self._door_default_logged:
                # Once per panel: installs without a phonebook would log it on every open.
                self._door_default_logged.add(door_target)
                _LOGGER.info("Door command: no door actuator for %s in the phonebook, sending the default %s",
                             door_target, body)

        _LOGGER.info("Door command: uri=%s body=%s (%s) registered=%s", uri, body, source, sip.registered)
        # The ring up when the command goes out: the panel's answer (or the retry)
        # can come after the ring has ended, and it was still opened during it.
        ring = self._ring_cid if self._ring_cid and sip.ringing(self._ring_cid) else None

        ok, msg, code, may_retry = await self._door_message(uri, body)

        self.stats["last_door_time"] = self._now()
        self.stats["last_door_target"] = door_target
        self.stats["last_door_command"] = body
        self.stats["last_door_command_source"] = source
        self.stats["last_door_result"] = msg
        self.stats["last_door_code"] = code
        if ok:
            self.stats["door_count"] += 1
        self._touch()

        if ok:
            _LOGGER.info("Door open OK: %s", code)
            self._opened_during_ring(ring)
            return ok, msg, code

        if not may_retry:
            return False, msg, code  # a second copy could open the door twice

        # Retry once after re-registration — handles stale connection
        _LOGGER.warning("Door command failed (%s %s), retrying after re-register...", msg, code)
        try:
            reg_ok = await sip.do_register()
            if reg_ok:
                ok2, msg2, code2, _ = await self._door_message(uri, body)
                self.stats["last_door_result"] = msg2
                self.stats["last_door_code"] = code2
                if ok2:
                    self.stats["door_count"] += 1
                    self._touch()
                    _LOGGER.info("Door open OK on retry: %s", code2)
                    self._opened_during_ring(ring)
                    return ok2, msg2, code2
                self._touch()
                _LOGGER.error("Door retry also failed: %s %s", msg2, code2)
                return ok2, msg2, code2
            else:
                _LOGGER.error("Re-registration failed, cannot retry door")
                return False, DOOR_NOT_REGISTERED, None
        except Exception as e:
            _LOGGER.error("Door retry error: %s", e)
            return False, DOOR_SEND_FAILED, None

    def _opened_during_ring(self, ring: str | None) -> None:
        """The door opened during `ring` (None: no ring was up): the ring log says
        Aperto, unless HA answered it (Rispondi or the away message).

        If the ring ended while the command was in flight, ring_ended has already
        counted it in missed_count. Left as is on purpose: undoing it would mean
        knowing whether ring_ended has run yet, a lot of code for a rare one-off."""
        if ring and ring == self._ring_cid and not self._ring_taken:
            self._ring_opened = True
            self._log_outcome("opened")

    @staticmethod
    async def _door_message(uri: str, body: str) -> tuple[bool, str, int | None, bool]:
        """One door MESSAGE: (ok, result key, SIP code, may_retry), may_retry False when a second copy
        could open the door twice. A 202 is not an open door: real opens answer
        200, the relay's 202 was only ever seen with no device behind it (#14).
        Over the cloud no answer, a failed signed send, or a 408/504 from the
        relay (forwarded, the door did not answer in time) may still reach the
        door: the relay can answer late (#14: 202 after ~15.2 s). sip.NOT_SENT and
        other final codes are retried."""
        try:
            ok, msg, code = await sip.send_message(
                uri, body, extra_headers={"Panda": "command"}, timeout=_message_timeout()
            )
        except OSError as e:
            # Retried. TLS: unsigned leg, assumes the relay always 407s it rather than forwarding.
            # UDP: same exposure as the UDP timeout retry, accepted.
            _LOGGER.warning("Door command to %s: send failed: %s", uri, e)
            return False, DOOR_SEND_FAILED, None, True
        if not R.USE_LOCAL_UDP and code in (None, 408, 504):
            _LOGGER.warning("Door command to %s: %s, not retried: the relay may still deliver it", uri, msg)
            return False, DOOR_UNCONFIRMED, code, False
        if code == 202:
            _LOGGER.warning(
                "Door command to %s: the relay answered 202 Accepted (queued, no device confirmed "
                "it), reported as not opened. If the door did open, please report it on GitHub.",
                uri,
            )
            return False, DOOR_QUEUED, code, False
        if ok:
            return True, DOOR_OPENED, code, True
        if code == sip.NOT_SENT:
            return False, DOOR_NOT_REGISTERED, None, True
        if code is None:
            return False, DOOR_TIMEOUT, None, True
        return False, DOOR_ERROR, code, True

    async def async_send_command(
        self,
        body: str,
        target: str | None = None,
        header_name: str | None = "Panda",
        header_value: str | None = "command",
    ) -> tuple[bool, str]:
        """Invia un SIP MESSAGE arbitrario al citofono (per test / comandi non ancora mappati).

        target può essere un ID (es. "55001") oppure un URI sip: completo.
        """
        target = target or R.SGA_TARGET
        if target.startswith("sip:"):
            # A whole URI goes into the request line as it is: only a plant id
            # on one of the plant's own domains, exactly sip:<digits>@<domain>.
            if not _plant_uri(target):
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
            ok, msg, code = await sip.send_message(uri, body, extra_headers=headers, timeout=_message_timeout())
        except Exception as e:  # noqa: BLE001
            ok, msg, code = False, str(e), None
        if code == 202:
            # Same as the door: the relay holds it, no device confirmed it (#14). Not resent.
            _LOGGER.warning(
                "Command to %s: the relay answered 202 Accepted (queued, no device confirmed it), reported as not done",
                uri,
            )
            ok, msg = False, QUEUED
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

        # The end of an earlier call, arriving after another one started
        # (do_hangup broadcasts only after stop_media): stale for the card too,
        # which would close the view of the call that is up.
        stale_end = msg_type == "call_ended" and self._busy_now
        if msg_type in ("ring", "ring_ended", "call_started", "call_ended", "registered", "error"):
            # Don't broadcast "ring" to WS clients if we initiated the call
            if msg_type == "ring" and self._busy_now:
                pass  # Will be handled below (suppress + decline)
            elif stale_end:
                pass
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

        if msg_type == "ring_ended":
            self._cancel_away()  # squillo annullato: niente messaggio per lui
            if not self._busy_now:
                self._video_ended()
            # Finita l'anteprima si chiude /av, e go2rtc lo riapre: non è uno spettatore.
            if self._stream_viewers:
                self._auto_ended_at = time.monotonic()
        elif msg_type == "call_started":
            self._start_call_timeout()
            self._start_keyframe_loop()
        elif stale_end:
            # The end of an earlier call, arriving after another one started
            # (do_hangup broadcasts only after stop_media): touching timers and
            # flags now would break the new call.
            _LOGGER.debug("Late call_ended: another call is already up, ignored")
        elif msg_type == "call_ended":
            self._cancel_call_timeout()
            self._cancel_keyframe_loop()
            # La chiamata e' chiusa: da qui in poi un INVITE in arrivo e' uno
            # squillo vero, non l'eco della nostra. Senza questo reset il ramo
            # "ring" piu' sotto continuerebbe a rispondere 603 Decline per
            # sempre quando a chiudere e' stato il citofono (il watchdog
            # _delayed_hangup non arriva: la sua guardia richiede sip.in_call).
            self._auto_called = False

        if msg_type == "ring":
            # If we initiated the call (tap to view / auto-call), the Tab5S
            # sends an INVITE back to us. Suppress ring + push — this is NOT
            # a doorbell ring, just the PBX echoing our outgoing call.
            # Non _auto_called: resta vero dal BYE della targa fino al call_ended
            # (dopo stop_media), e un visitatore che suona in quell'istante (la
            # targa chiude la visione e squilla) riceveva 603, che il PBX propaga
            # annullando lo squillo anche sul Tab.
            if self._busy_now:
                _LOGGER.info("Squillo ignorato: è l'eco della nostra chiamata (in_call=%s, calling=%s)",
                             sip.in_call, sip.calling)
                # 486, not the default 603: a 6xx means "decline everywhere", and
                # the PBX propagates it and cancels the ring on the Tab too.
                self._spawn(sip.do_decline_incoming("486 Busy Here"), "echo ring decline")
                return

            # Solo qui, non in fire_ring_callbacks(): quel metodo lo chiama anche il
            # servizio simulate_ring (test, senza SIP), che non ha un ring_ended o un
            # call_started dietro a chiudere lo squillo — il webhook di fine resterebbe
            # armato per sempre e scatterebbe al prossimo evento qualsiasi.
            # Un altro "ring" mentre si squilla già (re-INVITE) non riarma il webhook di
            # partenza: ne uscirebbero due per una fine sola (self._was_ringing sotto).
            # A real ring ends the test ring first: its end webhook, then this
            # ring's start, so the two never overlap.
            self._stop_simulated_ring()
            if not self._was_ringing:
                self.fire_ring_callbacks()
            self._was_ringing = True
            # IDR subito (INFO nel dialogo early del 183), non al giro della targa (~3 s):
            # foto, clip e card partono prima.
            self._request_keyframe()

            if R.SNAPSHOT_DIR:
                now = datetime.now().astimezone()
                # Millisecondi: due squilli nello stesso secondo (CANCEL e INVITE nuovo)
                # restano due voci, ognuna col suo esito e la sua foto.
                stem = now.strftime("squillo_%Y%m%d_%H%M%S_") + f"{now.microsecond // 1000:03d}"
                name = stem + ".jpg"
                self._ring_time = now.isoformat(timespec="milliseconds")
                ring = {"time": self._ring_time, "photo": name, "clip": stem + ".mp4",
                        "outcome": "missed", "caller": _uri_to_id(sip.pending_incoming.get("caller_uri"))}
                self._spawn(self._ring_log(lambda rings: rings.append(ring)), "ring log")
                # Clip come un Ring: il video dello squillo, dall'anteprima alla fine (o
                # alla fine della chiamata se rispondiamo noi), fino a CLIP_MAX_S.
                frame_grabber.record(os.path.join(R.SNAPSHOT_DIR, stem + ".mp4"), CLIP_MAX_S, self._clip_done)
                if self._photo_task:
                    self._photo_task.cancel()
                self._photo_task = asyncio.create_task(self._save_ring_photo(name))
            if self._away_enabled and R.away_message_configured():
                self._cancel_away()
                self._away_task = asyncio.create_task(
                    self._away_message(sip.pending_incoming["cid"]))

        # Era uno squillo vero (self._was_ringing, armato sopra nel ramo "ring") e
        # ora non lo è più: risposto (call_started — sip chiude lo squillo PRIMA di
        # diffondere l'evento, quindi qui is_ringing è già False), annullato o
        # scaduto (ring_ended, stesso ordine). Non basta guardare solo msg_type
        # "ring_ended": un INVITE risposto passa da "call_started", non da lì.
        if self._was_ringing and not self.is_ringing:
            self._ring_over()

    def _answered_elsewhere(self, cid: str | None = None) -> None:
        """Another device answered the last ring (indoor monitor, Vimar app): the ring
        log says so instead of "missed". Two signals, either one is enough: the CANCEL
        with cause=200 and the `C;<call_id>;ANSWERED` message that follows (#164)."""
        if cid is not None and cid != self._ring_cid:
            return
        if (self._ring_answered or self._ring_taken or self._ring_declined
                or self._ring_elsewhere or self._ring_opened):
            return
        self._ring_elsewhere = True
        _LOGGER.info("Ring answered on another device")
        self._log_outcome("answered_elsewhere")

    def _ring_over(self) -> None:
        self._was_ringing = False
        if R.RING_END_WEBHOOK_URL:
            self._spawn(webhook.fire(R.RING_END_WEBHOOK_URL), "ring end webhook")

    def simulate_ring(self, duration: float) -> bool:
        """Test ring (simulate_ring service): "ringing" for `duration` s, then an
        unanswered end, with the doorbell event and both webhooks. No SIP at all:
        sip.ringing() stays False, so nothing can answer it (the away message
        only runs for a real INVITE) and a real ring is not refused as busy.
        Not in the ring log or the stats: there is no visitor, photo or clip."""
        if self.call_active:
            return False
        self._sim_ring = self._spawn(self._simulated_ring(duration), "simulated ring")
        self._was_ringing = True
        self.fire_ring_callbacks()
        self._touch()
        return True

    async def _simulated_ring(self, duration: float) -> None:
        await asyncio.sleep(duration)
        self._stop_simulated_ring()

    def _stop_simulated_ring(self) -> bool:
        """End the test ring, if one is on (timeout, a real ring, answer/decline,
        unload). True if there was one."""
        task, self._sim_ring = self._sim_ring, None
        if task is None:
            return False
        if task is not asyncio.current_task():
            task.cancel()
        self._ring_over()
        self._touch()
        return True

    def _cancel_away(self) -> None:
        if self._away_task and not self._away_task.done():
            self._away_task.cancel()
        self._away_task = None

    async def _away_message(self, ring_cid) -> None:
        """Se dopo AWAY_MESSAGE_DELAY s QUESTO squillo suona ancora (nessuno ha
        risposto: Tab, telefono o HA), risponde, fa sentire il file (o il testo
        letto dal TTS, se non c'è un file) e riaggancia."""
        # Un solo ritardo: quello della segreteria del Tab, se il Tab lo ha dichiarato.
        await asyncio.sleep(self.stats.get("vm_timeout") or R.AWAY_MESSAGE_DELAY or C.DEFAULT_AWAY_DELAY)
        if not sip.ringing(ring_cid) or sip.in_call:
            return
        pcm = await (media.load_pcm(R.AWAY_MESSAGE_FILE) if R.AWAY_MESSAGE_FILE
                     else away_tts.load_pcm())
        if not pcm or not sip.ringing(ring_cid):
            return  # file illeggibile o TTS fallito: meglio lasciar squillare che rispondere muti
        ok, msg = await sip.do_answer_incoming()
        if not ok:
            _LOGGER.warning("Messaggio di assenza: risposta fallita (%s)", msg)
            return
        self._ring_answered = self._ring_taken = True
        self._log_outcome("away")

        def alive():
            return sip.in_call and sip.call_state["call_id"] == ring_cid

        await asyncio.sleep(0.5)  # il tempo di aprire il flusso RTP
        await media.send_pcm(pcm, alive)
        if alive():  # solo la nostra chiamata; se annullato ("Rispondi", unload) resta aperta
            await self.async_hangup()

    def _update_stats(self, msg_type: str, msg):
        """Aggiorna le statistiche in base agli eventi SIP."""
        st = self.stats
        now = self._now()
        try:
            if msg_type == "ring":
                # Squillo reale solo se non l'abbiamo originato noi
                if not self._busy_now:
                    caller = sip.pending_incoming.get("caller_uri") or ""
                    st["last_ring_time"] = now
                    st["last_caller_uri"] = caller or None
                    st["last_caller_id"] = _uri_to_id(caller)
                    # Only a caller that offered video can stand in for the video
                    # panel: a phone or an audio-only door station cannot.
                    if ((st["last_caller_id"] or "").isdigit()
                            and _offers_video(sip.pending_incoming.get("body"))):
                        self._last_ring_panel = st["last_caller_id"]
                    st["ring_count"] += 1
                    self._ring_answered = False
                    self._ring_declined = False
                    self._ring_cid = sip.pending_incoming.get("cid")
                    self._ring_elsewhere = self._ring_taken = self._ring_opened = False
                    # Foto e clip sono di questo squillo: quelli di prima non vanno in notifica
                    for k in ("last_photo", "last_photo_path", "last_photo_v", "last_clip", "last_clip_path"):
                        st[k] = None
            elif msg_type == "ring_ended":
                if (not (self._ring_answered or self._ring_declined or self._ring_opened)
                        and st["last_ring_time"]):
                    st["missed_count"] += 1  # not answered from HA, wherever else it was
                if msg == sip.ANSWERED_ELSEWHERE:
                    self._answered_elsewhere()
            elif msg_type == "call_started":
                self._call_started_mono = time.monotonic()
                st["last_call_start"] = now
                st["call_count"] += 1
                if not self._ring_answered:
                    st["last_call_direction"] = "out"
            elif msg_type == "call_ended" and not self._busy_now:
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
                # Il sensore è visibile a ogni utente: niente token della risposta lunga.
                st["last_message_in"] = log_redact.redact(str(msg))[:200]
                st["last_message_in_time"] = now
                self._handle_incoming_message(str(msg))
        except Exception:
            _LOGGER.exception("stats update error")
        self._touch()

    async def async_set_apt_param(self, param: str, value, timeout: float = 10.0) -> tuple[bool, str]:
        """SET_APT_PARAMS;{"MSGID","PARAM","VALUE"} al PICG con `Panda: set`.

        Riuscito solo con `ERRCODE: ERR_NONE` nella risposta: come l'app, una
        risposta senza ERRCODE, o nessuna risposta, è un fallimento.
        """
        import json
        import secrets
        msgid = secrets.token_hex(5)
        body = "SET_APT_PARAMS;" + json.dumps(
            {"MSGID": msgid, "PARAM": param, "VALUE": value}, separators=(",", ":"))
        fut = asyncio.get_running_loop().create_future()
        self._apt_param_waiters[msgid] = fut
        try:
            ok, msg = await self.async_send_command(
                body=body, target=R.PICG_TARGET, header_name="Panda", header_value="set")
            if not ok and msg != QUEUED:  # queued: the panel's own reply still decides
                return False, msg
            try:
                err = await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                return False, QUEUED if not ok else "Nessuna risposta dal citofono"
        finally:
            self._apt_param_waiters.pop(msgid, None)
        if err != "ERR_NONE":
            return False, f"Rifiutato dal citofono: {err or 'nessun ERRCODE'}"
        self._apply_apt_params({param: value})
        self._touch()
        return True, "ERR_NONE"

    async def async_request_status(self) -> None:
        """GET_INIT_STATUS al PICG: la risposta aggiorna segreteria, DND e il resto."""
        await self._request_init_status()

    @staticmethod
    def _probe_outcome(ok: bool, msg: str) -> str:
        """Esito SIP di una sonda, nelle categorie della regola dei tre esiti."""
        import re

        m = re.search(r"\b([1-6]\d\d)\b", msg or "")
        code = int(m.group(1)) if m else None
        if ok and code == 202:
            # 202 Accepted to a MESSAGE: the cloud relay took it but no device
            # did, so it holds it for later delivery (#14). Not an `exists`.
            return QUEUED
        if ok:
            return "exists"            # 2xx: l'indirizzo esiste
        if code == 404:
            return "absent"            # l'indirizzo non esiste nell'impianto
        if msg == "Timeout":
            return "no_response"
        return "error"

    async def async_find_picg(
        self,
        targets: list[str],
        *,
        probe: str = "GET_NICKS",
        reply_wait: float = 3.0,
        delay: float = 1.0,
        sip_timeout: float = 8.0,
    ) -> dict:
        """Interroga gli indirizzi uno alla volta finché uno fa rispondere il Tab.

        Due sonde, perché nessuna delle due va bene ovunque:

        * `GET_NICKS` (default): silenziosa sull'app [VERIFICATO 21/09], e la
          reply *dichiara* il PICG nel contenuto — vale anche se arriva tardi.
          Ma su un 40515 in cloud un indirizzo esistente l'ha lasciata senza
          risposta SIP (Timeout, issue #14).
        * `GET_INIT_STATUS`: su quello stesso impianto dà i tre esiti puliti, ma
          fa comparire «Configurazione appartamento modificata» a ogni invio
          all'SGA vero, e la reply **non** dice chi è il PICG: lo si deduce da
          quale sonda l'ha provocata. Una reply fuori finestra quindi non
          identifica nessuno: si riportano i due candidati.

        Nessuna scrittura nella configurazione: lo decide il chiamante.
        """
        if probe not in ("GET_NICKS", "GET_INIT_STATUS"):
            return {"ok": False, "error": f"sonda non supportata: {probe}"}
        if self._scan_lock.locked():
            return {"ok": False, "error": "Una ricerca è già in corso"}
        by_content = probe == "GET_NICKS"
        async with self._scan_lock:
            loop = asyncio.get_running_loop()
            probes: list[dict] = []
            picg: str | None = None
            nicks: list[dict] = []
            reply_after: str | None = None
            candidates: list[str] = []
            seq0 = self._nicks_seq if by_content else self._init_seq
            self._probe_kind = probe
            try:
                for i, target in enumerate(targets):
                    if i:
                        await asyncio.sleep(delay)
                    self._probe_waiter = loop.create_future()
                    try:
                        ok, msg = await sip.do_system_message(
                            sip_uri(target), probe, extra_headers={"Panda": "blue"},
                            timeout=sip_timeout)
                    except Exception as err:  # noqa: BLE001
                        ok, msg = False, str(err)
                    if msg == "Non registrato":
                        return {"ok": False, "error": "Non registrato: ricerca interrotta",
                                "probe": probe, "probes": probes}
                    entry = {"target": target, "outcome": self._probe_outcome(ok, msg), "sip": msg}
                    if ok:
                        try:
                            got = await asyncio.wait_for(
                                asyncio.shield(self._probe_waiter), reply_wait)
                        except TimeoutError:
                            got = None
                        if got:
                            entry["outcome"] = "replied"
                            reply_after = target
                            if by_content:
                                nicks = got
                                picg = rest_client.find_picg(nicks)
                            else:
                                picg = target
                    seq_now = self._nicks_seq if by_content else self._init_seq
                    if not picg and seq_now != seq0 and reply_after is None:
                        # Reply arrivata fuori dalla finestra d'attesa.
                        entry["late_reply"] = True
                        if by_content:
                            late = list(self.stats.get("nicknames") or [])
                            if rest_client.find_picg(late):
                                nicks, picg = late, rest_client.find_picg(late)
                                reply_after = target
                        else:
                            # Non dice chi è: l'ha provocata questa sonda o la precedente.
                            candidates = [p["target"] for p in probes[-1:]] + [target]
                    seq0 = self._nicks_seq if by_content else self._init_seq
                    probes.append(entry)
                    _LOGGER.info("find_sga[%s]: %s → %s (%s)", probe, target, entry["outcome"], msg)
                    if picg or candidates:
                        break
            finally:
                self._probe_waiter = None
                self._probe_kind = None
            return {
                "ok": True,
                "probe": probe,
                "picg": picg,
                "reply_after": reply_after,
                "candidates": candidates,
                "nicknames": [{"role": n["role"], "ext": n["ext"], "name": n["name"]} for n in nicks],
                "probes": probes,
            }

    async def _request_init_status(self):
        """Chiede lo stato iniziale al PICG (GET_INIT_STATUS, Panda: blue).

        La risposta arriva in modo asincrono come SIP MESSAGE
        (GET_INIT_STATUS_REPLY) → parsata in _handle_incoming_message.
        Funziona sia in UDP locale sia in cloud TLS.
        """
        try:
            # Sent directly, not through async_send_command: that one records
            # the user's last command (last_command_* sensors), and the switches
            # ask for the status after every command, so the sensor showed
            # GET_INIT_STATUS instead of what the user did.
            uri = _command_uri(R.PICG_TARGET)
            if uri is None:
                ok, msg = False, "target non valido"
            else:
                ok, msg = await sip.do_system_message(
                    uri, C.GET_INIT_STATUS, extra_headers={"Panda": "blue"})
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

    def _keepalive_due(self, last_tick: float) -> float:
        """When the next keepalive tick is due (time.monotonic()).

        REGISTER_INTERVAL after the last tick, or renew_delay() after the last
        successful REGISTER when that comes first: a registrar that grants 60 s
        would otherwise see the binding lapse 60 s into every 120 s cycle.
        """
        due = last_tick + sip.REGISTER_INTERVAL
        if sip.registered and sip.registered_at:
            due = min(due, max(last_tick, sip.registered_at) + sip.renew_delay())
        return due

    async def _keepalive_loop(self):
        # Wakes at least every MIN_REGISTER_INTERVAL to notice a REGISTER done
        # elsewhere (startup, reconnect) and the lifetime it was granted: the
        # first one lands while this loop is already waiting.
        last_tick = time.monotonic()
        while self._running:
            remaining = self._keepalive_due(last_tick) - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(remaining, sip.MIN_REGISTER_INTERVAL))
                continue
            last_tick = time.monotonic()
            await self._keepalive_tick()

    async def async_register_now(self) -> bool:
        """Register again at once, instead of at the next keepalive tick.

        The options flow's SIP test registers with our identity and then
        removes its binding (Expires: 0): where the registrar matches bindings
        by +sip.instance, that removes the live one too, and incoming calls
        would be missed until the next renewal.
        """
        ok = await sip.do_register()
        _LOGGER.info("SIP re-registration after the options test: %s", "OK" if ok else "FAILED")
        return ok

    async def _register_or_join_reconnect(self) -> bool:
        """REGISTER during a call; if the reader is already reconnecting, join it
        instead: a REGISTER now would open a second connection beside it."""
        if not sip.reconnecting():
            return await sip.do_register()
        ok = await sip.reconnect()
        if ok:
            self._init_status_sent = False  # back after a drop: ask the Tab's state again
        return ok

    async def _keepalive_tick(self):
        """Un giro di keepalive. Separato dal loop per poterlo testare."""
        try:
            failed = False  # one failed tick counts as one failure
            if sip.registered:
                ok = await sip.do_register()
                _LOGGER.debug("Keepalive: %s", "OK" if ok else "FAILED")
                if not ok:
                    self.stats["register_failures"] += 1
                    failed = True
                if not ok and self.call_active:
                    # A reconnect would tear down the connection the live call
                    # (or the ring, on TLS) runs on for one lost REGISTER answer.
                    # Retry the REGISTER once; if the connection is really gone
                    # the reader notices and reconnects on its own.
                    _LOGGER.warning("SIP re-registration failed during a call or ring: retrying once")
                    ok = await self._register_or_join_reconnect()
                elif not ok:
                    # We were registered and the renewal failed: reconnect now,
                    # not a whole keepalive interval later with the intercom
                    # unreachable meanwhile. sip.reconnect() joins an attempt
                    # already running (the reader's), it never starts a second.
                    _LOGGER.warning("SIP re-registration failed: reconnecting now")
                    ok = await sip.reconnect()
                    if ok:
                        self._init_status_sent = False
                        _LOGGER.info("Registrazione SIP recuperata")
            elif self.call_active:
                # Lapsed mid-call: never start a reconnect (on TLS it closes the call's connection).
                # Bounded: a ring ends in 90 s, a call in 5 minutes, then the else branch reconnects.
                ok = await self._register_or_join_reconnect()
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
            elif not failed:
                self.stats["register_failures"] += 1
            self._touch()
        except Exception as e:
            _LOGGER.error("Keepalive error: %s", e)
