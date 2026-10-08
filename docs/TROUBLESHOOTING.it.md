# Problemi e log

🇬🇧 *[English](TROUBLESHOOTING.md)* · [← README](../README.it.md)

## Per sintomo

Prima guarda la versione (Impostazioni → Dispositivi e servizi → Vimar Intercom): molti di questi sono
corretti in una release, indicata nella colonna *Cosa fare*. Poi attiva il log di debug (vedi
[Logging](#logging)) e riproduci il problema una volta. Non è qui? Apri una issue con quel log.

| Sintomo | Causa probabile | Cosa fare | Issue |
|---|---|---|---|
| Configurazione: «QR non valido o non riconosciuto» | Quello incollato non è il testo del QR di abbinamento: una foto, un link, una riga `ID=…` scritta a mano. Il testo è un'unica lunga stringa codificata | Leggi il codice con un'app per QR e incolla esattamente quello che mostra. Il motivo è dopo «In caso di errore:» nel modulo | — |
| Configurazione: «Registrazione SIP fallita» su un impianto cloud (Tab 5S Up 40515); il log mostra un REGISTER verso `…@127.0.0.1` | Il QR porta `domain=127.0.0.1`; il dominio dell'account è il `cdomain` | 1.0.1 o successiva, rifai la configurazione dal QR. Con le credenziali a mano usa il `cdomain`, non `127.0.0.1` né il proxy cloud | [#1](https://github.com/ha-vimar/ha-vimar-intercom/issues/1) |
| Segreteria o non disturbare rispondono `200 OK` ma non cambia nulla; la richiesta di stato non ha risposta | Comandi mandati all'SGA/PICG sbagliato. `55001` esiste su molti impianti ma non sempre è l'SGA | Prendi la rubrica ([PHONEBOOK.it.md](PHONEBOOK.it.md)), o imposta SGA/PICG a mano; per trovarli, il servizio `find_sga` | [#10](https://github.com/ha-vimar/ha-vimar-intercom/issues/10), [#14](https://github.com/ha-vimar/ha-vimar-intercom/issues/14) |
| L'app VIEW mostra «Configurazione appartamento modificata» | Ogni `GET_INIT_STATUS` mandato al vero SGA la fa comparire (visto due volte; il perché non si sa) | In `find_sga` lascia la sonda di default (`get_nicks`), che sull'impianto di riferimento non genera notifiche | [#10](https://github.com/ha-vimar/ha-vimar-intercom/issues/10), [#14](https://github.com/ha-vimar/ha-vimar-intercom/issues/14) |
| Il comando porta viene accettato ma la porta non si apre | Mandato alla targa sbagliata o con il comando sbagliato. Su un 2FV2 la porta la apre la targa della riga dell'attuatore porta (`GID_PE`), non l'SGA | 1.0.19 o successiva (la porta usa il comando dell'impianto); importa la rubrica, o imposta *Targa che apre la porta* | [#10](https://github.com/ha-vimar/ha-vimar-intercom/issues/10), [#58](https://github.com/ha-vimar/ha-vimar-intercom/issues/58) |
| `queued` / `202 Accepted` nei log o nel sensore *Intercom Ultimo Comando* | Il relay cloud ha accettato il comando ma non ne ha confermato la consegna; la sua risposta può arrivare dopo ~15 s | 1.0.20 o successiva: un 202 risulta `queued`, non eseguito, e i comandi via cloud aspettano 20 s. Controlla se è successo prima di riprovare | [#120](https://github.com/ha-vimar/ha-vimar-intercom/pull/120), [#139](https://github.com/ha-vimar/ha-vimar-intercom/pull/139), [#140](https://github.com/ha-vimar/ha-vimar-intercom/issues/140) |
| Porta e attuatori dicono «Non registrato» per ore dopo un calo di rete (cloud) | Due cicli di riconnessione giravano insieme | 1.0.10 o successiva | [#23](https://github.com/ha-vimar/ha-vimar-intercom/issues/23) |
| Camera: `404` da `55100`, niente video | La targa video di default non esiste sull'impianto | Importa la rubrica (imposta *Targa video*) o impostala a mano. Dalla 1.0.15 il campo vuoto usa la targa che ha suonato per ultima con il video | [#3](https://github.com/ha-vimar/ha-vimar-intercom/issues/3), [#129](https://github.com/ha-vimar/ha-vimar-intercom/issues/129) |
| Il video si sgrana, si blocca o va in ritardo, di più dalla 1.0.19; il log di debug mostra pacchetti video persi (`persa`) | La targa manda 2 Mbit/s tramite un relay cloud che perde qualche pacchetto su cento: dieci volte i pacchetti, dieci volte i fotogrammi rovinati (misurato su un Tab 7S Up 40517) | Impostazioni → *Qualità video dalla targa*: *Automatico* (256 kbit/s in cloud) o 256 kbit/s. Torna a 2 Mbit/s se lì l'immagine andava bene e la vuoi più nitida | [#161](https://github.com/ha-vimar/ha-vimar-intercom/pull/161) |
| Clip dello squillo salvata ma niente foto (Tab 7S 40517, cloud) | In analisi: la foto aspettava un secondo keyframe e questa targa forse ne manda uno solo | 1.0.20 o successiva (foto dal primo keyframe, decoder nel log di debug). Se manca ancora: aggiungi alla issue il log di debug di uno squillo | [#129](https://github.com/ha-vimar/ha-vimar-intercom/issues/129) (aperta) |
| Né anteprima né foto mentre suona, solo dopo la risposta | L'impianto non manda early media | Limite noto, niente da correggere lato HA | — |
| «AV stream: call not established after 25s» subito dopo un riaggancio (UDP locale) | La targa era ancora occupata con la chiamata precedente | 1.0.17 o successiva | [#41](https://github.com/ha-vimar/ha-vimar-intercom/issues/41) |
| Una dashboard aperta richiama la targa ~10 s dopo il riaggancio | La guardia dei 5 s si spostava a ogni riconnessione rifiutata | 1.0.19 o successiva | [#57](https://github.com/ha-vimar/ha-vimar-intercom/issues/57) |
| Uno squillo senza risposta continua a suonare in HA fino a 90 s | La targa smette dopo ~30 s senza mandare un CANCEL | 1.0.19 o successiva | [#60](https://github.com/ha-vimar/ha-vimar-intercom/issues/60) |
| Il microfono non fa niente | Il lettore video di HA non ha microfono; il browser lo permette solo in HTTPS | Parla da `custom:vimar-intercom-card`, aperta in HTTPS | — |
| Lo scaricamento della rubrica non funziona (dal citofono o dal cloud) | Il Tab non risponde in HTTP in LAN (40515), o l'impianto non manda il token cloud (40507) | Segui l'albero in [PHONEBOOK.it.md](PHONEBOOK.it.md) | [#5](https://github.com/ha-vimar/ha-vimar-intercom/issues/5) |

## Limiti noti

- **Audio bidirezionale solo dalla card del citofono**: il lettore video di HA non ha microfono, quindi
  rispondere da un pulsante, da una notifica o da Alexa prende la chiamata in silenzio. Per parlare
  usa `custom:vimar-intercom-card`, in HTTPS.
- **Anteprima allo squillo**: serve che l'impianto mandi early media (verificato su un Tab 5S Up
  40515 via cloud); altrimenti anteprima e foto dello squillo restano vuote finché qualcuno non
  risponde.
- **Impianti solo‑cloud** (es. Tab 5S Up 40515): il Tab risponde `503 You're not allowed` a qualsiasi
  richiesta SIP in LAN, quindi la modalità UDP locale lì non può funzionare; usa il cloud TLS.
  L'interfaccia HTTP locale del Tab (porta 80) rifiuta la connessione sui 40515 segnalati finora, quindi non c'è rubrica da leggere in LAN; camera, attuatori, apri‑porta e i comandi di stato funzionano lo
  stesso via SIP.
- **Segreteria/DND**: si comandano attraverso l'**SGA** (`SYSTEM.MAGIC_APT_INTERCOM` della rubrica,
  `55001` sull'impianto di sviluppo). Inviati a qualunque altro indirizzo vengono ignorati in
  silenzio: azzeccare l'SGA è ciò che li fa funzionare — impostalo in Options o lascialo riempire
  dall'import di `rubrica.db`.
- **Rubrica cloud**: serve un `token`. Gli impianti che rispondono al `GET_INIT_STATUS` in forma lunga
  lo consegnano direttamente, e a quel punto la rubrica si scarica con una sola richiesta autenticata —
  vedi [RUBRICA.md](RUBRICA.md) §0-bis, verificato su un 40515. Dalla 1.0.12 lo fa il menu delle opzioni:
  **"Scarica la rubrica dal cloud Vimar"** ([#5](https://github.com/ha-vimar/ha-vimar-intercom/issues/5)); il token si rilegge dall'impianto ogni
  volta e non viene salvato. Gli impianti che rispondono in forma corta (compreso quello di sviluppo) non
  hanno il token: lì si usa lo scaricamento dal citofono in LAN, o l'estrazione manuale.
- **Attuatori By‑me** (es. luci scala di domotica By‑me): potrebbero non rispondere via SIP anche se elencati in rubrica.
- **Lock**: nessun feedback fisico di stato (auto‑relock ottimistico dopo 5 s).
- **Squillo durante una nostra chiamata**: mentre Home Assistant chiama la targa o è in chiamata,
  un INVITE in arrivo riceve `486 Busy Here` e non genera l'evento campanello: sul campo non si
  distingue ancora dall'eco della nostra chiamata fatto dal PBX. Uno squillo subito dopo il BYE
  della targa è uno squillo normale.
- **Rubrica**: la via che funziona dipende dall'impianto (citofono in LAN, token cloud o un file
  `rubrica.db`): vedi l'albero in [PHONEBOOK.it.md](PHONEBOOK.it.md). Il token cloud non è
  un'impostazione dell'account: arriva nella risposta lunga dell'impianto al `GET_INIT_STATUS`, che non
  tutti gli impianti mandano.


---

## Logging

Il componente tiene un proprio buffer circolare (`log_buffer.py`, le ultime 3000 righe, `DEBUG`
compreso), leggibile dagli amministratori su `/api/vimar_intercom/debug?lines=N` (100 righe di
default).

Il log di Home Assistant riceve i record del componente dal livello impostato per
`custom_components.vimar_intercom` sotto `logger:` in `configuration.yaml` (o con il servizio
`logger.set_level`) in su, e da `WARNING` in su se nessun livello è impostato. Per vedere tutto:

```yaml
logger:
  logs:
    custom_components.vimar_intercom: debug
```

Entrambe le destinazioni oscurano password, risposte digest, token della rubrica e chiavi SRTP
prima di scrivere. **(1.0.21+)** Oscurano anche i dati dell'impianto (la 1.0.20 non ancora)
([#157](https://github.com/ha-vimar/ha-vimar-intercom/pull/157),
[#159](https://github.com/ha-vimar/ha-vimar-intercom/pull/159)), così un log va ripulito molto meno prima di allegarlo a una issue:

- **Indirizzi IP**: quelli privati ovunque, quelli pubblici dove possono essere solo tuoi o di un telefono
  (`received=`, l'host di un URI SIP, l'hop di un `Via`). Restano il primo e l'ultimo numero: `192.x.x.23`.
- **Id SIP**: quello di Home Assistant e quelli degli altri telefoni e dispositivi negli URI SIP (`<sip:id…45#9f1c@…>`).
  L'interno breve di una targa come `55001` resta leggibile: è quello che dice di quale targa parla una riga.
- **Il GID dell'appartamento** (`gid#xxxx`), il MAC del citofono, i domini SIP, IMEI e UUID del dispositivo, il nome del
  dispositivo e i nomi della rubrica.

Un'etichetta come `id…45#9f1c` tiene solo le ultime due cifre ed è uguale per lo stesso valore finché Home Assistant
non si riavvia, così due righe sullo stesso dispositivo si riconoscono ancora. Non è ancora coperto tutto: gli indirizzi
IPv6, quelli dentro le righe SDP e STUN e un id usato come nome visualizzato senza virgolette
(`From: 7798765 <sip:…>`) restano come sono, quindi rileggi un log prima di pubblicarlo.

**Il QR e il suo contenuto non vanno mai allegati**, né la foto né il testo: contiene la password SIP.

Le risposte attese al keepalive SIP (l'OPTIONS periodico) sono a `DEBUG`: non
serve alcun filtro `logger:` per tenere pulito il log.

