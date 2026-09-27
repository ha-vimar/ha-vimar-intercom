# HomeKit: the integration's own video doorbell

The integration can publish the intercom to Apple Home by itself, as a video doorbell (HomeKit
category 18). It uses HAP-python, the library Home Assistant's HomeKit bridge is built on, and it
changes nothing in Home Assistant. It is off by default.

## Why not Home Assistant's HomeKit bridge

The bridge cannot carry the talk direction. Its doorbell camera advertises a speaker, and the Home
app shows the Talk button, but when the phone sends `SetupEndpoints` the bridge answers with the
phone's own ports as if they were the accessory's. Nothing on the Home Assistant side listens there:
Home Assistant's audio proxy only opens that port to send. The voice of whoever answers goes to a
socket nobody reads. On a test call the visitor could be heard clearly, and could not hear anything.

This cannot be fixed from outside Home Assistant, and it applies to every camera or doorbell exposed
through the bridge, because the bridge's doorbell is the same class as its camera.

## What the accessory contains

One accessory, so the live view shows video, Talk and the gate together:

| Service | Purpose |
|---|---|
| `CameraRTPStreamManagement` ×3 | Up to three views at once (iPhone, Mac, Watch) |
| `Microphone` | Voice from the street to the phone |
| `Speaker` | Voice from the phone to the street |
| `Doorbell` and `StatelessProgrammableSwitch` | The ring notification, and the ring as a trigger for Home automations |
| `LockMechanism` "Cancello" (gate) | Opens the door through the same panel as the integration's lock. Reports unlocked, then returns to unknown after 5 s: the intercom only pulses the strike and cannot know whether the gate is shut, so it never reports locked |

The HAP server listens on port 21099. The pairing state is in
`.storage/vimar_intercom.<entry>.homekit.state` and the setup code in `.homekit.pin` (mode 0600).

## Pairing and options

Settings → Devices & services → Vimar Intercom → Configure → HomeKit:

- Publish to HomeKit turns the accessory on or off. Until it is paired, Home Assistant shows a
  notification with a QR code and the setup code. In the Home app choose Add Accessory and scan the
  QR, or choose More options, pick the intercom and type the code. iOS warns that the accessory is not
  certified; choose Add Anyway. The QR link stops working once the accessory is paired.
- Smoother video (re-encode), on by default. See below.

In the Home app, turn on Show as Separate Tiles for the accessory. The gate then gets its own tile and
appears in the camera's live view. Without it, the gate stays inside the camera tile and the live view
cannot reach it.

Saving the options reloads the integration for a few seconds. The pairing survives.

## How audio and video travel

Street to phone. ffmpeg reads the panel's RTP from an SDP file, encodes Opus and sends it in the
clear to `AudioBridge`, which encrypts it (SRTP) and sends it from the socket announced to the phone.
ffmpeg stamps Opus at 48 kHz whatever the real rate (RFC 7587); HomeKit wants the negotiated rate, so
the bridge rescales the timestamps.

Phone to street. The phone's voice arrives on the same socket (symmetric RTP). The bridge decrypts it
with the key the phone provided, restamps it at 48 kHz and hands it to a second ffmpeg that decodes it
to 8 kHz PCM. The PCM goes to the panel through `media.send_audio`, the same path as the web card's
microphone. A pacer sends exactly one 20 ms frame per tick, voice when there is some and silence
otherwise, so the RTP clock follows real time and the panel's jitter buffer does not grow. This second
ffmpeg starts with the first voice packet: an ffmpeg reading RTP exits after ten seconds without
input, and someone who opened the view and waited before pressing Talk was never heard. If it exits
during a long silence, it restarts on the next voice packet, and the packets that arrive while it
starts are kept.

Video, re-encoded (the default). ffmpeg decodes the panel's video, concealing lost packets, and
re-encodes it with a keyframe every second. One encoder per call, shared by all views, started at the
ring when early media is available.

Video, direct. No ffmpeg: the panel's H.264 packets go to the phone as they are, restamped for it
(negotiated SSRC and payload type, continuous sequence numbers) and encrypted. The group already in
memory goes first, from the keyframe on, with its timestamps squeezed into a few ticks so the phone
shows it at once instead of treating it as backlog. Then the live stream follows. An RTCP sender
report goes out every two seconds. This is how the VIEW app works: the panel's own stream, decoded by
the phone.

Early media. When the panel rings, the integration answers `183 Session Progress` with an SDP. The
panel starts sending while the call keeps ringing, so the indoor monitor can still answer, and whoever
opens the notification finds the stream already running. The `200 OK` repeats the `183` answer byte
for byte. With different SRTP keys the panel renegotiated, and the street light blinked on and off.
A second ring during a call gets no early media: that would replace the keys of the call in progress.

## Measurements (26 September 2026)

| | At home (Wi-Fi) | Away (5G) |
|---|---|---|
| Video, direct | under 2 s | about 3 s |
| Video, re-encoded | about 0.5 s more | about 0.5 s more |

Before this work opening took about 5 s. The VIEW app takes 2.5 s. The extra second away from home is
Apple's path (home hub, then iCloud relay): on our side the video starts after 1.5 to 1.6 s in both
cases.

## Things to know

- The cloud relay loses packets, two to four in a hundred, before they reach Home Assistant. The phone
  counts exactly as many missing packets as we do, and Wi-Fi loses none. In direct mode a lost packet
  freezes the picture until the panel's next keyframe, up to 3 s, because the panel ignores keyframe
  requests (PLI, FIR). That is why re-encoding is the default.
- When the panel hangs up, the view stays open on the last frame. The integration ends the session
  within a millisecond (RTCP BYE, streaming available again), but HomeKit gives an accessory no way to
  close the Home app's viewer. Home Assistant's cameras behave the same.
- Away from home, the home hub decides the packet time. A hub asks for 60 ms audio packets, and a
  60 ms Opus packet does not fit in Home Assistant's default 188-byte RTP packet, so the integration
  uses 1316.
- The panel's video parameters (SPS/PPS) are saved in `.storage/vimar_intercom.video_params.json`.
  Without them, the first view after every restart waited up to 3 s for the panel's next group.
- When the last HomeKit view of an automatic call closes, the integration hangs up instead of leaving
  the panel busy until its own timeout.

## The trap that still applies: controllers cache what you can do

HAP-python computes the accessory's fingerprint with `include_value=False`:

```python
# pyhap/accessory_driver.py
return hashlib.sha512(util.to_sorted_hap_json(
    self.get_accessories(include_value=False))).hexdigest()
```

Characteristic values do not count, and the list of resolutions or audio codecs is a value, not
structure. You can change everything the doorbell says it supports and the configuration number `c#`
does not move, so phones never re-read the accessory and keep the copy they took at pairing. The
symptom: the phone connects, downloads the snapshot and never asks for a stream, with no error
anywhere.

Adding or removing a service changes the structure and therefore `c#`. For a values-only change,
increment `config_version` by hand in the state file while Home Assistant is stopped. The pairing
survives.

## Reading a session

Everything is in the integration's own log, `/api/vimar_intercom/debug` (administrators only; Home
Assistant's log only gets warnings unless you raise the level). For each view:

```
[T] 0.000 richiesta di flusso
[T] 1.062 chiamata attiva
[T] 1.521 video diretto al telefono  pacchetti=6
HomeKit: flusso avviato verso 192.168.1.20 (…, video diretto)
HomeKit: ponte audio chiuso — al telefono 1409 pacchetti, dal telefono 220 (0 non decifrati), 219 fotogrammi di voce al citofono
HomeKit: video diretto chiuso — … persi PRIMA di noi (targa/relay): 15 … | RTCP dal telefono {'RR': 66, 'TMMBR': 55, 'PLI': 25}
```

- `dal telefono 0` while someone pressed Talk: the voice is not arriving.
- `persi PRIMA di noi` and the `PLI` count: relay losses, and the phone asking for a keyframe.
- An address that differs from the iPhone's is the home hub: the view is from away.

## History: the Home Assistant bridge

Until 26 September 2026 the intercom went through Home Assistant's HomeKit bridge in accessory mode.
Two traps cost days there: the `c#` cache above (changed resolutions the phones never saw, and a
stream that never started), and the doorbell sensor Home Assistant links by itself from the same
device unless you point it at one that does not exist. If you used the bridge for the intercom, remove
its `homekit:` entry before turning this accessory on.
