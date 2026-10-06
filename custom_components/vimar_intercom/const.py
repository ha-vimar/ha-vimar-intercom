"""Constants for Vimar Intercom integration.

Questo file contiene SOLO costanti statiche non sensibili.
Le credenziali SIP (user, password, domain, HA1) sono gestite dal config
flow e memorizzate cifrate in HA config entries — NON hardcodate qui.
"""

import os

DOMAIN = "vimar_intercom"

# ─── Device Info ──────────────────────────────────────────────────────────────
MANUFACTURER = "Vimar"
# Fallback generico: il modello reale viene rilevato dagli header SIP del
# citofono (vedi model_detect.py) e scritto nel device registry di HA.
MODEL        = "Elvox Tab 7S 2F+ WiFi"   # 40507 — dal QR: planttype=2F (Due Fili Plus), non IP

# ─── SIP — valori di default per cloud (override da config entry) ─────────────
SIP_PORT = 7042                          # porta TLS cloud
# SNI e Route non sono costanti: valgono <cproxy>, che arriva dal QR e sta nel
# config entry (runtime.SIP_PROXY, default "ipvdes.vimar.cloud"). Vedi
# sip_client._route_line() e la connect TLS.

# ─── Porta SIP locale (Flexisip sul citofono) ─────────────────────────────────
LOCAL_SIP_PORT = 5060

# ─── Transport mode — default, può essere sovrascritto da config entry ─────────
# Modificabile via Options Flow in HA: Impostazioni → Integrazioni → Vimar Intercom
USE_LOCAL_UDP  = True    # True = UDP locale; False = TLS/TCP cloud
LOCAL_UDP_PORT = 5060    # porta UDP locale su HA

# ─── Door targets ──────────────────────────────────────────────────────────────
# Costruiti a runtime da hub.py usando le credenziali del config entry.
# Il comando ATT_ID 8 = modulo 2F (Serratura)
DOOR_COMMAND = "OPEN_2F"

# ─── RTP / Media ──────────────────────────────────────────────────────────────
RTP_AUDIO_PORT     = 7200
RTP_VIDEO_PORT     = 9200
# Bandwidth we declare in the SDP (b=AS, kbit/s). The 40515 panel's encoder honours it:
# 256 gave ~14 KB keyframes at 16 fps, 2048 gives 30-58 KB at 25 fps (~1.5 Mbit/s, its
# ceiling: 4096 is the same), same 720x576, no loss over the cloud. Session = video + audio.
# The video bandwidth is an option (CONF_VIDEO_BANDWIDTH): 2048 suits the 40515, but
# the 40517 honours it too, and through a cloud relay that loses packets ten times
# the traffic means ten times the losses (smeared, frozen, laggy video). 256 is the
# value before 1.0.19 and the default. Session = video + audio.
CONF_VIDEO_BANDWIDTH = "video_bandwidth"
VIDEO_BANDWIDTH_LOW = "256"
VIDEO_BANDWIDTH_HIGH = "2048"
DEFAULT_VIDEO_BANDWIDTH = VIDEO_BANDWIDTH_LOW
SDP_BANDWIDTH = {VIDEO_BANDWIDTH_LOW: (256, 512), VIDEO_BANDWIDTH_HIGH: (2048, 2200)}
# Porte locali dell'ffmpeg AV (/api/vimar_intercom/av). Devono essere PARI e
# distanti almeno 2: per ogni riga m= dell'SDP ffmpeg apre la porta RTP **e** la
# RTCP (= RTP + 1). Fino alla 1.0.7 erano 19201/19202: l'RTCP del video cadeva
# sulla porta dell'audio, il bind falliva e /av non è mai partito (issue #8).
# Guardia: tests/test_camera_stream.py.
FFMPEG_AV_VIDEO_PORT = 19210  # AV ffmpeg video (RTCP 19211)
FFMPEG_AV_AUDIO_PORT = 19212  # AV ffmpeg audio (RTCP 19213)

# ─── Push Notifications — opzionale, non necessario per UDP locale ────────────
PN_APP_ID = "toga-prod"
PN_TYPE   = "firebase"

# ─── User-Agent — stesso dell'app originale per compatibilità Flexisip ─────────
USER_AGENT = "TOGA_Googlesdk_gphone64_arm64_Android34/1.0|AppVer:2.4.0|ProtVer:1.0|"

# ─── Identità dispositivo HA ──────────────────────────────────────────────────
# L'identità NON è una costante: viene generata una volta sola per installazione
# (runtime.new_device_identity) e salvata nel config entry. Il cloud Vimar traccia
# la registrazione per identità dispositivo, quindi due impianti che si presentano
# con lo stesso Mobile-IMEI / +sip.instance si contendono la stessa registrazione.
MY_NAME = "Home Assistant"
DEVICE_ID_DIGITS = 15   # cifre del finto IMEI usato nell'header Mobile-IMEI

# ─── Push notifications (opzionale — lascia vuoto per disabilitare) ───────────
# Riempi solo se vuoi ricevere push Firebase/FCM su dispositivi Android.
# In modalità UDP locale non è necessario.
PN_TOKEN = ""

# ─── Certificato CA Vimar (per modalità TLS cloud) ───────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CA_PATH    = os.path.join(SCRIPT_DIR, "vimar_rootca.pem")

# ─── Segreteria (answering machine) — comando DA CONFERMARE ──────────────────
# SGA (MAGIC_APT_INTERCOM) = destinatario di VOICEMAIL;ON/OFF e DND;ON/OFF con Panda: blue.
# CONFERMATO 19/08/2026 dalla rubrica.db reale (SYSTEM.MAGIC_APT_INTERCOM = "55001", PICG "Casa CG")
# e verificato sul campo: `VOICEMAIL;ON` → 55001 fa accendere la segreteria sul Tab.
# (I vecchi tentativi verso 55002/61000/60002/101 davano 200 senza effetto: target sbagliato.)
#
# Da qui in poi questi due valori sono SOLO IL DEFAULT DI FALLBACK per il primo
# impianto verificato: il valore effettivo usato a runtime è configurabile via
# options (manualmente o dall'importer rubrica.db) e vive in
# runtime.SGA_TARGET / runtime.PICG_TARGET (vedi runtime.configure()). Il resto
# del codice (hub.py, switch.py, button.py, lock.py, __init__.py) legge da lì,
# mai da queste costanti direttamente — e tests/test_no_hardcoded_plant_values.py
# fallisce se il letterale rientra in una di quelle piattaforme. Fino alla 1.0.4
# questa riga era falsa: button.py e lock.py passavano "55001" a mano.
SGA_TARGET              = "55001"
# PICG (capogruppo appartamento) — destinatario di GET_INIT_STATUS / GET_NICKS.
# Su questo impianto coincide con l'SGA (55001). [VERIFICATO 20/08/2026]
PICG_TARGET             = SGA_TARGET
# Targa interna — default del bottone "Chiama Casa (interno)" quando l'opzione
# `internal_panel_target` è vuota. 55002 vale sull'impianto di sviluppo (è il
# Tab stesso); su un 40515 visto in #3 il pannello interno è 60001. Non c'è una
# chiave di rubrica.db da cui ricavarlo con certezza, quindi l'importer non lo
# tocca: si imposta a mano nelle opzioni.
INTERNAL_PANEL_TARGET   = "55002"
SEGRETERIA_ON           = "VOICEMAIL;ON"
SEGRETERIA_OFF          = "VOICEMAIL;OFF"
SEGRETERIA_HEADER_NAME  = "Panda"
SEGRETERIA_HEADER_VALUE = "blue"   # dall'app VIEW: i messaggi di stato usano Panda: blue

# Non disturbare — "DND;ON" / "DND;OFF" verso l'SGA (Panda: blue).
DND_ON                  = "DND;ON"
DND_OFF                 = "DND;OFF"

# ─── Attuatori aggiuntivi (visti nell'app VIEW → Videocitofonia) ─────────────
# Stessa famiglia di OPEN_2F (header Panda: command). I token OPEN_F1/OPEN_F2
# sono quelli standard dei relè aux della targa; LUCE SCALA / Attuatore 02 /
# TIRO sono IPOTESI da confermare col test o con la cattura del MESSAGE.
# Formato: (key, nome, target, comando, icona)
ACTUATORS = []  # RIMOSSI 18/08/2026: i token ipotizzati (OPEN_F1/OPEN_2/OPEN_2F)
# aprivano la PORTA anziché comandare F1/F2/Luce Scala/Attuatore 02.
# Luce Scala e Attuatore 02 sono oggetti By-me (solo cloud, non SIP).
# Ripristinare solo con i comandi reali ricavati dall'APK decifrata.

# ─── Targa video (autoaccensione camera on-demand) ───────────────────────────
# Default di `camera_target` quando l'opzione è vuota. È la targa che la camera,
# «Chiama» e «Chiama Video (esterno)» chiamano per accendere il video; NON l'SGA
# (55001 qui dava 488 Not Acceptable Here; 61000 sul 40515 di #3 non è
# chiamabile). Qui: PHONEBOOK GID=55100 TYPE='PE' NAME='Video'. Su un altro
# impianto la PE è 55001: l'importer rubrica la ricava (PHONEBOOK.AUTO del
# proprio appartamento, altrimenti la prima riga PE) — rubrica_import.py.
CAMERA_TARGET = "55100"

# Secondi fra lo squillo e la foto migliore (la prima si salva subito): la telecamera
# della targa regola l'esposizione (Tab 5S Up 40515). 0 = solo la prima.
DEFAULT_SNAPSHOT_DELAY = 3
# Ritardo del messaggio di assenza se né il Tab né le opzioni ne danno uno: mai rispondere subito.
DEFAULT_AWAY_DELAY = 20
# Lunghezza massima del testo del messaggio di assenza (limite di un'entità text di HA).
AWAY_TEXT_MAX = 255
# Secondi di silenzio PCMU a "Vedi esterno" (0 = nessuno): via cloud la targa chiude la vista a ~10 s
# senza audio; sul 2 fili in locale (UDP) il silenzio tiene l'appartamento occupato fino a 300 s.
DEFAULT_VIEW_KEEPALIVE_CLOUD = 120

# ─── HomeKit video doorbell (homekit_accessory.py) ────────────────────────────
# Off by default: turning it on in the options shows a pairing QR code.
CONF_HOMEKIT_ACCESSORY = "homekit_accessory"
DEFAULT_HOMEKIT_ACCESSORY = False
# Installed when the option is on, not from the manifest (see __init__). The
# same libraries Home Assistant's own HomeKit integration uses; base36 and
# PyQRCode build the pairing QR (pyhap's setup payload).
HOMEKIT_REQUIREMENTS = ["HAP-python>=5.0.0", "PyQRCode>=1.2.1", "base36>=0.1.1"]
# Re-encode the video with a keyframe per second (on) or send the panel's own
# packets (off: opens faster, but a packet lost on the relay freezes the
# picture until the panel's next keyframe, up to 3 s).
CONF_HOMEKIT_SMOOTH = "homekit_video_smooth"
DEFAULT_HOMEKIT_SMOOTH = True
# During a ring: answer at the first word from the phone (Talk), or as soon as
# the view opens (which stops the ring on the Tab and the app).
CONF_HOMEKIT_ANSWER = "homekit_answer"
HOMEKIT_ANSWER_TALK = "talk"
HOMEKIT_ANSWER_OPEN = "open"
DEFAULT_HOMEKIT_ANSWER = HOMEKIT_ANSWER_TALK
# The ring also as a stateless programmable switch, for Home app automations.
# Off by default: it is one more tile in the Home app, and most rings are
# automated in Home Assistant.
CONF_HOMEKIT_RING_BUTTON = "homekit_ring_button"
DEFAULT_HOMEKIT_RING_BUTTON = False
# hass.data key of the HomeKit state shared between entries (the QR view, the
# pairing code and QR of each entry not yet paired). Outside hass.data[DOMAIN],
# which holds only the entries.
HOMEKIT_DATA = f"{DOMAIN}_homekit"
# The pairing QR, for administrators (see homekit_accessory._PairingQRView).
HOMEKIT_QR_URL = "/api/vimar_intercom/homekit_qr"

# ─── Comandi di stato (in USCITA, Panda: blue) ───────────────────────────────
GET_INIT_STATUS = "GET_INIT_STATUS"   # → PICG_TARGET; risposta GET_INIT_STATUS_REPLY

# ─── Eventi bus HA (in INGRESSO — vedi PROTOCOL.md §4) ────────────────────────
# Payload documentati in README §Eventi. Namespace = DOMAIN.
EVENT_MISSED_CALL       = f"{DOMAIN}_missed_call"        # {sip_id, ts, name}
EVENT_VIDEOMESSAGE      = f"{DOMAIN}_videomessage"       # {change: NEW|UPDATE, full}
EVENT_FUORIPORTA        = f"{DOMAIN}_fuoriporta"         # {sip_id, msg}
EVENT_CALL_INFO         = f"{DOMAIN}_call_info"          # {sip_id, reason, media_type, video_src}
EVENT_PHONEBOOK_CHANGED = f"{DOMAIN}_phonebook_changed"  # {gid, rubrica_ver}

