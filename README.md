# Vimar Intercom — Home Assistant integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

🇮🇹 *[Leggi questa pagina in italiano](README.it.md)*

Brings the **Vimar Elvox** video intercom (2 Fili Plus / IP / 2FV2) into Home Assistant: get the
doorbell ring, open the door or gate, view the camera **on demand**, control **voicemail** and
**do not disturb**, and drive your plant's **actuators** (F1/F2, stair lights, relays) as buttons.
Optionally it also publishes the intercom to Apple Home as a video doorbell with two-way audio
(see [HomeKit](#homekit)).

> **How it actually works.** This integration does **not** use RTSP. It implements a **custom SIP
> stack in Python/asyncio** that emulates the official **Vimar VIEW** app ("TOGA"): same `User-Agent`,
> same identity headers (`Mobile-IMEI`, `MyName`) and the proprietary **`Panda`** header. It talks
> either to the **Flexisip instance running on the Tab** (local UDP :5060) or to the **Vimar cloud over
> TLS** (SRV `_sips._tcp`). Video arrives **on demand** from the SIP call (H.264 RTP, decoded to MJPEG
> through ffmpeg), not from an always-on RTSP stream.

---

## Compatibility

Developed on the **Elvox Tab 7S 2F+ WiFi (art. 40507)**. Other Vimar 2F / 2FV2 / IP Tabs should work
too — the configuration comes from the pairing QR code — and the table below is what has actually been
reported so far.

| Model | Art. | Plant | Firmware | Connection | Status |
|---|---|---|---|---|---|
| Elvox Tab 7S 2F+ WiFi | 40507 | 2F | — | local UDP | Development platform: ring, call, answer/hang up, door open, on-demand video, actuators |
| Elvox Tab 5S UP 2 Wire WiFi | 40515 | 2FV2 | 2.1.0203 | cloud TLS | Working, reported by @CPietro — see the notes below |
| Elvox Tab 5S UP 2 Wire WiFi | 40515 | — | — | cloud TLS | Cloud registration working after the 1.0.1 fix, reported by @gtarraran992 ([#1](../../issues/1)) |
| Elvox Tab 7S Up | 40517 | 2FV2 | — | cloud TLS | Working, reported by @m4r1k: ring, two-way audio, video, door, HomeKit doorbell. The SGA is `61000` and the door panel is `55001`, so the door target comes from the phonebook |

**What differs between plants.** Both Tab 5S reports, plus the development plant, point at the same
practical conclusion: *what matters is the address you send to, and how much the Tab tells you back*.

- **Send state commands to the SGA.** On the development plant (40507 / 2F), `VOICEMAIL;ON|OFF` and
  `DND;ON|OFF` work when they are sent to the SGA — `55001` there, taken from the phonebook's
  `SYSTEM.MAGIC_APT_INTERCOM`. Sent anywhere else (the Tab's own address, the apartment group, the old
  `60001` default) they return a bare 200 OK and do nothing, or a 404. If your switches appear dead,
  the SGA/PICG options are the first thing to check — see the options table below.
- **`GET_INIT_STATUS` replies, but not with the same amount of detail.** On the 40507 the reply is
  short: `rubrica_ver`, `vm_ver`, `vm_level`, `dnd`, `voicemail`. On the 40515 / 2FV2 plant it is the
  full payload — `dnd`, `voicemail`, `rubrica_ver`, `vm_ver`, `vm_level`, `vm_timeout`,
  `vm_timeout_values`, `apt_names`, `GID`, `media_enc` and a `token`. Which is why the integration
  parses what it finds and ignores what it doesn't, instead of assuming a fixed set.
- **Media encryption is a per-plant value**, not a global default: the 40515 reports
  `media_enc: "srtp"`, while the development plant refused SRTP outright and runs plain RTP.
- Still open on the 40515: **voicemail switches on but not off**, under investigation by the reporter.

Got it running on a different model, or on the same one with different results? Please open a
[hardware compatibility report](../../issues/new?template=compatibility_report.yml) — reports where
everything just worked are as useful as the ones where something broke.

---

## Requirements

- Home Assistant **2024.1** or later, Python 3.11+.
- ffmpeg on the Home Assistant host (declared in the manifest) for the camera.
- The plant's **pairing QR code** (from the VIEW app) **or** the SIP parameters entered by hand
  (id, password, domain, cloud proxy).
- Python requirements, installed by Home Assistant: `pycryptodome` and `requests`, plus `HAP-python`
  and `PyQRCode` for the optional HomeKit doorbell. There is no external SIP library; the stack is custom.

---

## Installation

### Through HACS
1. HACS → Integrations → ⋮ menu → *Custom repositories* → add this repo with category *Integration*.
2. Install **Vimar Intercom**.
3. Restart Home Assistant.

### Manual
Copy `custom_components/vimar_intercom/` into your Home Assistant `config/custom_components/` folder
and restart.

---

## Configuration

Settings → Devices & services → Add integration → **Vimar Intercom**.

- **QR** (recommended): paste the text of the Vimar pairing QR code. The integration decodes it
  (`qr_decoder.py`: Base64(AESkey|AES-CBC|IV) → `KEY=VALUE` pairs) and fills in id, password, domain,
  cloud/local proxy, GID, MAC and plant type.
- **Manual**: enter `sip_user`, `sip_password`, `sip_domain` and `cloud_proxy` yourself.

### Options (after adding the integration)

Settings → Vimar Intercom → **Configure**:

| Option | Description |
|---|---|
| **Intercom IP** (`local_proxy`) | IP address of the Flexisip instance on the Tab |
| **Use local SIP UDP** (`use_local_udp`) | ON = local UDP; OFF = cloud TLS |
| **Local UDP port** (`local_udp_port`) | default 5060 |
| **Actuators (JSON)** (`actuators`) | JSON list of `{name, msg, target, icon}`; creates dynamic buttons. Empty = no buttons |
| **SGA** (`sga_target`) | Recipient of `VOICEMAIL;`/`DND;`. Empty = default `55001` |
| **PICG** (`picg_target`) | Recipient of `GET_INIT_STATUS`. On every plant verified so far it matches the SGA. Empty = default `55001` |
| **Video panel** (`camera_target`) | Panel called to open the video with no call in progress. Empty = `55100`; if that panel does not exist, the integration tries the panel that last rang and remembers it |
| **Door panel** (`door_target`) | Panel that receives the door command: the `GID_PE` of the door actuator in the phonebook. Empty = the door actuator's panel if actuators were imported, otherwise the SGA |

Most of these come from your plant's **phonebook**. In the options menu, **"Download the phonebook
from the intercom"** asks the Tab for it over the LAN with the SIP credentials you already have;
**"Import actuators from rubrica.db"** takes a file you extracted yourself (see `docs/RUBRICA.md`).
Confirming either one sets the actuators, the SGA, the PICG and the door panel. You can still edit the
values by hand in the "Settings" step.

The **HomeKit** page of the options turns the Apple Home doorbell on or off and picks the video mode.

---

## Entities

| Entity | Platform | Description |
|---|---|---|
| Intercom | `camera` | **On-demand** video: opening the stream makes the hub place the SIP call, and the H.264 RTP video is decoded to MJPEG through ffmpeg (no RTSP) |
| Doorbell | `event` | `event` entity (device class DOORBELL), event type `ring`, fired on ring (incoming INVITE) |
| Lock | `lock` | Opens the door (`OPEN_2F` to the door panel); auto-relocks after 5 s (there is no physical feedback) |
| Call | `button` | SIP call to the default outdoor unit |
| Call Video (outdoor) / Call Home (indoor) | `button` | Call to the SGA / to 55002 |
| Answer / Hang up | `button` | Answer (200 OK) / end the call (BYE) |
| Open Door | `button` | `OPEN_2F` to the outdoor unit |
| *Dynamic actuators* | `button` | One per entry in `options["actuators"]` (F1/F2, stair lights, relays…); sends `MSG` with `Panda: command` |
| Voicemail | `switch` | `VOICEMAIL;ON/OFF` (Panda: blue) to the SGA; state read from the Tab's announcements |
| Do Not Disturb | `switch` | `DND;ON/OFF` (Panda: blue) to the SGA; real state |
| Intercom SIP | `binary_sensor` | SIP registration active (connectivity) |
| Intercom In Call | `binary_sensor` | A call is up |
| Intercom Ringing | `binary_sensor` | ON while an outdoor unit is calling (attribute: caller) |
| Intercom Outgoing Call | `binary_sensor` | ON while Home Assistant is calling |
| Intercom State | `sensor` (enum) | offline / idle / ringing / in_call / calling (plus network attributes) |
| Intercom Last Caller | `sensor` | Outdoor unit or monitor of the last ring |
| Intercom Last Ring | `sensor` (timestamp) | Time of the last ring |
| Intercom Rings | `sensor` (counter) | Rings since startup |
| Intercom Calls | `sensor` (counter) | Connected calls |
| Intercom Last Call Duration | `sensor` (s) | Duration of the last call |
| Intercom Last Door Open | `sensor` (timestamp) | Last door opening (attributes: unit, outcome, counter) |
| Intercom Last Command | `sensor` | Outcome of the last `send_command` |
| Intercom Last Received Message | `sensor` | Last SIP MESSAGE from the intercom |

---

## Services (`services.yaml`)

| Service | Description | Fields |
|---|---|---|
| `vimar_intercom.send_command` | Arbitrary SIP MESSAGE (for testing). Administrators only | `body`, `target`, `header_name`, `header_value` |
| `vimar_intercom.call` | SIP call to an outdoor unit or monitor | `target` |
| `vimar_intercom.answer` | Answers the incoming call | — |
| `vimar_intercom.hangup` | Ends the active call | — |
| `vimar_intercom.open_door` | Door open command (`OPEN_2F`) | `target`, `command` |
| `vimar_intercom.fetch_local` | HTTP Digest GET against the Tab's local interface (home mode). Administrators only; private addresses only; files land in `config/vimar_intercom/` | `path`, `save_as`, `host`, `scheme` |

Example (Developer tools → Actions):

```yaml
action: vimar_intercom.send_command
data:
  body: OPEN_2F
  target: "55001"
  header_name: Panda
  header_value: command
```

---

## Events

The doorbell is exposed as an **`event` entity** (`event.<...>_doorbell`, event type `ring`), not as a
bus event. In automations, use a state trigger on the `event` entity (or on the ringing binary sensor).

On top of that, from the `MESSAGE`s the Tab sends, the integration fires these events on the Home
Assistant bus: `vimar_intercom_missed_call`, `vimar_intercom_videomessage`,
`vimar_intercom_fuoriporta`, `vimar_intercom_call_info`, `vimar_intercom_phonebook_changed`.
They are read-only — nothing is sent back. Example trigger:

```yaml
automation:
  - alias: "Intercom - Missed call"
    trigger:
      - platform: event
        event_type: vimar_intercom_missed_call
    action:
      - service: notify.mobile_app_phone
        data: { message: "Missed call at the intercom" }
```

---

## Example automations

`packages/vimar_intercom.yaml` (copy it into `config/packages/`) contains a
"ring → notification" automation driven by the state change of the `event` entity, plus a safety
hang-up that closes a call left open for two minutes:

```yaml
automation:
  - alias: "Intercom - Ring → notification"
    trigger:
      - platform: state
        entity_id: event.vimar_intercom_doorbell
    action:
      - action: notify.mobile_app_YOUR_PHONE
        data:
          title: "🔔 Someone at the door"
          message: "Ring at {{ now().strftime('%H:%M:%S') }}."
          data:
            image: "/api/camera_proxy/camera.vimar_intercom_intercom"
```

The camera image is the last frame saved during a call, or a placeholder if there has been no call
since Home Assistant started. At ring time that frame usually comes from the previous call, so
`camera.snapshot` in a ring automation does not show who is at the door. `camera.record` needs the
`stream` integration, which this camera does not provide. Don't point a `camera: platform: ffmpeg` at
`/api/vimar_intercom/av`: the endpoint needs Home Assistant authentication, and without an active call
the ffmpeg probe hangs until it times out.

`docs/lovelace_example.yaml` has a basic Lovelace card with the answer / open door / hang up buttons.

---

## HomeKit

The integration can publish the intercom to Apple Home as its own video doorbell: ring notification,
live video, two-way audio, and the gate as a lock in the same accessory. It is off by default.

1. Settings → Devices & services → Vimar Intercom → Configure → **HomeKit** → turn on
   **Publish to HomeKit**.
2. A notification with a QR code and an 8-digit code appears. In the Home app choose **Add
   Accessory** and scan it. iOS warns that the accessory is not certified; choose **Add Anyway**.
3. In the accessory settings, **Show as Separate Tiles** makes the gate reachable from the live view.

**Smoother video (re-encode)** is on by default. The cloud relay drops a few packets in every hundred,
and the panel ignores keyframe requests, so a direct stream can freeze for up to 3 seconds after a
loss. With re-encoding on, Home Assistant sends a keyframe every second: opening takes about half a
second longer and costs some CPU. Turn it off for the fastest opening on a clean local network.

Home Assistant's own HomeKit bridge cannot do the talk direction: its doorbell advertises a speaker
but never listens on the port it gives the phone. If you exposed the intercom camera through the bridge
before, remove it from there. `docs/HOMEKIT.md` has the details.

## Known limitations

- **Cloud-only plants**: the Tab's local HTTP interface (:80) may accept the TCP connection and then
  stay silent, so there is no local phonebook to read over the LAN. Camera, actuators, door opening
  and the state commands still work over SIP.
- **Voicemail / DND**: these are commanded through the **SGA** (`SYSTEM.MAGIC_APT_INTERCOM` in the
  phonebook, `55001` on the plant used for development). Sent to any other address they are silently
  ignored, so getting the SGA right is what makes them work — set it in the options or let the
  `rubrica.db` import fill it in.
- **Cloud phonebook**: needs a `token`. Plants that answer `GET_INIT_STATUS` with the long form hand it
  over directly, and the phonebook can then be downloaded with a single authenticated request — see
  `docs/RUBRICA.md` §0, verified on a 40515. Plants that answer with the short form (including the
  development one) don't carry a token, and there the manual extraction is still the way. Fetching it
  automatically isn't implemented yet ([#5](../../issues/5)).
- **By-me actuators** (e.g. stair lights on By-me home automation): these may not respond over SIP even
  when they are listed in the phonebook.
- **Lock**: no physical state feedback (optimistic auto-relock after 5 s).
- **Phonebook**: on cloud-only plants it has to be extracted once (see `docs/RUBRICA.md`); the
  automatic import over the cloud depends on a token provisioned by the account.

---

## Logging

The component keeps its own circular buffer (the last 3000 lines, `DEBUG` included), readable by
administrators at `/api/vimar_intercom/debug`. The Home Assistant log gets `WARNING` and above by
default. To see more there, set the level in `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.vimar_intercom: debug
```

Both destinations mask passwords, digest responses, phonebook tokens and SRTP keys before writing.

---

## Security

- SIP credentials (password and `ha1`) are stored in the Home Assistant config entry under
  `.storage`, in plain text like every other integration's secrets. They are never logged.
- `/av` requires Home Assistant authentication and a request from the local network. Home Assistant's
  own ffmpeg reaches it through a signed URL. The `/audio_ws` WebSocket requires authentication; its
  maintenance actions (raw commands, scans, re-registration) are for administrators only.
- `/api/vimar_intercom/debug`, `send_command` and `fetch_local` are for administrators only.
- The HomeKit pairing code is stored with mode 0600, and the QR link stops working once the accessory
  is paired.
- In local UDP mode the integration only accepts SIP from the configured intercom, and plain RTP only
  from the other end of the current call.
- No mandatory cloud dependency when running in local UDP mode.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: never guess SIP commands or tokens (a wrong
actuator token can physically open a door), keep credentials out of the repo and out of your logs, run
`pytest` before opening a PR, and say which hardware you tested on.

---

## Disclaimer

This project is **not affiliated with or endorsed by Vimar S.p.A.**. "Vimar", "Elvox" and "VIEW" are
trademarks of their respective owners. You supply your own credentials for your own plant.

## License

**MIT** — © [Noise Heroes](https://github.com/noiseheroes-lab) (upstream project) and the fork's
contributors.
