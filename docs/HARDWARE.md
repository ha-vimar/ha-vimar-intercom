# Supported plants: what changes from one model to another

Vimar plants do not all speak the same way. The differences matter: they decide
**which path calls take** and **whether the media is encrypted**. This document
collects what has been verified in the field, so the code can adapt instead of
assuming a single family.

The starting profile lives in `profiles.py`. The probe run during configuration
(`config_flow._probe_transport`) has the final word.

## Due Fili Plus (`planttype=2F`)

Example: **Elvox Tab 7S, code 40507**. The original integration was built on
this plant.

| | |
|---|---|
| Registration | **local UDP** to the intercom's IP, port 5060 |
| Calls | delivered over the same local path |
| Media | **plain RTP**. Offering SRTP does not work: the baresip entrance panel does not answer at all |
| Cloud | not needed |

## Due Fili Plus EVO (`planttype=2FV2`)

Example: **Elvox Tab 7S Up, code 40517**, firmware **2.1.0203** (the `fver` field
announced over mDNS `_eipvdes._tcp.local.`; the Vimar product page distributes
package 2.1.0303, but the two numbers are not directly comparable).

| | |
|---|---|
| Registration | **cloud TLS**, port 7042, SNI `ipvdes.vimar.cloud` |
| Proxy | from SRV `_sips._tcp.<cproxy>`, with `flexiprod{1,2,3}.ipvdes2.vimarsso.cloud` as fallback |
| Digest realm | the **cloud domain** from the QR (`cdomain`), not `domain` |
| Calls | **only** through the cloud relay |
| Media | **SRTP**, `RTP/SAVP` with `a=crypto AES_CM_128_HMAC_SHA1_80` |
| Video | H.264 Baseline level 3.1, 320x240 |

### Why the local network is not enough

On this plant the local path is not simply "slower". It does not carry calls.

* **Local UDP**: rejected with `503 You're not allowed to make this operation`,
  for both `OPTIONS` and `REGISTER`.
* **Local TCP on 5060**: registration **works**, with a regular Digest challenge
  and `200 OK`. This is also how a device pairs: registering with a freshly
  issued credential is enough for the intercom to list it among the paired
  devices, without going through the app.
* **But calls do not arrive**: with the local registration alive (the intercom
  answered keepalives and sent ARP for the host every 30 seconds), a call to
  that device produced no packet toward us. Verified with a packet capture on
  the host: 54 packets in the test window, all keepalives, renewals and ARP.

The intercom's PBX routes everything, and it reaches mobile devices through
their **cloud** binding. The `Via` chain of a message sent by a phone on the
same LAN shows it: the message goes up to the relay, enters the PBX, and comes
back down from the relay.

## Consequences for the code

1. **Verify the transport, do not infer it.** `planttype` gives the starting
   point. If the probe fails, the code tries the other path and saves the one
   that answers.
2. **Mirror media encryption from the offer.** One plant requires it, the other
   does not tolerate it. When answering, the code uses the offered profile. The
   plant setting applies only to our own offers.
3. **SIP addresses are specific to each installation.** On this plant the
   calling entrance panel is `55001` and `60001` is the Tab's internal monitor.
   The historical values `55100`/`55002` do not exist. They must be configured
   or read from the address book, never hard-coded.
4. **Not every outdoor station has a camera.** The QR declares it in the
   `video` field. An audio-only call must not create a camera that will never
   produce an image.

## How to collect this data on a new plant

* The pairing QR carries `planttype`, `pc` (product code), `video`, `domain`
  and `cdomain`.
* `avahi-browse -r _eipvdes._tcp` announces the address, model and `fver`.
* The tools in `research/` register and capture traffic without involving Home
  Assistant: see `research/docs/LOCAL_SIP_SESSION.md`.

## Keyframe requests: they do not exist on 2FV2

The entrance panel emits a keyframe every **3.00 seconds**, and there is no way
to bring it forward. All four possible channels were tried and measured in the
field (intervals between IDRs, twenty-five-second calls):

| Request | Result |
|---|---|
| SIP `INFO` `picture_fast_update` | ignored |
| RTCP PSFB **PLI** (RFC 4585) | ignored |
| RTCP PSFB **FIR** (RFC 5104) | ignored |
| RTCP **legacy FIR** (RFC 2032, PT=192) | ignored |

At rest: 3.00 s on average. With fourteen requests in twenty-five seconds:
3.00 s on average. No measurable difference.

This matches what the entrance panel declares. Its SDP offers `RTP/SAVP`
(**not** `SAVPF`) and contains no `a=rtcp-fb`: RTCP feedback is not negotiated,
so formally it is not part of the contract. The `a=rtcp-fb:96 ccm
fir` and `nack pli` lines seen in the traffic are **ours**, not the panel's.
This mistake is easy and costly to make. The panel runs linphone/oRTP
(`Linphonec/3.8.0-linphone-daemon (belle-sip/1.4.0)`, `s=Talk` in the SDP),
which handles feedback only if AVPF was agreed.

**The conclusion drawn from this was wrong, and it should be said.** From "the
keyframes are three seconds apart" we had concluded that three seconds of wait
before the image were irreducible. They are not: as soon as the panel answers
the call, it sends SPS, PPS and a FULL keyframe within half a second. Measured
several times: `+1,02 s` answer, `+1,51 s` complete keyframe. The three-second
cycle applies to the SUBSEQUENT keyframes, and matters only for a viewer that
joins midway.

We were the ones waiting three seconds, for two reasons of our own. We required
seeing SPS **and** PPS of the current call before declaring the video (and the
panel does not always put them in the same group), and we had a downstream
stage analyse an MPEG-TS we had just built. With both removed, first audio went
from 7 s to 1 s. The official app, on the same plant and over the same path,
shows everything in two and a half seconds. That was the missing benchmark, and
it disproved the conclusion.

The RTCP channel does **exist and work**, though: the panel sends `SR ‖ SDES ‖ XR`
every two or three seconds on a separate port (`a=rtcp:`, no `a=rtcp-mux`), and
the packets decrypt correctly with the SRTCP in `srtp.py`. The RTP+1 ports must
be opened. Otherwise they stay closed and answer with ICMP.

## The local path is for registering, not for calling

Open question for a long time: must calls go through the cloud relay in
Ireland, or can we talk directly to the intercom's PBX on the LAN? The cloud
costs about one second of call setup and a round trip to Ireland for every
packet, so the stakes were not small.

Tested, and the answer is clear.

**Local registration over TCP works.** The `503 You must upgrade your app
to use it!` that looked like a ban is not one. It is a check on the
`User-Agent`. The PBX accepts only clients that present themselves as the TOGA
app. With `User-Agent: probe` it answers 503. With
`TOGA_iPhone16,1_iOS27.0/5.4.73|AppVer:2.4.5|ProtVer:1.0|` (or with ours,
`AppVer:2.4.0`) it answers **200 OK**. So it is not about the version: the right
format is enough.

**Calls do not work, in either direction.** For inbound, we already knew the PBX
does not deliver them. For outbound (which nobody had tried, and which matters
for auto-on, where WE place the call), the INVITE is rejected instantly:

| Destination | Result |
|---|---|
| `55100` camera | `503 You're not allowed to make this operation` |
| `55001` outdoor entrance panel | same |
| `60001` internal monitor | same |
| `60002` intercom peer | same |
| `61000` controller | same |

All in under a hundredth of a second: this is a policy rejection, not a routing
failure.

**Consequence:** the cloud relay is mandatory for calls on this plant, and that
second or so of call setup cannot be removed by working on the transport. Local
registration remains useful only for pairing a device without the app (see
`research/docs/LOCAL_SIP_PROBE.md`).
