"""Video from the door panel to the phone, with no ffmpeg in between.

The panel already sends H.264 over RTP. Passing it through ffmpeg
(``-c:v copy``) did not transform it. It only repacketized it, at a cost of
about seven tenths of a second for startup and probing. Worse, it delivered the
opening keyframe to the phone inside a burst (half a second of video in one
millisecond) with a presentation time already in the past. The phone dropped
it and waited for the panel's next natural keyframe: the two and a half
seconds measured on 20 September, which no other fix had reduced.

Here the panel packets go to the phone as they are, restamped for it
(negotiated SSRC and payload type, continuous sequence numbers) and encrypted
with its key:

- first the group already in memory, from the keyframe on, with its times
  squeezed into a few ticks: the phone decodes all of it, references
  included, and shows it at once instead of treating it as backlog;
- then the live stream, at its own rate, with the clock continuing from there;
- an RTCP Sender Report with the first packet and then every two seconds, as
  ffmpeg did.

This is the same shape as the VIEW app: the panel stream, decoded directly,
with no second stream built on top.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import time
from collections import OrderedDict

from .homekit_audio import _MASK32, _Datagrams, is_rtcp
from .srtp import SRTCPContext, SRTPContext

_LOGGER = logging.getLogger(__name__)

_NTP_EPOCH = 2208988800           # 1900 → 1970
_VIDEO_CLOCK = 90000
# One panel frame (15 per second) on the 90 kHz clock.
_FRAME_TICKS = _VIDEO_CLOCK // 15
SR_INTERVAL = 2.0
# How many sent packets we keep to answer NACKs. At 20-30 packets per second
# this is about fifteen seconds, well beyond any phone's buffer.
SENT_KEEP = 512
_RTPFB_NAMES = {1: "NACK", 3: "TMMBR", 4: "TMMBN", 15: "TWCC"}
# After the panel stream changes SSRC, packets of the old one still in flight
# are dropped for this long instead of being followed back.
OLD_STREAM_GRACE = 1.0

_RTCP_NAMES = {200: "SR", 201: "RR", 202: "SDES", 203: "BYE", 204: "APP",
               205: "RTPFB", 206: "PSFB"}


def rtp_payload(packet: bytes) -> bytes:
    """The payload of an RTP packet, skipping CSRC and extension."""
    hlen = 12 + (packet[0] & 0x0F) * 4
    if packet[0] & 0x10 and len(packet) >= hlen + 4:
        hlen += 4 + struct.unpack_from("!H", packet, hlen + 2)[0] * 4
    return packet[hlen:]


def _seq(packet: bytes) -> int:
    return struct.unpack_from("!H", packet, 2)[0]


def _ts(packet: bytes) -> int:
    return struct.unpack_from("!I", packet, 4)[0]


def _ssrc(packet: bytes) -> int:
    return struct.unpack_from("!I", packet, 8)[0]


class DirectVideo:
    """A video session to one phone, from the port we gave it."""

    def __init__(self, sock: socket.socket, phone_addr: tuple[str, int],
                 srtp_key_b64: str, ssrc: int, payload_type: int) -> None:
        self._sock = sock
        self._phone = phone_addr
        self._srtp = SRTPContext(srtp_key_b64)
        self._srtcp = SRTCPContext(srtp_key_b64)
        self._srtcp_rx = SRTCPContext(srtp_key_b64)
        self._ssrc = ssrc & _MASK32
        self._pt = payload_type & 0x7F
        # Sequence and clock start from random values, as RFC 3550 requires.
        self._seq_off = int.from_bytes(os.urandom(2), "big")
        self._ts_base = int.from_bytes(os.urandom(4), "big")
        # Panel times → ours, for the frames of the initial group.
        self._ts_map: dict[int, int] = {}
        # The live anchor point: (panel time, our time).
        self._anchor: tuple[int, int] | None = None
        self._last_ts: int | None = None
        self._last_seq: int | None = None
        self._last_wall = 0.0
        # The panel stream we follow. A new SSRC is a new stream (the relay or
        # the encoder restarted), with its own sequence and clock.
        self._panel_ssrc: int | None = None
        # The stream we followed before it, and when we left it: its late
        # packets are dropped, not followed back (see _follow_new_stream).
        self._old_ssrc: int | None = None
        self._old_until = 0.0
        self._sr_sent = False
        self._tr: asyncio.DatagramTransport | None = None
        self._sr_task: asyncio.Task | None = None
        # Packets already sent, encrypted, to resend them when the phone
        # reports them lost (NACK).
        self._sent: OrderedDict[int, bytes] = OrderedDict()
        self._panel_max_seq: int | None = None
        # Called once, as the first packet leaves for the phone (the view's
        # timeline).
        self.on_first_packet = None
        self.stats = {"packets": 0, "octets": 0, "backlog": 0, "late_dropped": 0,
                      "rtcp_in": {}, "keyframe_requests": 0,
                      # Where packets get lost: before us (the panel, the
                      # relay) or after us (the Wi-Fi to the phone).
                      "upstream_missing": 0, "upstream_late": 0,
                      "nack_lost": 0, "nack_we_had": 0, "nack_never_had": 0,
                      "retransmitted": 0,
                      "rr_fraction_lost": None, "rr_cumulative_lost": None,
                      "rr_jitter_max": 0}

    async def open(self) -> None:
        """Open the socket in the event loop. Send nothing yet."""
        loop = asyncio.get_running_loop()
        self._tr, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self._from_phone), sock=self._sock)

    def begin(self, backlog: list[bytes], prefix: list[bytes] | None = None) -> None:
        """Send the initial group. Call it with NO await between reading the
        group and anchoring to the live stream, or a packet is lost."""
        prefix = prefix or []
        packets = list(prefix) + list(backlog)
        if backlog:
            first_seq = _seq(backlog[0])
            k = 0
            for pkt in packets:
                panel_ts = _ts(pkt)
                if panel_ts not in self._ts_map:
                    self._ts_map[panel_ts] = (self._ts_base + k) & _MASK32
                    k += 1
            for i, pkt in enumerate(prefix):
                # The prefix (SPS+PPS that we add) goes right before it.
                self._send(pkt, self._ts_map[_ts(pkt)],
                           (first_seq + self._seq_off - len(prefix) + i) & 0xFFFF)
            for pkt in backlog:
                self._send(pkt, self._ts_map[_ts(pkt)])
            last = backlog[-1]
            self._anchor = (_ts(last), self._ts_map[_ts(last)])
            self._panel_max_seq = _seq(last)
            self._panel_ssrc = _ssrc(last)
            self.stats["backlog"] = len(packets)
        # Without a group nothing is sent yet, and a report now would tie our
        # clock to a moment no packet belongs to: it goes with the first one.
        self._send_sr()
        self._sr_task = asyncio.get_running_loop().create_task(self._sr_loop())

    def on_live(self, packet: bytes) -> None:
        """A live packet from the panel, as it arrives."""
        if len(packet) < 12:
            return
        ssrc = _ssrc(packet)
        if self._panel_ssrc is None:
            self._panel_ssrc = ssrc
        elif ssrc != self._panel_ssrc:
            if ssrc == self._old_ssrc and time.monotonic() < self._old_until:
                # A late packet of the stream we just left. Followed, it
                # re-anchored our clock to the old stream and the next packet
                # of the new one re-anchored it back: two jumps per straggler.
                self.stats["late_dropped"] += 1
                return
            self._follow_new_stream(packet)
        self._count_upstream(_seq(packet))
        panel_ts = _ts(packet)
        if panel_ts in self._ts_map:
            # A late fragment of a frame already sent.
            self._send(packet, self._ts_map[panel_ts])
            return
        if self._anchor is None:
            self._anchor = (panel_ts, self._ts_base)
        anchor_panel, anchor_ours = self._anchor
        delta = (panel_ts - anchor_panel) & _MASK32
        if delta > 0x7FFFFFFF:
            # Older than the anchor and not among those sent: backlog.
            self.stats["late_dropped"] += 1
            return
        self._send(packet, (anchor_ours + delta) & _MASK32)
        if not self._sr_sent:
            self._send_sr()

    def _follow_new_stream(self, packet: bytes) -> None:
        """The panel stream restarted with a new SSRC (media_handler resyncs
        and keeps forwarding). Its sequence and clock have nothing to do with
        the old ones: against the old anchor about half of its packets looked
        older and were dropped for good, the rest jumped by hours. Our stream
        carries on from where it was, one frame later."""
        _LOGGER.info("HomeKit: the panel video restarted (SSRC %08x → %08x), following it",
                     self._panel_ssrc or 0, _ssrc(packet))
        self._old_ssrc, self._old_until = self._panel_ssrc, time.monotonic() + OLD_STREAM_GRACE
        self._panel_ssrc = _ssrc(packet)
        next_ts = (self._ts_base if self._last_ts is None
                   else (self._last_ts + _FRAME_TICKS) & _MASK32)
        self._anchor = (_ts(packet), next_ts)
        if self._last_seq is not None:
            self._seq_off = (self._last_seq + 1 - _seq(packet)) & 0xFFFF
        self._ts_map.clear()
        self._panel_max_seq = None

    def _count_upstream(self, seq: int) -> None:
        """Gaps in the panel sequence: packets that never reach us."""
        if self._panel_max_seq is None:
            self._panel_max_seq = seq
            return
        d = (seq - self._panel_max_seq) & 0xFFFF
        if 0 < d < 0x8000:
            self.stats["upstream_missing"] += d - 1
            self._panel_max_seq = seq
        elif d:
            # It arrives after a newer one: a gap that closes late.
            self.stats["upstream_late"] += 1
            if self.stats["upstream_missing"]:
                self.stats["upstream_missing"] -= 1

    def _send(self, packet: bytes, our_ts: int, our_seq: int | None = None) -> None:
        if self._tr is None:
            return
        if our_seq is None:
            our_seq = (_seq(packet) + self._seq_off) & 0xFFFF
        payload = rtp_payload(packet)
        rtp = (bytes([0x80, (packet[1] & 0x80) | self._pt])
               + struct.pack("!HII", our_seq, our_ts, self._ssrc) + payload)
        srtp = self._srtp.protect(rtp)
        self._tr.sendto(srtp, self._phone)
        if not self.stats["packets"] and self.on_first_packet:
            try:
                self.on_first_packet()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("HomeKit: first-packet callback failed")
        self._sent[our_seq] = srtp
        if len(self._sent) > SENT_KEEP:
            self._sent.popitem(last=False)
        self.stats["packets"] += 1
        self.stats["octets"] += len(payload)
        self._last_ts = our_ts
        self._last_seq = our_seq
        self._last_wall = time.time()

    def _sender_report(self) -> bytes:
        now = time.time()
        rtp_now = (self._last_ts + int((now - self._last_wall) * _VIDEO_CLOCK)) & _MASK32
        frac = int((now % 1) * (1 << 32)) & _MASK32
        return struct.pack("!BBHIIIIII", 0x80, 200, 6, self._ssrc,
                           (int(now) + _NTP_EPOCH) & _MASK32, frac, rtp_now,
                           self.stats["packets"] & _MASK32,
                           self.stats["octets"] & _MASK32)

    def _send_sr(self) -> None:
        # Only once a packet went out: the report maps our RTP clock to the
        # wall clock, and before that there is nothing to map it from.
        if self._tr is not None and self._last_ts is not None:
            self._tr.sendto(self._srtcp.protect(self._sender_report()), self._phone)
            self._sr_sent = True

    async def _sr_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(SR_INTERVAL)
                self._send_sr()
        except asyncio.CancelledError:
            pass

    def _from_phone(self, data: bytes, addr) -> None:
        """The phone's RTCP. For now we only observe it."""
        if addr[0] != self._phone[0] or not is_rtcp(data):
            return
        plain = self._srtcp_rx.unprotect(data)
        if plain is None:
            return
        off = 0
        while off + 4 <= len(plain):
            pt = plain[off + 1]
            fmt = plain[off] & 0x1F
            name = _RTCP_NAMES.get(pt, str(pt))
            length = (struct.unpack_from("!H", plain, off + 2)[0] + 1) * 4
            if pt == 206 and fmt in (1, 4):   # PLI, FIR: "send me a keyframe"
                name = "PLI" if fmt == 1 else "FIR"
                self.stats["keyframe_requests"] += 1
                if self.stats["keyframe_requests"] <= 3:
                    _LOGGER.info("HomeKit: the phone asks for a keyframe (%s)", name)
            elif pt == 205:
                name = _RTPFB_NAMES.get(fmt, f"RTPFB{fmt}")
                if fmt == 1:
                    self._on_nack(plain[off + 12:off + length])
            elif pt in (200, 201):
                self._on_report(plain[off:off + length], rc=fmt)
            counts = self.stats["rtcp_in"]
            counts[name] = counts.get(name, 0) + 1
            off += length

    def _on_nack(self, fci: bytes) -> None:
        """RFC 4585 §6.2.1: "I lost these". If we have them, we resend them."""
        for i in range(0, len(fci) - 3, 4):
            pid, blp = struct.unpack_from("!HH", fci, i)
            lost = [pid] + [(pid + b + 1) & 0xFFFF for b in range(16) if blp >> b & 1]
            for seq in lost:
                self.stats["nack_lost"] += 1
                packet = self._sent.get(seq)
                if packet is None:
                    self.stats["nack_never_had"] += 1
                    continue
                self.stats["nack_we_had"] += 1
                if self._tr is not None:
                    self._tr.sendto(packet, self._phone)
                    self.stats["retransmitted"] += 1
            if self.stats["nack_lost"] <= len(lost):
                _LOGGER.info("HomeKit: the phone lost %s", lost)

    def _on_report(self, packet: bytes, rc: int) -> None:
        """The reception block for our stream (RFC 3550 §6.4)."""
        first = 8 if packet[1] == 201 else 28
        for i in range(rc):
            off = first + i * 24
            if off + 24 > len(packet):
                return
            if struct.unpack_from("!I", packet, off)[0] != self._ssrc:
                continue
            self.stats["rr_fraction_lost"] = packet[off + 4]
            self.stats["rr_cumulative_lost"] = int.from_bytes(packet[off + 5:off + 8], "big")
            jitter = struct.unpack_from("!I", packet, off + 12)[0]
            self.stats["rr_jitter_max"] = max(self.stats["rr_jitter_max"], jitter)

    def send_bye(self) -> None:
        """RTCP BYE (RFC 3550 §6.6): "this stream has ended"."""
        if self._tr is not None:
            bye = struct.pack("!BBHI", 0x81, 203, 1, self._ssrc)
            self._tr.sendto(self._srtcp.protect(bye), self._phone)

    async def stop(self) -> None:
        if self._sr_task:
            self._sr_task.cancel()
        if self._tr:
            self._tr.close()
            self._tr = None
        st = self.stats
        _LOGGER.info(
            "HomeKit: direct video closed. %d packets to the phone (%d from the initial "
            "group), %d backlog packets dropped | lost BEFORE us (panel/relay): %d, "
            "arrived late: %d | the phone asked for %d: %d we had and "
            "resent, %d we never had | RR: cumulative loss %s, jitter "
            "max %.0f ms | RTCP from the phone %s",
            st["packets"], st["backlog"], st["late_dropped"],
            st["upstream_missing"], st["upstream_late"],
            st["nack_lost"], st["nack_we_had"], st["nack_never_had"],
            st["rr_cumulative_lost"], st["rr_jitter_max"] / 90, st["rtcp_in"])
