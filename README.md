# Vimar Intercom — Home Assistant integration

[![HACS](https://img.shields.io/badge/HACS-Custom-orange?style=for-the-badge)](https://hacs.xyz/docs/faq/custom_repositories/) [![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.10%2B-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white)](https://www.home-assistant.io/) [![Tests](https://img.shields.io/github/actions/workflow/status/ha-vimar/ha-vimar-intercom/validate.yml?branch=main&label=tests&style=for-the-badge)](https://github.com/ha-vimar/ha-vimar-intercom/actions/workflows/validate.yml) [![Last commit](https://img.shields.io/github/last-commit/ha-vimar/ha-vimar-intercom?style=for-the-badge)](https://github.com/ha-vimar/ha-vimar-intercom/commits/main) [![License](https://img.shields.io/github/license/ha-vimar/ha-vimar-intercom?style=for-the-badge)](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/LICENSE)

[![Open your Home Assistant instance and open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ha-vimar&repository=ha-vimar-intercom&category=integration)

🇮🇹 *[Leggi questa pagina in italiano](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/README.it.md)*

Brings the **Vimar Elvox** video intercom (2 Fili Plus / IP / 2FV2) into Home Assistant: the doorbell
ring, the door and the gate, live video on demand, two-way audio in its own dashboard card, voicemail
and do not disturb, and your plant's actuators as buttons.

It talks to the intercom the way the official Vimar VIEW app does: SIP, either directly to the Tab on
your network or through the Vimar cloud. No RTSP, and no Vimar account needed.

![The intercom card at rest, while the doorbell rings, and in a call](https://raw.githubusercontent.com/ha-vimar/ha-vimar-intercom/main/docs/images/intercom-card.png)

## Compatibility

| Tab | Art. | Plant | Connection | Status |
|---|---|---|---|---|
| Tab 7S 2F+ WiFi | 40507 | 2F | local UDP | Development plant: everything below |
| Tab 5S Up 2 Wire WiFi | 40515 | 2FV2 | cloud TLS | Working (ring, video, audio, door). On some plants the cloud does not deliver the status commands ([#14](https://github.com/ha-vimar/ha-vimar-intercom/issues/14)) |
| Tab 7S Up | 40517 | 2FV2 | cloud TLS | Working, HomeKit included |

Other Vimar 2F / 2FV2 / IP Tabs should work too: the setup comes from the pairing QR code the Tab
shows for the VIEW app. Wi-Fi kits that use the **View Door** app (e.g. K40955) are not supported: as
far as we know they have no such QR code ([#167](https://github.com/ha-vimar/ha-vimar-intercom/issues/167)). Got it
running, or not? Please open a [compatibility report](https://github.com/ha-vimar/ha-vimar-intercom/issues/new?template=compatibility_report.yml).
What changes from one plant to another: [Configuration](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CONFIGURATION.md#what-differs-between-plants).

## Installation

Coming from the original noiseheroes-lab integration? Remove it first: [Migration](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/MIGRATION.md).

1. Click the **Open in HACS** button above (or: HACS → ⋮ menu → *Custom repositories* → add
   `https://github.com/ha-vimar/ha-vimar-intercom`, category *Integration*).
2. Install **Vimar Intercom** and restart Home Assistant.
3. Settings → Devices & services → **Add integration** → **Vimar Intercom**, then paste the text of the
   pairing QR code from the VIEW app (or enter the SIP parameters by hand). On the local network the
   Tab often shows up by itself under *Discovered*.
4. In **Configure**, get your plant's phonebook (from the intercom, from the Vimar cloud, or a
   `rubrica.db` file): it sets the door, the actuators and the addresses the commands go to.

Requires Home Assistant 2025.10 or later and ffmpeg on the host. Manual installation, every option and
the phonebook: [Configuration](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CONFIGURATION.md).

## What you get

- **Doorbell**: an event entity and ring sensors for your automations; optionally a photo and a short
  clip of every visitor, and webhooks for Alexa or Scrypted; a *Test ring* button to try your automations. → [Entities, services and automations](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/ENTITIES.md)
- **Door, gate and actuators**: a lock, an *Open Door* button, and one button per actuator in your
  phonebook (F1/F2, stair lights, relays). → [Configuration](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CONFIGURATION.md)
- **Video and voice**: the camera calls the panel only when you open it, and shows the visitor while it
  rings; the dashboard card talks both ways over HTTPS. → [The intercom card](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CARD.md)
- **Voicemail and do not disturb** switches, and an away message (audio file or text-to-speech) when
  nobody answers. → [Configuration](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CONFIGURATION.md#voicemail)
- **Apple Home**: an optional native HomeKit video doorbell. → [HomeKit](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/HOMEKIT.md)
- **Scrypted, go2rtc, Frigate**: a continuous stream that never rings the panel. → [External systems](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/EXTERNAL.md)
- **Echo Show as an intercom**: live view and two-way talk from Alexa, through Scrypted. → [Echo Show (Scrypted)](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/SCRYPTED-ALEXA.md)

## Documentation

| Page | What it covers |
|---|---|
| [Configuration](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CONFIGURATION.md) | Requirements, setup, every option, phonebook, SGA/PICG, voicemail, what differs between plants, security notes |
| [Entities, services and automations](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/ENTITIES.md) | Every entity, the services, the bus events, example automations |
| [The intercom card](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/CARD.md) | Layouts, buttons, video and voice, ring history |
| [HomeKit](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/HOMEKIT.md) | The native Apple Home doorbell |
| [External systems](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/EXTERNAL.md) | Scrypted, Alexa, Echo Show, go2rtc, Frigate |
| [Echo Show as an intercom](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/SCRYPTED-ALEXA.md) | Step by step: Echo Show watches, calls and talks to the panel through Scrypted |
| [Getting the phonebook](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/PHONEBOOK.md) | Which way works on your plant: intercom on the LAN, cloud token, `rubrica.db` file, by hand |
| [Phonebook internals](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/RUBRICA.md) | Where `rubrica.db` comes from and what it contains (in Italian) |
| [Troubleshooting](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/TROUBLESHOOTING.md) | Symptom → cause → what to do, known limitations, how to read the logs |
| [Migration](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/docs/MIGRATION.md) | Moving over from the original noiseheroes-lab integration |
| [Changelog](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/CHANGELOG.md) | What changed in each release |

## Support and contributing

Questions and bugs: [issues](https://github.com/ha-vimar/ha-vimar-intercom/issues). Security problems: privately, see [SECURITY.md](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/SECURITY.md).
Contributions are welcome, see [CONTRIBUTING.md](https://github.com/ha-vimar/ha-vimar-intercom/blob/main/CONTRIBUTING.md): never guess SIP commands (a wrong
one can open a door), keep credentials out of the repo, and say which hardware you tested on.

## Disclaimer

This project is **not affiliated with or endorsed by Vimar S.p.A.**. "Vimar", "Elvox" and "VIEW" are
trademarks of their respective owners. You supply your own credentials for your own plant.

## License

**MIT** — © [Noise Heroes](https://github.com/noiseheroes-lab) (upstream project) and the fork's
contributors.
