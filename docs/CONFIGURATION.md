# Configuration

🇮🇹 *[Italiano](CONFIGURATION.it.md)* · [← README](../README.md)

## Requirements

- Home Assistant **2025.10** or later, Python 3.13+ (what HA 2025.10 ships).
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
- **Manual**: enter `sip_user`, `sip_password`, `sip_domain` and `cloud_proxy` yourself. These are
  the values inside the decoded QR (`ID`, `PWD`, `DOMAIN`, `CPROXY`), so with the QR you never type
  them. The domain is the credential's own, not the cloud proxy: when the QR says
  `DOMAIN=127.0.0.1` (as on a 40515) use its `CDOMAIN`.

**Found on the network** (since 1.0.12, [#6](https://github.com/ha-vimar/ha-vimar-intercom/issues/6)): the Tab announces itself over mDNS
(`_eipvdes._tcp`, the same service the VIEW app looks for), and Home Assistant shows it under
*Discovered*. The QR or the credentials are still needed (the announcement carries no secret), but
the intercom's address and the local SIP domain come from the Tab itself. This matters on plants
whose QR says `domain=127.0.0.1`, such as a 40515: the domain the Tab announces (its own address)
is used for local registration instead of the cloud domain. An intercom that is already set up
is recognised by its MAC, and if the DHCP gives it a new address Home Assistant offers the new
one under Discovered, and switches to it once you confirm (local mode only). The confirmation is
there because anyone on the local network can announce the Tab's MAC. If you press Ignore by
mistake, later moves are ignored too: Settings → Devices & services → Ignored → Vimar Intercom →
Stop ignoring. Where mDNS is filtered, nothing changes: add it by hand as before.

### Options (after adding the integration)

Settings → Vimar Intercom → **Configure**:

| Option | Description |
|---|---|
| **Intercom IP** (`local_proxy`) | IP address of the Flexisip instance on the Tab |
| **Use local SIP UDP** (`use_local_udp`) | ON = local UDP; OFF = cloud TLS |
| **Local UDP port** (`local_udp_port`) | default 5060 |
| **Actuators (JSON)** (`actuators`) | JSON list of `{name, msg, target, icon}`; creates dynamic buttons. Empty = no buttons |
| **SGA** (`sga_target`) | Recipient of `VOICEMAIL;`/`DND;`, and of the door command when `door_target` is empty. Empty = default `55001` |
| **PICG** (`picg_target`) | Recipient of `GET_INIT_STATUS`. On the development plant it matches the SGA; on others it does not (60001 on a 40515). Empty = default `55001` |
| **Video entrance panel** (`camera_target`) | Panel called by the camera, *Call* and *Call Video (outdoor)*: the `PHONEBOOK` row with `TYPE='PE'`. **Not the SGA.** Empty = the panel learned from the last ring (a panel that rang with video, used when `55100` does not exist on the plant), otherwise the default `55100` |
| **Internal panel** (`internal_panel_target`) | Target of *Call Home (indoor)*. The phonebook does not say which one it is: set it by hand. Empty = default `55002` |
| **Entrance panel that opens the door** (`door_target`) | Recipient of the door command (lock, *Open Door*, `open_door` without `target`, actuators with target `AUTO`): the `GID_PE` of the door actuator in the phonebook. **Not always the SGA**: on a 2FV2 the SGA is `61000` and the door is opened by panel `55001`. Empty = the saved door actuator's panel, otherwise the SGA |
| **Ring snapshot folder** (`snapshot_dir`) | Where the visitor's photo (`squillo_YYYYMMDD_HHMMSS_mmm.jpg` + `ultimo_squillo.jpg`) and the ring clip (`squillo_YYYYMMDD_HHMMSS_mmm.mp4`: the preview video, and the call if answered from HA, up to 60 s, no audio) are saved on every ring, e.g. `/config/media/citofono`. Must be writable by HA. Empty = off |
| **Seconds for the better photo** (`snapshot_delay`) | The first photo is saved as soon as the first frame arrives (~1 s after the ring); after this many seconds it is replaced by a frame with the exposure settled (the panel's first keyframe is dark). Default 3 (Tab 5S Up 40515), 0 = keep the first |
| **View silence** (`view_keepalive`) | Seconds of audio silence sent during "Vedi esterno" (0 = none). Over the cloud the panel closes the view after ~10 s without it; on the 2-wire plant in local mode it keeps the apartment busy (up to 300 s). Default: 120 over the cloud, 0 in local mode; on a 2-wire plant use 0 or 30 |
| **Video quality from the panel** (`video_bandwidth`) | The video bandwidth asked of the panel, for every video (card, camera, `/av`, HomeKit). **Automatic** (default): 2 Mbit/s on local UDP, 256 kbit/s over the cloud relay. **256 kbit/s**: fewer damaged frames on a relay that loses packets, but a smaller picture on some panels (a Tab 5S Up 40515 drops to 320x240 at 15 fps). **2 Mbit/s**: the sharpest picture (720x576 at 24 fps on a 40515), ten times the traffic; over a lossy relay (a Tab 7S Up 40517) the video smears, freezes and lags |
| **Allowed users** (`allowed_users`) | Limits the card, the ring history (`GET /api/vimar_intercom/rings`, photos and clips) and `/audio_ws` to these HA users. Admins are always allowed. Empty = every logged-in user (default). It is **not** per user for the camera entity nor for `/av` used with its key (HA's own camera stream, go2rtc: local network only, see below): anyone who can open the camera sees and hears the stream. If the photo folder is under an HA media directory (e.g. `/config/media/citofono`), photos and clips also show up in the media browser for every user |
| **Away message** (`away_message_file`, `away_message_delay`) | Audio file (mp3, wav…) played to the visitor if nobody answers within N seconds; then the integration hangs up. If the Tab exposes the voicemail delay, that one is used instead (*Voicemail · delay*) |
| **Away message from text** (`away_message_text`, `away_message_tts`) | If the file field is empty, this text is read by Home Assistant's text-to-speech (`away_message_tts` = a `tts.*` entity; empty = HA's default engine) in HA's language, max 30 s. The audio is generated at startup and cached; if TTS fails the doorbell keeps ringing as usual |
| **Media encryption (SRTP)** (`media_enc`) | **Automatic** (default since 1.0.11): follows the `media_enc` the plant declares in its `GET_INIT_STATUS` reply (`"srtp"` on a cloud 40515); plants with the short reply (the 40507) stay on plain RTP. **On** / **Off** force it. Entries saved as "on" by 1.0.10 or earlier stay on; "off" becomes automatic. Try **On** if the camera stays black or the call fails with `488` |
| **Enable video in outgoing calls** (`video_enabled`) | Initially follows the pairing QR (ON if the field is absent). Enable it if the entrance has a camera but *View outside* connects with audio only (`Stream has no video`). Disable for audio-only entrances. Incoming calls still follow the media offered by the system. Saving reloads the integration. |
| **Voice answer** (`voice_answer`) | Who can answer a ringing call by talking on `/audio_ws`: **Declared** (default, only with `?voice_answer=1`), **Off** (never), **Any** (any connection with a mic; a wall tablet with its mic left open can answer by itself on household noise) |
| **Ring webhooks** (`ring_webhook_url`, `ring_end_webhook_url`) | Optional GET (fire-and-forget, 5 s timeout) fired when a ring starts and when it ends (answered, cancelled or missed) — e.g. the `turnOn`/`turnOff` URLs of a Scrypted Dummy Switch (see [docs/EXTERNAL.md](EXTERNAL.md)). A failure only logs a warning, never blocks the ring. Empty = off |

**/av stream key** (options menu → *`/av` stream key*): plain `/api/vimar_intercom/av` places a call, so it only answers an authenticated Home Assistant user or a request carrying this installation's key as `?auth=<key>`. The key is created at first start, never expires and is added by the integration's camera by itself; the page shows it for go2rtc, Frigate or Scrypted (see [EXTERNAL.md](EXTERNAL.md#the-av-key)). **Regenerate** it if a log or a URL with the key ever reached someone else: the old key stops working at once and the integration reloads.

The camera image between calls (the last ring photo) is visible to every Home Assistant user who can see the camera entity; `allowed_users` limits the ring history and live media, not the camera entity.

### Voicemail

There is one *Voicemail* switch (Configuration, device page). Turned on, it uses Home
Assistant's away message if a text or an audio file is set (and turns the Tab's own voicemail off);
otherwise it turns the Tab's voicemail on. Turned off, both are off. If the Tab switches its voicemail
on by itself, the Tab's wins. There is a single delay, *Voicemail · delay* (from the Tab; if the Tab does
not expose it, the `away_message_delay` option, 0 = 20 s): the away message starts after that many seconds.
The message is set from the same page: *Voicemail · message text* and *Voicemail · audio file* (a pick-list of the files in
`<first HA media folder>/citofono/messaggi`, created on demand; upload from Media > Local media; refreshed every minute).
The file can also be uploaded from the integration's settings (*Or upload the audio file*: mp3, wav or m4a, max 5 MB): it lands in that folder and becomes the away message at once.
These are the integration's options, applied at once without a reload. Only administrators can change the message text and file (since 1.0.17, #64); the card's settings dialog hides those rows from other users.

Example, Tab 5S Up 40515 (Due Fili Plus, cloud): SGA `61000`, PICG `60001`, video and door panel
`55001`. These values come from the VIEW app's phonebook, not from the defaults.

The actuator list and the SGA/PICG values come from your plant's **phonebook** (`rubrica.db`): in the
options menu pick **"Download the phonebook from the intercom"** (LAN), **"Download the phonebook from
the Vimar cloud"** (plants that send the long `GET_INIT_STATUS` reply) or **"Import actuators from
rubrica.db"**, upload the file (you can get it through the VIEW app or with root access, see
[RUBRICA.md](RUBRICA.md)) and confirm (which one fits your plant: the decision tree in
[PHONEBOOK.md](PHONEBOOK.md)) — actuators, SGA, PICG, the video
entrance panel and the panel that opens the door are then set automatically. You can also enter the values by hand in the "Settings" step, which is handy if you
already know your plant's SGA or want to tweak the imported actuator list.


---

## What differs between plants

Both Tab 5S reports, plus the development plant, point at the same
practical conclusion: *what matters is the address you send to, and how much the Tab tells you back*.

- **Send state commands to the SGA.** On the development plant (40507 / 2F), `VOICEMAIL;ON|OFF` and
  `DND;ON|OFF` work when they are sent to the SGA — `55001` there, taken from the phonebook's
  `SYSTEM.MAGIC_APT_INTERCOM`. Sent anywhere else (the Tab's own address, the apartment group, the old
  `60001` default) they return a bare 200 OK and do nothing, or a 404. If your switches appear dead,
  the SGA/PICG options are the first thing to check — see [the options](#options-after-adding-the-integration).
- **`GET_INIT_STATUS` replies, but not with the same amount of detail.** On the 40507 the reply is
  short: `rubrica_ver`, `vm_ver`, `vm_level`, `dnd`, `voicemail`. On the 40515 / 2FV2 plant it is the
  full payload — `dnd`, `voicemail`, `rubrica_ver`, `vm_ver`, `vm_level`, `vm_timeout`,
  `vm_timeout_values`, `apt_names`, `GID`, `media_enc` and a `token`. Which is why the integration
  parses what it finds and ignores what it doesn't, instead of assuming a fixed set.
- **Media encryption is a per-plant value**, not a global default: the 40515 reports
  `media_enc: "srtp"`, while the development plant refused SRTP outright and runs plain RTP.
- Still open on the 40515: **voicemail switches on but not off**, under investigation by the reporter.

Got it running on a different model, or on the same one with different results? Please open a
[hardware compatibility report](https://github.com/ha-vimar/ha-vimar-intercom/issues/new?template=compatibility_report.yml) — reports where
everything just worked are as useful as the ones where something broke.


---

## Security

- SIP credentials (password and `ha1`) are stored in the Home Assistant config entry under
  `.storage`, in plain text like every other integration's secrets. They are never logged.
- Internal HTTP endpoint: `/av` is **LAN-only** (`_is_local_request`), and plain `/av` (which
  places a call) also wants an authenticated HA user or the installation's key (`?auth=<key>`,
  compared in constant time; a wrong or missing key gets a 403). The key is masked in the
  integration's logs and in HA's stream logs; the `/audio_ws`
  WebSocket requires Home Assistant authentication, and its debug actions (`command`, `probe`,
  `scan`, `register`, `reconnect`) are admin-only. The QR payload is never logged at INFO level.
- Talking on `/audio_ws?voice_answer=1` while the doorbell rings answers the call (mic RMS above a threshold
  for 200 ms): how Echo Show and HomeKit answer through Scrypted. Who may answer is the **Voice answer**
  option (`voice_answer`): `declared` (default, only with the flag), `off` (never), `any` (any connection
  with a mic: a wall tablet with the mic left open can answer by itself on household noise). In every mode
  a connection that was in a call never answers until it goes back to idle. While idle, mic frames are dropped.
  External clients using a signed URL (`auth/sign_path`): put `voice_answer=1` in the path *before* signing;
  appending it afterwards gets a 401, because HA validates the signed query. Or pick the `any` option.
- Ring history for the card: `GET /api/vimar_intercom/rings` (list, `?limit=` up to 50) and
  `GET /api/vimar_intercom/rings/<name>` (the photo or the clip, with HTTP ranges) require Home
  Assistant authentication (the card loads them through signed paths). The second serves only
  `squillo_YYYYMMDD_HHMMSS[_mmm].jpg` / `.mp4` files inside `snapshot_dir`, nothing else (not
  even a clip still being written); the folder is never exposed under `/local`.
- Away message upload from the card: `POST /api/vimar_intercom/away_upload?name=<file name>` with the
  file as the body. Administrators only (others get 401); same rules as the settings upload (file name
  only, mp3/wav/m4a, max 5 MB, checked while the body is read, never overwrites another file).
- In local UDP mode, SIP packets from any host other than the intercom are dropped, so another
  device on the LAN can't fake a ring.
- Plain RTP (no SRTP) is accepted only from the other end of the current call.
- The HomeKit pairing code (optional HomeKit doorbell) is stored in a file with mode 0600.
- No mandatory cloud dependency when running in local UDP mode.
- Found a vulnerability? Report it privately, not in a public issue: see [SECURITY.md](../SECURITY.md).

