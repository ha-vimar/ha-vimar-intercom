# Entities, services and automations

🇮🇹 *[Italiano](ENTITIES.it.md)* · [← README](../README.md)

## Entities

| Entity | Platform | Key | Description |
|---|---|---|---|
| Vimar Intercom (the device's name) | `camera` | `camera` | **On-demand** video (stream): opening it places the SIP call (not on a reconnection within 5 s of the last viewer leaving, for 60 s after the end of a call: `/av` answers 503; `/av` also answers 503 at once when the call it was waiting for is refused or ends, unless the iOS app's WebSocket is connected: then it waits up to 25 s for the app's call); during a ring it shows the preview without answering. Snapshots are instant during a call or ring (clean keyframes), none otherwise, so thumbnails never ring the panel |
| Doorbell | `event` | `doorbell` | `event` entity (device class DOORBELL), event type `ring`, fired on ring (incoming INVITE) |
| Apartment door bell | `event` | `fuoriporta` | Event type `fuoriporta`, fired on the Tab's `FP` MESSAGE (the doorbell at the apartment's own door, *fuoriporta*), with the caller's `sip_id` and `msg`. Known from the app's code, not yet seen on a real plant |
| Door lock | `lock` | `lock` | Opens the door: the command of the phonebook's door actuator (`OPEN_2F` if there is none) → `door_target`; auto-relocks after 5 s (there is no physical feedback) |
| Call | `button` | `call` | SIP call to the default outdoor unit |
| Call the entrance panel (video) / Call the indoor monitor | `button` | `call_ext` / `call_int` | Call to `camera_target` / `internal_panel_target` |
| Answer / Hang up | `button` | `answer` / `hangup` | Answer (200 OK) / end the call (BYE) |
| Decline | `button` | `decline` | Only while it rings: refuses the call with `603 Decline`, so the whole house stops ringing, as in the app. Also the `vimar_intercom.decline` service |
| Open door | `button` | `door_street` | Same as the lock: the phonebook's door command (else `OPEN_2F`) to `door_target` |
| Test ring | `button` (diagnostic) | `test_ring` | A whole ring without the panel, as `vimar_intercom.simulate_ring` with its default 20 s: try your ring automations and notifications. Fails while a real call or ring is in progress |
| *Dynamic actuators* | `button` | `act_<name>_<command>` | One per entry in `options["actuators"]` (F1/F2, stair lights, relays…); sends `MSG` with `Panda: command` |
| Voicemail | `switch` | `segreteria` | `VOICEMAIL;ON/OFF` (Panda: blue) to the SGA; state read from the Tab's announcements and from `GET_INIT_STATUS`, asked after every command. The commanded value is shown for 10 s at most: with no confirmation the state becomes *unknown* ([#9](https://github.com/ha-vimar/ha-vimar-intercom/issues/9)) |
| Do not disturb | `switch` | `dnd` | `DND;ON/OFF` (Panda: blue) to the SGA; same rules as Voicemail |
| Voicemail delay | `select` | `vm_timeout` | Only on plants that send the long `GET_INIT_STATUS` reply: `vm_timeout`, one of the plant's own `vm_timeout_values`, written with `SET_APT_PARAMS` ([#4](https://github.com/ha-vimar/ha-vimar-intercom/issues/4)). It does not appear on plants with the short reply |
| Voicemail audio file | `select` (config) | `away_file` | Audio file of the away message, picked among those in `<media>/citofono/messaggi` (upload them from Media → Local media; the list is read again every minute), or *None (use the text)*. Admins only |
| Voicemail message text | `text` (config) | `away_text` | Text of the away message, read by text-to-speech when no audio file is set (max 255 characters). Admins only |
| SIP registration | `binary_sensor` | `sip_registered` | SIP registration active (connectivity) |
| In call | `binary_sensor` | `in_call` | A call is up |
| Ringing | `binary_sensor` | `ringing` | ON while an outdoor unit is calling (attribute: caller) |
| Outgoing call | `binary_sensor` | `calling` | ON while Home Assistant is calling |
| New video message | `binary_sensor` | `new_videomessage` | ON when the Tab announces a new video message (`VM;VIDEO_MESSAGE_CHANGE;NEW`), OFF on the next change that isn't new (attributes: the last announcement, the video message space) |
| Devices | `sensor` | `devices` | Number of devices seen on the plant (phones sharing the SIP account, panels); attribute `dispositivi` lists them with the identifier masked and no address, kept across restarts. Every Home Assistant user can read the attribute, device names included (a phone's name is often its owner's) |
| Status | `sensor` (enum) | `status` | offline / idle / ringing / in_call / calling (plus network attributes, and on plants with the long reply the apartment `GID`, `apt_names` and the declared `media_enc`) |
| Last caller | `sensor` | `last_caller` | Outdoor unit or monitor of the last ring |
| Last ring | `sensor` (timestamp) | `last_ring` | Time of the last ring (attributes: caller, `foto`/`foto_url` and `clip`/`clip_url` with `snapshot_dir`) |
| Rings | `sensor` (counter) | `ring_count` | Rings since startup |
| Calls | `sensor` (counter) | `call_count` | Connected calls |
| Last call duration | `sensor` (s) | `last_call_duration` | Duration of the last call |
| Last door opening | `sensor` (timestamp) | `last_door` | Last door opening (attributes: unit, outcome as in `open_door`'s `result`, SIP code, counter) |
| Last command | `sensor` | `last_command` | Outcome of the last `send_command` |
| Last message received | `sensor` | `last_message_in` | Last system SIP MESSAGE (`Panda: blue`) from the intercom; other messages are ignored |
| Voicemail storage | `sensor` | `vm_level` | Video message storage used/total, from `GET_INIT_STATUS` |
| Phonebook version | `sensor` | `rubrica_ver` | Version of the Tab's phonebook (attribute `vm_ver`): when it changes, import the actuators again |
| Last missed call | `sensor` | `last_missed_call` | Name (or SIP id) of the last missed call the Tab announced, kept across restarts (attributes: `sip_id`, `ts`, total) |

Names follow the language of Home Assistant (Settings → System → General) and start with the
device's name, "Vimar Intercom": *Vimar Intercom Status* in English, *Vimar Intercom Stato* in
Italian. The entity_ids do not depend on the language: a new install gets the ones listed in
these pages and in the examples (for instance `sensor.vimar_intercom_intercom_stato`), and an
existing install keeps its own. A name or entity_id you changed in Home Assistant stays as you set it.


---

## Services (`services.yaml`)

| Service | Description | Fields |
|---|---|---|
| `vimar_intercom.send_command` | Arbitrary SIP MESSAGE (for testing). Admins and automations only. Response: `ok`, `result`; a `202` from the cloud relay is `ok: false`, `result: queued` (not delivered yet) | `body`, `target`, `header_name`, `header_value` |
| `vimar_intercom.call` | SIP call to an outdoor unit or monitor | `target` |
| `vimar_intercom.answer` | Answers the incoming call | — |
| `vimar_intercom.hangup` | Ends the active call | — |
| `vimar_intercom.decline` | Only while it rings: refuses the incoming call with `603 Decline`, so the whole house stops ringing, as in the app | — |
| `vimar_intercom.open_door` | Door open command. Without `command`: the body of the phonebook's door actuator for that panel, else `OPEN_2F`; a given `command` must be `OPEN` / `OPEN_*`. Without `target` it goes to `door_target`. Response: `ok`, `result` (`opened`, `busy`, `queued`, `unconfirmed`, `not_registered`, `timeout`, `error`, `send_failed`) and the SIP `code` | `target`, `command` |
| `vimar_intercom.fetch_local` | HTTP Digest GET against the Tab's local interface (home mode). Admins and automations only | `path`, `save_as`, `host`, `scheme` |
| `vimar_intercom.find_sga` | Finds the PICG by probing a range of addresses ([#14](https://github.com/ha-vimar/ha-vimar-intercom/issues/14)). Admins and automations only | `start`, `end`, `targets`, `probe`, `delay`, `reply_wait`, `sip_timeout`, `apply`, `apply_sga` |
| `vimar_intercom.simulate_ring` | Test ring (admin): a whole ring without the panel and with no SIP traffic. The state goes to ringing (card, sensors), the doorbell event and the start webhook fire, and after `duration` seconds it ends like an unanswered ring (end webhook). It cannot be answered, the away message ignores it, a real ring replaces it, and it stays out of the ring log | `duration` (1 to 90 s, default 20) |

Example (Developer tools → Actions):

```yaml
action: vimar_intercom.send_command
data:
  body: OPEN_2F
  target: "55001"
  header_name: Panda
  header_value: command
```

**Finding the SGA/PICG without the phonebook** (1.0.13, for cloud-only plants where neither the LAN nor
the cloud download works): `find_sga` probes one address at a time (default `55000`–`55010`, at most 50)
and stops at the first `GET_NICKS_REPLY`; the entry with role `PICG` is the answer. It changes nothing
unless you set `apply` (writes `picg_target`) and/or `apply_sga` (also writes `sga_target`); the
integration then reloads.

```yaml
action: vimar_intercom.find_sga
data:
  start: "55000"
  end: "55010"
response_variable: result
```

If a plant leaves `GET_NICKS` without any SIP answer (reported on a 40515 in cloud mode: `Timeout` where
`GET_INIT_STATUS` got `200`), use `probe: get_init_status`. It gives the three clean outcomes, and the
address whose probe triggers the `GET_INIT_STATUS_REPLY` is the PICG, but sent to the real SGA it makes
the VIEW app show "Configurazione appartamento modificata" every time. `sip_timeout` (default 8 s)
bounds how long each probe waits for its SIP answer; over the cloud relay use 20, because its `202` can
take ~15 s. The response lists every probe with its outcome: `absent` (404), `exists` (accepted, no
reply), `queued` (`202`: the cloud relay accepted the message but no device took it, so nothing at that
address can answer over the cloud), `replied`, `no_response`, `error`; plus `picg` and the nicknames the
intercom declared.


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

[`packages/vimar_intercom.yaml`](../packages/vimar_intercom.yaml) (copy it into `config/packages/`) contains a
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

### Clear the ring notification on the other phones

With the ring notification on several phones, the others keep showing it after someone answers. Send it
with a fixed `tag` and clear it by that tag when the *Intercom Ringing* binary sensor goes off: answered
(from the card, a phone or the Tab), declined, or the ring is over.

```yaml
automation:
  - alias: "Intercom - Ring → notification"
    trigger:
      - platform: state
        entity_id: event.vimar_intercom_doorbell
    action:
      - action: notify.ALL_PHONES  # a notify group, or one action per phone
        data:
          title: "🔔 Someone at the door"
          message: "Ring at {{ now().strftime('%H:%M:%S') }}."
          data:
            tag: intercom-ring
            image: "/api/camera_proxy/camera.vimar_intercom_intercom"

  - alias: "Intercom - Ring over → clear the notification"
    trigger:
      - platform: state
        entity_id: binary_sensor.vimar_intercom_intercom_ringing  # your Intercom Ringing sensor
        from: "on"
        to: "off"
    action:
      - action: notify.ALL_PHONES
        data:
          message: clear_notification
          data:
            tag: intercom-ring
```

`tag` and `clear_notification` are features of the Home Assistant Companion app (Android and iOS). The
binary sensor's entity id depends on the language Home Assistant had when the integration was added: check it
on the device page. Try it with the *Test ring* button.

`camera.snapshot` works during a call or a ring. For a photo of every visitor you don't need an
automation: set **Ring snapshot folder** in the options. Test your automations with
`vimar_intercom.simulate_ring`. Don't point a `camera: platform: ffmpeg` at `/api/vimar_intercom/av`:
that hangs Home Assistant until the ffmpeg probe times out.

**Scrypted (Alexa chime, Echo Show), go2rtc, Frigate**: use
`/api/vimar_intercom/av?autocall=0&idle=image`, a continuous stream that never calls the panel
(standby frame while idle, live video during rings and calls; `?autocall=0` alone answers 503
while idle instead), and forward the doorbell `event` with an automation. Setup in
[[EXTERNAL.md](EXTERNAL.md)](EXTERNAL.md).

[lovelace_example.yaml](lovelace_example.yaml) has a basic Lovelace card with the answer / open door / hang up buttons.
The video pane shows live video while a call or a ring is up. For voice, use [the intercom card](CARD.md).
