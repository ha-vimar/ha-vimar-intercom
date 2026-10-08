# Configurazione

🇬🇧 *[English](CONFIGURATION.md)* · [← README](../README.it.md)

## Requisiti

- Home Assistant **2025.10** o successivo, Python 3.13+ (quello di HA 2025.10).
- ffmpeg sull'host HA (dipendenza dichiarata nel manifest) per la camera.
- Il **QR di abbinamento** dell'impianto Vimar (dall'app VIEW) **oppure** i parametri SIP manuali
  (id, password, domain, cloud proxy).
- Requisiti Python, installati da Home Assistant: `pycryptodome` e `requests`, più `HAP-python` e
  `PyQRCode` per il campanello HomeKit facoltativo. Nessuna libreria SIP esterna: lo stack è custom.


---

## Installazione

### Via HACS
1. HACS → Integrazioni → menu ⋮ → *Custom repositories* → aggiungi il repo come categoria *Integration*.
2. Installa **Vimar Intercom**.
3. Riavvia Home Assistant.

### Manuale
Copia `custom_components/vimar_intercom/` nella cartella `config/custom_components/` di HA e riavvia.


---

## Configurazione

Impostazioni → Dispositivi e servizi → Aggiungi integrazione → **Vimar Intercom**.

- **QR** (consigliato): incolla il testo del QR di abbinamento Vimar; l'integrazione lo decodifica
  (`qr_decoder.py`: Base64(AESkey|AES‑CBC|IV) → coppie `KEY=VALUE`) e compila id, password, domain,
  cloud/local proxy, GID, MAC, planttype.
- **Manuale**: inserisci `sip_user`, `sip_password`, `sip_domain`, `cloud_proxy`. Sono i valori
  contenuti nel QR decodificato (`ID`, `PWD`, `DOMAIN`, `CPROXY`): con il QR non si scrivono mai a
  mano. Il dominio è quello della credenziale, non il proxy cloud: se il QR dice `DOMAIN=127.0.0.1`
  (come su un 40515) usa il suo `CDOMAIN`.

**Trovato in rete** (dalla 1.0.12, [#6](https://github.com/ha-vimar/ha-vimar-intercom/issues/6)): il Tab si annuncia via mDNS
(`_eipvdes._tcp`, lo stesso servizio che cerca l'app VIEW) e Home Assistant lo propone tra i
*Rilevati*. Servono ancora il QR o le credenziali (l'annuncio non porta segreti), ma l'indirizzo del
citofono e il dominio SIP locale arrivano dal Tab stesso. Conta sugli impianti il cui QR dice
`domain=127.0.0.1`, come un 40515: per la registrazione locale si usa il dominio annunciato dal Tab
(il suo indirizzo) invece di quello cloud. Un citofono già configurato si riconosce dal MAC, e se il
DHCP gli dà un altro indirizzo Home Assistant propone quello nuovo tra i dispositivi trovati e lo usa
quando confermi (solo in modalità locale). La conferma serve perché chiunque nella rete locale può
annunciarsi con il MAC del Tab. Se premi Ignora per sbaglio, anche i cambi successivi vengono
ignorati: Impostazioni → Dispositivi e servizi → Ignorati → Vimar Intercom → Smetti di ignorare. Dove
l'mDNS è
filtrato non cambia nulla: si aggiunge a mano come prima.

### Opzioni (dopo l'aggiunta)

Impostazioni → Vimar Intercom → **Configura**:

| Opzione | Descrizione |
|---|---|
| **IP del citofono** (`local_proxy`) | IP del Flexisip locale sul Tab |
| **Usa SIP UDP locale** (`use_local_udp`) | ON = UDP locale; OFF = TLS cloud |
| **Porta UDP locale** (`local_udp_port`) | default 5060 |
| **Attuatori (JSON)** (`actuators`) | lista JSON `{name, msg, target, icon}`; crea bottoni dinamici. Vuoto = nessun bottone |
| **SGA** (`sga_target`) | destinatario di `VOICEMAIL;`/`DND;`, e del comando di apertura se `door_target` è vuota. Vuoto = default `55001` |
| **PICG** (`picg_target`) | destinatario di `GET_INIT_STATUS`. Sull'impianto di sviluppo coincide con l'SGA, su altri no (60001 su un 40515). Vuoto = default `55001` |
| **Targa video** (`camera_target`) | targa chiamata dalla camera, da *Chiama* e da *Chiama Video (esterno)*: la riga `PHONEBOOK` con `TYPE='PE'`. **Non è l'SGA.** Vuoto = la targa imparata dall'ultimo squillo (una targa che ha suonato con il video, usata quando `55100` non esiste sull'impianto), altrimenti il default `55100` |
| **Pannello interno** (`internal_panel_target`) | destinatario di *Chiama Casa (interno)*. La rubrica non lo dice: va inserito a mano. Vuoto = default `55002` |
| **Targa che apre la porta** (`door_target`) | destinatario del comando di apertura (serratura, *Apri Porta*, `open_door` senza `target`, attuatori con target `AUTO`): il `GID_PE` dell'attuatore porta nella rubrica. **Non sempre è l'SGA**: su un 2FV2 l'SGA è `61000` e la porta la apre la targa `55001`. Vuoto = la targa dell'attuatore porta salvato, altrimenti l'SGA |
| **Cartella foto squillo** (`snapshot_dir`) | dove salvare, a ogni squillo, la foto di chi suona (`squillo_AAAAMMGG_HHMMSS_mmm.jpg` + `ultimo_squillo.jpg`) e il clip dello squillo (`squillo_AAAAMMGG_HHMMSS_mmm.mp4`: il video dell'anteprima, e della chiamata se si risponde da HA, fino a 60 s, senza audio), es. `/config/media/citofono`. Deve essere scrivibile da HA. Vuoto = disattivato |
| **Secondi per la foto migliore** (`snapshot_delay`) | la prima foto si salva appena arriva il primo fotogramma (~1 s dallo squillo); dopo questi secondi la sostituisce un fotogramma con l'esposizione regolata (il primo keyframe della targa è scuro). Default 3 (Tab 5S Up 40515), 0 = resta la prima |
| **Silenzio della vista** (`view_keepalive`) | secondi di silenzio audio mandati durante «Vedi esterno» (0 = nessuno). Via cloud senza silenzio la targa chiude la vista a ~10 s; sul 2 fili in locale tiene occupato l'appartamento (fino a 300 s). Default: 120 via cloud, 0 in locale; sul 2 fili metti 0 o 30 |
| **Qualità video dalla targa** (`video_bandwidth`) | la banda video chiesta alla targa, per tutto il video (card, camera, `/av`, HomeKit). **Automatico** (default): 2 Mbit/s in UDP locale, 256 kbit/s tramite il relay cloud. **256 kbit/s**: meno fotogrammi rovinati con un relay che perde pacchetti, ma immagine più piccola su alcune targhe (un Tab 5S Up 40515 scende a 320x240 a 15 fps). **2 Mbit/s**: l'immagine più nitida (720x576 a 24 fps su un 40515), dieci volte il traffico; con un relay che perde pacchetti (un Tab 7S Up 40517) il video si sgrana, si blocca e va in ritardo |
| **Utenti ammessi** (`allowed_users`) | Limita la card, lo storico squilli (`GET /api/vimar_intercom/rings`, foto e clip) e `/audio_ws` a questi utenti HA. Gli amministratori sempre. Vuoto = ogni utente autenticato (default). **Non** è per utente per l'entità camera né per `/av` usato con la sua chiave (la camera di HA, go2rtc: solo rete locale, vedi sotto): chi può aprire la camera vede e sente lo stream. Se la cartella foto è sotto una cartella media di HA (es. `/config/media/citofono`), foto e clip compaiono nel browser media per tutti gli utenti |
| **Messaggio di assenza** (`away_message_file`, `away_message_delay`) | file audio (mp3, wav…) fatto sentire al visitatore se nessuno risponde entro N secondi; poi l'integrazione riaggancia. Se il Tab espone il ritardo della segreteria si usa quello (*Segreteria · ritardo*) |
| **Messaggio di assenza da testo** (`away_message_text`, `away_message_tts`) | se il campo file è vuoto, questo testo lo legge la sintesi vocale di Home Assistant (`away_message_tts` = un'entità `tts.*`; vuoto = il motore predefinito di HA) nella lingua di HA, max 30 s. L'audio si genera all'avvio e resta in cache; se il TTS fallisce il citofono squilla come sempre |
| **Cifratura del media (SRTP)** (`media_enc`) | **Automatico** (default dalla 1.0.11): segue il `media_enc` che l'impianto dichiara nella risposta a `GET_INIT_STATUS` (`"srtp"` su un 40515 in cloud); gli impianti con la risposta corta (il 40507) restano in RTP chiaro. **Attivo** / **Disattivo** lo forzano. Chi aveva salvato «attivo» con la 1.0.10 o prima resta attivo; «spento» diventa automatico. Prova **Attivo** se la camera resta nera o la chiamata fallisce con `488` |
| **Risposta a voce** (`voice_answer`) | Chi può rispondere a uno squillo parlando su `/audio_ws`: **Solo dichiarato** (default, solo con `?voice_answer=1`), **Mai**, **Chiunque** (qualsiasi connessione con microfono; un tablet a muro col microfono rimasto aperto può rispondere da solo coi rumori di casa) |
| **Webhook squillo** (`ring_webhook_url`, `ring_end_webhook_url`) | GET opzionale (fire-and-forget, timeout 5 s) inviata quando inizia uno squillo e quando finisce (risposto, annullato o non risposto) — es. gli URL `turnOn`/`turnOff` di un Dummy Switch Scrypted (vedi [docs/EXTERNAL.md](EXTERNAL.md)). Un fallimento logga solo un warning, mai blocca lo squillo. Vuoto = disattivato |

**Chiave dello stream /av** (menu delle opzioni → *Chiave dello stream /av*): `/api/vimar_intercom/av` semplice chiama la targa, quindi risponde solo a un utente di Home Assistant autenticato o a una richiesta con la chiave di questa installazione, `?auth=<chiave>`. La chiave nasce al primo avvio, non scade e la camera dell'integrazione la aggiunge da sola; la pagina la mostra per go2rtc, Frigate o Scrypted (vedi [EXTERNAL.md](EXTERNAL.md#the-av-key)). **Rigenerala** se un log o un URL con la chiave è finito in mani altrui: la vecchia smette subito di funzionare e l'integrazione si ricarica.

L'immagine della camera fra una chiamata e l'altra (l'ultima foto dello squillo) è visibile a ogni utente di Home Assistant che vede l'entità camera; `allowed_users` limita la cronologia degli squilli e i media dal vivo, non l'entità camera.

### Segreteria

C'è un solo switch *Segreteria* (Configurazione, pagina del dispositivo). Acceso, usa il
messaggio di assenza di Home Assistant se c'è un testo o un file audio (e spegne la segreteria del Tab);
altrimenti accende quella del Tab. Spento, sono spente entrambe. Se il Tab accende da solo la propria
segreteria, vale quella del Tab. Il ritardo è uno solo, *Segreteria · ritardo* (quello del Tab; se il Tab non lo
espone, l'opzione `away_message_delay`, 0 = 20 s): il messaggio di HA parte dopo quei secondi.
Il messaggio si imposta dalla stessa pagina: *Segreteria · testo del messaggio* e *Segreteria · file audio* (elenco dei file in
`<prima cartella media di HA>/citofono/messaggi`, creata se manca; si carica da Media > Local media; l'elenco si aggiorna ogni minuto).
Il file si può caricare anche dalle impostazioni dell'integrazione (*Oppure carica il file audio*: mp3, wav o m4a, massimo 5 MB): finisce in quella cartella e diventa subito il messaggio di assenza.
Sono le stesse opzioni dell'integrazione, applicate subito senza ricaricarla. Solo gli amministratori possono cambiare testo e file del messaggio (dalla 1.0.17, #64); il dialog delle impostazioni della card nasconde quelle righe agli altri utenti.

Esempio, Tab 5S Up 40515 (Due Fili Plus, cloud): SGA `61000`, PICG `60001`, targa video e apri‑porta
`55001`. Sono i valori della rubrica dell'app VIEW, non i default.

Gli attuatori e i valori SGA/PICG si ricavano dalla **rubrica dell'impianto** (`rubrica.db`): dal menu
delle opzioni scegli **"Scarica la rubrica dal citofono"** (in LAN), **"Scarica la rubrica dal cloud
Vimar"** (impianti con la risposta lunga di `GET_INIT_STATUS`) oppure **"Importa attuatori da
rubrica.db"**, carica il file (lo trovi con l'app VIEW o via root, vedi [RUBRICA.md](RUBRICA.md)) e conferma (quale via fa per il tuo impianto: l'albero in
[PHONEBOOK.it.md](PHONEBOOK.it.md)) — attuatori, SGA, PICG, targa video e targa che apre la porta vengono impostati in automatico.
In alternativa puoi inserire i valori a mano nello step "Impostazioni" (utile se conosci già l'SGA del
tuo impianto o vuoi modificare la lista attuatori prodotta dall'import).


---

## Cosa cambia da impianto a impianto

Le due segnalazioni sui Tab 5S, messe accanto all'impianto di
sviluppo, portano alla stessa conclusione pratica: *conta l'indirizzo a cui mandi il comando, e quanto
il Tab ti racconta di ritorno*.

- **I comandi di stato vanno all'SGA.** Sull'impianto di sviluppo (40507 / 2F) `VOICEMAIL;ON|OFF` e
  `DND;ON|OFF` funzionano se inviati all'SGA — lì `55001`, preso da `SYSTEM.MAGIC_APT_INTERCOM` della
  rubrica. Mandati altrove (l'indirizzo del Tab stesso, il gruppo appartamento, il vecchio default
  `60001`) tornano un 200 OK senza effetto, o un 404. Se i tuoi switch sembrano morti, le opzioni
  SGA/PICG sono la prima cosa da controllare — vedi [le opzioni](#opzioni-dopo-laggiunta).
- **`GET_INIT_STATUS` risponde, ma con quantità di dettaglio diverse.** Sul 40507 la risposta è corta:
  `rubrica_ver`, `vm_ver`, `vm_level`, `dnd`, `voicemail`. Sull'impianto 40515 / 2FV2 è il payload
  completo — `dnd`, `voicemail`, `rubrica_ver`, `vm_ver`, `vm_level`, `vm_timeout`,
  `vm_timeout_values`, `apt_names`, `GID`, `media_enc` e un `token`. Per questo l'integrazione legge
  quello che trova e ignora quello che manca, invece di aspettarsi un insieme fisso.
- **La cifratura media è un valore dell'impianto**, non un default globale: il 40515 dichiara
  `media_enc: "srtp"`, mentre l'impianto di sviluppo rifiuta SRTP e lavora in RTP chiaro.
- Ancora aperto sul 40515: **la segreteria si accende ma non si spegne**, in corso di verifica da chi
  l'ha segnalato.

Se lo fai funzionare su un modello diverso, o sullo stesso con risultati diversi, apri una
[segnalazione di compatibilità hardware](https://github.com/ha-vimar/ha-vimar-intercom/issues/new?template=compatibility_report.yml) — anche
i casi in cui ha funzionato tutto al primo colpo sono utili quanto quelli in cui si è rotto qualcosa.


---

## Sicurezza

- Credenziali SIP (password e `ha1`) memorizzate nella config entry di HA sotto `.storage`, in
  chiaro come i segreti di ogni altra integrazione. Non vengono mai loggate.
- Endpoint HTTP interno: `/av` è filtrato **solo LAN** (`_is_local_request`), e `/av` semplice
  (che chiama la targa) vuole anche un utente HA autenticato o la chiave dell'installazione
  (`?auth=<chiave>`, confrontata a tempo costante; chiave sbagliata o assente = 403). La chiave è
  mascherata nei log dell'integrazione e in quelli dello stream di HA; il WebSocket
  `/audio_ws` richiede autenticazione HA, e le sue azioni di debug (`command`, `probe`, `scan`,
  `register`, `reconnect`) sono riservate agli amministratori. Il payload del QR non viene loggato
  a livello INFO.
- Parlare su `/audio_ws?voice_answer=1` mentre squilla risponde alla chiamata (RMS del microfono sopra soglia
  per 200 ms): così rispondono Echo Show e HomeKit via Scrypted. Chi può rispondere lo decide l'opzione
  **Risposta a voce** (`voice_answer`): `declared` (default, solo col flag), `off` (mai), `any` (qualsiasi
  connessione con microfono: un tablet a muro col microfono rimasto aperto può rispondere da solo coi
  rumori di casa). In ogni modo una connessione che era in chiamata non risponde finché non torna a
  riposo. A riposo l'audio si butta.
  Client esterni con URL firmato (`auth/sign_path`): metti `voice_answer=1` nel path *prima* di firmare;
  aggiungerlo dopo dà 401, perché HA valida la query firmata. Oppure scegli l'opzione `any`.
- Ultimi squilli per la card: `GET /api/vimar_intercom/rings` (elenco, `?limit=` fino a 50) e
  `GET /api/vimar_intercom/rings/<nome>` (la foto o il clip, anche a pezzi con Range) richiedono
  l'autenticazione HA (la card li carica con percorsi firmati). Il secondo serve solo file
  `squillo_AAAAMMGG_HHMMSS[_mmm].jpg` / `.mp4` dentro `snapshot_dir`, nient'altro (nemmeno un
  clip ancora in scrittura); la cartella non viene mai esposta sotto `/local`.
- Caricamento del messaggio di assenza dalla card: `POST /api/vimar_intercom/away_upload?name=<nome file>`
  col file come corpo. Solo amministratori (gli altri hanno 401); stesse regole del caricamento dalle
  opzioni (solo il nome del file, mp3/wav/m4a, massimo 5 MB, controllati mentre si legge, mai sopra un
  altro file).
- In modalità UDP locale i pacchetti SIP che non arrivano dal citofono vengono scartati: un altro
  dispositivo in LAN non può simulare uno squillo.
- L'RTP in chiaro (senza SRTP) è accettato solo dall'altro capo della chiamata in corso.
- Il codice di abbinamento HomeKit (campanello HomeKit facoltativo) è salvato in un file con
  permessi 0600.
- Nessuna dipendenza cloud obbligatoria in modalità UDP locale.
- Hai trovato una vulnerabilità? Segnalala in privato, non in una issue pubblica: vedi [SECURITY.md](../SECURITY.md) (in inglese).

