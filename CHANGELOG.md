# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versioning: [semver](https://semver.org/).
Newest entries on top. **Entries are written in English from 1.0.1 onwards**; earlier ones are in
Italian and are kept as they were written.

## [Unreleased]

## [1.0.25] - 2026-10-22

### Enhancements

- On local UDP the intercom's state is read over its local HTTP API, so the Vimar app no longer shows "Apartment configuration changed" on the phones at every Home Assistant start or DND/voicemail change ([#209](https://github.com/ha-vimar/ha-vimar-intercom/pull/209)).
- New option to open the calling entrance during a call (OPEN_CURRENT), for 2FV2 buildings behind a gateway where the door opens only during a call ([#186](https://github.com/ha-vimar/ha-vimar-intercom/pull/186)) ([#210](https://github.com/ha-vimar/ha-vimar-intercom/pull/210)).

### Bug fixes

- "View outside" and outgoing calls no longer lose the panel's first video frame, which could leave the picture black for about 3 seconds ([#207](https://github.com/ha-vimar/ha-vimar-intercom/pull/207)).
- The device page shows the paired Tab's model from the pairing QR, instead of the name of another model when the intercom does not tell it over SIP ([#211](https://github.com/ha-vimar/ha-vimar-intercom/pull/211)) ([#213](https://github.com/ha-vimar/ha-vimar-intercom/pull/213)).

### Other changes

- Start an isolated, automatically onboarded Home Assistant development instance from the current checkout with Docker Compose ([#183](https://github.com/ha-vimar/ha-vimar-intercom/pull/183)).

## [1.0.24] - 2026-10-20

> **⚠ Before you update**
>
> - the displayed entity names change (for example "Intercom Stato" becomes "Vimar Intercom Stato"); automations and dashboards that use entity_ids keep working, names you renamed yourself stay as they are, and only templates that read `friendly_name` may need a look ([#191](https://github.com/ha-vimar/ha-vimar-intercom/pull/191))
> - the `nota` attribute now holds a short key (for example `since_integration_start`) instead of an Italian sentence; it is still shown as a sentence in Home Assistant, but a template that read its text must be updated ([#194](https://github.com/ha-vimar/ha-vimar-intercom/pull/194))
> - on any install not in Italian the panel names change (for example "Targa Esterna" becomes "Entrance panel"); an automation or template that compared them to the Italian name must use the new one or the SIP id (`chiamante_id`, `target_id`) ([#195](https://github.com/ha-vimar/ha-vimar-intercom/pull/195))
> - if "Call Home (indoor)" does not ring your indoor monitor, you may have 55002 stored from an earlier version (the settings form used to fill it in): run Configure → Find indoor monitor and confirm the proposed PICG, or clear the field. If it works for you with no target set, check it after updating: an empty field now calls the PICG; to keep 55002, set it there ([#198](https://github.com/ha-vimar/ha-vimar-intercom/pull/198)).
> - if your entrance panel really has no camera, turn off "Enable video in outgoing calls" (Configure → Network and actuators) after updating: a pairing QR with VIDEO=0 no longer turns it off. If your QR had VIDEO=0 and you ever saved that settings page, the switch was saved off: turn it on to get video ([#204](https://github.com/ha-vimar/ha-vimar-intercom/pull/204)).

### Enhancements

- The dashboard card, its visual editor and its card picker entry follow your Home Assistant language: Italian for Italian, English for every other language ([#178](https://github.com/ha-vimar/ha-vimar-intercom/pull/178)) ([#187](https://github.com/ha-vimar/ha-vimar-intercom/pull/187))
- Entity names follow your Home Assistant language (English or Italian) and start with the device name, e.g. "Vimar Intercom Status"; entity_ids do not change ([#178](https://github.com/ha-vimar/ha-vimar-intercom/pull/178)) ([#191](https://github.com/ha-vimar/ha-vimar-intercom/pull/191))
- Error messages from the buttons, the voicemail settings, the setup and the options follow your Home Assistant language ([#178](https://github.com/ha-vimar/ha-vimar-intercom/pull/178)) ([#193](https://github.com/ha-vimar/ha-vimar-intercom/pull/193))
- Entity attributes are shown with names in your Home Assistant language, and the example package also comes in English ([#178](https://github.com/ha-vimar/ha-vimar-intercom/pull/178)) ([#194](https://github.com/ha-vimar/ha-vimar-intercom/pull/194))
- The names of the plant's panels and monitor (Last caller, caller and panel attributes) follow your Home Assistant language ([#178](https://github.com/ha-vimar/ha-vimar-intercom/pull/178)) ([#195](https://github.com/ha-vimar/ha-vimar-intercom/pull/195))
- Reconfigure (⋮ menu of the integration) takes a new pairing QR without deleting the integration: entities, automations and options stay ([#196](https://github.com/ha-vimar/ha-vimar-intercom/pull/196)).
- When the intercom refuses Home Assistant's registration (403 or 404), a repair now says so and explains how to pair again, instead of the entities just going unavailable ([#197](https://github.com/ha-vimar/ha-vimar-intercom/pull/197)).

### Bug fixes

- A phone running the Video Door app on the same plant is no longer detected as an "Elvox Pixel" entrance panel ([#190](https://github.com/ha-vimar/ha-vimar-intercom/pull/190)).
- Only the intercom's system messages (Panda: blue) are read: a text message can no longer change Do Not Disturb or voicemail, and a message with several commands, one per line, is read in full. The Last received message sensor no longer shows text messages ([#192](https://github.com/ha-vimar/ha-vimar-intercom/pull/192)).
- "Call Home (indoor)" and "Find indoor monitor" use the apartment's PICG, which rings the indoor monitor, instead of a fixed 55002 that some intercoms reject ([#198](https://github.com/ha-vimar/ha-vimar-intercom/pull/198)).
- On a page opened over HTTP the card's Audio button now says it needs HTTPS instead of doing nothing ([#200](https://github.com/ha-vimar/ha-vimar-intercom/pull/200)).
- Reconfigure refuses a QR that belongs to another configured intercom instead of moving the entry onto it ([#201](https://github.com/ha-vimar/ha-vimar-intercom/pull/201)).
- Over the cloud, the intercom's status request waits for the relay's slower answer and is no longer sent again at every registration renewal ([#202](https://github.com/ha-vimar/ha-vimar-intercom/pull/202)).
- A pairing QR with VIDEO=0 no longer takes video out of the calls Home Assistant makes: in the Vimar apps that field only rules the preview while it rings ([#204](https://github.com/ha-vimar/ha-vimar-intercom/pull/204)).

### Documentation

- Record additional Tab 7S Up 40517 local-call refusals, development QR policy and same-VLAN VIEW cloud routing observations ([#184](https://github.com/ha-vimar/ha-vimar-intercom/pull/184)).
- The Open door service no longer labels 55001 and 55002 as outdoor and indoor panel: they are only examples, the right address comes from the phonebook ([#188](https://github.com/ha-vimar/ha-vimar-intercom/pull/188)).

### Security

- Debug logs also hide other phones' 5-digit SIP ids (6xxxx), in SIP addresses and in plant messages; panels' extensions and the protocol's own addresses (60000-60002, 61000) stay readable ([#203](https://github.com/ha-vimar/ha-vimar-intercom/pull/203)).

### Other changes

- Reconfigure and the intercom's new address reload the integration through its update listener, as Home Assistant 2026.12 requires ([#199](https://github.com/ha-vimar/ha-vimar-intercom/pull/199)).
- Tests no longer contain values of the development plant, and a guard keeps them out ([#201](https://github.com/ha-vimar/ha-vimar-intercom/pull/201)).

## [1.0.23] - 2026-10-14

### Enhancements

- Propose a declared PICG as the Call Home destination when it differs from the SGA, preserving the current destination otherwise and distinguishing unavailable lookups from missing replies ([#173](https://github.com/ha-vimar/ha-vimar-intercom/pull/173)).
- Enable or disable video in outgoing intercom calls from Settings, including when the pairing QR initially disables it ([#175](https://github.com/ha-vimar/ha-vimar-intercom/pull/175)).

### Bug fixes

- Cloud phonebook downloads distinguish an unanswered status request from a status reply without a token, with guidance to check the PICG address ([#171](https://github.com/ha-vimar/ha-vimar-intercom/pull/171)).
- Phonebook import confirmation displays the correct source and no longer suggests that changing the SGA overwrites an independently configured PICG ([#174](https://github.com/ha-vimar/ha-vimar-intercom/pull/174)).

## [1.0.22] - 2026-10-12

> **⚠ Before you update**
>
> - If your video was fine at 2 Mbit/s over the cloud (e.g. a Tab 5S Up 40515), set "Video quality from the panel" to 2 Mbit/s in the settings ([#161](https://github.com/ha-vimar/ha-vimar-intercom/pull/161)).

### Bug fixes

- Over the cloud the panel's video is back to 256 kbit/s by default: 2 Mbit/s smeared, froze and lagged on a Tab 7S Up 40517. A new setting, "Video quality from the panel", picks Automatic (2 Mbit/s on local UDP, 256 kbit/s over the cloud), 256 kbit/s or 2 Mbit/s ([#161](https://github.com/ha-vimar/ha-vimar-intercom/pull/161)).
- A ring answered on the indoor monitor or in the Vimar app now shows as "Risposto altrove" in the card's ring history instead of "Nessuna risposta" (on plants that report it, like the 40517) ([#165](https://github.com/ha-vimar/ha-vimar-intercom/pull/165)).
- "Risposto altrove" in the ring history no longer depends on the order of the values in the panel's Reason header, and a ring answered from HA (or by the away message) is never relabelled as answered elsewhere afterwards ([#166](https://github.com/ha-vimar/ha-vimar-intercom/pull/166)).
- A ring where you opened the door from Home Assistant now shows as *Aperto* in the card history, instead of *Rifiutato* when you close the popup afterwards ([#170](https://github.com/ha-vimar/ha-vimar-intercom/pull/170)).

### Documentation

- The README says that Wi-Fi kits using the View Door app (e.g. K40955) are not supported ([#169](https://github.com/ha-vimar/ha-vimar-intercom/pull/169)).

## [1.0.21] - 2026-10-07

### Documentation

- The entity list in the docs now covers every entity and action, with the key at the end of each unique id ([#156](https://github.com/ha-vimar/ha-vimar-intercom/pull/156)).
- New troubleshooting table by symptom and a phonebook guide (which way works on your plant: intercom, cloud, file, by hand); clearer setup texts on the pairing QR and the SIP credentials ([#144](https://github.com/ha-vimar/ha-vimar-intercom/issues/144)) ([#158](https://github.com/ha-vimar/ha-vimar-intercom/pull/158)).
- The troubleshooting guide lists the plant data the logs now mask (addresses, SIP ids, GID, …), what is not masked yet, and that the pairing QR code must never be attached to an issue ([#162](https://github.com/ha-vimar/ha-vimar-intercom/pull/162)).

### Security

- Debug logs now also mask the plant's private IP addresses, the account's SIP id, IMEI and device name, and names and caller ids from the phonebook, so a log needs far less cleaning before it is shared in an issue ([#157](https://github.com/ha-vimar/ha-vimar-intercom/pull/157)).
- The logs now also mask the other phones' ids in SIP addresses, public addresses in the Via header and in SIP addresses without a user, and the apartment GID ([#159](https://github.com/ha-vimar/ha-vimar-intercom/pull/159)).
- On Home Assistant running on Windows, uploading the away message no longer writes through a symlink left in the messages folder (follow-up to [#91](https://github.com/ha-vimar/ha-vimar-intercom/pull/91)) ([#160](https://github.com/ha-vimar/ha-vimar-intercom/pull/160)).

### Other changes

- ci: bump actions/checkout from 4 to 7 ([#150](https://github.com/ha-vimar/ha-vimar-intercom/pull/150))
- ci: bump actions/setup-python from 5 to 7 ([#151](https://github.com/ha-vimar/ha-vimar-intercom/pull/151))
- chore: bump the dev-tools group with 7 updates ([#152](https://github.com/ha-vimar/ha-vimar-intercom/pull/152))
- Code kept for Home Assistant versions older than 2025.10 removed ([#155](https://github.com/ha-vimar/ha-vimar-intercom/pull/155))
- A test keeps docs/ENTITIES.md in line with the entities and actions in the code ([#156](https://github.com/ha-vimar/ha-vimar-intercom/pull/156)).

## [1.0.20] - 2026-10-05

> **⚠ Before you update**
>
> - Home Assistant 2025.10 or newer is now required ([#127](https://github.com/ha-vimar/ha-vimar-intercom/pull/127))
> - automations that read the `open_door` response (`result`) or the "Ultima Apertura" `esito` attribute now get a key instead of Italian text: `opened`, `busy`, `queued`, `unconfirmed`, `not_registered`, `timeout`, `error` or `send_failed`, plus the SIP code in `code` / `codice` ([#132](https://github.com/ha-vimar/ha-vimar-intercom/pull/132)).
> - automations reading the `send_command` response or the "Last Command" sensor get `ok: false` and `queued` where a 202 used to give `ok: true` and `OK (202)` ([#139](https://github.com/ha-vimar/ha-vimar-intercom/pull/139)).

### Enhancements

- The card no longer receives every media twice over the network: less upload from home when you watch the intercom remotely ([#142](https://github.com/ha-vimar/ha-vimar-intercom/pull/142)).

### Bug fixes

- The status sensor no longer goes "offline" in the middle of a call or a ring while a registration renewal is retried ([#119](https://github.com/ha-vimar/ha-vimar-intercom/pull/119)).
- Opening the door over the cloud no longer sends a second open command after a timeout; the card asks you to wait before trying again ([#120](https://github.com/ha-vimar/ha-vimar-intercom/pull/120)).
- A "202 Accepted" answer to a door command is no longer counted as an open door ([#120](https://github.com/ha-vimar/ha-vimar-intercom/pull/120)).
- A stray video packet from the cloud relay no longer freezes the HA camera, go2rtc, Frigate or HomeKit picture for several seconds ([#121](https://github.com/ha-vimar/ha-vimar-intercom/pull/121)).
- Video and audio no longer fail for the rest of a call when the cloud relay jumps the packet numbers right before a wrap, and reordered packets no longer move the SRTP window ([#122](https://github.com/ha-vimar/ha-vimar-intercom/pull/122)).
- The card now says why the microphone can't start (permission denied, Home Assistant opened over HTTP, or no microphone in the app) instead of doing nothing ([#124](https://github.com/ha-vimar/ha-vimar-intercom/pull/124)).
- Door command errors (lock, Open Door buttons, card) now follow the Home Assistant language instead of always being in Italian ([#132](https://github.com/ha-vimar/ha-vimar-intercom/pull/132)).
- Light, relay and switch commands the cloud relay only queued (202) are no longer reported as done ([#139](https://github.com/ha-vimar/ha-vimar-intercom/pull/139)).
- The ring photo is taken from the first keyframe, so it shows up about 3 s sooner and also on panels that send a single keyframe per ring ([#141](https://github.com/ha-vimar/ha-vimar-intercom/pull/141)).
- Over the cloud, commands (phonebook actuators, voicemail and DND switches, voicemail delay, send_command) now wait 20 s like the door, so a late answer from the relay is no longer reported as a failure ([#143](https://github.com/ha-vimar/ha-vimar-intercom/pull/143)).

### Documentation

- docs(github): issue templates point to Discussions Q&A ([#116](https://github.com/ha-vimar/ha-vimar-intercom/pull/116))
- docs(contributing): version bump only in release PRs ([#118](https://github.com/ha-vimar/ha-vimar-intercom/pull/118))
- Links point to the new ha-vimar organisation ([#136](https://github.com/ha-vimar/ha-vimar-intercom/pull/136))
- Migration notes for users of the original noiseheroes integration ([#147](https://github.com/ha-vimar/ha-vimar-intercom/pull/147))

### Other changes

- test(homekit): give the port reservation test its own clock ([#117](https://github.com/ha-vimar/ha-vimar-intercom/pull/117))
- ci: declare read-only permissions in the Validate workflow ([#125](https://github.com/ha-vimar/ha-vimar-intercom/pull/125))
- Changelog entries now come from the PR descriptions; PR template and CI check for them ([#126](https://github.com/ha-vimar/ha-vimar-intercom/pull/126))
- Home Assistant 2025.10 or newer is now required; CI runs on Python 3.13 and 3.14 ([#127](https://github.com/ha-vimar/ha-vimar-intercom/pull/127))
- Regression tests for cloud and local ring edge cases (renewal during a ring, forked and crossed CANCELs, retransmitted INVITE, two cards, crossed hang-ups) ([#131](https://github.com/ha-vimar/ha-vimar-intercom/pull/131))
- The `open_door` response and the last door sensor report a result key (`opened`, `busy`, `timeout`, ...) and the SIP code instead of Italian text ([#132](https://github.com/ha-vimar/ha-vimar-intercom/pull/132)).
- Card upload tests follow the config entry lookup used since 1.0.19 ([#133](https://github.com/ha-vimar/ha-vimar-intercom/pull/133))
- The ring photo decoder now logs what it does (debug log), to help find why a ring sometimes has no photo ([#134](https://github.com/ha-vimar/ha-vimar-intercom/pull/134)).
- GOVERNANCE.md and CODEOWNERS: roles, who tests what, reviews, releases ([#137](https://github.com/ha-vimar/ha-vimar-intercom/pull/137))
- Release notes and the changelog are composed from the PR descriptions (tools/release_notes.py) ([#138](https://github.com/ha-vimar/ha-vimar-intercom/pull/138))

## [1.0.19] - 2026-10-02

### Added

- **Docs: clear the ring notification on the other phones** ([#99](../../issues/99)): a recipe in
  `docs/ENTITIES.md` (and the Italian page) that sends the ring notification with a fixed `tag` and clears it
  with `clear_notification` when the ringing binary sensor goes off, so nobody taps a ring that is already taken.
- **Test ring button** (diagnostic, on the device page): a 20 s test ring in one tap, the same as the
  `simulate_ring` service, to try your ring automations and notifications without writing YAML. It reports an
  error if a real call or ring is in progress.
- **Upload the away message from the settings**: a file field next to *Message if nobody answers (audio file)*
  takes an mp3, wav or m4a (max 5 MB), saves it in `<media folder>/citofono/messaggi` and makes it the away
  message. Only the file name is kept (no paths); a different file with the same name gets `-2` added instead of
  being overwritten.
- **Upload the away message from the card**: in the card's *Intercom settings*, under *Away message audio file*,
  an *Upload* button (*Replace* when a file is already chosen) sends the file to the new admin-only
  `POST /api/vimar_intercom/away_upload`, with the same rules as the settings upload, and selects it. The old file
  stays in the folder. The file list now shows *None (use the text)* instead of `none`.

### Changed

- **Debug logs**: the plant message parsing now logs under `custom_components.vimar_intercom.plant_messages` and
  the ring media under `custom_components.vimar_intercom.ring_media`, no longer under `...hub`. Adjust your
  `logger:` entry if you filter on the old name.
- **README**: an *Open in HACS* button (My Home Assistant) and a last-commit badge; the release, downloads and
  stars badges are gone, since HACS already shows them at the top of the page.

### Fixed

- **Card popup, iPhone: closing it no longer taps the card underneath.** The tap on X or Hang up also landed on the compact card that takes the popup's place: during a ring it hit Answer (the popup came back and the card answered), after a hang-up it reopened the popup and called the panel. For 0.8 s after the popup closes, taps on the compact card are ignored.
- **Card popup: X during a ring means "not now".** It sends nothing to the panel (the ring goes on for everyone else) and the popup no longer opens by itself again for that ring, whatever follows (answered elsewhere, call ended, the card rebuilt). A new ring opens it as before.
- **Card: no black box while waiting for video.** Until the first frame arrives the live view shows the doorbell icon and "In attesa del video…".
- **Away message upload errors** ([#91](../../pull/91)): an expired upload showed the raw text
  "File does not exist" in the form; any failure other than a wrong type or a too big file now shows the
  translated *File not saved* error. An empty file or a name over 200 bytes is refused as a wrong file instead of
  failing with an error in the log.
- Live video from the 40515 was soft and choppy (16 fps, ~14 KB keyframes) because we asked the panel for 256 kbit/s. We now ask for 2 Mbit/s: it sends 25 fps at about 1.5 Mbit/s, same 720x576 picture. A keyframe is now a burst of about 40 packets, which filled the 64-packet cap of the reorder buffer before the 80 ms wait ran out and dropped the picture until the next keyframe, so the cap is now 512.
- Card video colours looked off (skin and sky tints): the panel's SD H.264 carries no colour info, so browsers assumed BT.709. The decoder is now configured with BT.601.
- **A dashboard left open no longer calls the panel again ~10 s after a hang-up** ([#57](../../issues/57)): go2rtc and the stream worker reconnect to `/av` with a growing back-off, and each refused reconnect moved the 5 s quick-reopen window forward, so the first one more than 5 s apart placed a call. Now a reconnect from a viewer that was watching `/av` when the call ended waits the full 60 s pause. Opening the camera within the minute still calls if `/av` was not open at the end (for example after answering from the card), and HomeKit is unchanged. So a viewer that was watching `/av` when the call ended now gets 503 for up to 60 s instead of about 5 s, for example when reopening the camera right after the panel closes a view at 120 s. That is intended.
- **An unanswered ring now ends when the panel stops ringing** ([#60](../../issues/60)): on local UDP the panel
  gives up without a `CANCEL`, so HA kept ringing up to 90 s and a late answer opened a dead call. The panel
  does stop its ring preview when it gives up: 3 s without any preview RTP now ends the ring (`ring_ended`, end
  webhook, the card closes) and a late answer finds no ring. A panel that sends no preview keeps the 90 s limit.
  Cloud TLS is unchanged: the relay sends `CANCEL`, and a gap in the preview there must not end the ring.
- **Passive stream stuck on the last panel frame** ([#87](../../issues/87)) (`/av?autocall=0&idle=image`): after a call the stream could keep the panel's last frame until restart; the standby frame now comes back one second after the video ends.
- **Choppy or frozen `/av` video on cloud plants** ([#87](../../issues/87)): the RTP forward reaches ffmpeg in arrival order, and with
  its default 0.1 s `max_delay` ffmpeg gave up on late packets ("max delay reached") and dropped frames. The
  input now waits up to 0.3 s (`-max_delay 300000 -reorder_queue_size 1024`). This can add up to 0.3 s of latency to `/av`.
- **Passive stream load on Home Assistant** ([#87](../../issues/87)): the live decoder now hands over the 10 fps the encoder uses, not all 25 (about 11 MB/s of raw video on the event loop).
- **"bind failed" after an `/av` client left mid-start** ([#87](../../issues/87)): an `/av` client leaving while ffmpeg was starting could leave that ffmpeg running with the UDP ports, and
  the next call's ffmpeg failed with "bind failed". It is now killed, and the next one waits until it is gone.
- **`/audio_ws` `state` messages carry `ringing`**: a client that connects while the panel is ringing (an Echo
  Show through Scrypted) now knows it and can watch instead of placing a call over the ring.
- Live video froze for up to 3 s after each keyframe on cloud plants: packets the relay delivered a few milliseconds out of order were treated as lost. The reorder buffer now waits up to 80 ms for a gap.
- **The door is opened with your plant's own command** ([#58](../../issues/58)): the lock, the *Open Door* button,
  HomeKit's gate and `open_door` without `command` now send the `MSG` of the phonebook's door actuator for that
  panel, as the VIEW app does, instead of a fixed `OPEN_2F`. The body is taken only together with its panel, since
  a relay module can use the same body towards another one. Without a door actuator in the phonebook nothing
  changes (`OPEN_2F`, with a log line saying so). The *Last Door Open* sensor shows the command sent and where it
  came from (`phonebook`, `default`, `explicit`). **Takes effect on update, without a new import**: if the
  phonebook you already imported has a door actuator, its `MSG` is used from now on (for example `OPEN` instead
  of `OPEN_2F`). If the door stops opening, set that actuator's `msg` back to `OPEN_2F` in Options → *Actuators
  (JSON)*, and please report it on #58. A row naming the panel wins over an `AUTO` row, and the "no door
  actuator" line is logged once per panel.

### Security

- **Test ring button is admin only** ([#85](../../pull/85)): it starts the same fake ring as the admin-only
  `simulate_ring` service (ring webhooks, announcements, automations), but any user could press it. Now a
  non-admin user gets *Unauthorized*; automations and scripts (no user) can still press it, like they can call the
  service.
- **Away message upload never writes through a link** ([#91](../../pull/91)): the file is created with
  `O_CREAT|O_EXCL|O_NOFOLLOW`, so a dangling symlink (or a file that appears in the meantime, including a
  concurrent upload with the same name) is never written through or overwritten; the upload gets the next `-n`
  name instead.
- **`/av` key** ([#63](../../issues/63)): plain `/api/vimar_intercom/av` places a call to the panel, and
  until now any client that looked local got it (a port-forward with SNAT, a guest Wi-Fi device). It now
  also wants an authenticated Home Assistant user or the installation's key in `?auth=<key>`. The key is
  created at first start, stored in the entry, never expires (the stream worker reuses its URL on every
  reconnect) and the integration's camera adds it by itself. Refusals are 403, not 401, so Home Assistant's
  "invalid authentication" warning never prints the URL; `auth` is also the name HA masks in its stream
  logs, and the integration masks it in its own. Options → *`/av` stream key* shows it and regenerates it.
  Passive URLs (`autocall=0`, go2rtc/Frigate/Scrypted) keep working without it for now; a wrong key is
  refused everywhere. See `docs/EXTERNAL.md`.
- **Symlink in the snapshot folder** ([#46](../../issues/46)): a symlink or hard link left in the ring photo folder at
  the name of `ultimo_squillo.jpg` (or of a ring photo) was followed, so the photo overwrote another file, and the
  camera could show any file Home Assistant can read. Photos are now written to a new file and renamed over the
  name, and the last photo is read only if it is a regular file. The ring clip (and its audio remux), which
  ffmpeg opens by name, now goes to a file the integration creates first, after removing whatever was at that
  name. A photo or clip with more than one name (a hard link) is no longer served.
- **Slow patterns on SIP headers** ([#46](../../issues/46)): the Tab 5S/7S Plus model patterns and the SIP user and host
  patterns of the device inventory could take seconds on a crafted header (a long run of spaces, or `sip:`
  repeated without an `@`), stalling Home Assistant. The patterns no longer backtrack that way, and the
  User-Agent is cut at 256 characters before it is matched. Real headers match as before.
- **SRTP replay window** ([#46](../../issues/46)): the receiving side of the call media had no replay protection, so one
  captured voice or video packet, sent again, was accepted every time. An index already received among the
  last 128 per stream is now dropped after authentication; packets merely out of order still pass. A lone
  packet far from the window never moves it, and passes once if ahead (one stray on a cloud ring stalled the
  stream). Three in a row move it (50 past half a wrap): a sender restarting lower loses 2. Known gaps: 3
  captured old packets in a row move it back too, and far-ahead ones sent in turn, not in a row, pass every
  time.

## [1.0.18] - 2026-10-02

### Changed

- **HACS installs from a release zip**: every release gets `vimar_intercom.zip` attached by a workflow, and
  HACS downloads that instead of the source archive (`zip_release`). Nothing changes for users; it lets the
  README show a download count (installs and updates from the first release with a zip on).
- **README badges**: latest release, HACS, minimum Home Assistant version, test status, licence and stars.
- **Documentation**: the README (the page HACS shows) is now short: what the integration does, compatibility,
  installation in four steps, and links. The reference moved, unchanged, to `docs/` in English and Italian:
  `CONFIGURATION`, `ENTITIES`, `CARD`, `TROUBLESHOOTING`, plus an Italian `HOMEKIT` page. Three statements
  were corrected on the way: the away message entities are admin-only since 1.0.17, the 40515's port 80
  refuses the connection, and how long "Vedi esterno" lasts depends on the plant.

### Fixed

- **One slow card no longer delays the others**: each audio WebSocket client gets its own small
  queue and sender, so the voice and video loops never wait for a client's network. A client that
  falls too far behind drops its own backlog and gets video again from the next keyframe (#54).
- `vimar_intercom.simulate_ring` now simulates a whole ring ([#33](../../issues/33) item 14): the
  state goes to `ringing` (card popup, sensors, binary sensor), the doorbell event and the start
  webhook fire, and after `duration` seconds (default 20) it ends like an unanswered ring, end
  webhook included. Before, the end never came and a doorbell switch driven by the webhooks stayed
  on. Still no SIP traffic: Answer just ends it, the away message never answers it, a real ring
  replaces it, and it is not written to the ring log or counted in the stats.

## [1.0.17] - 2026-10-01

### Added

- **Card `idle_picture`** option (also in the visual editor): `last_ring` (default) keeps the last
  ring's photo in the scene and the compact photo button at rest; `standby` shows the doorbell icon
  there instead. The photos stay in the history drawer.

### Fixed

- `/av`'s input SDP is written to a private temporary file (mode 0600, an unpredictable name) instead
  of a fixed, guessable path in the temp directory, and removed when ffmpeg is stopped.
- **Card history photos and clips** no longer stay broken behind an expired signed path: a photo or
  clip the browser fetches again after the ~30 s signature has expired (HA logged "Login attempt or
  request with invalid authentication" for `/api/vimar_intercom/rings/*.jpg`) is signed again once
  and reloaded.
- **Card voice playback** no longer clicks 50 times a second: the 8 kHz voice is resampled to the
  audio context's rate continuously across packets and low-passed at 3.6 kHz, and the playout
  buffer starts 120 ms ahead and grows by 40 ms at every underrun, up to 300 ms (#53).
- The visitor's voice clicked on the cloud relay: audio never looked at the RTP sequence number, so a
  packet lost or reordered on the way was skipped or played out of place. Audio is now put back in
  order like the video, duplicates are dropped, and a lost packet is filled with the previous 20 ms at
  half volume, and a source that restarts its sequence numbers is followed. The server keeps at most
  1 s of voice waiting instead of 4 s (#53).
- **Card live video on iPhone** no longer smears between keyframes: the WebCodecs decoder is fed
  AVC (an `avcC` description built from SPS/PPS, length-prefixed NAL units) instead of Annex B, is
  reconfigured only when SPS/PPS change, and runs with `optimizeForLatency` off on iOS (#53).
- **Away message text and audio file** can be changed by administrators only. The two entities
  were writable by any user who could call the service; a call with no user (automations) still works.
- **Card shortcut buttons** no longer inject markup: the `friendly_name` and `icon` of a `button.*`
  entity are escaped, and an icon not shaped like `prefix:name` falls back to the default.
- **Card popup layout**: the popup now opens when the card first sees a call already in progress
  (notification "Answer", app reopened after the ring), not only on `ringing` or with the anchor in
  the URL. A period that starts with `calling` (a tap on the card, HomeKit, Alexa) still needs the
  anchor (#35).
- "View outside" opened right after hanging up no longer fails after 25 s on local UDP (#41). The
  view waits for the panel to answer our BYE (at most 6 s) before calling it again. A call to the
  video panel (a view's, the card's "view outside", the call buttons) that the panel leaves
  unanswered is cancelled and tried again, within 21 s of the first try and 24 s of the tap
  (inside `/av`'s 25 s): after 3 s when the panel's proxy said 100
  Trying and the panel never rang, else after 6 s. Every try waits until 5 s after the last call
  on the line (or our own cancelled try) ended: a 2-wire panel ignores an INVITE that comes
  sooner. Calls to a flat or the switchboard, and cloud plants, are unchanged. The 487 that ends our own cancelled INVITE is
  logged at DEBUG, and `/av`'s "call not established" says how long it really waited.
- The card's Fill/Fit button (#42). It is hidden while the video and its box have the same shape
  (a 4:3 panel in the default 4:3 box), where filling and fitting draw the same picture and the
  button seemed to do nothing. When the card falls back to Home Assistant's picture card (no
  WebCodecs, or the card's player failed), the choice now reaches it as `fit_mode`.
- In cloud mode, finding the local IP resolved the proxy's name on the event loop, at setup and at
  every reconnect: with the network down (Home Assistant starting before the router) that DNS
  lookup froze every integration for seconds. It runs in the executor now.
- HomeKit: two ffmpegs starting together (the re-encoder and a view) could be handed overlapping
  loopback ports, and one logged "bind failed: Address in use". Every ffmpeg RTP input now gets an
  even port with the next one free (RTCP), and a pair handed out is not handed out again for 10 s.

### Changed

- SRTP costs a quarter less per packet (28 µs instead of 38 µs for a video packet on a Raspberry
  Pi 5): the IV is one integer XOR, the AES-CTR cipher is set up without a Counter object, and the
  HMAC is copied from one keyed once. The bytes on the wire are the same (known-answer tests).
- The voice to the panel (the card's microphone, HomeKit's Talk, the away message) is μ-law encoded
  through a lookup table: 13 µs instead of 113 µs per 20 ms packet on a Raspberry Pi 5, the same
  bytes, on the event loop that also carries the call's video.
- HomeKit views open faster. The view's audio no longer waits for the video path (re-encoder and
  keyframe included), and with Smoother video on, the re-encoder starts while the view's call is
  still connecting instead of after its video arrives.
- `find_sga` reports a `202 Accepted` as `queued` instead of `exists`: the cloud relay accepted the
  probe but no device took it, so nothing at that address can answer over the cloud (#14). The
  service's `sip_timeout` now goes up to 30 s, because over the relay that `202` can take ~15 s.

### Security

- One crafted SIP datagram could stall Home Assistant for seconds: the `Authorization` pattern in
  `log_redact` was quadratic on a run of bare line feeds, and every received message goes through it
  on the event loop. The pattern now matches only spaces and tabs around the header name, and a log
  line longer than 16 KB is cut once it has been redacted (#46).
- A device on the local network could change the intercom's address in Home Assistant by announcing
  the Tab's MAC (which the Tab publishes) over mDNS with another address: REGISTER, door commands
  and calls then went there. A new address is now offered only when it is a private IPv4 address that
  the record announces as its own, and applied only after you confirm it under Discovered (#46).

## [1.0.16] - 2026-09-30

### Added

- **HomeKit video doorbell** (`homekit_accessory.py`), off by default: Options → HomeKit shows the
  pairing QR code and setup code, to administrators only; the setup code changes after 10 wrong
  attempts in a row. Ring notification, live video, two-way audio and the gate in one accessory. Home
  Assistant's own HomeKit bridge cannot carry the talk direction: it answers the phone's
  `SetupEndpoints` with the phone's own ports and never listens on them.
- Video to the phone is re-encoded with one keyframe per second by default (a packet lost on the
  cloud relay is a brief smear instead of a freeze of up to 3 s), or sent as the panel's own H.264
  packets when the option is off.
- During a ring, opening the view previews the street without answering, and the first word (Talk)
  answers; an option answers as soon as the view opens instead. Closing the last HomeKit view ends a
  call HomeKit placed or answered.
- The ring as a programmable button for Home app automations is an option, off by default.
- Each HomeKit view logs where its opening time went, at INFO: at "stream started", and when the
  first audio packet reaches the phone.
- The ring notification's picture comes from the re-encoder, which conceals packets the relay lost,
  so it is ready a few tenths of a second into the ring; without re-encoding it waits up to 3 s for
  the ring's first complete frame. The Home tile keeps the last picture of a call or ring after it
  ends.
- The gate reports unlocked when it opens and then unknown, never locked: the intercom only pulses
  the strike.
- `media_handler`: `rtp_sinks` on the audio and video RTP protocols, the taps the doorbell reads the
  call from. `srtp`: `SRTCPContext`, checked byte for byte against libsrtp.

### Fixed

- The admin probe of a panel (the card's `probe` action) no longer reports "Errore: 100" when the
  proxy asks for credentials and then answers 100 Trying before 200 OK: provisional answers to
  the authenticated retry are skipped, as they already were before the challenge.
- Saving the network settings page dropped the options it does not show (a HomeKit doorbell that
  was on, for instance, was turned off).

### Changed

- `requests` is no longer listed in the manifest's requirements: it ships with Home Assistant
  itself, and hassfest now rejects it in a custom integration. Nothing changes on an install.

### Changed (contributors)

- `AGENTS.md` for coding agents (`CLAUDE.md` points to it). The CI fails below 95% line and branch
  coverage of `custom_components/vimar_intercom`; every fix comes with a test that fails without it.
  New code comments, commit messages and PR text are in English.

## [1.0.15] - 2026-09-30

### Changed

- **One installation per Home Assistant.** The SIP and media state are module-wide, so a second
  entry never worked reliably: both entries shared one registration and one call. Adding a second
  entry (by hand or from discovery) is now refused with a clear message. Existing installations
  are not touched: an existing second entry still loads, with the same shared state as before; if
  you have two, keep the one you use and delete the other (Settings → Devices & services → Vimar
  Intercom → ⋮ → Delete). The limit lives in the config flow rather than in `single_config_entry`
  in the manifest: with that flag Home Assistant stops every new flow before the integration sees
  it, discovery included, and the Tab's new address after a DHCP change was no longer picked up
  in local UDP mode.

### Added

- New "Intercom Dispositivi" sensor: the devices seen on the plant (phones sharing the SIP
  account, panels). The list is kept across restarts and its attributes are kept out of the
  recorder. The attribute shows device names to every Home Assistant user; identifiers are
  masked and addresses are left out (the hub keeps them, to merge devices).
- The video panel can be learned: when `camera_target` is empty and the default panel does not
  exist (404 or 604), the panel that last rang with video is tried and saved. A choice in the
  options always wins; a busy (486), unavailable (480) or media-refusing (488) panel is not
  replaced. The learning is logged at WARNING.
- The voicemail and DND switches warn when the plant's state contradicts a command it accepted
  with 200 OK (usually a wrong `sga_target`).
- The device name the integration pairs with (MyName) is configurable, and validated: 1 to 64
  printable characters.
- Setup probes the transport: it tries the path the plant profile suggests and falls back between
  cloud and local, storing what worked. A fallback is logged at WARNING, shown when the entry is
  created and kept in the entry data (`setup_note`). The plant profile only sets the transport
  default; media encryption keeps following the plant's own declaration (`media_enc` auto).
- Audio-only entrances: when the pairing QR says `video=0`, our own INVITE offer carries no
  `m=video`. Entries made before keep video; answers still mirror the panel's offer.
- Between calls the camera image is the last ring photo, when there is one (the latest, or
  `ultimo_squillo.jpg` in the snapshot folder after a restart), instead of the error Home
  Assistant made of an empty image. A thumbnail still never calls the panel.
  Note: Home Assistant shows the camera image to every user who can see the camera entity;
  `allowed_users` does not cover it.
- An RTCP probe for debugging (`rtcp.py`): with the integration's logger set to DEBUG, the RTCP
  ports (RTP + 1) listen during a call, the path to the panel's RTCP port is opened, and what
  arrives is logged (type and SSRC; every record for plain RTCP). Off by default: nothing is
  bound and nothing is sent.

### Fixed

Call and media:

- `/av` no longer starts with a wrapped audio timestamp. When the panel's audio arrived before its
  video, the AAC encoder's first packet (-1920 in the 90 kHz clock) was written as 2^33 - 1920, and
  Home Assistant's stream worker logged "Timestamp discontinuity detected" and restarted. The
  output timestamps now start 50 ms later.
- The talk queue is capped at 80 ms, oldest audio dropped first: a network hiccup no longer
  leaves the rest of the call late. There is no pre-buffer: a queued packet goes out on the next
  tick.
- Answering no longer waits for the reply to the keyframe INFO, a round trip through the relay:
  the request still goes out, in the background.
- The SDP answer mirrors the offer's encryption per media line: RTP/SAVP with the crypto tag and
  suite the offer chose for that line (AES_CM_128_HMAC_SHA1_80 or _32), RTP/AVP for a plain line.
  A line whose suites are all unsupported is refused. Our own offers still follow the plant
  setting.
- Plain RTP is accepted only from the call's address (SRTP is authenticated by its key).
- A viewer that opens the camera during an automatic hang-up waits for the call to end locally
  (at most about 0.5 s more), not for the answer to the BYE that the cloud never sends. The
  hang-up is shielded from the new view, bounded by a timeout, and its errors are logged. A
  viewer that leaves during that wait no longer gets a call dialled.
- Stale `call_ended` events from a previous call are ignored; the call is hung up and the SIP
  state reset when the integration is unloaded.
- An audio WebSocket open anywhere (the card, the app) no longer stops a view from calling.
- The echo of our own call gets 486 Busy Here instead of a decline that ended the ring everywhere.
- The SDP answer has exactly the offer's media lines, in the offer's order (RFC 3264): an
  audio-only panel no longer gets a live `m=video` it never asked for, and a video line offered
  with port 0 is answered with port 0. Our own offers keep both lines. A re-INVITE whose lines
  differ from the running call gets a new answer, and a session without video closes the video
  side.
- `/av` replays the cached group of pictures in RTP sequence order, not arrival order: ffmpeg
  dropped every packet older than the first one it saw.
- The `/av` ffmpeg's real errors reach the Home Assistant log at WARNING; known harmless lines
  (concealment, "max delay reached", "RTP: missed", "dropping old packet") and everything after
  we asked it to stop stay at DEBUG. "FU-A middle/end without start" is logged at DEBUG.
- The camera's stream source is a loopback URL on Home Assistant's own HTTP port and scheme, not
  `internal_url`, which may point at a reverse proxy that `/av` refuses (403).

- Keepalive: a REGISTER renewal that fails during a call is retried once instead of tearing
  down the connection the call runs on (the reader still reconnects if the link is really gone).
  A failed tick counts as one registration failure, not two.
- A late `call_ended` from an earlier call no longer reaches the cards, which closed the view of
  the call that was up.
- A re-INVITE answered with a new SDP keeps the local SRTP keys the media already sends with;
  if a line gets a new key the media is set up again.
- The cached keyframe group, its WebSocket form and the video packet count are cleared when the
  media stops and when a session has no video: the next call's viewer no longer got the previous
  call's picture. A duplicate packet waiting for reordering is cached once.
- The Hang up button, the card and the away message hang up through the same guard as the
  automatic hang-up: a view opening meanwhile waits for the call to end. The BYE runs in the
  background and the hang-up returns once it has left, not after the answer the cloud never
  sends. Unloading cancels the hub's background tasks.
- An older hang-up finishing late no longer drops the guard of a newer one, and a late auto-call
  failure no longer clears the flag of the auto-call that replaced it.
- The last talk packet after an underrun is sent after one tick instead of waiting for a second
  one, and the tail of an away message is padded to a whole packet.
- Plain RTP dropped because it came from another address is logged at INFO, once per call.
- An RTP/SAVP line refused in our answer (no supported crypto suite) gets no media. Media lines
  we do not handle (`m=text`, `m=application`, a second `m=audio`) are answered with port 0 in
  their place, and their `c=`/`a=` lines no longer change the line before them.
- An auto-call that connects after its viewer already left is hung up after the usual delay,
  instead of staying up with nobody watching until the 5 minute cap.
- μ-law audio is decoded through two byte tables instead of a loop per sample (about 20 times
  faster per packet, same output), and the debug log buffer is a bounded deque that no longer
  moves every line when it drops the oldest one.
- No periodic keyframe request (SIP INFO every 5 s) during a call: the panel ignores it and sends
  a keyframe about every 3 s on its own, and each INFO crossed the cloud relay. The burst at call
  start and the request after a lost video packet stay.
- `/av`: ffmpeg's RTP input gets a 640 KB receive buffer (`-buffer_size 655360`; loopback bursts
  lost packets, "RTP: missed N packets", and broke the H.264 stream) and `-muxpreload 0`. The RTP
  forwarding starts as soon as ffmpeg's ports are bound (read from `/proc/net/udp`, at most
  0.5 s) instead of after a fixed 0.3 s. When the last client leaves while the call's video goes
  on, ffmpeg is kept for 10 s for a client that reconnects; the end of the call still stops it at
  once.

SIP:

- A late response nobody waits for any more ("Stale response") is logged at DEBUG, not
  WARNING.
- REGISTER retries once more when the registrar rotates its nonce, and answers a 407 with
  Proxy-Authorization.
- The framer handles the CRLF keepalive pongs; the request processor survives a reply that cannot
  be sent.
- The framer also reads the compact `l:` header. A Content-Length that is not a number or is over
  1 MB breaks the stream: the connection is dropped and reconnected instead of holding every later
  message. A negative value still counts as 0.
- When the periodic REGISTER fails while registered, the reconnection starts at once instead of at
  the next keepalive; it joins a reconnection already running. A registrar granting less than
  150 s is logged at WARNING.
- The registration is renewed before the lifetime the registrar grants runs out: at the grant
  minus min(60 s, 20 %), at least 5 s, and never later than the 120 s keepalive, which stays
  the cadence for grants of 150 s or more. This includes the grant of the first REGISTER at
  startup.
- Background tasks of the hub (ring webhooks, auto-call, ring log, echo decline, WebSocket state)
  are held until they finish, and their errors are logged.
- SIP tags, branches and Call-IDs come from `secrets`.
- MESSAGE and NOTIFY requests delivered twice by the relay are answered but broadcast once.
- Commands sent to a whole SIP URI are accepted only as `sip:<digits>@<plant domain>`.
- The RTP sockets ask for a 1 MB receive buffer (SO_RCVBUF), so a video burst is not dropped.
- Every connection made through the unverified TLS fallback to the cloud proxy is logged at
  WARNING.

- A framing error reconnects at most once every 2 s, and the errors are counted.
- Unloading closes and forgets the SIP transport, wakes whoever still waits for a response and
  no longer calls back into the hub being unloaded.
- Three challenges with a rotated nonce are reported as refused credentials.
- The SIP client reads a Digest challenge whose quoted values hold commas (a realm such as
  `"plant, north"`) whole, like the setup flow does; the response was signed with a cut realm.
  A challenge offering only `qop=auth-int` gets the plain RFC 2069 response instead of a claimed
  `qop=auth`.

Setup and configuration:

- Entries from the m4r1k fork keep working: `sip_cloud_domain` is read as the cloud domain, and a
  legacy `device_id` becomes both device identifiers.
- HA1 is always recomputed from the password on the domain in use.
- The options form no longer pre-fills `camera_target` with 55100: saved once, it counted as a
  user choice and the panel learned from the last ring was never used. Empty stays empty.
- The SIP test in the options uses the local domain when the plant has one, as setup does.
- The HTTP views pick the active entry only among entries (a dict with a hub), never another key
  under the integration's data.
- Saving values into the entry data (detected model, learned panel) no longer reloads the
  integration; only a change of the options does.

Documentation:

- Security: SIP credentials are stored in plain text in `.storage` like every integration's
  secrets (the README said encrypted); the HomeKit pairing code file is 0600; plain RTP is
  accepted only from the call's address.
- Logging rewritten to match `log_buffer.py`; `HAP-python` and `PyQRCode` listed among the
  requirements; the Tab 7S Up 40517 added to the compatibility table.
- New `docs/HARDWARE.md` (what differs between plants) and `docs/TEST_PLAN.md` (field test round).
  The Italian README inside the component folder is now a pointer to the root README.

### Security

- SRTP master keys are kept out of the logs: the parsed SDP keeps only the key it uses, the SDP
  log lines drop it, and the log filter masks `inline:` keys, any value under a `key` or `*_key`
  field and a key-sized base64 value after `key:`. The debug log buffer is bigger.
- The device list attribute no longer carries the devices' addresses.
- The SIP test in the options removes its own registration (Expires: 0, same Contact) once it
  passes: with the integration running it replaced the live binding with one pointing at the
  test's closed socket.
- `fetch_local`: a host such as `10.0.0.1:1@attacker.tld` passed the private-address check (it was
  read as `10.0.0.1`), while in the URL `@` separates credentials from the host, so the request —
  with the SIP password in Digest — went to `attacker.tld`. Only a bare IP, `IPv4:port`, `[IPv6]` or
  `[IPv6]:port` is accepted now, with a numeric port in range.
- Log redaction: `sip_password=…` (key with a prefix), quoted values with spaces (`password="my
  secret"`, `token: 'x y z'`) and dict reprs (`{'sip_password': '…'}`) were left in clear in the
  debug buffer and in the Home Assistant log. They are masked now.

## [1.0.14] - 2026-09-30

### Added

- Do Not Disturb and Voicemail switches are available as soon as the link is registered. Until the
  Tab reports their state they show it as unknown (or the state it confirmed before a restart)
  instead of a guess, so they stay usable on plants that never announce it (#9). Protocol ported
  from noiseheroes-lab/ha-vimar-intercom by Luca Lo Tito (MIT). *Corrected on 2026-10-01: this
  entry first said the switches stay unavailable until the Tab has reported their state; the
  released code never behaved that way.*
- **Decline** button (only while it rings) and `vimar_intercom.decline` service: answers `603 Decline`, so the
  whole house stops ringing, as in the official app. Protocol ported from
  noiseheroes-lab/ha-vimar-intercom by Luca Lo Tito (MIT).
- Ring webhooks: optional options `ring_webhook_url` and `ring_end_webhook_url`, a GET
  (fire-and-forget, 5 s timeout) fired when a ring starts and when it ends (answered, cancelled
  or missed) — e.g. the `turnOn`/`turnOff` URLs of a Scrypted Dummy Switch linked via a Custom
  Doorbell Button (see `docs/EXTERNAL.md`). A failed request only logs a warning and never
  blocks or interrupts the ring; the URL (it may carry a secret token) is never logged in full,
  only scheme and host. Empty = off.
- The panel's last SPS/PPS are kept in HA storage (`.storage/vimar_intercom.<entry>.sps_pps`,
  a few bytes, rewritten only when the panel sends different ones). The first `/av` after an HA
  restart gets `sprop-parameter-sets` in its SDP right away, instead of waiting for the in-band
  SPS (~6 s on the 40515 when the keyframe request is not answered quickly); the card, the
  WebSocket and the ring photo start from the first IDR as on every later call. One pair per
  calling panel (SIP id): with two panels of different resolution the SDP `sprop`, the ring
  photo and the clip use the pair of the panel that is ringing or being called, never the
  other one's.
- Away message from text: options `away_message_text` (multiline) and `away_message_tts` (a
  `tts.*` entity; empty = HA's default engine). When `away_message_file` is empty, the text is
  synthesised by Home Assistant's TTS in HA's language, decoded to 8 kHz PCM through the same
  ffmpeg path as a file (same 30 s cap) and cached per (text, engine, language): generated once
  HA has started (and again when the options change), so the ring never waits for the network.
  If TTS fails (no engine, network) a warning is logged and the doorbell keeps ringing.
- Ring clip, Ring-like: with `snapshot_dir` set, every ring also records the panel's video
  (the preview, and the call if answered from HA) as `squillo_YYYYMMDD_HHMMSS_mmm.mp4` next to
  the photo, from the first keyframe until the ring or call ends, 60 s at most. The H.264 is
  copied, not re-encoded (one extra ffmpeg per ring, `-c copy`, wall-clock timestamps,
  faststart MP4). The panel's audio (the same PCM tap `av_passive` uses) is muxed in as AAC
  once the video is closed, when there was any; otherwise the clip stays silent as before. The
  clip is listed in `squillo.json` (`clip`), deleted with the
  photo when the entry leaves the log, served by `GET /api/vimar_intercom/rings/<name>.mp4`
  (`video/mp4`, HTTP ranges) and exposed by the "Intercom Ultimo Squillo" sensor as `clip`
  (path) and `clip_url` (relative URL, HA authentication) once the file is closed. In the card's
  history a ring with a clip shows a play icon on its thumbnail; a tap plays it in the dialog
  (`<video controls playsinline>`) instead of the photo. A clip cut short (HA reload) is
  discarded rather than left unplayable.
- Keyframe request (SIP INFO `picture_fast_update`) also during the ring preview (early
  dialog of our 183): sent as soon as the doorbell rings, and when a packet is lost during the
  preview, instead of only inside a call.
- Passive AV stream for external tools: `/api/vimar_intercom/av?autocall=0` (or `?mode=passive`)
  never places a call. During a ring or a call it attaches to the same MPEG-TS fan-out as `/av`;
  otherwise it answers 503 at once, so Scrypted, go2rtc or Frigate reconnecting in a loop never
  touch the panel. It is not counted as a viewer: a call started for Home Assistant's camera
  still ends when that viewer leaves. Plain `/av` is unchanged (it still starts a "view" call
  when idle).
- Continuous passive stream: `/api/vimar_intercom/av?autocall=0&idle=image` never ends. A dark
  standby frame (`standby.png`: intercom icon, "Standby") while idle, the panel's live video
  during a ring or a call on the same connection, standby again afterwards. One re-encoded
  H.264 stream with constant parameters (640x480, 10 fps, keyframe every second, baseline) and
  an AAC track (48 kHz mono as `/av`: the panel's audio during a ring or a call, silence while
  idle, fed 100 ms per frame from the same clock so audio and video stay in step), a single
  ffmpeg shared by all clients (started with the first, stopped with the last), plus a decoder
  alive only while there is video; nothing changes for the card, plain
  `/av` or the frame grabber. Recommended for Frigate, Scrypted and go2rtc: one stream that is
  always up, so no reconnect loops and Echo Show opens instantly. Needs ffmpeg with `libx264`.
- `docs/EXTERNAL.md`: Scrypted (Alexa chime + Echo Show live view via a Doorbell Button and a
  webhook fed by the doorbell `event`), go2rtc and Frigate (record on the doorbell event, no
  detection on a standby frame).
- Talking on `/audio_ws` while the doorbell rings answers the call: mic PCM whose RMS stays
  above a threshold for 200 ms takes the same path as the card's "Answer", then the audio is
  forwarded as usual. Below the threshold (or while idle) the frames are dropped. For Scrypted
  (Echo Show, HomeKit through the scrypted-vimar-intercom mixin), where there is no button.
  The card's explicit "Answer" is unchanged.
- New option `allowed_users` (multi-select of Home Assistant users): only they can read
  `GET /api/vimar_intercom/rings` and `/rings/<name>`, open the card's live video and voice on
  `/audio_ws`, or use `/av` with an HA token. Admins are always allowed; empty = every
  logged-in user (the previous behaviour). `/av` without a token (HA's own camera stream,
  go2rtc on the LAN) is unchanged.
- Card option `confirm_open` (default true) keeps the two-tap **Apri**; false opens the door on
  the first tap. Toggle in the visual editor.
- Card option `listen_on_ring` (default false): when on, plays the panel's voice already during
  the ringing/calling/in-call preview, over the same playback path as the talk mode, with no
  microphone and without answering or calling. Stops itself when the ring ends or when real
  two-way audio takes over. Same iOS fallback as the talk button: if the browser keeps the
  `AudioContext` suspended for lack of a real gesture, it gives up silently and shows the
  "Audio" tap instead.

### Fixed

- A ring declined with the **Decline** button (or `vimar_intercom.decline`) stayed *Nessuna risposta*
  (`"outcome": "missed"`) in `squillo.json` and in the card history: it is now recorded as
  `"declined"` and the card shows it as *Rifiutato*. The missed-call counter was already right.
  Found in the field test on the 40507.
- Ring photo: a panel calling for the first time since the HA restart that shipped the
  per-panel SPS/PPS cache (or after an update from the older, single-pair storage format)
  had nothing cached for it, and its first IDR often arrives without in-band SPS/PPS (the
  40515 sends those only every ~6 s): the photo failed with "anteprima video non arrivata"
  even though video was flowing. The old single-pair storage is now migrated instead of
  discarded, and a cache miss for the calling panel falls back to any other panel's cached
  SPS/PPS (the resolution rarely differs) instead of none. The ring photo also keeps waiting
  for a decodable frame for as long as the ring lasts, not just 6 s.
- Card: arriving on the `#citofono` anchor while the call was already `in_call` (e.g.
  **Rispondi** pressed on the ring notification, answered by an automation before the anchor
  scroll ran) never joined the call's audio — the video was already state-driven, but the
  anchor's `hashchange`/`location-changed` handler never re-ran the render that starts
  playback. It now joins (receive + mic) by itself, without ever answering or calling on its
  own.

### Changed

- **Breaking:** the "Messaggio di assenza" switch is merged into *Segreteria* (the orphaned entity is removed
  on update). HA's away message no longer needs a delay above 0: a text or file is enough, and it only
  answers rings after you turn *Segreteria* on (on update it starts off, so nothing answers by itself).
  Do Not Disturb and Segreteria are now in the Configuration category: they are no longer in auto-generated
  dashboards and are not exposed to voice assistants by default.
- The audio silence sent during "Vedi esterno" (only the outgoing view, not answered calls) now stops after
  `view_keepalive` seconds (option; default 120 over the cloud, 0 = none in local/UDP mode), so on the
  2-wire 40507 it no longer keeps the apartment busy for up to 300 s.
- Voicemail and away message merged: one *Segreteria* switch (on = HA's away message if a text or file
  is set, otherwise the Tab's voicemail; attribute `modo`) and one delay, *Segreteria · ritardo*, which
  also drives the away message (the `away_message_delay` option is the fallback when the Tab does not
  expose it). The "Messaggio di assenza" switch and the "Ritardo messaggio di assenza" number are removed;
  the message text and file entities are renamed *Segreteria · testo del messaggio* / *Segreteria · file audio*.
- `/av` audio is AAC-LC (48 kHz mono, 32 kb/s) instead of the panel's raw G.711. In MPEG-TS
  PCMU ends up as private data (`bin_data`), so HA's stream worker (HLS, `camera.record`) and
  HomeKit had no audio. Transcoding and low-latency mux flags as in #21 by @m4r1k; 48 kHz
  instead of 24 because the muxer holds the first video packet until the first AAC frame
  (measured +10 ms vs +60 ms). Video is still copied.
- Ring photo as soon as possible: the first decoded frame is saved about a second after the ring
  (`squillo_...jpg` + `ultimo_squillo.jpg`) and the "Intercom Ultimo Squillo" sensor gets `foto`
  (path) and `foto_url` (relative URL with `?v=`, HA authentication) for notifications right
  away. `snapshot_delay` now means "replace it with a better frame after N s" (the panel's
  first keyframe is dark while the camera adjusts): same file name, new `photo_v` in
  `/api/vimar_intercom/rings` so the card refreshes its cache; 0 keeps the first photo only. The
  card reloads its history when the photo or the clip arrive. `camera.snapshot` still skips
  the first keyframe.

### Removed

- The iOS push code, as in #21 by @m4r1k: `push_sender.py` (APNs VoIP over aiohttp, which
  speaks HTTP/1.1 while APNs needs HTTP/2, so it could never deliver), the
  `/api/vimar_intercom/push_token` view that stored device tokens inside the integration's
  folder, its call on ring and the `APNS_*` constants. With `APNS_KEY_ID` empty none of it ever
  ran; the "APNs push not configured" warning at every start is gone. The Vimar-cloud FCM
  parameters (`PN_*`, `connectProfiles`) are unchanged.

## [1.0.13] - 2026-09-28

### Added

- **`vimar_intercom.find_sga`: find the PICG without the phonebook**
  ([#14](https://github.com/lollox80/ha-vimar-intercom/issues/14), PR #18 brought up to date). It is
  for cloud-only systems where neither the intercom's local HTTP API nor the cloud phonebook (1.0.12)
  is available.
  - The action probes a small range of addresses one at a time, with a pause between probes:
    default `55000`–`55010`, at most 50.
  - It stops at the first reply. With `GET_NICKS` (the default) the intercom declares its own PICG in
    `GET_NICKS_REPLY`.
  - Each probe is reported with the three-outcome rule: `absent` (404), `exists` (accepted, no reply)
    or `replied`.
  - A late `GET_NICKS_REPLY` still counts, because it names the PICG by content.
  - **`probe: get_init_status`** is the fallback for the 40515 in cloud mode where `GET_NICKS` got
    `Timeout`. There the PICG is the address whose probe triggered the reply, and a late reply is
    reported as two candidates. Sent to the real SGA, it makes the VIEW app show "Configurazione
    appartamento modificata".
  - `sip_timeout` (default 8 s) bounds each probe; `do_system_message` gained a `timeout` argument.
  - Nothing is written unless `apply` / `apply_sga` is set.
  - **Admin only**, like `send_command`, since it sends MESSAGEs to a range of addresses and can
    change the configuration.
- `GET_NICKS_REPLY` is parsed wherever it arrives: the declared nicknames and PICG are kept in the
  hub's stats.

## [1.0.12] - 2026-09-28

### Added

- **The intercom is found on the network** ([#6](https://github.com/lollox80/ha-vimar-intercom/issues/6)).
  The Tab announces `_eipvdes._tcp` over mDNS, the service the VIEW app looks for, and Home Assistant
  now offers it under *Discovered*. The QR or the credentials are still asked for, since the TXT record
  carries no secret, but the address and the local SIP domain come from the record:
  - `proxy` fills the intercom address;
  - `domain` becomes the local SIP domain. On a 40515 whose QR says `domain=127.0.0.1` this is the
    Tab's own address, instead of the cloud domain that local registration refused with `503`. This
    is not yet verified on such a plant;
  - on the 40507 it is the cloud domain, as before.
  Only `mac`, `proxy` and `domain` are relied on (the 40507 record has four keys, the 40515 eleven);
  `dev` and `fver` are shown when present. An intercom already set up is recognised by its MAC in
  any notation (or by its address, for manual entries without MAC) and not offered again; when its
  address changes, the entry follows it and reloads (local mode only). No `ZeroconfServiceInfo`
  import, which exists only from HA 2024.12: the blocker of the old PR #7.
- **Phonebook from the Vimar cloud** ([#5](https://github.com/lollox80/ha-vimar-intercom/issues/5)):
  options → *Download the phonebook from the Vimar cloud*. It uses the recipe @CPietro verified on a
  40515 / 2FV2:
  - `GET https://<cproxy>/phonebook/domains/<cdomain without .cproxy>/<rubrica_ver>`;
  - HTTP Digest with the **full** `cdomain` as user and the `token` as password;
  - `User-Agent: TOGA/2.4.0`.
  The token and `rubrica_ver` come from the plant's long `GET_INIT_STATUS` reply; if the token is
  missing the integration asks once and waits a few seconds. The file is the VIEW app's own
  `rubrica.db` and goes through the usual import confirmation; the GID offered is the one the plant
  declares. Plants with the short reply (no token, like the 40507) get a message pointing to the LAN
  download or the file import.

### Security

- The token is never stored: it stays in memory from the last reply. It is masked in the
  *Last received message* sensor, which any user can read. Errors never include it.

## [1.0.11] - 2026-09-28

Closes [#9](https://github.com/lollox80/ha-vimar-intercom/issues/9) and
[#4](https://github.com/lollox80/ha-vimar-intercom/issues/4), with the long `GET_INIT_STATUS_REPLY`
that @CPietro published from a Tab 5S Up 40515 / 2FV2.

### Changed

- **Voicemail and Do Not Disturb no longer show a guess as a fact** (#9). After a command that
  the plant accepted, the switch shows the commanded value for at most 10 s, and meanwhile
  asks the plant for its state (`GET_INIT_STATUS`). The Tab's announcement (`VOICEMAIL;`/`DND;`)
  or the reply replaces it at once, even when it says nothing changed. With neither, the state
  becomes *unknown* instead of keeping the commanded value forever. After a restart only a state
  the Tab had confirmed is restored, not the last command. A failed command now raises an error.
- **`media_enc` has three values: Automatic (new default), On, Off** (#4). Automatic follows the
  `media_enc` the plant declares in the long reply (`"srtp"` → SRTP); a plant with the short reply
  (40507 / 2F) stays on plain RTP. Entries saved as on by earlier versions stay on; off becomes
  Automatic, which on plants that do not declare it is the same thing.

### Added

- **Voicemail delay** `select` (#4): `vm_timeout`, with the values the plant itself declares in
  `vm_timeout_values`, written with `SET_APT_PARAMS` (`Panda: set`) and changed only when the reply
  says `ERR_NONE` (no reply or no `ERRCODE` is a failure, as in the VIEW app). The entity is
  created when the plant first sends the list, so it does not appear at all on plants with the
  short reply. `APT_PARAMS_CHANGED` from the Tab or the app updates it.
- The state sensor shows the apartment `GID`, `apt_names`, the declared `media_enc` and whether
  SRTP is in use, on plants that send them.

## [1.0.10] - 2026-09-28

Small follow-up to 1.0.9, from the first days of use on the 2F (Tab 7S 40507).

### Changed

- **"Rispondi" (Answer) is available only while the doorbell rings.** At rest the button is greyed
  out instead of accepting the press and logging `Answer failed: Nessuna chiamata in arrivo`. To
  look at the entrance panel, open the camera: answering is for a ring.
- The intercom card finds its entities by itself. Entity ids change from one installation to
  another (the device's area, renames), so the defaults `sensor.vimar_intercom_intercom_stato` and
  `lock.vimar_intercom_serratura` often did not exist and the card showed "offline". An entity
  written in the card config still wins when it exists; otherwise the card takes the
  integration's camera from the entity registry, and the state, last ring and lock from the
  camera's new `card_entities` attribute.

### Fixed

- The *Open door*, *Call*, *Answer* and actuator **buttons now raise an error when they fail**
  (the lock already did in 1.0.9): `button.press` used to "succeed" with the door still closed and
  only a line in the log ([#23](https://github.com/lollox80/ha-vimar-intercom/issues/23)).
- *Last opening*, *Last ring*, *Last caller*, *Last call duration* and *Last missed call* keep
  their value (and attributes) across a Home Assistant restart, until the next event replaces it.
  They used to go back to "Unknown". The counters since start-up are not restored.

### Added

- DEBUG log of the outgoing voice level every 2 s (`Voce verso la targa: picco N/32767`): the
  `tx=` packet count says packets leave, not that they carry a voice, so a muted or wrong
  microphone can now be told apart from a panel that does not play it.

### Fixed

- Ring photo: a panel calling for the first time since the HA restart that shipped the
  per-panel SPS/PPS cache (or after an update from the older, single-pair storage format)
  had nothing cached for it, and its first IDR often arrives without in-band SPS/PPS (the
  40515 sends those only every ~6 s): the photo failed with "anteprima video non arrivata"
  even though video was flowing. The old single-pair storage is now migrated instead of
  discarded, and a cache miss for the calling panel falls back to any other panel's cached
  SPS/PPS (the resolution rarely differs) instead of none. The ring photo also keeps waiting
  for a decodable frame for as long as the ring lasts, not just 6 s.

### Changed

- `/av` audio is AAC-LC (48 kHz mono, 32 kb/s) instead of the panel's raw G.711. In MPEG-TS
  PCMU ends up as private data (`bin_data`), so HA's stream worker (HLS, `camera.record`) and
  HomeKit had no audio. Transcoding and low-latency mux flags as in #21 by @m4r1k; 48 kHz
  instead of 24 because the muxer holds the first video packet until the first AAC frame
  (measured +10 ms vs +60 ms). Video is still copied.

### Removed

- The iOS push code, as in #21 by @m4r1k: `push_sender.py` (APNs VoIP over aiohttp, which
  speaks HTTP/1.1 while APNs needs HTTP/2, so it could never deliver), the
  `/api/vimar_intercom/push_token` view that stored device tokens inside the integration's
  folder, its call on ring and the `APNS_*` constants. With `APNS_KEY_ID` empty none of it ever
  ran; the "APNs push not configured" warning at every start is gone. The Vimar-cloud FCM
  parameters (`PN_*`, `connectProfiles`) are unchanged.

## [1.0.9] - 2026-09-28

Builds on 1.0.8. Field-tested on a Tab 5S Up 40515 (cloud TLS + SRTP).

### Added

- Video preview while the doorbell rings (early media): the ring gets `183 Session Progress` with
  our SDP instead of a bare `180 Ringing`, so the panel streams video before anyone answers, as
  the VIEW app's preview does. Snapshots and `/av` work during the ring; answering reuses the same
  SDP and SRTP keys. Not done during one of our own calls. A UDP retransmission of the same
  INVITE gets the same response instead of counting as a new ring; a ring whose CANCEL never
  arrives ends after 90 s; placing a call while it rings is refused ("Squillo in corso").
- Away message: options `away_message_file` (mp3, wav...) and `away_message_delay` (seconds,
  0 = off, max 60). If that ring is still ringing after the delay (nobody answered from the panel,
  a phone or HA), the file is decoded first (max 30 s; an unreadable file leaves it ringing), then
  the integration answers, plays it at real time (8 kHz, 20 ms RTP packets, SRTP included) and
  hangs up only its own call. `answer` during the message takes the call over.
- Ring snapshot: options `snapshot_dir` (folder, must be writable by HA; empty = off) and
  `snapshot_delay` (seconds after the ring, default 3 for the Tab 5S Up 40515). On every ring the
  integration saves the visitor's photo from the preview as `squillo_YYYYMMDD_HHMMSS_mmm.jpg` and
  `ultimo_squillo.jpg`.
- Dashboard card `custom:vimar-intercom-card`, loaded by the integration (no Resources entry):
  live video only during a ring or call (opening the card never calls the panel), **Vedi
  esterno** when idle, buttons that follow the call state, **Apri** with two taps, and two-way
  audio over `/api/vimar_intercom/audio_ws` (signed path, 8 kHz PCM, browser echo cancellation).
  The microphone needs HTTPS; over HTTP the card still answers (video only). Microphone and
  WebSocket close when the call ends or never starts.
- Card: one row without video (photo of the last ring, name, state, last ring, buttons); the 4:3
  video pane appears on ringing / calling / in call and goes away when idle (1.5 s after a
  hang-up, buttons off, so a second tap does not land on the card below). The buttons keep their
  slots. Option `layout`: `overlay` (default, the card is the video, buttons on a dark strip at
  the bottom) or `sotto` (video above the row, buttons under it). Visual editor (camera, name,
  layout, history); the card is "Citofono Vimar" in the card picker, with a preview. Errors
  replace the state line for a few seconds instead of a permanent line under the buttons.
- Card: low-latency video. Over HTTPS, with WebCodecs (Chrome, Edge, Firefox, Safari/iOS 16.4+),
  the card decodes the panel's H.264 NALs from `audio_ws` and paints them on a canvas: first
  frame about 0.1 s after the panel sends it (HA's stream took 2-4 s, past half of the ~10 s the
  40515 allows), without opening `/av`. A ring joined mid-GOP gets the current GOP replayed, so
  the first frame does not wait for the next IDR. No WebCodecs, or an unsupported codec: HA's
  stream as before. A decoder error or a dropped WebSocket does not fall back (HA's stream is
  blank off-LAN, no TURN): the decoder restarts at the next IDR, the WebSocket reopens and gets
  the current GOP again. Frames are dropped down to the next IDR when the decoder falls behind.
  RTP the panel still sends after our BYE is discarded until the next call (it used to reach the
  card and the replayed GOP); a call placed during the 1.5 s hold gets a fresh player; a card
  HA detaches and re-attaches during a call (view switch) shows the video again at once.
- Card option `anchor` (default `citofono`, empty = off): with `#citofono` in the URL (e.g. a
  notification opening `/lovelace/camera#citofono`) the card scrolls itself into view, also on a
  later hash change.
- Ring history: with `snapshot_dir` set, every ring is logged in `squillo.json` next to the photos
  (time, photo, caller, outcome; last 200), and the outcome becomes `answered` on `answer` or
  `away` when the away message picks up. New authenticated endpoints `GET /api/vimar_intercom/rings`
  (newest first, `?limit=` max 50) and `GET /api/vimar_intercom/rings/<name>` (only
  `squillo_YYYYMMDD_HHMMSS_mmm.jpg` inside `snapshot_dir`). The card shows the latest rings (option
  `history`, default 8, 0 = off) with photo, time and outcome; a tap opens the photo large. The
  ring photo is now named after the ring time, not the moment it was taken.
- "Vedi esterno" lasts as long as the panel allows (about 10 s on the Tab 5S Up 40515): when the
  panel hangs up the video ends and the card closes. To look again, press "Vedi esterno" again, as
  on the in-home monitor.
- Admin service `vimar_intercom.simulate_ring`: fires the doorbell event (and the automations
  on it) without the entrance panel. No SIP, push, WebSocket broadcast or statistics.

### Fixed

- Stills (`camera.snapshot`, notifications) come from a per-call frame grabber instead of the
  stream: one ffmpeg decodes the H.264 NALs from the first SPS of the call (or ring preview) and
  keeps the latest JPEG, so a snapshot is instant instead of waiting for the next IDR (>8 s at
  night on the 40515). They also work during the ring. No image and no call outside a call.
- `/av` clients share one ffmpeg: go2rtc (WebRTC) and HA's stream worker open it together, and
  each new client used to kill the previous one's ffmpeg. A slow client is dropped instead of
  corrupting the MPEG-TS for everyone. Two simultaneous opens place one call, not two.
- First decodable frame in about 1 s instead of the next in-band IDR (~8 s on the 40515, past
  the panel's hang-up): SPS/PPS are cached even with no WebSocket client and written to the AV
  SDP (`sprop-parameter-sets`), and a keyframe is requested when `/av` attaches.
- Local UDP mode: in-dialog requests (BYE, re-INVITE, INFO) from the panel's own address are
  accepted when the plant has no Record-Route: the source filter also allows the hosts of the
  current dialog (Contact/Via of the ring, Contact of the call). Before, HA stayed "in call" and
  the panel kept retransmitting its BYE.
- Auth retries are capped (at most 2 per INVITE or INFO, and only on `stale=true` or a new
  nonce): a retransmitted 407 no longer produces an INVITE storm. The proxy challenge is cached,
  so the keyframe INFOs are sent with `Proxy-Authorization` from the first one (no 407 for each
  of the 8 in the initial burst), and an authenticated resend waits for its own answer.
- Incoming INVITEs are keyed on (Call-ID, Via branch): the second branch of a forked ring (the
  cloud relay sends two INVITEs a few ms apart) gets `482 Loop Detected` on its own Via, and a
  CANCEL is matched on the branch too. The relay's CANCEL of its duplicate branch, sent ~70 ms
  after our 200 OK, no longer ends the call just answered. The 200 OK is not retransmitted and a
  missing ACK never turns into a BYE: over the cloud relay the ACK of our 200 never arrives.
- Frame grabber: the first IDR is kept as the photo until a later one replaces it (with an IDR
  every ~3 s, or >8 s at night, a ~10 s view could end with no photo); a re-INVITE with a new
  SDP restarts the grabber without clearing the photo already taken.
- The GOP cached for the `/av` replay is filled after the reorder filter: an old keyframe
  resent by the 40515 no longer resets it to "old IDR fragment + new P frames".
- `away_message_file` must be inside a folder HA may read (`is_allowed_path`, like
  `snapshot_dir`) and is passed to ffmpeg as `file:<path>`.
- `/av`: ffmpeg is spawned off the event loop and its stderr reader task is kept referenced.
- Card: the video WebSocket reopens with a backoff (1, 2, 4... s, max 10) and stops when the
  player is closed; the microphone is released if the card left the page while the browser asked
  for permission; a failed photo tap shows its error on the card.
- A single RTP packet lost on the cloud path (`FU-A seq gap ... gap=1, continuing` in the log)
  produced a NAL with a hole and smeared video on the card until the next IDR (~3 s). A gap
  inside an FU-A now drops that NAL, P slices are dropped for the WebSocket clients, the frame
  grabber and the GOP replay until the next IDR (the canvas freezes on the last good frame
  instead), and a keyframe is requested at once (at most one per second). `/av` still gets the
  raw RTP: ffmpeg does its own concealment.
- `/av` waits up to 25 s for the call (cloud call setup sometimes takes ~15 s), and a client
  that leaves early no longer leaves the viewer count, and so the auto-hangup, stuck.
- Card: each button keeps its place (call | voice | door), so "Riaggancia" no longer slides under
  the finger that meant "Parla". Failed services (`{ok: false}`) and a failed door opening (the
  lock now raises an error) show on the card; a double tap on Parla/Rispondi places one call; the
  microphone is released on failure; "Audio interrotto" when the audio channel drops; iOS/Safari
  audio works (AudioContext created in the tap); the second tap of Apri after the 3 s re-arm works.
- Speaking into the card's microphone takes the call: the away message no longer hangs it up and
  an auto-call is no longer hung up when the video closes. An INVITE during a call no longer turns
  the state into "ringing", and `answer` during a call is refused instead of breaking it.
- SIP: hanging up while the panel rings sends CANCEL (before, the call went up anyway on the late
  200 OK); a late or orphan 200 OK gets ACK + BYE, a retransmitted 200 OK of the current call gets
  its ACK again, the ACK of a 4xx/6xx uses the INVITE's branch, and a 45 s INVITE timeout sends
  CANCEL. re-INVITE and UPDATE inside the call get 200 OK instead of a new ring. A second INVITE
  while busy gets `486 Busy Here` (a 603 made the PBX cancel the call on the other phones too); a
  ring that times out gets `480`. Responses to other methods no longer reach the INVITE
  transaction. A retransmitted 407 is only ACKed.
- TLS: the reader no longer deadlocks on reconnect (the new REGISTER waited for responses only the
  reader itself could read: ~2.5 min deaf after every server close); it uses the same dispatcher
  as UDP; a negative Content-Length no longer freezes the event loop.
- Media: outgoing voice leaves in 20 ms packets from a pacer (the browser sent 43 ms bursts against
  our `a=ptime:20`); no voice during the ring preview or when the panel says `sendonly`; never
  plain RTP inside an SRTP session. Video: the reorder buffer no longer stalls on old or restarted
  sequence numbers, SRTP keeps rollover state per SSRC, RTP header extensions are skipped, and
  video reaches ffmpeg as PT 96 whatever the panel's payload type (black `/av` before).
- HTTP views follow the active config entry (after removing and re-adding the integration they
  answered 503 until HA restarted); WebSocket broadcasts no longer fail when a client connects
  mid-send. `fetch_local` has its service strings.
- A retransmitted INVITE after we answered gets the 200 OK again instead of silence (a lost 200
  over UDP used to end the call), and the 90 s ring timer is cancelled when the ring ends.
- Hanging up during a ring no longer stops its preview, so a later answer has audio and video. A
  malformed SDP in the ring no longer silences the doorbell (the preview is skipped instead). A
  second INVITE or a failed 200 OK no longer leaves the preview running.
- The ring photo folder can't be under `/config/www` (served on `/local` without login); disk
  errors are logged, and a reload during the delay cancels the photo.
- While the doorbell is ringing, opening the stream neither calls the panel (603) nor answers:
  a dashboard or wall tablet with the camera open would otherwise steal the ring from the
  in-home panel. Answer explicitly with the `answer` service.
- The `open_door` service default follows the lock's door target.
- A BYE for another dialog no longer ends the active call (481 instead of 200).
- Answering clears the pending ring before the first await, so two answers (timer + user) can't
  send two 200 OKs.
- `send_command` defaults to the configured SGA instead of a hardcoded 55001; a WebSocket panel
  switch can no longer leave SIP broadcasts muted if cancelled.
- A doorbell ring right after the panel ended a view call got `603 Decline`, which the PBX
  propagated, cancelling the ring on the Tab too. Only a call we are placing or holding now
  suppresses a ring.
- `/av` answers 503 as soon as nothing is coming (call refused, cancelled or ended) instead of
  spinning for 25 s and counting as a viewer. With the iOS app's WebSocket connected it still waits
  (up to 25 s) for the call the app places. The call end reaches HA without the 3 s wait for
  ffmpeg to quit.
- BYE is retransmitted over UDP like the other requests (a lost BYE left the panel busy, 486 on
  the next view), waits only for its own response (not the 200 of a keyframe INFO), and the call
  is closed locally even when the BYE can't be sent (TLS down): before, it stayed "in call" with
  the media on and every ring got 486. Crossed BYEs no longer tear down what came next. Declining
  a ring with the connection down ends the ring anyway.
- The INVITE is retransmitted over UDP (Timer A): a lost first INVITE made "Vedi esterno" wait
  45 s. Closing the camera while the call is still connecting cancels it after the usual delay
  (it stayed up, unwatched, until the 5-minute cap).
- An incoming call without SDP in the INVITE (late offer) takes the panel's answer from the ACK;
  before, the call was up with no audio or video.
- `/av` keeps one continuous RTP stream towards ffmpeg when the panel restarts its stream (new
  SSRC or sequence numbers): ffmpeg dropped every packet as "too late" and the video froze.
- One reconnection at a time: reader, keepalive and the WebSocket `reconnect` action each opened a
  TLS connection and closed the other's. Unloading the integration stops a pending reconnection.
- The cloud unreachable at startup (HA up before the router after a power cut) makes HA retry the
  setup by itself (`ConfigEntryNotReady`) instead of leaving the entry failed until a manual reload.
- Card: if the ring ends while the browser asks for the microphone after "Rispondi", the card
  says the ring is over instead of calling the panel. When the panel refuses a call the
  card closes the video pane and says so ("La targa non ha accettato, riprova.") instead of
  staying on "Collegamento…".
- Hanging up while the panel ends the call at the same moment (crossed BYEs) returns at once
  instead of waiting 5 s for a response that no longer comes.
- A malformed `squillo.json` (entries that are not objects) no longer breaks the ring list or the
  next ring's entry; the log is rewritten through a fresh temporary file (a symlink planted in the
  folder is not followed).
- Local UDP mode: the SDP of a 183/200 to our INVITE is held to the same LAN-only rule as a ring
  (a 200 pointing the media outside the LAN is ACKed and closed with BYE), and a late ACK no longer
  restarts the media of a call that has ended in the meantime.

### Removed

- The MJPEG path: the camera's stream override and the `/api/vimar_intercom/video` view. Both
  looped on `hub.video_frame` (always `None`) and never wrote a frame; the view also needed no
  login and placed a call to the panel. Also the unused `FFMPEG_VIDEO_PORT`, `do_door` (no
  callers), the UDP re-register every 3400 s (the 120 s keepalive already re-registers) and the
  constants `SEGRETERIA_TARGET`/`DND_TARGET`.

### Changed

- The camera no longer calls the panel by itself on a reconnection: go2rtc and HA's stream
  worker reopen `/av` as soon as a stream ends, and each reopen placed a new call (486 from the
  still-busy panel, then another). Now, for 60 s after any call ends (answered, cancelled or
  refused), after a failed auto-call or after a watched ring preview, a reopen of `/av` within
  5 s of the last viewer leaving places no call and gets 503 at once. Opening the camera later
  (dashboard, HomeKit) calls as before; `/av` still waits for the video when a call is up or
  being placed.
- H.264: the answer to a ring mirrors the panel's offer (payload type, `packetization-mode`,
  `profile-level-id`), and our own offer proposes both modes (96 mode 1, 97 mode 0). The 2F
  panel (baresip, Tab 7S 40507) offers and accepts only mode 0: answering mode 1 left it with no
  common format and no video at all. Video is still re-stamped to PT 96 towards ffmpeg.
- `packages/vimar_intercom.yaml`: the ring notification has "Rispondi" (answers and opens the
  camera) and "Apri" (device authentication on iOS only, and only within 3 minutes of the ring).
  The visitor's photo comes from the `snapshot_dir` option instead of an automation.

### Security

- Local UDP mode drops SIP packets that don't come from the intercom. Before, any host on the LAN
  could send an INVITE; with the ring preview that would also have started media towards it,
  saved its picture as the visitor's photo and played it the away message.
- `/audio_ws` debug actions (`command`, `probe`, `scan`, `register`, `reconnect`) are admin-only,
  and the received actions are logged at DEBUG instead of INFO.
- `send_command` (arbitrary SIP MESSAGE) and `fetch_local` (Digest with the SIP password towards a
  LAN host) are admin-only; automations still work. They bypassed the `/audio_ws` restriction.
- Every SIP id used for calls, the door, buttons and actuators must be numeric (`sip_uri`):
  `x@other.domain` no longer changes the request URI. `open_door` accepts only `OPEN` and `OPEN_*`
  commands (the `MSG` of the door actuator, or `OPEN_2F`).
- In local UDP mode, media from a ring goes only to LAN addresses: a spoofed INVITE could point
  the preview, the away message and the card's microphone at an outside address.

### Requirements

- Home Assistant **2024.7** or later (`hacs.json`): the card's static path needs
  `async_register_static_paths`.

## [1.0.8] - 2026-09-26

Two field reports from a **Tab 5S Up 40515** (2-wire, Wi-Fi, cloud TLS) by @Apeiv, in
[#3](https://github.com/lollox80/ha-vimar-intercom/issues/3) and
[#8](https://github.com/lollox80/ha-vimar-intercom/issues/8). The camera fix is his, as he
proposed and tested it.

### Fixed

- **The camera was always black** ([#8](https://github.com/lollox80/ha-vimar-intercom/issues/8)).
  Three causes, all fixed:
  - the camera forced MJPEG and did not declare `CameraEntityFeature.STREAM`, so Home Assistant
    never used `stream_source()`, while the MJPEG path read `hub.video_frame`, which is always
    `None`. The camera now declares `STREAM` and goes through HA's stream worker (HLS/WebRTC);
  - the ffmpeg behind `/api/vimar_intercom/av` could never start: ffmpeg opens RTP **and** RTCP
    (RTP + 1) for each `m=` line, and with ports 19201/19202 the video RTCP landed on the audio
    port (`bind failed: Address already in use`). The ports are now 19210/19212;
  - the reader of ffmpeg's stderr stopped as soon as the process had exited, which is exactly
    when stderr says why. It now reads to EOF, and a startup failure logs ffmpeg's last lines.
- **Calls went to the SGA** ([#3](https://github.com/lollox80/ha-vimar-intercom/issues/3)).
  *Call* and *Call Video (outdoor)* invited the SGA, which takes the state commands but does not
  necessarily accept a call (`488` on the development plant; `488`/`408` on the 40515, where the
  SGA is `61000`). They now call the video entrance panel, like the camera.
- **The door did not open on a 2FV2** (reported by @Apeiv on
  [#20](https://github.com/lollox80/ha-vimar-intercom/pull/20)). `OPEN_2F` went to the SGA; on his
  Tab 5S Up 40515 the SGA is `61000`, which answers `200` and does nothing. The command has to go
  to the entrance panel that owns the relay (`55001` there), which is where the VIEW app sends it:
  the `GID_PE` of the door actuator in the phonebook. The lock, the *Open Door* button, the
  `open_door` action without `target` and the actuators with target `AUTO` now use that panel. On
  plants where the SGA and the door panel are the same address (the development plant: `55001`)
  nothing changes. `VOICEMAIL;`, `DND;` and `GET_INIT_STATUS` still go to the SGA/PICG.
- The `open_door` action filled `target` with the SGA when it was omitted, and ignored `command`
  when `target` was missing. Both fixed.

### Added

- **`camera_target` option**: the video entrance panel called by the camera, *Call* and *Call Video
  (outdoor)*. Default `55100`. The phonebook import fills it in with the app's own rule:
  `PHONEBOOK.AUTO` of your apartment row, otherwise the first `PE`/`PE_EXT` row.
- **`internal_panel_target` option**: the target of *Call Home (indoor)*. Default `55002`. The
  phonebook does not say which one it is, so the import leaves it alone.
- **`door_target` option**: the entrance panel that receives the door command. Empty by default.
  The phonebook import (from file or downloaded from the intercom) fills it in with the `GID_PE`
  of the first door actuator and shows it in the summary. When it is empty: the door actuator
  already saved in the entry (entries that imported the phonebook with 1.0.7 have the actuators
  but not the option), otherwise the SGA, as before.

### Changed

- Thumbnails and `camera.snapshot` come from the stream **only during a call**. Outside a call
  there is no picture on purpose: producing one would mean calling the panel at every thumbnail
  refresh. A snapshot on the ring alone still writes nothing.
- **Opening the live view places the call.** A dashboard card with `camera_view: live` now calls
  the panel every time the dashboard is shown: use `camera_view: auto` (the example card in
  `docs/lovelace_example.yaml` does).
- `media_enc` (SRTP) description: the 40515 only accepts the call with SRTP **on**, the opposite of
  the development plant. It is per plant.


## [1.0.7] - 2026-09-21

Stability release from a full debug pass against the decompiled VIEW app, the SIP logs of
the reference plant and the MITM capture. Every fix below has a regression test.
It also ships the local phonebook download from #16.

### Added

- **The phonebook can now be downloaded from the intercom itself.** Options → *Download the
  phonebook from the intercom* fetches `rubrica.db` over the intercom's own local HTTP API and
  imports the actuators, with no file to extract by hand: no rooted phone, no WSA, no adb, no
  cloud token. Authentication is HTTP Digest with **the SIP credentials the integration already
  has** in the config entry — no Vimar account is involved, and nothing leaves the local network.
  The existing *Import actuators from rubrica.db* entry stays: it is the only route for systems
  that are reachable only through the cloud.
- **The intercom now tells us its own PICG.** The same step asks
  `get_info.php?action=nickname`, whose reply carries the `PICG` role and its extension, and
  offers it as `picg_target` in the confirmation screen. On systems where the intercom is
  reachable on the local network this answers
  [#10](https://github.com/lollox80/ha-vimar-intercom/issues/10): the address does not have to be
  guessed. Cloud-only systems still need the scan proposed in
  [#14](https://github.com/lollox80/ha-vimar-intercom/issues/14). `sga_target` still comes from the phonebook's
  `SYSTEM.MAGIC_APT_INTERCOM` — two distinct values from two distinct sources, as `runtime.py`
  always documented.
- New pure module `rest_client.py` (`requests`, already a requirement; no Home Assistant imports)
  with `get_status()`, `get_nicknames()`, `download_db()`, `probe()` and the matching parsers.
  `parse_status()` reads exactly the body of a `GET_INIT_STATUS_REPLY;`, so the SIP and HTTP
  routes share one parser. 38 new tests in `tests/test_rest_client.py`.

Three quirks of the intercom's HTTP server are handled in `rest_client.py`, and are worth knowing
before touching it: its Digest `nonce` arrives as the `repr()` of a Python `bytes` object
(`nonce="b'…'"`) and must be echoed verbatim; responses carry the illegal header
`Content-Encoding: none`, so the body is read with `decode_content=False`; and **an unknown
resource is answered with 401, never 404** — a failed download and wrong credentials are
indistinguishable, which is why `RestAuthError` says both.

### SIP transport

- **Requests over UDP were sent once and never retransmitted.** A single lost datagram
  turned into "REGISTER: no useful final response (0 responses)" and two minutes of
  "Not registered" — the pattern that showed up in the reference plant's log every one to
  three hours. `REGISTER` and `MESSAGE` now retransmit per RFC 3261 (Timer E: 0.5 s,
  doubling up to 4 s) until any response arrives, and stop on a provisional one. TLS is
  unchanged. The response queue is also created *before* sending, so an immediate answer
  is no longer discarded as stale.
- **The keyframe request could block Home Assistant's event loop for ~3 s.** When an
  unrelated response sat in the dialog's queue, the wait loop re-queued it and picked it
  up again without ever yielding (Python 3.12+). Unrelated responses are now set aside
  and put back after the loop.
- **`Content-Length` counted characters, not bytes.** Any body with an accented letter
  (a nickname, a room name) was sent with a wrong length. It is now the UTF-8 byte count.
- The `REGISTER` failure warning now lists the response codes, the target and the
  transport, so a log line says what actually happened.

### Incoming messages

- **`NEW_PHONEBOOK;<version>;<gid>` was read with the two fields swapped.** The
  "Phonebook version" sensor showed the GID, and the next `GET_INIT_STATUS_REPLY`
  "changed" it back, firing a second, spurious `phonebook_changed`. Order confirmed in the
  app's receiver.
- **`CALL_INFO` turned every `0` into `None`.** `MEDIA_TYPE 0` means audio, `REASON 0`
  means declined, `VIDEO_SRC 0` means the source can't be switched: all three were lost.

### Voicemail and Do Not Disturb switches

- **A failed command flipped the switch anyway** (timeout, 404, "Not registered"), and
  with no announcement from the intercom to correct it, the wrong state survived a
  restart. Only a successful send now moves the state, and only while the real state is
  unknown.
- While the intercom has never announced its state, the switches report
  `assumed_state`, so Home Assistant shows on/off buttons instead of presenting a guess
  as fact (#9).

### Logging

- **`logger.set_level` and `logger:` had no effect on Home Assistant's log.** The
  component's logger was pinned to `DEBUG` and forwarded only `WARNING` and above. The
  forwarding threshold now follows the level you set; with nothing set it is still
  `WARNING`. The internal buffer at `/api/vimar_intercom/debug` still gets everything.
- **Lines forwarded to Home Assistant's log were not redacted** — only the internal buffer
  was. Both now go through the same filter.
- **The redaction filter missed the forms the token actually takes:** `VALUE` before
  `PARAM` (as the 40507 sends it), JSON objects (`"token": "…"`), dict reprs, and an
  `Authorization` header inside a message logged with `%r`.
- Logging setup moved to a pure module, `log_buffer.py`, so it is tested without Home
  Assistant.

### Services

- **`send_command` with an empty `header_value` sent `Panda: None`.** The header is now
  added only when both name and value are present, and a CR/LF in either is rejected.

## [1.0.6] - 2026-09-19

The last of the audit findings: the registration state machine, and four ways an
unvalidated value reached a SIP message or the filesystem.

### Registration: three bugs that all reported success

- **Once the SIP registration was lost, it never came back.** The keepalive loop ran inside
  `if sip.registered:`, so the moment that flag went false the loop spun forever doing
  nothing. In local UDP — the default — there was no other recovery path: the intercom
  stayed disconnected until Home Assistant was restarted. The loop now calls
  `reconnect()` when it finds itself unregistered.
- **A failed `REGISTER` left `registered` at `True`.** Three exit paths returned `False`
  without touching the flag, so Home Assistant kept reporting the intercom as reachable
  while it wasn't — sometimes for an hour. Every negative exit now clears it, and says why
  in the log.
- **After a recovery, the plant state was never re-read.** Voicemail, DND and the phonebook
  version can change while Home Assistant is disconnected. Regaining the registration now
  clears `_init_status_sent`, so `GET_INIT_STATUS` is asked again instead of carrying on
  with values from before the outage.

### The bind fallback advertised a port nobody was listening on

`connect()` falls back to an ephemeral UDP port when the configured one is busy, but
`_my_port()` kept returning the configured value — and that value goes into `Via`, into the
`REGISTER` `Contact` and into the in-dialog contacts. Registration still succeeded, because
responses come back to the source port, so everything looked fine: the binary sensor was
green and "open door" worked. But the incoming `INVITE` was routed to a port where nothing
was listening, so **the doorbell never rang again, with no error anywhere**. `_my_port()`
now reads the port from the socket, and the fallback logs a warning instead of happening
silently.

### Input validation

New pure module `validate.py`, applied at the boundaries:

- **`/api/vimar_intercom/video?target=…`** went straight into the request line of an
  `INVITE`. An arbitrary value called an arbitrary address; a `CRLF` split the SIP message
  in two and injected headers. Now digits only, and `hub.sip_uri()` refuses newlines as a
  second net for the authenticated paths.
- **`fetch_local`'s `save_as`** went into `hass.config.path()` as given: a `../` wrote
  anywhere under the Home Assistant user. Now it is a filename, and the file is created in
  `/config/vimar_intercom/`.
- **`fetch_local`'s `host`** was arbitrary, and that request carries the SIP password in
  Digest — anyone able to call the service could have it sent to a server of their choosing.
  Now only literal private or loopback addresses; DNS names are refused on purpose, because
  resolving them here means trusting a resolution that can change between the check and the
  request. Redirects are no longer followed, and the scheme is restricted to http/https.
- **`_is_local_request` trusted `request.remote`.** Behind a reverse proxy or Remote UI that
  is the proxy's address, which made all of Internet "local". A declared proxy hop is now
  reason enough to refuse: the legitimate consumers of these endpoints talk to Home
  Assistant directly and never declare one.

### Cloud TLS is verified when it can be

`_create_ssl_context()` fell back to `CERT_NONE` whenever `vimar_rootca.pem` was missing —
which is every installation, since that file is not in the repo — silently. `connect()` now
tries with verification first (the bundled CA if present, the system trust store otherwise)
and only falls back to an unverified handshake if the certificate is what stopped it,
saying so in the log. Plants that worked before keep working; the ones whose certificate can
be verified now get an authenticated connection without configuring anything.

New tests in `tests/test_validate.py`, `tests/test_sip_transport.py` and
`tests/test_hub_keepalive.py`; 11 of them fail on 1.0.5. `_keepalive_loop` was split so the
per-tick logic is testable. Full suite: 164 passing.

## [1.0.5] - 2026-09-19

- **The lock and the "Apri Porta" button ignored the configured SGA.** `sga_target` has
  been configurable since 1.0.0 — from the options flow or imported from `rubrica.db` —
  and `const.py` claimed that every platform read it from `runtime`. That was not true:
  `lock.py` passed the literal `"55001"` and did not even import `runtime`, and `button.py`
  did the same for the door and call buttons.

  On the plant this was developed against, `SYSTEM.MAGIC_APT_INTERCOM` happens to be
  `55001`, so nothing looked wrong. On a plant where it differs, the switches and the
  actuators imported from the phonebook followed the configured address while **the lock
  entity — the one exposed to Apple Home — and the "Apri Porta" button kept sending
  `OPEN_2F` to 55001**. As far as we know that returns a bare `200 OK` with no effect,
  which `hub.async_door` counts as success: the lock showed *unlocked* for five seconds
  while the door stayed shut. Failing while reporting success is the worst of the options.

  Both now pass no target at all, so `hub.async_door` resolves it from
  `runtime.DOOR_ESTERNO` like every other path. The "Chiama Video (esterno)" button uses
  `runtime.SGA_TARGET`.

- The internal panel address used by "Chiama Casa (interno)" moved to
  `const.INTERNAL_PANEL_TARGET`. It is **not** configurable: there is no config entry field
  and no `rubrica.db` key to derive it from, and guessing it (SGA+1) is exactly the kind of
  assumption this project does not make. On a different plant that button will call an
  address that does not exist and the call will fail — no side effect, unlike the door.

`tests/test_no_hardcoded_plant_values.py` now fails if a plant address reappears as a
literal in `lock.py`, `button.py` or `switch.py`, and `tests/test_hub_stats.py` covers the
targetless `async_door()` path the two entities now rely on.

## [1.0.4] - 2026-09-19

Security fix. Anyone who paired with a QR code should update.

- **The SIP password was readable over HTTP by any logged-in Home Assistant user.**
  Three pieces, each harmless on its own. `qr_decoder` logged the decrypted pairing
  payload at DEBUG — and that payload contains `PWD=<your SIP password>`. The internal
  ring buffer raises the `vimar_intercom` logger to DEBUG unconditionally, so that line
  was captured whether or not you had configured `logger:`. And `/api/vimar_intercom/debug`
  serves that buffer to any authenticated user, guest accounts included.

  All three are now closed: the payload is never logged (only how many fields were found
  and their names, which is what actually helps diagnose a wrong QR), the debug endpoint
  requires an **administrator**, and every line entering the buffer passes through a new
  `log_redact` module that masks credential-shaped values — `pwd`/`password`/`ha1`/`token`
  assignments, `Authorization` and `Proxy-Authorization` headers, Digest `response=`
  fields, and the `{"PARAM":"token","VALUE":"…"}` form carried by
  `GET_INIT_STATUS_REPLY`. The masking is a safety net, not the rule: credentials must not
  be logged in the first place.

  If your Home Assistant has non-administrator users, or you have ever shared a debug
  dump, treat the SIP password as exposed and re-pair from the intercom panel to rotate it.

New tests in `tests/test_log_redact.py`, plus two in `tests/test_qr_decoder.py` that fail
if the password ever reaches a log record again.

## [1.0.3] - 2026-09-18

Three bugs in `hub.py`, all found by a code audit rather than in the field, and all of the
kind that fails without looking like a failure.

- **Video auto-start never worked.** Opening the camera stream is supposed to place a SIP
  call to the video entry panel. The URI was built from `sip.C.SIP_DOMAIN` — but `sip_client`
  imports `const as C`, and `SIP_DOMAIN` does not exist there: the active domain lives in
  `runtime`, because it differs between local UDP and cloud mode. Every auto-call therefore
  raised `AttributeError`, which the surrounding `except` turned into a single
  `Auto-call error:` line, so the stream simply showed nothing. `CAMERA_TARGET` was reached
  through the same wrong module. Both now read from where the value actually lives.
- **After a call ended, the doorbell stopped ringing.** `_auto_called` marks a call we
  placed ourselves, so that the INVITE the plant echoes back is not announced as a doorbell
  ring. It was cleared on six paths but not when the call ended — and the watchdog that
  would have cleared it requires `sip.in_call`, which is already `False` by then. From that
  point on every genuine ring matched the "we started this" test and was answered with
  `603 Decline`, until Home Assistant was restarted. It is now cleared on `call_ended`.
- **A failed `GET_INIT_STATUS` was never retried.** The flag marking the request as sent was
  set regardless of the outcome, which made the retry branch in the keepalive
  (`if not self._init_status_sent`) unreachable. One transient failure left `rubrica_ver`,
  `vm_ver`, `vm_level` and the real voicemail/DND states at `None` until a restart. The flag
  now follows the result of the send.

New regression tests in `tests/test_hub_autocall.py`, including a guard that fails if a
`SIP_DOMAIN` constant reappears in `const.py`.

Note for plants that answer `200 OK` to `GET_INIT_STATUS` but never send the reply
([#1](https://github.com/lollox80/ha-vimar-intercom/issues/1)): this release does not change
that behaviour — the send succeeds, so the retry is not triggered. That case is tracked
separately.

## [1.0.2] - 2026-09-17

- **Per-installation device identity.** `const.py` shipped `DEVICE_IMEI = "351234567890123"` (and `DEVICE_UUID = DEVICE_IMEI`), a constant every installation sent in the SIP `Mobile-IMEI` header and in the `+sip.instance` contact parameter. The Vimar cloud ties a registration — and its push routing — to the device identity, so two plants presenting the same one compete for the same registration. The identity is now generated once per installation by `runtime.new_device_identity()` (15-digit IMEI, random UUIDv4) and stored in the config entry; existing entries are migrated silently on the next start, and `runtime.configure()` falls back to a fresh ephemeral identity when none is stored, so no code path can reuse a shared value. `const.py`, `runtime.py`, `__init__.py`, `sip_client.py`. New tests in `tests/test_device_identity.py`, including a regression guard that fails if a hardcoded identity reappears in `const.py`.
- **The cloud SNI and `Route` header come from the config entry.** `const.SIP_SNI` and `const.SIP_ROUTE` were fixed to `ipvdes.vimar.cloud` even though the pairing QR carries the plant's own `cproxy` (already stored as `runtime.SIP_PROXY`). On an installation whose `cproxy` differs, the TLS handshake presented the wrong server name and the `Route` header pointed at the wrong proxy. Both now use `runtime.SIP_PROXY`, and the two constants are gone. `const.py`, `sip_client.py`.
- **Options flow keeps what you typed.** All three option steps rebuilt their form from the saved entry data, so a validation error on one field silently discarded every other edit made alongside it — the rubrica GID included. The redisplayed form now starts from the submitted values; the saved ones remain the fallback, and the change-detection used to decide whether to re-run the live SIP test still compares against what is actually stored. `config_flow.py`.
- Door statistics no longer record a hardcoded `"55001"` as the target when none is given: `SGA_TARGET` has been configurable since 1.0.0, and `hub.py` now reports the configured one. New guards in `tests/test_no_hardcoded_plant_values.py` fail if any of these plant-specific values reappear as constants.
- **`do_call()` no longer strands the `calling` flag.** The INVITE transaction cleaned up on each `return` path but had no `finally`: if `parse_sdp()` or `media.setup_media()` raised after the 200 OK, `pending_responses` kept the queue and `calling` stayed `True` forever — and the guard at the top of `do_call()` then refused every later call until Home Assistant was restarted. The response loop is now wrapped in `try`/`finally` that always pops the queue and clears `calling`. `sip_client.py`.

## [1.0.1] - 2026-09-17

- **Fixed cloud SIP registration on Tab 5S UP** ([#1](https://github.com/lollox80/ha-vimar-intercom/issues/1), reported by @gtarraran992): the pairing QR code carries two distinct SIP domains — `domain` (the intercom's local domain) and `cdomain` (the cloud one) — but the decoder only kept the first, and on some Tab 5S UP units that field is `127.0.0.1`. The result was `sip:<user>@127.0.0.1` URIs and cloud registration failing every time. `qr_decoder.extract_sip_credentials()` now keeps both domains (`local_domain`/`cloud_domain`) and picks the local one as the pairing-time default only when it is actually routable — loopback, `0.0.0.0` and `localhost` fall back to `cdomain` — while `runtime.configure()` selects the active domain based on `use_local_udp`, recomputing HA1 when the active mode's domain differs from the saved one. Config entries created by earlier versions have neither of the new keys and keep using `sip_domain` as before. `qr_decoder.py`, `runtime.py`. New tests in `tests/test_qr_domain_selection.py`.
- Added `CONTRIBUTING.md` and issue templates (bug report, hardware compatibility report).

## [1.0.0] - 2026-09-07

**Prima release pubblica** su `github.com/lollox80/ha-vimar-intercom` (HACS custom repository).
Le versioni precedenti (2.0.0–3.1.4, più sotto) sono state sviluppo privato su un singolo impianto,
mai distribuite pubblicamente: restano nel changelog come storico/riferimento "beta" pre-1.0.

- **`sga_target`/`picg_target` configurabili** (gap principale pre-pubblicazione): erano hardcoded a `"55001"` in giro per il codice, un valore verificato solo su questo impianto — impianti diversi possono avere un SGA/PICG diverso. Ora sono in `runtime.SGA_TARGET`/`runtime.PICG_TARGET`, popolati da `runtime.configure()` da `options["sga_target"]`/`["picg_target"]` con fallback al default storico in `const.py`. Due nuovi campi testo nello step "Impostazioni" dell'options flow (validati: solo cifre); l'importer rubrica.db, alla conferma, li imposta entrambi automaticamente dal valore rilevato (`SYSTEM.MAGIC_APT_INTERCOM`) invece di limitarsi a segnalarlo. Aggiornati anche gli usi hardcoded residui: `switch.py` (target segreteria/DND), `button.py` (sentinella target "AUTO"), `hub.py` (target di `GET_INIT_STATUS`), e i due servizi HA `send_command`/`open_door` in `__init__.py` (default dinamico invece di `"55001"` fisso). `CAMERA_TARGET` (targa video, `"55100"`) resta fuori scope. `runtime.py`, `hub.py`, `switch.py`, `button.py`, `__init__.py`, `config_flow.py`, `strings.json`, `translations/{it,en}.json`. Nuovi test in `tests/test_runtime.py`; `runtime` aggiunto a `PURE` in `tests/test_smoke.py`.
- **Importer `rubrica.db` nell'options flow**: nuovo menu nelle opzioni dell'integrazione con due voci, "Impostazioni di rete e attuatori" (il form esistente, ora sullo step `settings`) e "Importa attuatori da rubrica.db". Quest'ultimo fa caricare il file `rubrica.db` (selettore file nativo HA, `homeassistant.components.file_upload`), lo legge in sola lettura, estrae attuatori + parametri `SYSTEM` (stessa logica read-only di `tools/parse_rubrica.py`, isolata nel nuovo modulo puro `rubrica_import.py`) e mostra un riepilogo di conferma (numero/nome attuatori trovati, SGA rilevato, con avviso se diverso da quello attualmente in uso) prima di sostituire la lista attuatori salvata — senza più copiare a mano l'output JSON del tool. Nuova dipendenza manifest: `file_upload`. `config_flow.py`, `rubrica_import.py` (nuovo), `strings.json`, `translations/{it,en}.json`, `manifest.json`. Test dedicati in `tests/test_rubrica_import.py` (schema completo, filtro per GID, fallback senza `ACTUATOR_RULES`/`ICON_LIST`, file mancante/non-SQLite, sola lettura); aggiunto a `PURE` in `tests/test_smoke.py`.
- Fix logging: il buffer di debug interno (`_debug_log`) alzava il logger `custom_components.vimar_intercom` a DEBUG, e per propagazione ai logger Python questo scavalcava il livello WARNING impostato in `logger:` di HA. Ora il logger ha `propagate = False` e un handler dedicato inoltra al log HA solo WARNING+. `__init__.py`, `README.md`.
- Fix log "Stale response 407" sul keepalive SIP: `_send_options_ping` non registrava il proprio Call-ID tra le risposte attese, quindi la risposta del proxy al keepalive OPTIONS finiva loggata come WARNING anche se normale. `_dispatch_message` ora declassa a DEBUG le risposte con Call-ID `ping-*`. Con questa fix + quella sopra non serve più alcun filtro `logger:` in `configuration.yaml`. `sip_client.py`, `README.md`.

---

## Storico pre-1.0.0 — sviluppo privato, versioni "beta" mai pubblicate

> Numerazione interna usata durante lo sviluppo su un solo impianto privato, prima della release
> pubblica. Tenuta per riferimento/tracciabilità, non corrisponde a versioni mai distribuite.

## [3.1.4] - 2026-08-20

- Fix pipeline camera: forward RTP video->ffmpeg, conflitto porte AV risolto (lock + terminazione pulita, no piu 'Address in use'), SDP scritto in executor (no blocking I/O), keyframe INFO al peer reale (non 55001) con gestione 407. Probe: comando call ora riceve e conta i pacchetti RTP (audio/video) con NAT punch

## [3.1.3] - 2026-08-20

- Media in RTP chiaro di default (media_enc off): l'impianto non accetta SRTP; build_sdp offre RTP/AVP senza crypto, media_handler passa RTP non cifrato. Opzione media_enc (SRTP) in options flow. Comando sip_probe call per test chiamata/autoaccensione da terminale

## [3.1.2] - 2026-08-20

- Camera on-demand (autoaccensione): chiama la targa video 55100 (const.CAMERA_TARGET dalla rubrica) invece del PICG 55001 che dava 488 Not Acceptable Here

## [3.1.1] - 2026-08-20

- Fix riaggancia per chiamate in arrivo: il BYE ora punta al chiamante (era R.INTERCOM); card Lovelace citofono base in docs/lovelace_example.yaml (video + rispondi/apri/riaggancia)

## [3.1.0] - 2026-08-20

### Added
- **Stato iniziale via `GET_INIT_STATUS`**: dopo il REGISTER (avvio e reconnect) l'hub invia `GET_INIT_STATUS` (Panda: blue) al PICG (`const.PICG_TARGET = SGA_TARGET = 55001`). La risposta `GET_INIT_STATUS_REPLY;[{PARAM,VALUE}]` è parsata in modo generico e robusto (fallback a regex se il body arriva troncato) e popola `voicemail`, `dnd`, `vm_level`, `rubrica_ver`, `vm_ver` (e `init_status` grezzo per token/altri param). `hub.py`, `const.py`.
- Sensori: `sensor` **Spazio Segreteria** (`vm_level`), **Versione Rubrica** (`rubrica_ver`, attr `vm_ver`), **Ultima Chiamata Persa** (`missed_call_count`, `sip_id`, `ts`). `sensor.py`.
- `binary_sensor` **Nuovo Videomessaggio** (ON su `VM;VIDEO_MESSAGE_CHANGE;NEW`). `binary_sensor.py`.
- Entità `event` **Fuoriporta** (event_type `fuoriporta`) su `FP;{...}`. `event.py`.
- Eventi bus HA in ingresso: `vimar_intercom_missed_call` `{sip_id, ts, name}`, `vimar_intercom_videomessage` `{change, extra, full}`, `vimar_intercom_fuoriporta` `{sip_id, msg}`, `vimar_intercom_call_info` `{sip_id, reason, media_type, video_src}`, `vimar_intercom_phonebook_changed` `{gid, rubrica_ver}` (emesso al cambio di `rubrica_ver` e su `NEW_PHONEBOOK;<gid>;<ver>`). Costanti `EVENT_*` in `const.py`, fire in `__init__.py` via callback dell'hub. Parsing solo in lettura (difensivo JSON/`;`, nessun comando in uscita).
- Test parser `GET_INIT_STATUS_REPLY` (completo, reale, troncato) ed eventi `MISSED_CALL`/`VM;VIDEO_MESSAGE_CHANGE`/`FP`/`CALL_INFO`/`NEW_PHONEBOOK`/`phonebook_changed`. `tests/test_hub_stats.py`.
- Stato iniziale via GET_INIT_STATUS->55001 (segreteria/DND all'avvio, sensori vm_level e rubrica_ver); eventi in arrivo: chiamata persa, videomessaggio, fuoriporta, call_info, phonebook_changed; fix troncamento body MESSAGE (200->4096) per non perdere dnd/voicemail

## [3.0.1] - 2026-08-19

- Fix form opzioni: rimosse graffe ICU nella descrizione attuatori (INVALID_ARGUMENT_TYPE); il test SIP live nelle opzioni ora si esegue solo se cambiano i parametri SIP (non blocca il salvataggio dei soli attuatori)

## [3.0.0] - 2026-08-19

 — in preparazione 3.0.0 (2026-08-19)
### Added
- Attuatori dinamici come bottoni: letti da `options["actuators"]` (lista JSON `{name, msg, target, icon}` prodotta da `tools/parse_rubrica.py`), inviati con `Panda: command`. `button.py` (`VimarActuatorButton`), `runtime.py` (`R.ACTUATORS`), `config_flow.py` (campo/validazione options), `strings.json`+traduzioni.
- `tools/parse_rubrica.py`: legge `rubrica.db` (SQLite, read-only) e ne estrae attuatori JSON per HA + SGA (`SYSTEM.MAGIC_APT_INTERCOM`) + parametri `SYSTEM`.
- `tools/sip_probe.py`: opzione `--cloud` (TLS via SRV `_sips._tcp`, SNI `ipvdes.vimar.cloud`, porta 5070), `VIMAR_SIP_HA1` (usa l'`ha1` senza password in chiaro), `--imei`/`--myname` per test di impersonazione.
- `docs/RUBRICA.md`: schema completo di `rubrica.db` (23 tabelle), metodo di estrazione via root (Samsung A32) e flusso `rubrica.db` → opzioni HA.
### Fixed
- **SGA reale = 55001** (`const.py`): `VOICEMAIL;ON/OFF` e `DND;ON/OFF` (Panda: blue) verso `SYSTEM.MAGIC_APT_INTERCOM = 55001` **accendono/spengono davvero** segreteria e non disturbare sul Tab (verificato sul campo). I precedenti target 55002/61000/60002/101 davano 200 senza effetto.
### Changed
- `README.md` e `ARCHITECTURE.md` riscritti in modo veritiero: stack SIP custom che emula VIEW (header `Panda`, UDP locale/TLS cloud), camera on-demand, **nessun RTSP**. Rimossi i riferimenti errati all'evento bus `vimar_intercom_campanello` (il campanello è un'entità `event`). Aggiornati `docs/STATE.md`, `docs/PROTOCOL.md`, `docs/ROADMAP.md`.
- SGA 55001 (segreteria/DND funzionanti), attuatori dinamici da rubrica, parse_rubrica, sip_probe --cloud/--ha1, docs veritieri

## [2.9.4] - 2026-08-19
### Fixed
- SRTP migrato ad AES-CTR di pycryptodome: rimossa la dipendenza non dichiarata da cryptography (srtp.py la importava senza che fosse nei requirements del manifest). Aggiunti test con i vettori ufficiali RFC 3711 B.3 che verificano keystream e KDF byte per byte.

## [2.9.3] - 2026-08-18
### Changed
- `const.py`: `ACTUATORS = []` — rimossi i token ipotizzati OPEN_F1/OPEN_2 (aprivano la porta). ADR-4.
- Commenti/documentazione dei comandi Segreteria/DND (Panda: blue, SGA).

## [2.9.2] - 2026-08-18
### Fixed
- Modalità cloud: risoluzione SRV `_sips._tcp.ipvdes.vimar.cloud` → `flexiprod{1,2,3}.ipvdes2.vimarsso.cloud:7042`, TLS con SNI. ADR-5.

## [2.9.0] - 2026-08-17
### Added
- Servizio `vimar_intercom.fetch_local` (GET HTTP Digest verso il proxy locale) per tentare il download di rubrica/mailbox.

## [2.8.0] - 2026-08-17
### Security
- Hardening endpoint HTTP: `/audio_ws`, `/push_token`, `/debug` autenticati; `/video`, `/av` solo da LAN (`_is_local_request`). ADR-3.

## [2.2.0 – 2.7.0] - 2026-08-16/17
### Added
- Switch Segreteria e Non disturbare (stato letto dagli annunci `VOICEMAIL;`/`DND;` da 55002).
- Rilevamento modello da header SIP (`model_detect.py`), options flow UDP locale / cloud, statistiche estese.

## [2.1.0] - 2026-08-16
### Added
- Sensori estesi (stato, ultimo chiamante, contatori, durata, ultima apertura, ultimo comando, ultimo messaggio).
- Servizi `send_command`, `call`, `answer`, `hangup`, `open_door`.

## [2.0.0] - 2026-08-15
- Fork di noiseheroes-lab/ha-custom-components `vimar_intercom`; adattamento a Tab 7S 2F+ WiFi (40507), UDP locale.
