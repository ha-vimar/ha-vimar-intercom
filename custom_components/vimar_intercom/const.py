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
# Ogni flusso RTP occupa DUE porte: N per l'RTP e N+1 per l'RTCP (RFC 3550).
# Porte adiacenti quindi si scontrano: con video 19201 e audio 19202, l'RTCP
# del video (19202) è la porta RTP dell'audio e ffmpeg falliva il bind di
# entrambe ("bind failed: Address in use"), uscendo subito all'avvio.
# Vanno tenute pari e distanziate di almeno 2.
RTP_AUDIO_PORT     = 7200     # + 7201 RTCP
RTP_VIDEO_PORT     = 9200     # + 9201 RTCP
FFMPEG_AV_VIDEO_PORT = 19202  # AV ffmpeg video (+ 19203 RTCP)
FFMPEG_AV_AUDIO_PORT = 19204  # AV ffmpeg audio (+ 19205 RTCP)
# 19206-19217: le coppie di porte delle sessioni HomeKit (media_handler).

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
# Targa interna — usata solo dal bottone "Chiama Casa (interno)".
# A differenza di SGA/PICG **non è configurabile**: non abbiamo un campo del
# config entry né una chiave di rubrica.db da cui ricavarla, e indovinarla
# (SGA+1) sarebbe esattamente il tipo di supposizione che questo progetto non
# fa. Su un impianto diverso il bottone chiamerà un indirizzo inesistente e la
# chiamata fallirà: nessun effetto collaterale, al contrario dell'apri-porta.
INTERNAL_PANEL_TARGET   = "55002"
SEGRETERIA_ON           = "VOICEMAIL;ON"
SEGRETERIA_OFF          = "VOICEMAIL;OFF"
SEGRETERIA_HEADER_NAME  = "Panda"
SEGRETERIA_HEADER_VALUE = "blue"   # dall'app VIEW: i messaggi di stato usano Panda: blue

# Non disturbare — "DND;ON" / "DND;OFF" verso l'SGA (Panda: blue).
DND_ON                  = "DND;ON"
DND_OFF                 = "DND;OFF"


# ─── Targa video (autoaccensione camera on-demand) ───────────────────────────
# La camera on-demand chiama QUESTA targa per accendere il video (autoaccensione),
# NON il PICG 55001 (che dava 488 Not Acceptable Here). Dalla rubrica.db:
# PHONEBOOK GID=55100 TYPE='PE' NAME='Video'. [da rubrica 20/08/2026]
# TODO: rendere configurabile in options / ricavare da PHONEBOOK (TYPE PE).
CAMERA_TARGET = "55100"

# ─── Comandi di stato (in USCITA, Panda: blue) ───────────────────────────────
GET_INIT_STATUS = "GET_INIT_STATUS"   # → PICG_TARGET; risposta GET_INIT_STATUS_REPLY

# ─── Eventi bus HA (in INGRESSO — vedi PROTOCOL.md §4) ────────────────────────
# Payload documentati in README §Eventi. Namespace = DOMAIN.
EVENT_MISSED_CALL       = f"{DOMAIN}_missed_call"        # {sip_id, ts, name}
EVENT_VIDEOMESSAGE      = f"{DOMAIN}_videomessage"       # {change: NEW|UPDATE, full}
EVENT_FUORIPORTA        = f"{DOMAIN}_fuoriporta"         # {sip_id, msg}
EVENT_CALL_INFO         = f"{DOMAIN}_call_info"          # {sip_id, reason, media_type, video_src}
EVENT_PHONEBOOK_CHANGED = f"{DOMAIN}_phonebook_changed"  # {gid, rubrica_ver}


# Pubblica il citofono in HomeKit come videocitofono nostro (vedi
# homekit_accessory.py). Spento se non detto altrimenti.
CONF_HOMEKIT_ACCESSORY = "homekit_accessory"

# Video HomeKit ricodificato sul Pi (più fluido) invece che diretto (più veloce).
CONF_HOMEKIT_SMOOTH = "homekit_video_smooth"
# HomeKit è una scelta: spento finché qualcuno non lo accende dalle opzioni.
# Il video ricodificato invece è il default: con il video diretto il relay
# perde abbastanza pacchetti da fermare l'immagine fino a 3 s alla volta.
DEFAULT_HOMEKIT_ACCESSORY = False
DEFAULT_HOMEKIT_SMOOTH = True
