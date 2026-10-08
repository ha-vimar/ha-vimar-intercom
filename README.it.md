# Vimar Intercom — Integrazione Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-orange?style=for-the-badge)](https://hacs.xyz/docs/faq/custom_repositories/) [![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.10%2B-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white)](https://www.home-assistant.io/) [![Tests](https://img.shields.io/github/actions/workflow/status/ha-vimar/ha-vimar-intercom/validate.yml?branch=main&label=tests&style=for-the-badge)](https://github.com/ha-vimar/ha-vimar-intercom/actions/workflows/validate.yml) [![Last commit](https://img.shields.io/github/last-commit/ha-vimar/ha-vimar-intercom?style=for-the-badge)](https://github.com/ha-vimar/ha-vimar-intercom/commits/main) [![License](https://img.shields.io/github/license/ha-vimar/ha-vimar-intercom?style=for-the-badge)](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/LICENSE)

[![Apri il tuo Home Assistant e questo repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ha-vimar&repository=ha-vimar-intercom&category=integration)

🇬🇧 *[Read this page in English](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/README.md)*

Integra il videocitofono **Vimar Elvox** (2 Fili Plus / IP / 2FV2) in Home Assistant: lo squillo, la
porta e il cancello, il video dal vivo su richiesta, l'audio nei due sensi nella sua card per la
dashboard, segreteria e non disturbare, e gli attuatori del tuo impianto come pulsanti.

Parla con il citofono come fa l'app ufficiale Vimar VIEW: SIP, direttamente con il Tab sulla tua rete
oppure attraverso il cloud Vimar. Niente RTSP, e non serve un account Vimar.

![La card del citofono a riposo, mentre suona e in chiamata](https://raw.githubusercontent.com/ha-vimar/ha-vimar-intercom/main/docs/images/intercom-card.png)

## Compatibilità

| Tab | Art. | Impianto | Connessione | Stato |
|---|---|---|---|---|
| Tab 7S 2F+ WiFi | 40507 | 2F | UDP locale | Impianto di sviluppo: tutto quello che c'è qui sotto |
| Tab 5S Up 2 Wire WiFi | 40515 | 2FV2 | TLS cloud | Funziona (squillo, video, audio, porta). Su alcuni impianti il cloud non consegna i comandi di stato ([#14](https://github.com/ha-vimar/ha-vimar-intercom/issues/14)) |
| Tab 7S Up | 40517 | 2FV2 | TLS cloud | Funziona, HomeKit compreso |

Anche gli altri Tab Vimar 2F / 2FV2 / IP dovrebbero funzionare: la configurazione arriva dal QR di
abbinamento che il Tab mostra per l'app VIEW. I kit Wi-Fi che usano l'app **View Door** (per esempio
K40955) non sono supportati: per quanto ne sappiamo non hanno questo QR ([#167](https://github.com/ha-vimar/ha-vimar-intercom/issues/167)). Funziona da te, o no? Apri una [segnalazione di compatibilità](https://github.com/ha-vimar/ha-vimar-intercom/issues/new?template=compatibility_report.yml).
Cosa cambia da un impianto all'altro: [Configurazione](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CONFIGURATION.it.md#cosa-cambia-da-impianto-a-impianto).

## Installazione

Arrivi dall'integrazione originale di noiseheroes-lab? Prima toglila: [Migrazione](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/MIGRATION.it.md).

1. Premi il pulsante **Apri in HACS** qui sopra (oppure: HACS → menu ⋮ → *Repository personalizzati* →
   aggiungi `https://github.com/ha-vimar/ha-vimar-intercom`, categoria *Integrazione*).
2. Installa **Vimar Intercom** e riavvia Home Assistant.
3. Impostazioni → Dispositivi e servizi → **Aggiungi integrazione** → **Vimar Intercom**, poi incolla il
   testo del QR di abbinamento dell'app VIEW (oppure inserisci a mano i parametri SIP). Sulla rete
   locale il Tab compare spesso da solo tra i *Rilevati*.
4. In **Configura** scarica la rubrica del tuo impianto (dal citofono, dal cloud Vimar o da un file
   `rubrica.db`): imposta la porta, gli attuatori e gli indirizzi a cui vanno i comandi.

Serve Home Assistant 2025.10 o successivo e ffmpeg sull'host. Installazione manuale, tutte le opzioni e
la rubrica: [Configurazione](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CONFIGURATION.it.md).

## Cosa ottieni

- **Squillo**: un'entità evento e i sensori dello squillo per le tue automazioni; a scelta una foto e
  una breve clip di ogni visitatore, e webhook per Alexa o Scrypted; un pulsante *Squillo di prova* per provare le automazioni. → [Entità, servizi e automazioni](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/ENTITIES.it.md)
- **Porta, cancello e attuatori**: una serratura, il pulsante *Apri porta* e un pulsante per ogni
  attuatore della rubrica (F1/F2, luci scala, relè). → [Configurazione](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CONFIGURATION.it.md)
- **Video e voce**: la camera chiama la targa solo quando la apri, e mostra chi c'è mentre suona; la card
  della dashboard parla nei due sensi in HTTPS. → [La card del citofono](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CARD.it.md)
- **Segreteria e non disturbare** come interruttori, e un messaggio di assenza (file audio o sintesi
  vocale) quando nessuno risponde. → [Configurazione](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CONFIGURATION.it.md#segreteria)
- **Casa di Apple**: un videocitofono HomeKit nativo, facoltativo. → [HomeKit](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/HOMEKIT.it.md)
- **Scrypted, go2rtc, Frigate**: uno stream continuo che non fa mai suonare la targa. → [Sistemi esterni](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/EXTERNAL.md) (in inglese)
- **Echo Show come citofono**: video e voce nei due sensi da Alexa, passando per Scrypted. → [Echo Show (Scrypted)](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/SCRYPTED-ALEXA.it.md)

## Documentazione

| Pagina | Cosa contiene |
|---|---|
| [Configurazione](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CONFIGURATION.it.md) | Requisiti, installazione, tutte le opzioni, rubrica, SGA/PICG, segreteria, cosa cambia da impianto a impianto, note di sicurezza |
| [Entità, servizi e automazioni](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/ENTITIES.it.md) | Tutte le entità, i servizi, gli eventi, automazioni di esempio |
| [La card del citofono](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CARD.it.md) | Layout, pulsanti, video e voce, storico degli squilli |
| [HomeKit](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/HOMEKIT.it.md) | Il videocitofono nativo per la Casa di Apple |
| [Sistemi esterni](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/EXTERNAL.md) | Scrypted, Alexa, Echo Show, go2rtc, Frigate (in inglese) |
| [Echo Show come citofono](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/SCRYPTED-ALEXA.it.md) | Passo per passo: l'Echo Show guarda, chiama e parla con la targa tramite Scrypted |
| [Come avere la rubrica](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/PHONEBOOK.it.md) | Quale via funziona sul tuo impianto: citofono in LAN, token cloud, file `rubrica.db`, a mano |
| [Rubrica, dettagli](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/RUBRICA.md) | Da dove viene `rubrica.db` e cosa contiene |
| [Problemi e log](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/TROUBLESHOOTING.it.md) | Sintomo → causa → cosa fare, limiti noti, come leggere i log |
| [Migrazione](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/MIGRATION.it.md) | Passare dall'integrazione originale di noiseheroes-lab a questa |
| [Changelog](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/CHANGELOG.md) | Cosa è cambiato in ogni versione (in inglese) |

## Supporto e contributi

Domande e problemi: [issue](https://github.com/ha-vimar/ha-vimar-intercom/issues). Problemi di sicurezza: in privato, vedi [SECURITY.md](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/SECURITY.md).
I contributi sono benvenuti, vedi [CONTRIBUTING.md](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/CONTRIBUTING.md): mai indovinare comandi SIP (uno
sbagliato può aprire una porta), niente credenziali nel repo, e scrivi su che hardware hai provato.

## Disclaimer

Questo progetto **non è affiliato né approvato da Vimar S.p.A.**. "Vimar", "Elvox" e "VIEW" sono marchi
dei rispettivi proprietari. Le credenziali sono le tue, per il tuo impianto.

## Licenza

**MIT** — © [Noise Heroes](https://github.com/noiseheroes-lab) (progetto originale) e i contributori
del fork.
