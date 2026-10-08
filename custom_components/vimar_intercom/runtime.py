"""Runtime credentials — popolate da HA config entry all'avvio.

Espone le stesse costanti di const.py per le credenziali dinamiche,
così sip_client.py può fare «from . import runtime as R» senza cambiare
la struttura del codice.

L'inizializzazione avviene in __init__.py → async_setup_entry():
    runtime.configure(entry.data)
"""

from __future__ import annotations

import hashlib as _hashlib
import hmac as _hmac
import logging as _logging
import re as _re
import secrets as _secrets
import uuid as _uuid

from . import const as _const
from . import log_redact as _log_redact
from . import plant_state as _plant_state

_LOGGER = _logging.getLogger(__name__)

# ─── Valori di default (vuoti) ───────────────────────────────────────────────
SIP_USER:     str = ""
SIP_PASSWORD: str = ""
SIP_DOMAIN:   str = ""   # dominio SIP attivo (locale o cloud, vedi configure)
LOCAL_DOMAIN: str = ""   # dominio SIP locale del citofono (QR «domain»)
CLOUD_DOMAIN: str = ""   # dominio SIP cloud Vimar (QR «cdomain»)
SIP_HA1:      str = ""
SIP_PROXY:    str = "ipvdes.vimar.cloud"   # cloud proxy (da QR cproxy)
LOCAL_PROXY:  str = ""                      # IP citofono locale
GID:          str = ""
PLANT_TYPE:   str = ""
MAC_CITOFONO: str = ""

# ─── Impostazioni di rete (da config entry / options flow) ───────────────────
USE_LOCAL_UDP:  bool = True   # True = UDP locale; False = TLS/TCP cloud
LOCAL_UDP_PORT: int  = 5060   # porta UDP locale su HA

# ─── Video ───────────────────────────────────────────────────────────────────
# Not every entrance has a camera: the pairing QR says "video=0" on those. Our
# own offers then carry no m=video (answers mirror the offer anyway). Entries
# made before this field was stored have no value: True, as before.
VIDEO_ENABLED: bool = True

# ─── Media encryption ────────────────────────────────────────────────────────
# False (default) = RTP in chiaro (RTP/AVP, nessun a=crypto). Verificato sul
# campo 20/08/2026: la targa baresip di questo impianto NON accetta SRTP.
# True = SRTP AES_CM_128_HMAC_SHA1_80 (RTP/SAVP + a=crypto) per impianti che lo
# negoziano (media_enc).
#
# Dalla 1.0.11 l'opzione ha tre valori (issue #4): "auto" (default) segue il
# `media_enc` che l'impianto dichiara nel GET_INIT_STATUS_REPLY lungo ("srtp" su
# un 40515/2FV2 in cloud); senza dichiarazione (risposta corta, come sul 40507)
# resta in chiaro. "on" / "off" forzano. Il valore effettivo, quello che legge
# sdp.build_sdp, è plant_state.MEDIA_ENC.
MEDIA_ENC_MODES = ("auto", "on", "off")
MEDIA_ENC_OPTION: str = "auto"

# Risposta a voce su /audio_ws: "declared" (default) solo chi manda ?voice_answer=1,
# "off" mai, "any" qualsiasi connessione con microfono.
VOICE_ANSWER_MODES = ("declared", "off", "any")
VOICE_ANSWER_DEFAULT = "declared"
VOICE_ANSWER: str = VOICE_ANSWER_DEFAULT


def voice_answer_mode(value) -> str:
    return value if value in VOICE_ANSWER_MODES else VOICE_ANSWER_DEFAULT


def media_enc_mode(value) -> str:
    """Valore salvato nelle options → "auto" | "on" | "off".

    Fino alla 1.0.10 era un booleano: True → "on"; False (il vecchio default, che
    nessuno distingueva da "non toccato") → "auto".
    """
    if value is True:
        return "on"
    if isinstance(value, str) and value.strip().lower() in MEDIA_ENC_MODES:
        return value.strip().lower()
    return "auto"


# ─── Attuatori dinamici (da options flow, ricavati dalla rubrica) ────────────
# Lista di dict {"name","msg","target","icon"} prodotta da tools/parse_rubrica.py
# e incollata dall'utente nell'options flow. Default vuoto → nessun bottone.
ACTUATORS: list = []

# ─── SGA / PICG (da options flow, ricavati dalla rubrica o inseriti a mano) ──
# SGA = destinatario di VOICEMAIL;/DND; (Panda: blue). L'apri-porta ha un suo
# destinatario (DOOR_TARGET, sotto), che coincide con l'SGA solo su alcuni impianti.
# PICG = destinatario di GET_INIT_STATUS. Sugli impianti verificati finora
# coincidono (55001), ma sono due valori distinti nella rubrica (SYSTEM.
# MAGIC_APT_INTERCOM vs PID_LIST ruolo PICG) e vanno tenuti configurabili
# separatamente. Default = const.SGA_TARGET/PICG_TARGET finché non
# sovrascritti in options (manualmente o dall'importer rubrica.db).
SGA_TARGET:  str = _const.SGA_TARGET
PICG_TARGET: str = _const.PICG_TARGET
# Messaggio di assenza: file audio fatto sentire se dopo N s squilla ancora (0 = mai).
# Senza file, un testo letto dal TTS di HA (motore AWAY_MESSAGE_TTS; vuoto = il
# predefinito di HA): vedi away_tts.py.
AWAY_MESSAGE_FILE: str = ""
AWAY_MESSAGE_TEXT: str = ""
AWAY_MESSAGE_TTS: str = ""
AWAY_MESSAGE_DELAY: int = 0


AWAY_KEYS = ("away_message_file", "away_message_text", "away_message_delay")


def set_away_option(key: str, value) -> None:
    """Applica in memoria una delle AWAY_KEYS (senza ricaricare l'integrazione)."""
    globals()[key.upper()] = int(value or 0) if key == "away_message_delay" else str(value or "").strip()


def away_message_configured() -> bool:
    return bool(AWAY_MESSAGE_FILE or AWAY_MESSAGE_TEXT)


# Foto di chi suona: cartella (vuoto = non salvare) e secondi dopo lo squillo.
SNAPSHOT_DIR: str = ""
SNAPSHOT_DELAY: int = _const.DEFAULT_SNAPSHOT_DELAY
# kbit/s declared in the SDP (b=AS) for the video and the session: see const.SDP_BANDWIDTH.
VIDEO_BANDWIDTH, SESSION_BANDWIDTH = _const.SDP_BANDWIDTH[_const.VIDEO_BANDWIDTH_LOW]
VIEW_KEEPALIVE: float = _const.DEFAULT_VIEW_KEEPALIVE_CLOUD   # s di silenzio a "Vedi esterno", 0 = nessuno


def video_bandwidth_choice(option, use_local_udp: bool) -> str:
    """The bandwidth to ask for (const.SDP_BANDWIDTH key): the option when it is
    one of the values, else (automatic, unset, unknown) by transport: 2048 on
    local UDP, 256 over the cloud relay, which loses packets (#161)."""
    if str(option) in _const.SDP_BANDWIDTH:
        return str(option)
    return _const.VIDEO_BANDWIDTH_HIGH if use_local_udp else _const.VIDEO_BANDWIDTH_LOW


def view_keepalive_default(use_local_udp: bool) -> int:
    """Locale (UDP, il 2 fili): niente silenzio, occuperebbe l'appartamento; cloud: 120 s."""
    return 0 if use_local_udp else _const.DEFAULT_VIEW_KEEPALIVE_CLOUD
# Id degli utenti HA ammessi a squilli, foto, clip e media live (vuoto = tutti).
ALLOWED_USERS: list[str] = []

# ─── Webhook squillo (da options flow; opzionale) ────────────────────────────
# GET fire-and-forget per accendere/spegnere un interruttore fittizio (es.
# Scrypted "Dummy Switch" collegato a un Custom Doorbell Button), o qualsiasi
# altro automatismo esterno. Vuoto = disattivato. Vedi hub.py/webhook.py.
RING_WEBHOOK_URL: str = ""
RING_END_WEBHOOK_URL: str = ""

# ─── Targhe da chiamare (da options flow; issue #3) ──────────────────────────
# CAMERA_TARGET = targa video (PE) chiamata dalla camera, da «Chiama» e da
# «Chiama Video (esterno)». INTERNAL_PANEL_TARGET = «Chiama Casa (interno)».
# Vuoto in options → default storico in const.py.
CAMERA_TARGET:         str = _const.CAMERA_TARGET
# True when the user chose the video panel: a learned one never replaces it.
CAMERA_TARGET_CONFIGURED: bool = False
INTERNAL_PANEL_TARGET: str = _const.INTERNAL_PANEL_TARGET
# La targa che apre la porta: il GID_PE dell'attuatore porta nella rubrica.
# Non è per forza l'SGA: su un 2FV2 l'SGA è il 61000 e la porta la apre la
# targa 55001 (è lì che la manda l'app VIEW). Senza un valore dalla rubrica si
# resta sull'SGA, come prima.
DOOR_TARGET: str = _const.SGA_TARGET

# ─── URI calcolati da SIP_DOMAIN (popolati in configure) ─────────────────────
INTERCOM:     str = ""   # sip:<CAMERA_TARGET>@<domain> — destinatario di default delle chiamate
DOOR_ESTERNO: str = ""   # sip:<DOOR_TARGET>@<domain> — destinatario dell'apri-porta

# ─── Identità dispositivo (per installazione, dal config entry) ──────────────
# Popolati da configure() con i valori salvati nell'entry; __init__.py li genera
# al primo avvio se mancano. Mai costanti: vedi nota in const.py.
DEVICE_IMEI: str = ""
DEVICE_UUID: str = ""
# Key for /av (#63): plain /av places a call, so it wants an authenticated HA user
# or this key in the `auth` query parameter. Generated once, stored in the entry,
# never expires (the stream worker reuses its URL on every reconnect). `auth` is
# the name HA's stream component masks in its own logs; log_redact masks it in ours.
AV_KEY: str = ""
AV_KEY_PARAM = "auth"
# The MyName header. A local pairing binds (identifier, name) to the credential
# and refuses any change with a 503, so it is stored rather than hard-coded.
DEVICE_NAME: str = _const.MY_NAME

def new_device_identity() -> dict[str, str]:
    """Genera l'identità dispositivo di questa installazione.

    Va chiamata una volta sola e il risultato salvato nel config entry: il cloud
    Vimar associa la registrazione (e le push) all'identità, quindi un valore
    costante nel sorgente farebbe litigare fra loro installazioni diverse.
    """
    return {
        "device_imei": "".join(
            _secrets.choice("0123456789") for _ in range(_const.DEVICE_ID_DIGITS)
        ),
        "device_uuid": str(_uuid.UUID(bytes=_secrets.token_bytes(16), version=4)),
    }


def new_av_key() -> str:
    """A fresh /av key: 32 URL-safe characters, from the OS's CSPRNG."""
    return _secrets.token_urlsafe(24)


# Never empty, even before configure() runs: an empty key would match an empty `auth=`.
AV_KEY = new_av_key()


def av_key_valid(given) -> bool:
    """True when `given` is this installation's /av key (constant-time compare)."""
    if not AV_KEY or not isinstance(given, str) or not given:
        return False
    return _hmac.compare_digest(given.encode(), AV_KEY.encode())


def door_from_actuators(actuators) -> str:
    """La targa che apre la porta, dalla rubrica: il GID_PE del primo
    attuatore con l'icona della porta. Vuoto se la rubrica non lo dice."""
    for act in actuators or []:
        target = str((act or {}).get("target") or "")
        if (act or {}).get("icon") == "door" and target.isdigit():
            return target
    return ""


# A door command body from the phonebook's MSG column: upper-case letters, digits
# and underscores only (OPEN, OPEN_2F, ...). Anything else falls back to the
# default rather than going on the wire.
_DOOR_BODY = _re.compile(r"[A-Z0-9_]{1,32}")


def door_command_for(target) -> tuple[str, str]:
    """The body that opens the door at panel `target`, and where it came from.

    The phonebook's door actuator (icon "door") pairs a body (MSG) with the
    panel that owns the relay (GID_PE); both come from the same row, because
    the body alone does not identify a door (a relay module can use the same
    body towards another panel, #58). "AUTO" as a target means DOOR_TARGET,
    and only counts after the rows naming the panel itself: [{AUTO, OPEN},
    {55002, OPEN_X}] sends OPEN_X to 55002 even when DOOR_TARGET is 55002.
    Returns (MSG, "phonebook") for a match, else (OPEN_2F, "default").
    """
    target = str(target or "")
    doors = [a for a in (act or {} for act in ACTUATORS or []) if a.get("icon") == "door"]
    exact = [a for a in doors if str(a.get("target") or "").upper() != "AUTO"
             and str(a.get("target") or "") == target]
    auto = [a for a in doors if str(a.get("target") or "").upper() == "AUTO" and DOOR_TARGET == target]
    for a in exact + auto:
        msg = str(a.get("msg") or "").strip()
        if _DOOR_BODY.fullmatch(msg):
            return msg, "phonebook"
    return _const.DOOR_COMMAND, "default"


def configure(data: dict) -> None:
    """Popola il modulo con i dati del config entry.

    Chiamato da async_setup_entry() prima di avviare hub/sip_client.
    «data» è il dizionario salvato nel config entry da config_flow.
    """
    global SIP_USER, SIP_PASSWORD, SIP_DOMAIN, SIP_HA1
    global LOCAL_DOMAIN, CLOUD_DOMAIN
    global SIP_PROXY, LOCAL_PROXY
    global GID, PLANT_TYPE, MAC_CITOFONO
    global USE_LOCAL_UDP, LOCAL_UDP_PORT, MEDIA_ENC_OPTION, VIDEO_ENABLED
    global INTERCOM, DOOR_ESTERNO, VOICE_ANSWER
    global ACTUATORS
    global SGA_TARGET, PICG_TARGET
    global CAMERA_TARGET, INTERNAL_PANEL_TARGET, DOOR_TARGET, CAMERA_TARGET_CONFIGURED
    global AWAY_MESSAGE_FILE, AWAY_MESSAGE_TEXT, AWAY_MESSAGE_TTS, AWAY_MESSAGE_DELAY
    global SNAPSHOT_DIR, SNAPSHOT_DELAY, VIEW_KEEPALIVE, ALLOWED_USERS
    global VIDEO_BANDWIDTH, SESSION_BANDWIDTH
    global RING_WEBHOOK_URL, RING_END_WEBHOOK_URL
    global DEVICE_IMEI, DEVICE_UUID, DEVICE_NAME, AV_KEY

    SIP_USER     = data.get("sip_user", "")
    SIP_PASSWORD = data.get("sip_password", "")
    SIP_DOMAIN   = data.get("sip_domain", "")
    SIP_HA1      = data.get("sip_ha1", "")
    SIP_PROXY    = data.get("cloud_proxy", "ipvdes.vimar.cloud")
    LOCAL_PROXY  = data.get("local_proxy", "")
    GID          = data.get("gid", "")
    PLANT_TYPE   = data.get("plant_type", "")
    MAC_CITOFONO = data.get("mac", "")

    USE_LOCAL_UDP  = bool(data.get("use_local_udp", True))
    LOCAL_UDP_PORT = int(data.get("local_udp_port", 5060))
    VIDEO_ENABLED  = bool(data.get("video_enabled", True))
    MEDIA_ENC_OPTION = media_enc_mode(data.get("media_enc"))
    VOICE_ANSWER = voice_answer_mode(data.get("voice_answer"))

    # ─── Scelta del dominio SIP attivo ───────────────────────────────────────
    # Il QR porta due domini: «domain» (locale del Tab) e «cdomain» (cloud).
    # In modalità cloud il dominio locale non è instradabile — su alcuni Tab 5S
    # vale addirittura 127.0.0.1 — quindi va usato quello cloud, e viceversa.
    # sip_domain resta il valore salvato dal config flow (o inserito a mano)
    # ed è il fallback quando il dominio della modalità attiva non è noto.
    LOCAL_DOMAIN = (data.get("local_domain") or "").strip()
    # "sip_cloud_domain": the same value, under the name the m4r1k fork used
    # before 1.0.7 introduced "cloud_domain"; entries saved then still have it.
    CLOUD_DOMAIN = (data.get("cloud_domain") or data.get("sip_cloud_domain")
                    or "").strip()

    if USE_LOCAL_UDP:
        SIP_DOMAIN = LOCAL_DOMAIN or SIP_DOMAIN
    else:
        SIP_DOMAIN = CLOUD_DOMAIN or SIP_DOMAIN
        if not CLOUD_DOMAIN:
            _LOGGER.warning(
                "Cloud mode without a cloud domain in the credentials: using %r, "
                "which the relay does not recognise. Set the integration up "
                "again from the pairing QR.", SIP_DOMAIN)

    # HA1 only holds for the domain it was computed on. With the password it
    # is always recomputed on the domain in use: an entry saved before the
    # domain choice can hold an HA1 for the wrong realm even when sip_domain
    # matches. Without the password it is kept only for the saved domain.
    if SIP_PASSWORD and SIP_USER and SIP_DOMAIN:
        SIP_HA1 = _hashlib.md5(
            f"{SIP_USER}:{SIP_DOMAIN}:{SIP_PASSWORD}".encode()
        ).hexdigest()
    elif SIP_DOMAIN != (data.get("sip_domain") or "").strip():
        SIP_HA1 = ""

    # Attuatori dinamici: lista già validata dall'options flow (o default vuoto).
    acts = data.get("actuators", [])
    ACTUATORS = acts if isinstance(acts, list) else []

    # SGA/PICG: valore in options (manuale o da importer rubrica.db) se presente
    # e non vuoto, altrimenti il default storico in const.py (55001).
    SGA_TARGET  = (str(data.get("sga_target") or "").strip()) or _const.SGA_TARGET
    PICG_TARGET = (str(data.get("picg_target") or "").strip()) or _const.PICG_TARGET

    # A panel chosen by the user, else one learned from the plant (see
    # hub._camera_fallback), else the historical default.
    configured = str(data.get("camera_target") or "").strip()
    CAMERA_TARGET_CONFIGURED = bool(configured)
    CAMERA_TARGET = (configured
                     or str(data.get("learned_camera_target") or "").strip()
                     or _const.CAMERA_TARGET)
    INTERNAL_PANEL_TARGET = (
        (str(data.get("internal_panel_target") or "").strip())
        or _const.INTERNAL_PANEL_TARGET
    )
    # Apri-porta: opzione door_target (compilata dall'import rubrica o a mano);
    # se vuota, la targa dell'attuatore porta fra quelli salvati — le entry
    # che hanno importato la rubrica con la 1.0.7 hanno gli attuatori ma non il
    # campo —; se nemmeno quelli lo dicono, l'SGA come prima.
    DOOR_TARGET = ((str(data.get("door_target") or "").strip())
                   or door_from_actuators(ACTUATORS) or SGA_TARGET)

    # URI calcolati — devono essere aggiornati dopo SIP_DOMAIN e i target.
    # Le chiamate vanno alla targa video, non all'SGA: l'SGA riceve i MESSAGE
    # di stato ma non è detto che accetti un INVITE (488 sull'impianto di
    # sviluppo, 488/408 sul 40515 di #3). L'apri-porta va alla targa che
    # possiede il relè (su un 2FV2 l'SGA 61000 risponde 200 e non apre); i
    # comandi di stato (VOICEMAIL;, DND;, GET_INIT_STATUS) restano all'SGA/PICG.
    INTERCOM     = f"sip:{CAMERA_TARGET}@{SIP_DOMAIN}"
    DOOR_ESTERNO = f"sip:{DOOR_TARGET}@{SIP_DOMAIN}"

    AWAY_MESSAGE_FILE  = str(data.get("away_message_file") or "").strip()
    AWAY_MESSAGE_TEXT  = str(data.get("away_message_text") or "").strip()
    AWAY_MESSAGE_TTS   = str(data.get("away_message_tts") or "").strip()
    AWAY_MESSAGE_DELAY = int(data.get("away_message_delay") or 0)
    SNAPSHOT_DIR   = str(data.get("snapshot_dir") or "").strip()
    SNAPSHOT_DELAY = int(data.get("snapshot_delay", _const.DEFAULT_SNAPSHOT_DELAY))
    _ka = data.get("view_keepalive")
    VIEW_KEEPALIVE = int(_ka) if _ka is not None else view_keepalive_default(USE_LOCAL_UDP)
    VIDEO_BANDWIDTH, SESSION_BANDWIDTH = _const.SDP_BANDWIDTH[
        video_bandwidth_choice(data.get(_const.CONF_VIDEO_BANDWIDTH), USE_LOCAL_UDP)]
    ALLOWED_USERS  = [str(u) for u in data.get("allowed_users") or []]
    RING_WEBHOOK_URL     = str(data.get("ring_webhook_url") or "").strip()
    RING_END_WEBHOOK_URL = str(data.get("ring_end_webhook_url") or "").strip()

    # Identità dispositivo: salvata nell'entry al primo avvio. Se manca (entry
    # creato da una versione precedente, o probe/test senza entry) se ne genera
    # una effimera valida per questa sessione, così nessun percorso finisce per
    # usare un valore condiviso fra installazioni diverse.
    DEVICE_NAME = str(data.get("device_name") or "").strip() or _const.MY_NAME
    DEVICE_IMEI = (str(data.get("device_imei") or "")).strip()
    DEVICE_UUID = (str(data.get("device_uuid") or "")).strip()
    if not DEVICE_IMEI or not DEVICE_UUID:
        fallback = new_device_identity()
        DEVICE_IMEI = DEVICE_IMEI or fallback["device_imei"]
        DEVICE_UUID = DEVICE_UUID or fallback["device_uuid"]
    # Same for the /av key: an entry without one gets an ephemeral key, never a
    # shared or empty one (empty would make every key "valid").
    AV_KEY = str(data.get("av_key") or "").strip() or new_av_key()
    # The account's identity, masked in every log line from now on (#146), in place of
    # the previous one. The GID stays out: a small integer, it would match any number
    # (log_redact masks it by its shape).
    plant = [("id", SIP_USER), ("imei", DEVICE_IMEI), ("uuid", DEVICE_UUID), ("name", DEVICE_NAME)]
    mac = str(MAC_CITOFONO or "").strip()
    bare = _re.sub(r"[^0-9A-Fa-f]", "", mac)
    for form in {mac, mac.lower(), mac.upper(), bare, bare.lower(), bare.upper()}:
        plant.append(("mac", form))
    # The SIP domains name the plant (the cloud one is also the Digest realm and username),
    # in the three forms of PROTOCOL §4-bis: whole, without `.<cproxy>`, with `.` → `_`.
    # A domain that is an address (a local one can be the Tab's IP, or 127.0.0.1) is left
    # to the address masking.
    for domain in (CLOUD_DOMAIN, LOCAL_DOMAIN, SIP_DOMAIN):
        short = domain.removesuffix(f".{SIP_PROXY}") if SIP_PROXY else domain
        for form in {domain, short, short.replace(".", "_")}:
            if form != SIP_PROXY and not _re.fullmatch(r"[\d._]+", form):
                plant.append(("domain", form))
    _log_redact.set_plant_values(plant)

    # What the plant said in the previous session: media encryption is forgotten,
    # the detected model starts again from the entry.
    _plant_state.reset(data, MEDIA_ENC_OPTION)

