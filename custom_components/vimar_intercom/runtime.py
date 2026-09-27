"""Runtime credentials — popolate da HA config entry all'avvio.

Espone le stesse costanti di const.py per le credenziali dinamiche,
così sip_client.py può fare «from . import runtime as R» senza cambiare
la struttura del codice.

L'inizializzazione avviene in __init__.py → async_setup_entry():
    runtime.configure(entry.data)
"""

from __future__ import annotations

import hashlib as _hashlib
import logging
import secrets as _secrets
import uuid as _uuid

from . import const as _const

_LOGGER = logging.getLogger(__name__)

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

# ─── Identità del dispositivo abbinato ───────────────────────────────────────
# L'abbinamento lega la coppia (identificativo, nome) alle credenziali: il
# citofono rifiuta con 503 una registrazione che cambi uno dei due valori.
# Vanno quindi persistiti nel config entry, non derivati da costanti condivise.
DEVICE_NAME: str = _const.MY_NAME        # header MyName

# ─── Impostazioni di rete (da config entry / options flow) ───────────────────
USE_LOCAL_UDP:  bool = True   # True = UDP locale; False = TLS/TCP cloud
LOCAL_UDP_PORT: int  = 5060   # porta UDP locale su HA

# ─── Video ───────────────────────────────────────────────────────────────────
# Non tutti i posti esterni hanno la telecamera: alcuni sono solo citofoni, con
# microfono e altoparlante. Il QR lo dichiara nel campo "video".
VIDEO_ENABLED: bool = True

# ─── Media encryption ────────────────────────────────────────────────────────
# False (default) = RTP in chiaro (RTP/AVP, nessun a=crypto). Verificato sul
# campo 20/08/2026: la targa baresip di questo impianto NON accetta SRTP.
# True = SRTP AES_CM_128_HMAC_SHA1_80 (RTP/SAVP + a=crypto) per impianti che lo
# negoziano (media_enc). In futuro ricavabile da GET_INIT_STATUS_REPLY.
MEDIA_ENC: bool = False

# ─── Attuatori dinamici (da options flow, ricavati dalla rubrica) ────────────
# Lista di dict {"name","msg","target","icon"} prodotta da tools/parse_rubrica.py
# e incollata dall'utente nell'options flow. Default vuoto → nessun bottone.
ACTUATORS: list = []

# ─── SGA / PICG (da options flow, ricavati dalla rubrica o inseriti a mano) ──
# SGA = destinatario di VOICEMAIL;/DND; (Panda: blue) e dell'apri-porta/AUTO.
# PICG = destinatario di GET_INIT_STATUS. Sugli impianti verificati finora
# coincidono (55001), ma sono due valori distinti nella rubrica (SYSTEM.
# MAGIC_APT_INTERCOM vs PID_LIST ruolo PICG) e vanno tenuti configurabili
# separatamente. Default = const.SGA_TARGET/PICG_TARGET finché non
# sovrascritti in options (manualmente o dall'importer rubrica.db).
SGA_TARGET:  str = _const.SGA_TARGET
PICG_TARGET: str = _const.PICG_TARGET
# Targa video chiamata dall'autoaccensione. Come SGA/PICG è specifica
# dell'impianto: il default vale finché non viene impostata nelle options.
CAMERA_TARGET: str = _const.CAMERA_TARGET
# La targa che apre la porta: il GID_PE dell'attuatore porta nella rubrica.
# Non è per forza l'SGA: su un 2FV2 l'SGA è il 61000 e la porta la apre la
# targa 55001. Senza un valore dalla rubrica si resta sull'SGA, come prima.
DOOR_TARGET: str = _const.SGA_TARGET
# Se la targa video l'ha scelta qualcuno (options): allora non la si cambia
# mai da soli. Altrimenti è il default, o quella imparata (vedi hub).
CAMERA_TARGET_CONFIGURED: bool = False

# ─── URI calcolati da SIP_DOMAIN (popolati in configure) ─────────────────────
INTERCOM:     str = ""   # sip:<SGA_TARGET>@<domain> — targa esterna citofono
DOOR_ESTERNO: str = ""   # stesso target per comando apertura porta

# ─── Identità dispositivo (per installazione, dal config entry) ──────────────
# Popolati da configure() con i valori salvati nell'entry; __init__.py li genera
# al primo avvio se mancano. Mai costanti: vedi nota in const.py.
DEVICE_IMEI: str = ""
DEVICE_UUID: str = ""

# ─── Modello rilevato via SIP (vedi model_detect.py) ─────────────────────────
# Popolato all'avvio dal config entry (ultimo valore rilevato) e aggiornato a
# runtime appena il citofono si presenta con il suo User-Agent SIP.
DETECTED_MODEL:    str = ""   # es. "Elvox Tab 7S"
DETECTED_FW:       str = ""   # versione firmware, se presente nello User-Agent
DETECTED_UA:       str = ""   # User-Agent grezzo, per diagnostica
DETECTED_PRIORITY: int = 99   # indice del pattern che ha rilevato il modello


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


def door_from_actuators(actuators) -> str:
    """La targa che apre la porta, dalla rubrica: il GID_PE del primo
    attuatore con l'icona della porta. Vuoto se la rubrica non lo dice."""
    for act in actuators or []:
        target = str((act or {}).get("target") or "")
        if (act or {}).get("icon") == "door" and target.isdigit():
            return target
    return ""


def configure(data: dict) -> None:
    """Popola il modulo con i dati del config entry.

    Chiamato da async_setup_entry() prima di avviare hub/sip_client.
    «data» è il dizionario salvato nel config entry da config_flow.
    """
    global SIP_USER, SIP_PASSWORD, SIP_DOMAIN, SIP_HA1
    global LOCAL_DOMAIN, CLOUD_DOMAIN
    global SIP_PROXY, LOCAL_PROXY
    global GID, PLANT_TYPE, MAC_CITOFONO
    global USE_LOCAL_UDP, LOCAL_UDP_PORT, MEDIA_ENC, VIDEO_ENABLED
    global DEVICE_NAME
    global INTERCOM, DOOR_ESTERNO
    global DETECTED_MODEL, DETECTED_FW, DETECTED_UA, DETECTED_PRIORITY
    global ACTUATORS
    global SGA_TARGET, PICG_TARGET, CAMERA_TARGET, DOOR_TARGET, CAMERA_TARGET_CONFIGURED
    global DEVICE_IMEI, DEVICE_UUID

    SIP_USER     = data.get("sip_user", "")
    SIP_PASSWORD = data.get("sip_password", "")
    SIP_DOMAIN   = data.get("sip_domain", "")
    SIP_PROXY    = data.get("cloud_proxy", "ipvdes.vimar.cloud")
    LOCAL_PROXY  = data.get("local_proxy", "")
    GID          = data.get("gid", "")
    PLANT_TYPE   = data.get("plant_type", "")
    MAC_CITOFONO = data.get("mac", "")

    # Il nome con cui l'integrazione compare fra i dispositivi abbinati.
    DEVICE_NAME = str(data.get("device_name") or "").strip() or _const.MY_NAME

    USE_LOCAL_UDP  = bool(data.get("use_local_udp", True))

    SIP_HA1        = data.get("sip_ha1", "")
    LOCAL_UDP_PORT = int(data.get("local_udp_port", 5060))
    MEDIA_ENC      = bool(data.get("media_enc", False))
    VIDEO_ENABLED  = bool(data.get("video_enabled", True))

    # ─── Scelta del dominio SIP attivo ───────────────────────────────────────
    # Il QR porta due domini: «domain» (locale del Tab) e «cdomain» (cloud).
    # In modalità cloud il dominio locale non è instradabile — su alcuni Tab 5S
    # vale addirittura 127.0.0.1 — quindi va usato quello cloud, e viceversa.
    # sip_domain resta il valore salvato dal config flow (o inserito a mano)
    # ed è il fallback quando il dominio della modalità attiva non è noto.
    LOCAL_DOMAIN = (data.get("local_domain") or "").strip()
    # «sip_cloud_domain»: lo stesso dato, con il nome usato dal fork m4r1k
    # prima che la 1.0.7 lo introducesse; le entry salvate allora lo hanno così.
    CLOUD_DOMAIN = (data.get("cloud_domain") or data.get("sip_cloud_domain")
                    or "").strip()

    if USE_LOCAL_UDP:
        SIP_DOMAIN = LOCAL_DOMAIN or SIP_DOMAIN
    else:
        SIP_DOMAIN = CLOUD_DOMAIN or SIP_DOMAIN
        if not CLOUD_DOMAIN:
            _LOGGER.warning(
                "Modalità cloud senza dominio cloud nelle credenziali: verrà "
                "usato %r, che il relay non riconosce. Riconfigura "
                "l'integrazione dal QR.", SIP_DOMAIN,
            )

    # HA1 vale solo per il dominio con cui è stato calcolato. Con la password
    # si ricalcola sempre sul dominio in uso: un'entry salvata prima della
    # scelta del dominio può contenere un HA1 per il realm sbagliato anche
    # quando sip_domain coincide. Senza password si tiene solo se il dominio è
    # quello salvato.
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
    configured = str(data.get("camera_target") or "").strip()
    CAMERA_TARGET_CONFIGURED = bool(configured)
    CAMERA_TARGET = (configured
                     or str(data.get("learned_camera_target") or "").strip()
                     or _const.CAMERA_TARGET)
    # Le entry che hanno importato la rubrica prima che door_target esistesse
    # hanno gli attuatori ma non il campo: la targa si ricava da quelli.
    DOOR_TARGET = ((str(data.get("door_target") or "").strip())
                   or door_from_actuators(ACTUATORS) or SGA_TARGET)

    # URI calcolati — devono essere aggiornati dopo SIP_DOMAIN e SGA_TARGET
    INTERCOM     = f"sip:{SGA_TARGET}@{SIP_DOMAIN}"
    DOOR_ESTERNO = f"sip:{DOOR_TARGET}@{SIP_DOMAIN}"

    # Identità dispositivo: salvata nell'entry al primo avvio. Se manca (entry
    # creato da una versione precedente, o probe/test senza entry) se ne genera
    # una effimera valida per questa sessione, così nessun percorso finisce per
    # usare un valore condiviso fra installazioni diverse.
    DEVICE_IMEI = (str(data.get("device_imei") or "")).strip()
    DEVICE_UUID = (str(data.get("device_uuid") or "")).strip()
    if not DEVICE_IMEI or not DEVICE_UUID:
        fallback = new_device_identity()
        DEVICE_IMEI = DEVICE_IMEI or fallback["device_imei"]
        DEVICE_UUID = DEVICE_UUID or fallback["device_uuid"]

    # Modello rilevato in una sessione precedente: riparte da lì, così le
    # entità mostrano subito il valore giusto anche prima del primo dialogo SIP.
    DETECTED_MODEL    = data.get("detected_model", "") or ""
    DETECTED_FW       = data.get("detected_fw", "") or ""
    DETECTED_UA       = data.get("detected_ua", "") or ""
    DETECTED_PRIORITY = 99 if not DETECTED_MODEL else int(data.get("detected_priority", 98))
