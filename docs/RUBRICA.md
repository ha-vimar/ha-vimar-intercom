# RUBRICA.md — La rubrica Vimar (`rubrica.db`): schema, estrazione, uso in HA

**Aggiornato:** 21 settembre 2026.

La `rubrica.db` è il database SQLite che l'app VIEW / il Tab usano per sapere **chi chiamare**,
**quali attuatori mostrare**, **con quale comando/target**, e **chi è l'SGA** (il destinatario dei
comandi di stato segreteria/DND). È la fonte di verità per rendere l'integrazione "universale":
gli attuatori HA, l'SGA e i target non sono indovinati, si leggono da qui.

> ⚠️ **Fino al 19/09 questo file diceva che sull'impianto di sviluppo la rubrica non era ottenibile a
> runtime, perché «la porta 80 del Tab non risponde».** Era sbagliato: la porta 80 risponde, con
> autenticazione Digest. Se il citofono è raggiungibile in rete locale, la rubrica si scarica direttamente
> da lì con le credenziali SIP (§0) — ed è quello che fa l'integrazione dalla PR #16. L'estrazione dal
> telefono (§3, §3-bis) resta per chi il citofono non lo raggiunge in LAN.

> **Nessuna credenziale in questo file.** Non riportare mai password SIP, `ha1`, token account,
> IMEI o PIN del Tab. I comandi `adb`/`su` qui sotto sono generici.

## 0. Via più semplice: chiederla al citofono [VERIFICATO 20/09/2026]

Se il citofono è raggiungibile in rete locale basta una GET, autenticata con **le credenziali SIP** che
l'integrazione ha già — niente token, niente account Vimar, niente cloud, niente telefono rootato:

```bash
curl -sS --digest -u "<sip_user>:<sip_password>" \
     "http://<ip-del-citofono>/rest/get_file.php?name=rubrica" -o rubrica.db
```

Verificato su un Tab 7S (40507): `200`, un SQLite identico a quello estratto dall'app Android.
**Dall'integrazione: Opzioni → «Scarica la rubrica dal citofono»**, che nella stessa occasione chiede
`get_info.php?action=nickname` e ne ricava il **PICG dichiarato dall'impianto**.

Quattro trappole del server del citofono, tutte gestite da `rest_client.py`:

- il nome va **senza** `.db`: `name=rubrica` funziona, `name=rubrica.db` risponde 401;
- risponde **401 anche a una risorsa che non conosce**, mai 404 — un 401 non significa per forza
  credenziali sbagliate;
- il `nonce` del Digest arriva come `nonce="b'…'"` (apici compresi) e va rimandato identico;
- la risposta dichiara `Content-Encoding: none`, un valore non standard: non va decodificata.

Le risposte portano `Last-Modified`: con `If-Modified-Since` si sa se la rubrica è cambiata senza
riscaricarla. Lo stesso endpoint serve anche la segreteria: `get_file.php?name=mailbox`.

## 0-bis. Dal cloud, con il `token` — solo impianti che lo mandano [VERIFICATO 17/09/2026]

Se il tuo impianto risponde al `GET_INIT_STATUS` in **forma lunga** (con `token` e `rubrica_ver` fra i
PARAM — il parsing è in `plant_messages.py`, il download in `cloud_phonebook.py`), la rubrica si scarica
con una sola richiesta autenticata,
**senza telefono rootato, senza WSA, senza adb**:

```bash
curl -sS --digest -u "<cdomain-completo>:<token>" \
     -A "TOGA/2.4.0" \
     "https://<cproxy>/phonebook/domains/<cdomain-troncato>/<rubrica_ver>" \
     -o rubrica.db
```

⚠ **Il `cdomain` compare in due forme diverse nella stessa richiesta** — è il modo più facile di
prendersi un 401 o un 404:

| Dove | Forma | Esempio |
|---|---|---|
| username del Digest | `cdomain` **completo**, suffisso cloud incluso | `1234567890ab.FFFFFFFFFF.ipvdes.vimar.cloud` |
| path dell'URL | `cdomain` **senza `.<cproxy>`** | `1234567890ab.FFFFFFFFFF` |
| host dell'URL | solo il `cproxy` | `ipvdes.vimar.cloud` |

In codice:

```python
path_domain = cdomain[:-(len(cproxy) + 1)] if cdomain.endswith("." + cproxy) else cdomain
url = f"https://{cproxy}/phonebook/domains/{path_domain}/{rubrica_ver}"
# Digest: username = cdomain completo, password = token
```

| Valore | Dove si prende |
|---|---|
| `cdomain`, `cproxy` | QR di abbinamento decodificato |
| `token`, `rubrica_ver` | `GET_INIT_STATUS_REPLY` via SIP |

Lo User-Agent conta: è quello che manda l'app VIEW.

Il file scaricato è **identico a quello che l'app Android tiene in locale**, quindi si dà in pasto
direttamente all'importer delle opzioni (o a `tools/parse_rubrica.py`) senza conversioni.

**Dalla 1.0.12 lo fa l'integrazione** (issue #5): opzioni → **«Scarica la rubrica dal cloud Vimar»**
(`cloud_phonebook.py`). `cdomain` e `cproxy` dal config entry, `token` e `rubrica_ver` dall'ultima
risposta a `GET_INIT_STATUS` (se manca il token la richiede e aspetta qualche secondo); il GID proposto è
quello che l'impianto dichiara. Il token resta in memoria, non va né nel config entry né nel sensore
«Ultimo messaggio ricevuto» (oscurato).

Verificato da @CPietro su un impianto 40515/2FV2 in cloud (issue pubblica #5). **Sull'impianto di
sviluppo non è applicabile**: la reply è corta e il token non c'è. Serve soprattutto a chi non raggiunge
il citofono in LAN: dove il citofono risponde su HTTP, §0 è più semplice e non dipende dal token.

> Il `token` va trattato come la password SIP: mai nei log, mai nel repo, mai in un incolla pubblico.
> Se lo pubblichi da qualche parte, considera compromessa la rubrica dell'impianto.

## 1. Come l'app la ottiene (dai sorgenti `com.vimar.vmsipsdk` decompilati) [APK]

Tre vie, a seconda del tipo di impianto (`CLOUD` nel QR: 0 home+cloud, 1 prima home poi cloud, 2 solo cloud):

- **Home mode** (`CLOUD=0/1`, LAN): `http://<proxy>/rest/get_file.php?name=rubrica` (HTTP Digest `sipID`/password SIP)
  — **funziona sull'impianto di sviluppo** [VERIFICATO 20/09]; la vecchia nota «:80 muto» era sbagliata.
- **SIP**: `GET_INIT_STATUS` (Panda: blue → PICG) → `GET_INIT_STATUS_REPLY`, che **su alcuni impianti**
  contiene `token` e `rubrica_ver`, poi download cloud (sotto).
- **Cloud phonebook**: `GET https://<cproxy>/phonebook/domains/<cdomain_senza_.cproxy>/<ver>` con
  **HTTP Digest user=`<cDomain>` pass=`<token>`** (`VMClientRepository.startCloudDownloadRubrica`).
  Il `token` viene **solo** dal `GET_INIT_STATUS_REPLY` (o dal suo equivalente HTTP). L'ipotesi che lo
  fornisse l'account Vimar è **smentita**: l'account non ha accesso al citofono.

## 2. Schema (23 tabelle) — dati reali di questo impianto + note universali

| Tabella | Colonne principali | Contenuto qui / note |
|---|---|---|
| `ACTUATOR_LIST` | `ID, NAME, GID_PE, ATT_ID, MSG, DTMF, OUTPUT, TIME, ICON` | **6 attuatori, tutti `GID_PE=55001`**: Serratura(`MSG=OPEN`, dtmf `*0000`, ICON=1 DOOR), TIRO(`TARGA_ULTIMA_SERRATURA`, DOOR), "F1 ultima targa"(`TARGA_ULTIMA_F1`, ICON=3 SWITCH), "F2 ultima targa"(`TARGA_ULTIMA_F2`, SWITCH), "LUCE SCALA"(`ATTUATORE_01`, SWITCH), "Attuatore 02"(`ATTUATORE_02`, SWITCH). `TIME=500` (ms). **Il body SIP inviato = `MSG` letterale**, `Panda: command`, verso `GID_PE`. `ATT_ID` = id modulo, NON il target. |
| `ACTUATOR_RULES` | `ID, GA_GID, ACTUATOR_ID` | quali attuatori vede il GID appartamento (101). |
| `ACTUATOR_DEFAULT` | `PRODUCT, TERMINAL, MSG, DTMF` | comandi default per modulo: AV→`OPEN`(*0010), Keyboard→`AUX1`(*0011), RFID→`AUX2`(*0012), FP→`AUX3`(*0013). |
| `ICON_LIST` | `ID, NAME` | 1=DOOR, 2=LIGHT, 3=SWITCH → mappate a device_class/icona in HA. |
| `PHONEBOOK` | `GID, TYPE, MID, NAME, …ENABLE, GATE…` | 101=Casa(GA), **55001=Casa CG (PICG=SGA)**, **55100=Video (PE targa/camera, ENABLE=1, GATE=1)**, 55200=Centralino(P). |
| `PID_LIST` | `…PID, PID_ROLE…` | appartamento 101 → PID **55001**, role **PICG**. |
| `SYSTEM` | `PARAM, VALUE` | `MAGIC_APT_INTERCOM=55001` (**SGA**), `VM_PREFIX=8000`, `SCHEMA_VER=1`, `DB_VER=0`. |
| `PRODUCT_LIST` | `PRODUCT, …` | TAB 7S=40607, TAB 7=40605, TAB 4.3S=40603, TAB 4.3=40601; moduli AV=41006, Tastiera=41019, RFID=41017, FP=41016, Tastiera CA=41020, LCD=41018, Relè=40636. |
| `TVCC_LIST` / `TVCC_RULES` | — | telecamere IP: **vuote qui**, schema noto per impianti che le hanno. |
| `ENTRANCE_LIST` | — | ingressi/pulsantiere: vuota qui. |
| `INPUT_LIST` / `INPUT_DEFAULT` | — | ingressi/allarmi: vuoti qui. |
| `MONITORING` / `MONITORING_LIST` | — | monitoraggi: vuoti qui. |
| `PLAYLIST` / `ACTUATOR_PLAYLIST` | — | sequenze attuatori: vuote qui. |
| `ACTION_LOG`, `DENY_IP_LIST` | — | vuote qui. |
| `BUILDING_LIST`, `MID_LIST`, `STAIR_LIST`, `ZONE_LIST` | — | contenitori gerarchici (default 911). |

> ⚠ **Attenzione ai due 55xxx:** la **camera/targa reale è 55100**; **55001 è il PICG/capogruppo** (SGA)
> a cui vanno attuatori e comandi di stato. Non confonderli.

## 3. Estrazione via root (metodo riproducibile) — muletto Samsung A32

L'app ha `allowBackup=false`, quindi `adb backup` non funziona: serve accesso `su` al filesystem.
Metodo usato (senza credenziali):

1. **Muletto**: Samsung A32 `SM-A325F`, Android 13. Root con **Magisk**: sblocco bootloader
   (OEM unlocking), `AP` patchato con Magisk e flashato via **Odin**.
2. Installare **VIEW** (`com.vimar.view`) e fare **login con l'account Vimar dell'utente**.
   L'app registra ogni nuovo device con un **id SIP nuovo** (es. 60903) e salva le credenziali
   come `ha1` nel `linphonerc` (mai in chiaro).
3. Copiare il db con `adb` + `su`:
   ```bash
   adb shell "su -c 'ls -1 /data/data/com.vimar.view/files/*_rubrica.db'"
   adb shell "su -c 'cp /data/data/com.vimar.view/files/<domain>_rubrica.db /sdcard/rubrica.db'"
   adb pull /sdcard/rubrica.db ./rubrica.db
   ```
   (Il prefisso `<domain>` è il `CDOMAIN` dell'impianto.)
4. **Non** copiare `linphonerc`/prefs nel repo: contengono `ha1` e device id.

### 3-bis. Estrazione senza telefono rootato — VIEW su WSA (segnalato da @CPietro)

Se non hai un muletto da rootare, si può installare l'app VIEW nel **WSA** (Windows Subsystem for
Android) e tirare fuori il db con `adb` da lì: WSA espone un endpoint adb e, non essendo un device
di produzione, l'accesso ai dati dell'app è molto più semplice. Il resto della procedura è identico
al punto 3 (login con l'account Vimar, poi copia di `<domain>_rubrica.db`).

Metodo riportato da @CPietro su un impianto 40515/2FV2 nel thread della
[HA Community](https://community.home-assistant.io/t/vimar-tab5s-up-2-wire-wifi-elvox-40515/833805);
non verificato direttamente su questo impianto. Vale la stessa regola del punto 4: `linphonerc` e le
preferenze non escono da lì.

## 4. Stato delle vie sull'impianto di sviluppo [aggiornato 20/09/2026]

Fino al 19/09 questa sezione elencava quattro vie «chiuse». Com'è davvero:

| Via | Stato |
|---|---|
| **HTTP locale** (porta 80 del Tab) | ✅ funziona, con Digest e credenziali SIP (§0) |
| SIP `GET_INIT_STATUS` | ✅ funziona **verso il PICG** (qui 55001); prima lo mandavamo all'indirizzo sbagliato |
| `adb backup` | ❌ `allowBackup=false` — non serve più |
| Cloud col `token` | ⛔ non applicabile qui: la reply è corta, niente token |

> Su un 40515/2FV2 in cloud (@CPietro) la porta 80 era stata segnalata chiusa. Quella prova è di prima che
> sapessimo del Digest: vale la pena ripeterla con le credenziali SIP, come in §0.

## 5. Da `rubrica.db` a opzioni HA — `tools/parse_rubrica.py`

```bash
python tools/parse_rubrica.py rubrica.db --gid 101 --json
```
- apre il db **read‑only**, non stampa credenziali;
- estrae gli **attuatori** visibili al GID appartamento come lista JSON `{name, msg, target, icon}`
  (`msg` = body SIP letterale, `target` = `GID_PE`, `icon` = door|light|switch da `ICON_LIST`);
- riporta l'**SGA** reale (`SYSTEM.MAGIC_APT_INTERCOM`) e i parametri `SYSTEM` utili.

L'output JSON si incolla nell'**options flow** dell'integrazione (campo "Attuatori (JSON)"):
crea i bottoni dinamici (`button.py` → `VimarActuatorButton`), inviati con `hub.async_send_command`
(`Panda: command`). `target: "AUTO"` usa la targa di default dell'apri‑porta.

## 6. Rendere l'importazione "universale" (ROADMAP)

- ~~Importer `rubrica.db` → `options["actuators"]` senza copia‑incolla manuale.~~ **Fatto** (file caricato).
- ~~Auto‑detect `sga_target` da `SYSTEM.MAGIC_APT_INTERCOM`.~~ **Fatto** (applicato alla conferma).
- ~~Scaricare la rubrica senza estrarla a mano.~~ **Fatto**: «Scarica la rubrica dal citofono», con il PICG
  dichiarato dall'impianto.
- ~~Mappatura `ICON_LIST` → icona HA.~~ **Fatto**: `door`/`light`/`switch` diventano icone mdi sui bottoni
  attuatore (`button.py`). Un device_class vero non c'è: quelli dei bottoni HA (identify, restart, update)
  non c'entrano.
- Supporto `TIME`. (La colonna `DTMF` c'è, ma l'app non la usa mai.)
- Entità derivate dal `PHONEBOOK` (targhe/camere 551xx, centralino, appartamento) + supporto `TVCC_LIST` dove presente.
- ~~Import cloud phonebook quando il `token` è disponibile.~~ **Fatto** dalla 1.0.12: «Scarica la rubrica dal
  cloud Vimar» (§0-bis).
