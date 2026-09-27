# Field test plan

The full round to say the integration works, not that it seems to. Where a test has an expected
number, it is the one measured on 26 September 2026 on an Elvox Tab 7S Up (40517) over the cloud
relay.

How to report results: say which test you ran, roughly when, and what you saw. The rest is already
recorded in the integration's own log (`/api/vimar_intercom/debug`, administrators only), with the
timeline of every opening (`[T] …`) and, when each view closes, the audio bridge and video counters
(packets lost before us, voice received from the phone, keyframe requests).

At home means the iPhone on Wi-Fi, streaming straight to the Pi. Away means the iPhone on 5G, going
through the home hub (Apple TV or HomePod) and Apple's relay: about one second slower is normal.

---

## 1. HomeKit: pairing and options

| # | Test | Expected |
|---|---|---|
| 1.1 | In the Home app the intercom accessory is there, with separate tiles (doorbell/camera and gate) in the same room | Everything responds; no "No Response" |
| 1.2 | Options → HomeKit: turn Smoother video off and on | It saves, the integration reloads in a few seconds, and the accessory stays paired |
| 1.3 | Save that page, then open Settings and save without changes | The HomeKit choices stay as they were |
| 1.4 | (Optional, costs a new pairing) Turn Publish to HomeKit off, then on | Off: the accessory disappears. On: it comes back paired; if not, the QR notification appears |

## 2. Opening the camera from the Home app (nobody rang)

Half of these with Smoother video off, half with it on.

| # | Test | Expected |
|---|---|---|
| 2.1 | Open cold (no call for at least a minute), at home | Picture in under 2 s (re-encoded: about half a second more) |
| 2.2 | The same away, on 5G | Picture in about 3 s; the view does not die after a second or two |
| 2.3 | Keep it open 30 s while cars pass | Direct: smooth, but can freeze up to 3 s when the relay loses packets. Re-encoded: always moving, at most a brief smear. Never a grey blob |
| 2.4 | Close and reopen at once, five times | Always the same; no opening stays black |
| 2.5 | Open on two devices at once (iPhone and Mac) | Both see it |
| 2.6 | Close all views | The call ends by itself within a few seconds (the street panel light goes off) |
| 2.7 | Restart Home Assistant, then open at once | No slower than usual: the video parameters are saved on disk |

## 3. Someone rings (needs someone at the street)

| # | Test | Expected |
|---|---|---|
| 3.1 | Ring the bell | One HomeKit notification (plus VIEW's, if still enabled) |
| 3.2 | Open the notification | Picture faster than opening by hand: the call is already warm from the ring |
| 3.3 | From the view: Talk, and listen | Voice both ways, clean |
| 3.4 | From the view: open the gate | Asks for confirmation, opens, notifies the unlock once; after a few seconds the state returns to unknown, with no "locked" notification; the call stays up |
| 3.5 | Ring and answer from the indoor monitor | The indoor monitor answers and opens as always; Home Assistant does not steal the call |
| 3.6 | Ring and do not answer | The ring times out cleanly; no call left hanging; street panel light off |
| 3.7 | Ring twice a few seconds apart | The second ring behaves like the first |
| 3.8 | Answer from the iPhone and let the street panel hang up | Audio stops and the view stays on the last frame until you close it. That is a HomeKit limit: the Home app does not let an accessory close its viewer. Nothing may stay hanging |

## 4. Talking

| # | Test | Expected |
|---|---|---|
| 4.1 | Open the view, wait 15 s, then press Talk | The street hears you from the first word |
| 4.2 | Talk, stay silent 20 s, talk again | The second time is heard too |
| 4.3 | The same away, on 5G | Voice both ways |

## 5. The indoor monitor calls Home Assistant (from its display)

| # | Test | Expected |
|---|---|---|
| 5.1 | Call Home Assistant from the display and answer on the iPhone | Ring, then voice both ways |
| 5.2 | Look at the video during that call | A black placeholder: that call has no video |
| 5.3 | Hang up on the monitor | The call ends at once in Home Assistant too |

## 6. Preview image

| # | Test | Expected |
|---|---|---|
| 6.1 | After a call, look at the tile in the Home app | The last frame of the call, however old |
| 6.2 | Restart Home Assistant and look again | The placeholder until a new call, then the last frame again. Never an empty rectangle |
| 6.3 | The camera card in Home Assistant | Real snapshots; the entity is never "unavailable" between calls |

## 7. Voicemail and Do not disturb

| # | Test | Expected |
|---|---|---|
| 7.1 | Turn voicemail on and off from Home Assistant | The switch moves at once; the monitor shows the change |
| 7.2 | Turn Do not disturb on from Home Assistant | LED blinking, as from the VIEW app |
| 7.3 | Change the state on the monitor | Home Assistant follows |
| 7.4 | Change the state from the VIEW app | Home Assistant follows |

## 8. Living with VIEW

| # | Test | Expected |
|---|---|---|
| 8.1 | Use the VIEW app normally with Home Assistant running | No kick-outs, no lost registration |
| 8.2 | Ring and answer from VIEW | Works as always; Home Assistant does not interfere |
| 8.3 | The devices sensor after a while | The devices that talked show up |

## 9. Failures and restarts

| # | Test | Expected |
|---|---|---|
| 9.1 | Restart Home Assistant | Registers again by itself; the accessory is reachable again in Home |
| 9.2 | Restart Home Assistant during a call | No call left hanging on the intercom |
| 9.3 | Cut the Pi's network for about a minute | Reconnects and registers without help |
| 9.4 | Restart the intercom | Registration comes back when it does |

## 10. Time only (leave it running)

| # | Test | Expected |
|---|---|---|
| 10.1 | At least 90 minutes | At least one registration renewal around minute 59; registration never lost |
| 10.2 | One night | No reconnections, or reconnections followed by a successful registration |
| 10.3 | Home Assistant's log at the end of the day | No `vimar_intercom` warnings for normal ffmpeg shutdowns, cancelled calls or relay duplicates |

---

## A note on privacy

The preview is a photo of whoever was at the door. It stays in `/tmp` inside the container and in
memory until the next call: outside Home Assistant's backups and gone when the container is
recreated, but not encrypted. Mount `/tmp` as tmpfs if that matters, or ask for the feature to be
removed.
