"""What the HomeKit doorbell needs from the call's media, on top of media_handler.

The accessory does not go through /av: it sends the panel's own H.264 packets to
the phone (or re-encodes them), and its ffmpeg reads the panel's audio from a
local SDP. This module is the small adapter between that and media_handler:

- the video and audio RTP taps (``rtp_sinks`` on the two protocols);
- the keyframe group to replay to a phone that joins mid-call, built from
  media_handler's own GOP cache, with SPS/PPS added when the panel sent them
  earlier than the group;
- one local audio port and SDP per open view.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import socket
import struct
import time

from . import frame_grabber, ring_log
from . import media_handler as media
from . import runtime as R
from .homekit_audio import _MASK32, _signed32, free_udp_port, write_sdp

_LOGGER = logging.getLogger(__name__)

# A dark 320x240 JPEG for the Home tile before the first call. Returning
# nothing made the tile show "No Response".
IDLE_IMAGE = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjI4LjEwMgD/2wBDAAgYGBwYHCEhISEhISckJygoKCcnJycoKCgrKyszMzMrKysoKCsrMDAzMzc5NzQ0MzQ5OTw8PEhIRUVUVFdnZ3z/xABLAAEBAAAAAAAAAAAAAAAAAAAABwEBAAAAAAAAAAAAAAAAAAAAABABAAAAAAAAAAAAAAAAAAAAABEBAAAAAAAAAAAAAAAAAAAAAP/AABEIAPABQAMBIgACEQADEQD/2gAMAwEAAhEDEQA/AI8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/2Q=="
)


# ─── video ───────────────────────────────────────────────────────────────────

def add_video_sink(sink) -> None:
    if media.video_proto and sink not in media.video_proto.rtp_sinks:
        media.video_proto.rtp_sinks.append(sink)


def remove_video_sink(sink, proto=None) -> None:
    """Detach a sink from ``proto``, by default media_handler's video protocol
    (one per hub, the same for every call)."""
    proto = proto or media.video_proto
    if proto and sink in proto.rtp_sinks:
        proto.rtp_sinks.remove(sink)


def video_ready(hub) -> bool:
    """Video packets from the panel are arriving for this call or ring preview."""
    vp = media.video_proto
    return bool(hub.video_active and vp and vp.pkt_count > 0)


def parameter_sets(panel_uri: str | None = None) -> tuple[bytes | None, bytes | None]:
    # Only the calling panel's own parameters: on a plant with several panels
    # another panel's SPS/PPS would garble the first frames. A panel never
    # seen before costs a slower first picture instead, never a wrong one.
    # panel_uri: the panel about to be called, before its call has set the
    # current one (after a restart there is none; before a view, the last
    # call's panel, maybe another one).
    vp = media.video_proto
    if not vp:
        return None, None
    ps = vp.sps_pps_of(media.panel_id(panel_uri)) if panel_uri else vp.sps_pps(own_only=True)
    return ps if ps else (None, None)


def _nal_types(rtp: bytes) -> list[int]:
    """NAL unit types carried by one RTP packet (inside a STAP-A too)."""
    if len(rtp) < 13:
        return []
    hlen = 12 + (rtp[0] & 0x0F) * 4
    if len(rtp) <= hlen:
        return []
    first = rtp[hlen] & 0x1F
    if first == 28:  # FU-A: only the start fragment names the NAL
        if len(rtp) > hlen + 1 and rtp[hlen + 1] & 0x80:
            return [rtp[hlen + 1] & 0x1F]
        return []
    if first == 24:  # STAP-A
        types, i = [], hlen + 1
        while i + 2 < len(rtp):
            size = int.from_bytes(rtp[i:i + 2], "big")
            types.append(rtp[i + 2] & 0x1F)
            i += 2 + size
        return types
    return [first]


def _stap_a(sps: bytes, pps: bytes, template: bytes) -> bytes:
    """One RTP packet carrying SPS and PPS as a STAP-A (RFC 6184 §5.7.1).

    The panel does not always put the SPS in the same group as the keyframe;
    without it the phone has nothing to decode until a group that has one.
    The header is copied from the group's first packet (same seq, timestamp
    and SSRC), so the sequence stays consistent.
    """
    hdr = bytearray(template[:12])
    hdr[0] &= 0xF0  # no CSRC in what we build
    nri = max(sps[0] & 0x60, pps[0] & 0x60)
    payload = bytes([nri | 24])
    for nal in (sps, pps):
        payload += len(nal).to_bytes(2, "big") + nal
    return bytes(hdr) + payload


def _gop_in_sequence_order() -> list[bytes]:
    """media_handler's cached keyframe group, sorted by RTP sequence number.

    The cache fills in the order packets ARRIVE, ahead of the reorder buffer.
    Replayed as it is, the phone's decoder takes the first packet as its
    reference and drops the ones that belong before it: an incomplete IDR,
    and a black picture until the next keyframe (2.8 s measured).

    Empty when the protocol has had no packet since the call began: the
    protocol lives as long as the hub, and the end of a call resets its
    packet count but leaves the cached group, the previous visitor's, in
    place until the next call's media setup.
    """
    vp = media.video_proto
    if not vp or not vp.pkt_count:
        return []
    return vp.gop_in_sequence_order()


def gop_has_keyframe() -> bool:
    """True when the cached group holds a WHOLE IDR, not only its first pieces.

    An IDR split in FU-A counts only with both its start and its end fragment:
    the phone silently drops a truncated IDR and stays black until the next
    natural keyframe, three seconds later. media_handler caches the group in
    ARRIVAL order, so it is put in sequence order first: only then does the
    start fragment come before the end.
    """
    started = False
    for rtp in _gop_in_sequence_order():
        if len(rtp) < 13:
            continue
        hlen = 12 + (rtp[0] & 0x0F) * 4
        if len(rtp) <= hlen + 1:
            continue
        if rtp[hlen] & 0x1F != 28:            # single NAL or STAP-A
            if 5 in _nal_types(rtp):
                return True
            continue
        fu = rtp[hlen + 1]
        if fu & 0x1F != 5:
            continue
        if fu & 0x80:
            started = True
        elif fu & 0x40 and started:
            return True
    return False


def gop_for_direct_video() -> tuple[list[bytes], list[bytes]]:
    """(prefix, group): the last keyframe group in sequence order, plus SPS/PPS
    when it lacks them, placed right before the group's first packet.

    Taken with no await in between by the caller, together with adding its
    sink, so the replayed group and the live packets meet without a gap.
    """
    gop = _gop_in_sequence_order()
    if not gop:
        return [], []
    if any(7 in _nal_types(p) for p in gop):
        return [], gop
    sps, pps = parameter_sets()
    return ([_stap_a(sps, pps, gop[0])] if sps and pps else []), gop


# Largest photo read back for the Home tile (a ring photo is ~20-60 KB).
_PHOTO_MAX = 2 * 1024 * 1024


def last_ring_photo() -> bytes | None:
    """The newest ring photo on disk, for the Home tile after a restart.

    Blocking file I/O: run it in the executor. Only a photo that ring_log
    lists and that is really inside the snapshot folder (ring_photo_path).
    """
    folder = R.SNAPSHOT_DIR
    if not folder:
        return None
    for ring in ring_log.recent_rings(folder, 10):
        path = ring_log.ring_photo_path(folder, str(ring.get("photo") or ""))
        if not path:
            continue
        try:
            if os.path.getsize(path) > _PHOTO_MAX:
                continue
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            continue
    return None


def last_frame() -> bytes | None:
    """The frame grabber's latest JPEG of this call or ring preview."""
    return frame_grabber.last_jpeg


# ─── audio ───────────────────────────────────────────────────────────────────

_PCMU_RATE = 8000
_SLOT = 0.02            # one PCMU packet, 160 samples


class AudioTap:
    """The panel's audio for one HomeKit view: RTP to a local port, and an SDP
    that the view's ffmpeg opens.

    Plain PCMU (payload type 0, 8 kHz), with the tap's own sequence, SSRC and
    clock. When the panel sends no audio (the ring preview: early media
    carries video only) the tap sends silence instead: without an audio stream
    coming in, the phone sends nothing when Talk is pressed, and answering on
    Talk never happens. Each view has its own port and file, so a view that
    is closing never takes a new one with it.

    Timestamps follow time, not only the payload length. The panel's packets
    keep the panel's own spacing (a packet lost upstream leaves its gap), the
    silence advances with the wall clock, and after a stall the panel's late
    burst is not stacked behind the silence already sent: what falls inside it
    is dropped. Stamped back to back, every stall added its length to the
    phone's playout delay.
    """

    #: No panel audio for this long: fill with silence until it comes back.
    #: Longer than any relay jitter, so a live call is never padded.
    FILL_AFTER = 0.2
    #: The panel's clock moved this far from ours: its stream restarted.
    RESYNC_AHEAD = _PCMU_RATE          # 1 s
    RESYNC_BEHIND = 10 * _PCMU_RATE    # 10 s
    _SILENCE = b"\xff" * 160

    def __init__(self) -> None:
        self.port = free_udp_port()
        self.sdp_path = write_sdp(
            "vimar_intercom_homekit_",
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Vimar\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {self.port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n")
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._dest = ("127.0.0.1", self.port)
        self._attached = False
        self._seq = random.randrange(1 << 16)
        self._ssrc = random.randrange(1, 1 << 32)
        # Our clock: the timestamp of the newest packet sent and when it left.
        self._last_ts = random.randrange(1 << 32)
        self._last_at: float | None = None
        # Panel timestamp → ours, while the panel's audio flows.
        self._offset: int | None = None
        self._panel_ssrc: int | None = None
        # Where the last silence we sent ends, on our clock.
        self._fill_end: int | None = None
        self._last_audio = 0.0
        self._filler: asyncio.Task | None = None
        self.filled = 0
        self.late_dropped = 0

    def _now_ts(self, now: float) -> int:
        """Our clock now: the newest packet plus the 20 ms slots gone by since
        it left (at least one, so time never stands still)."""
        if self._last_at is None:
            return self._last_ts
        slots = max(1, round((now - self._last_at) / _SLOT))
        return (self._last_ts + slots * 160) & _MASK32

    def _emit(self, payload: bytes, ts: int, now: float) -> None:
        header = struct.pack("!BBHII", 0x80, 0, self._seq, ts, self._ssrc)
        self._seq = (self._seq + 1) & 0xFFFF
        if self._last_at is None or _signed32(ts - self._last_ts) > 0:
            self._last_ts, self._last_at = ts, now
        try:
            self._sock.sendto(header + payload, self._dest)
        except OSError:
            pass

    def _send(self, rtp: bytes) -> None:
        """A decrypted packet of the call's audio."""
        if len(rtp) < 12 or rtp[1] & 0x7F != 0:  # PCMU only (not DTMF, not PCMA)
            return
        hlen = 12 + (rtp[0] & 0x0F) * 4
        if rtp[0] & 0x10 and len(rtp) >= hlen + 4:  # header extension
            hlen += 4 + int.from_bytes(rtp[hlen + 2:hlen + 4], "big") * 4
        if len(rtp) <= hlen:
            return
        panel_ts, ssrc = struct.unpack_from("!II", rtp, 4)
        now = time.monotonic()
        self._last_audio = now
        ts = None
        if self._offset is not None and ssrc == self._panel_ssrc:
            ts = (panel_ts + self._offset) & _MASK32
            drift = _signed32(ts - self._now_ts(now))
            if drift > self.RESYNC_AHEAD or drift < -self.RESYNC_BEHIND:
                ts = None                    # the panel's stream restarted
            elif self._fill_end is not None and _signed32(ts - self._fill_end) < 0:
                # Late: silence already went out in its place.
                self.late_dropped += 1
                return
        if ts is None:
            # First packet, after a restart: the panel's clock from here on is
            # tied to ours at this moment.
            ts = self._now_ts(now)
            self._offset = (ts - panel_ts) & _MASK32
            self._panel_ssrc = ssrc
        self._emit(rtp[hlen:], ts, now)

    def _fill_once(self) -> None:
        now = time.monotonic()
        if now - self._last_audio < self.FILL_AFTER:
            return
        ts = self._now_ts(now)
        self._emit(self._SILENCE, ts, now)
        self._fill_end = (ts + len(self._SILENCE)) & _MASK32
        self.filled += 1

    async def _fill(self) -> None:
        loop = asyncio.get_running_loop()
        tick = loop.time()
        try:
            while True:
                tick += _SLOT
                await asyncio.sleep(max(0.0, tick - loop.time()))
                if tick < loop.time() - 0.2:
                    tick = loop.time()  # the loop stalled: no burst to catch up
                self._fill_once()
        except asyncio.CancelledError:
            pass

    def attach(self) -> None:
        if media.audio_proto and not self._attached:
            media.audio_proto.rtp_sinks.append(self._send)
            self._attached = True
        if self._filler is None:
            try:
                self._filler = asyncio.get_running_loop().create_task(self._fill())
            except RuntimeError:  # no loop (tests): only the forwarding
                pass

    def close(self) -> None:
        if self._filler:
            self._filler.cancel()
            self._filler = None
        if self._attached and media.audio_proto and self._send in media.audio_proto.rtp_sinks:
            media.audio_proto.rtp_sinks.remove(self._send)
        self._attached = False
        self._sock.close()
        # What the relay cost this view: silence put in for lost audio, late packets dropped.
        _LOGGER.debug("HomeKit audio: %d silence packets filled, %d late packets dropped",
                      self.filled, self.late_dropped)
        try:
            os.unlink(self.sdp_path)
        except OSError:
            pass
