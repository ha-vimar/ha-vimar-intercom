# Supported plants: what changes from one model to another

Vimar plants do not all speak the same way. The differences decide **which path
calls take** and **whether the media is encrypted**. This document collects what
has been verified in the field, so the code can adapt instead of assuming a
single family.

The starting profile lives in `profiles.py`. The probe run during configuration
(`config_flow._probe_transport`) has the final word.

## Due Fili Plus (`planttype=2F`)

Example: **Elvox Tab 7S, code 40507**. The integration was first built on this
plant.

| | |
|---|---|
| Registration | **local UDP** to the intercom's IP, port 5060 |
| Calls | delivered over the same local path |
| Media | **plain RTP**. Offering SRTP does not work: the baresip entrance panel does not answer at all |
| H.264 | the panel offers and accepts only `packetization-mode=0`; the answer uses the offered parameters |
| Cloud | not needed |
| Outgoing view | the panel ends it by itself after exactly 120 s (its BYE, 2 minutes after the 200 OK) |
| A panel that just ended a call | may swallow the next INVITE: `100 Trying` and no `180`, or `180` and no `200`. A cancel and a new INVITE a few seconds later go through |

## Due Fili Plus EVO (`planttype=2FV2`)

Example: **Elvox Tab 7S Up, code 40517**, firmware **2.1.0203** (the `fver` field
announced over mDNS `_eipvdes._tcp.local.`).

| | |
|---|---|
| Registration | **cloud TLS** to the proxy named in the pairing QR |
| Digest realm | the **cloud domain** from the QR (`cdomain`), not `domain` |
| Calls | **only** through the cloud relay |
| Media | **SRTP**, `RTP/SAVP` with `a=crypto AES_CM_128_HMAC_SHA1_80` |
| Video | H.264 Baseline level 3.1, 320x240 |

### Why the local network is not enough

On this plant the local path is not simply "slower". It does not carry calls.

* **Local UDP**: rejected with `503 You're not allowed to make this operation`,
  for both `OPTIONS` and `REGISTER`.
* **Local TCP on 5060**: registration **works**, with a regular Digest challenge
  and `200 OK`, but only for a client whose `User-Agent` has the format of the
  Vimar app (any other value gets `503 You must upgrade your app to use it!`).
  A successful local registration is also enough for the intercom to list the
  device among its paired devices, without going through the app.
* **But calls do not arrive**: with the local registration alive (the intercom
  answered keepalives), a call to that device produced no packet toward us. A
  packet capture on the host showed only keepalives, renewals and ARP.
* **Outbound calls are refused too**: an INVITE over the local registration to
  any address of the plant (entrance panels, the internal monitor, the
  controller) is rejected with the same 503 in under a hundredth of a second, a
  policy rejection rather than a routing failure.

The intercom's PBX routes everything, and it reaches mobile devices through
their **cloud** binding. The `Via` chain of a message sent by a phone on the
same LAN shows it: the message goes up to the relay, enters the PBX, and comes
back down from the relay. The integration therefore registers over the cloud on
this plant; the local path is not used.

## Consequences for the code

1. **Verify the transport, do not infer it.** `planttype` gives the starting
   point. If the probe fails, the code tries the other path and saves the one
   that answers.
2. **Mirror media encryption from the offer.** One plant requires it, the other
   does not tolerate it. When answering, each m-line uses the profile and crypto
   suite the offer used for it. The plant setting applies only to our own
   offers.
3. **Answer exactly the offered media lines.** Not every entrance panel has a
   camera. An audio-only offer gets an audio-only answer, and a video line the
   offer declined (port 0) stays declined.
4. **SIP addresses are specific to each installation.** On one plant the
   calling entrance panel is `55001` and `60001` is the Tab's internal monitor;
   on another the controller is `61000`. They come from the options or from the
   phonebook, never from constants. When no video panel is configured, the one
   learned from the last ring with video is used, else `55100`.

## How to collect this data on a new plant

* The pairing QR carries `planttype`, `pc` (product code), `video`, `domain`
  and `cdomain`.
* `avahi-browse -r _eipvdes._tcp` announces the address, model and `fver`.
* The debug buffer (`/api/vimar_intercom/debug?lines=N`, administrators only)
  holds the SIP trace with credentials and SRTP keys masked.

## Keyframe requests: it depends on the panel

Both panels measured so far emit a keyframe every **3.0 seconds** on their own.
Whether a request brings one forward differs.

**Tab 7S Up 40517 (2FV2): ignored.** Four channels were tried and measured in
the field (intervals between IDRs, twenty-five-second calls):

| Request | Result |
|---|---|
| SIP `INFO` `picture_fast_update` | ignored |
| RTCP PSFB **PLI** (RFC 4585) | ignored |
| RTCP PSFB **FIR** (RFC 5104) | ignored |
| RTCP **legacy FIR** (RFC 2032, PT=192) | ignored |

At rest: 3.00 s on average. With fourteen requests in twenty-five seconds:
3.00 s on average. No measurable difference.

This matches what the entrance panel declares. Its SDP offers `RTP/SAVP`
(**not** `SAVPF`) and contains no `a=rtcp-fb`: RTCP feedback is not negotiated.
The `a=rtcp-fb:96 ccm fir` and `nack pli` lines seen in the traffic are **ours**,
not the panel's. The panel runs linphone/oRTP (`s=Talk` in the SDP), which
handles feedback only if AVPF was agreed.

**Tab 5S Up 40515 (cloud relay): honoured.** Measured by @Apeiv on a twenty-second
"Vedi esterno" view, matching every `INFO picture_fast_update` with the next IDR:
the IDR follows **0.21 to 0.27 s** after each request, and a requested IDR
restarts the panel's own three-second timer.

So a periodic request every 5 s brings nothing on either panel (their own
cadence is shorter), while a request when the call starts and after a lost video
packet cuts recovery to about 0.25 s on the 40515 instead of up to 3 s. That is
what the integration does: a short burst at call start and one request per lost
packet, repeated at most once a second while the IDR has not come, never otherwise.
One INFO at a time, so concurrent requests do not read each other's answers.

Only in a confirmed call. In the ring preview (the early dialog of our `183`) the
40515 over the cloud answered every INFO `491 Request pending` or `501 Not
implemented`, with the authentication already accepted (two rings measured), so
the preview waits for the panel's own keyframes.

The three-second cycle does not mean three seconds of wait before the first
image. As soon as the panel answers the call it sends SPS, PPS and a full
keyframe within about half a second (measured: answer at +1.0 s, complete
keyframe at +1.5 s). The cycle applies to the keyframes after that, and matters
only for a viewer that joins midway. That is why `/av` replays the last group of
pictures, in RTP sequence order, when it starts its ffmpeg during a call. A viewer
that joins an ffmpeg already running (another viewer is connected, or the last one
left less than 10 seconds ago) starts at the panel's next keyframe.

The panel also sends RTCP (`SR`, `SDES`, `XR`) every two or three seconds on a
separate port (`a=rtcp:`, no `a=rtcp-mux`). The integration does not use it; with
its logger at DEBUG it listens on its RTCP ports and logs what arrives (`rtcp.py`).

## Where the latency goes (40517, cloud relay)

On this plant the relay is mandatory for calls (see above), and it costs about
one second of call setup plus a round trip to the relay for every packet. That
second cannot be won back by working on the transport. Everything after it is
ours.

For a while we believed the panel's three-second keyframe cycle was a floor.
It is not (see the measurements above): the first keyframe arrives about half a
second after the answer. The three seconds were two waits of our own:

1. **Requiring SPS and PPS from the current call** before declaring the video.
   The panel does not always send them in the same group, and one measured
   session lost 3.1 s this way while a complete, decodable keyframe went past.
   They describe the panel's encoder and do not change between calls, so the
   ones seen before are good (the integration keeps them per panel).
2. **Letting ffmpeg probe a stream whose codecs we already knew.** ffmpeg waits
   out its whole `analyzeduration`, once, for all tracks together, so the audio
   waited for the picture. Describing the RTP with an SDP leaves nothing to
   probe, and audio and video become independent again.

With both removed, first audio went from 7 s to about 1 s. The benchmark that
exposed the gap was the official VIEW app, on the same panel and the same
relay: 2.5 s to show everything. When a change adds a wait in this path, it is
worth checking against that number again.
