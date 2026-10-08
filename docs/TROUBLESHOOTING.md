# Troubleshooting

🇮🇹 *[Italiano](TROUBLESHOOTING.it.md)* · [← README](../README.md)

## By symptom

First check the version (Settings → Devices & services → Vimar Intercom): many of these are fixed in a
release, given in the *What to do* column. Then turn on the debug log (see [Logging](#logging)) and
reproduce the problem once. Not listed here? Open an issue with that log.

| Symptom | Likely cause | What to do | Issue |
|---|---|---|---|
| Setup: "Invalid or unrecognized QR code" | What was pasted is not the text of the pairing QR code: a photo, a link, a hand-typed `ID=…` line. The text is one long encoded string | Read the code with a QR scanner app and paste exactly what it shows. The reason is after "On error:" in the form | — |
| Setup: "SIP registration failed" on a cloud plant (Tab 5S Up 40515); the log shows a REGISTER to `…@127.0.0.1` | The QR carries `domain=127.0.0.1`; the account domain is `cdomain` | 1.0.1 or newer, set up again from the QR. With manual credentials use the `cdomain`, not `127.0.0.1` nor the cloud proxy | [#1](https://github.com/ha-vimar/ha-vimar-intercom/issues/1) |
| Voicemail or do-not-disturb answer `200 OK` but nothing changes; the status request gets no reply | Commands sent to the wrong SGA/PICG. `55001` exists on many plants but is not always the SGA | Get the phonebook ([PHONEBOOK.md](PHONEBOOK.md)), or set SGA/PICG by hand; to find them, the `find_sga` service | [#10](https://github.com/ha-vimar/ha-vimar-intercom/issues/10), [#14](https://github.com/ha-vimar/ha-vimar-intercom/issues/14) |
| The VIEW app shows "Configurazione appartamento modificata" | Every `GET_INIT_STATUS` sent to the real SGA raises it (seen twice; why is not known) | In `find_sga` keep the default probe (`get_nicks`), which raises no notification on the reference plant | [#10](https://github.com/ha-vimar/ha-vimar-intercom/issues/10), [#14](https://github.com/ha-vimar/ha-vimar-intercom/issues/14) |
| The door command is accepted but the door does not open | Sent to the wrong panel or with the wrong command. On a 2FV2 the door is opened by the panel in the door actuator's row (`GID_PE`), not by the SGA | 1.0.19 or newer (the door uses the plant's own command); import the phonebook, or set *Entrance panel that opens the door* | [#10](https://github.com/ha-vimar/ha-vimar-intercom/issues/10), [#58](https://github.com/ha-vimar/ha-vimar-intercom/issues/58) |
| `queued` / `202 Accepted` in the logs or in the *Intercom Ultimo Comando* sensor | The cloud relay accepted the command but did not confirm delivery; its answer can take ~15 s | 1.0.20 or newer: a 202 is reported as `queued`, not as done, and cloud commands wait 20 s. Check whether it happened before retrying | [#120](https://github.com/ha-vimar/ha-vimar-intercom/pull/120), [#139](https://github.com/ha-vimar/ha-vimar-intercom/pull/139), [#140](https://github.com/ha-vimar/ha-vimar-intercom/issues/140) |
| Door and actuators say "Not registered" for hours after a network drop (cloud) | Two reconnection loops ran at the same time | 1.0.10 or newer | [#23](https://github.com/ha-vimar/ha-vimar-intercom/issues/23) |
| Camera: `404` from `55100`, no video | The default video panel does not exist on the plant | Import the phonebook (it sets *Video entrance panel*) or set it by hand. Since 1.0.15 an empty field uses the panel that last rang with video | [#3](https://github.com/ha-vimar/ha-vimar-intercom/issues/3), [#129](https://github.com/ha-vimar/ha-vimar-intercom/issues/129) |
| Video smears, freezes or lags, more since 1.0.19; the debug log shows lost video packets (`persa`) | The panel sends 2 Mbit/s through a cloud relay that loses a few packets in a hundred: ten times the packets, ten times the damaged frames (measured on a Tab 7S Up 40517) | Settings → *Video quality from the panel*: *Automatic* (256 kbit/s over the cloud) or 256 kbit/s. Back to 2 Mbit/s if the picture was fine there and you want it sharper | [#161](https://github.com/ha-vimar/ha-vimar-intercom/pull/161) |
| Ring clip saved but no ring photo (Tab 7S 40517, cloud) | Under investigation: the photo waited for a second keyframe and this panel may send only one | 1.0.20 or newer (photo from the first keyframe, decoder logged at debug). Still missing: add the debug log of one ring to the issue | [#129](https://github.com/ha-vimar/ha-vimar-intercom/issues/129) (open) |
| No preview and no photo while it rings, only after answering | The plant sends no early media | Known limitation, nothing to fix on the HA side | — |
| "AV stream: call not established after 25s" right after hanging up (local UDP) | The panel was still busy with the previous call | 1.0.17 or newer | [#41](https://github.com/ha-vimar/ha-vimar-intercom/issues/41) |
| An open dashboard calls the panel again ~10 s after hanging up | The 5 s guard moved at every refused reconnection | 1.0.19 or newer | [#57](https://github.com/ha-vimar/ha-vimar-intercom/issues/57) |
| An unanswered ring keeps ringing in HA for up to 90 s | The panel stops after ~30 s without sending a CANCEL | 1.0.19 or newer | [#60](https://github.com/ha-vimar/ha-vimar-intercom/issues/60) |
| The microphone does nothing | HA's camera player has no microphone; the browser only allows it over HTTPS | Talk from `custom:vimar-intercom-card`, opened over HTTPS | — |
| A phonebook download fails (from the intercom or from the cloud) | The Tab does not answer HTTP on the LAN (40515), or the plant sends no cloud token (40507) | Follow the tree in [PHONEBOOK.md](PHONEBOOK.md) | [#5](https://github.com/ha-vimar/ha-vimar-intercom/issues/5) |

## Known limitations

- **Two-way audio only in the intercom card**: HA's own camera player has no microphone, so
  answering from a button, a notification or Alexa picks up the call silently. Talk from
  `custom:vimar-intercom-card`, over HTTPS.
- **Ring preview** needs the plant to send early media (verified on a Tab 5S Up 40515 over the
  cloud). If it doesn't, the preview and the ring photo stay empty until someone answers.
- **Cloud-only plants** (e.g. Tab 5S Up 40515): the Tab answers `503 You're not allowed` to any SIP
  request on the LAN, so local UDP mode can't work there; use cloud TLS. The Tab's local HTTP interface (port 80) refuses the connection on the 40515s reported so far, so there is no phonebook to read over the LAN. Camera, actuators, door opening and the state commands still work over SIP.
- **Voicemail / DND**: these are commanded through the **SGA** (`SYSTEM.MAGIC_APT_INTERCOM` in the
  phonebook, `55001` on the plant used for development). Sent to any other address they are silently
  ignored, so getting the SGA right is what makes them work — set it in the options or let the
  `rubrica.db` import fill it in.
- **Cloud phonebook**: needs a `token`. Plants that answer `GET_INIT_STATUS` with the long form hand it
  over directly, and the phonebook can then be downloaded with a single authenticated request — see
  [RUBRICA.md](RUBRICA.md) §0-bis, verified on a 40515. Since 1.0.12 the options menu does it:
  **"Download the phonebook from the Vimar cloud"** ([#5](https://github.com/ha-vimar/ha-vimar-intercom/issues/5)); the token is read from the
  plant each time and never stored. Plants that answer with the short form (including the development
  one) don't carry a token: there use the download from the intercom on the LAN, or the manual extraction.
- **By-me actuators** (e.g. stair lights on By-me home automation): these may not respond over SIP even
  when they are listed in the phonebook.
- **Lock**: no physical state feedback (optimistic auto-relock after 5 s).
- **A ring during one of our own calls**: while Home Assistant is calling the panel or is in a
  call, an incoming INVITE gets `486 Busy Here` and fires no doorbell event: on the field it can't
  yet be told apart from the PBX echoing our own call. A ring right after the panel's BYE is a
  normal ring.
- **Phonebook**: which way works depends on the plant (intercom on the LAN, cloud token, or a
  `rubrica.db` file): see the decision tree in [PHONEBOOK.md](PHONEBOOK.md). The cloud token is not
  an account setting: it comes in the plant's long `GET_INIT_STATUS` reply, which not every plant sends.


---

## Logging

The component keeps its own circular buffer (`log_buffer.py`, the last 3000 lines, `DEBUG`
included), readable by administrators at `/api/vimar_intercom/debug?lines=N` (100 lines by default).

The Home Assistant log receives the component's records from the level set for
`custom_components.vimar_intercom` under `logger:` in `configuration.yaml` (or with the
`logger.set_level` service), and `WARNING` and above when no level is set. To see everything there:

```yaml
logger:
  logs:
    custom_components.vimar_intercom: debug
```

Both destinations mask passwords, digest responses, phonebook tokens and SRTP keys before writing.
**(1.0.21+)** They also mask the plant's data (1.0.20 does not yet)
([#157](https://github.com/ha-vimar/ha-vimar-intercom/pull/157),
[#159](https://github.com/ha-vimar/ha-vimar-intercom/pull/159)), so a log needs far less cleaning before it goes into an issue:

- **IP addresses**: private ones everywhere, public ones where they can only be yours or a phone's (`received=`, the
  host of a SIP URI, the hop of a `Via`). They keep the first and last number: `192.x.x.23`.
- **SIP ids**: Home Assistant's own and the other phones' and devices' in SIP URIs (`<sip:id…45#9f1c@…>`). A panel's
  short extension such as `55001` stays readable, since it is what tells you which panel a line is about.
- **The apartment's GID** (`gid#xxxx`), the intercom's MAC, the SIP domains, the device IMEI and UUID, the device
  name and the names in the phonebook.

A tag such as `id…45#9f1c` keeps only the last two digits and is the same for the same value until Home Assistant
restarts, so two lines about the same device can still be matched. Not everything is covered yet: IPv6 addresses, the
addresses inside SDP and STUN lines, and an id used as an unquoted display name (`From: 7798765 <sip:…>`) stay as
they are, so read a log before posting it.

**The pairing QR code and its content must never be attached**, neither the picture nor the text: it holds the SIP
password.

The SIP keepalive's expected replies (the periodic OPTIONS) are logged at `DEBUG`, so no `logger:`
filter is needed to keep the log quiet.

