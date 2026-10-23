"""Video transcoded on the Pi: smoother, at a cost of a few tenths of a second.

Direct video delivers the panel packets to the phone as they arrive from the
relay. When the relay loses one (it happens, two or three in a hundred), the
phone can no longer rebuild the picture. It freezes, asks for a keyframe that
the panel ignores, and waits for the panel's next one, up to three seconds.
Measured on 26 September: 13 keyframe requests in one view with a car passing
by, and all losses upstream, none on the Wi-Fi.

Here ffmpeg decodes the panel video. It conceals a lost packet (a brief smear
shows, but the picture keeps moving) and re-encodes with a keyframe every
second: the worst the phone sees is one second, not three. It costs some CPU
(320x240 is nothing for a Pi 5) and a few tens of milliseconds.

One transcoder per call, shared by all views. It starts with the call (or with
the ring, when early media is available), and every view that opens reads
from a stream that is already warm. media_handler's video protocol is one per
hub, reused by every call, so "this call's" transcoder is told apart by the
accessory's call generation, not by the protocol it reads.
"""

from __future__ import annotations

import asyncio
import base64
import bisect
import logging
import socket
import struct
import time
from collections import OrderedDict

from .homekit_audio import (
    LOOPBACK_INPUT,
    _Datagrams,
    _unlink,
    ffmpeg_binary,
    free_udp_port,
    is_rtcp,
    log_stderr,
    port_bound,
    stop_ffmpeg,
    write_sdp,
)

_LOGGER = logging.getLogger(__name__)

# One keyframe per second: the worst case after a loss is one second, not three.
KEYFRAME_SECONDS = 1.0
KEYFRAME_INTERVAL = 15            # 1 s at 15 frames per second
# The panel frame rate (320x240 at 15 frames per second, measured).
PANEL_FPS = 15
# How long to wait for ffmpeg to open its input port.
BIND_TIMEOUT = 3.0
# A group longer than this (about 30 s of video) never saw its next SPS: the
# encoder stopped sending keyframes. Kept, it would grow for the whole call.
GOP_MAX_PACKETS = 1500
# Pictures for the Home app (the ring notification, the tile): two a second
# from the same decoder. It conceals lost packets, so it has a picture well
# before the frame grabber, which waits for a complete keyframe (up to 3 s
# on a panel that ignores keyframe requests, when the relay lost part of the
# first one).
SNAPSHOT_FPS = 2
# The frame-delay statistic: input frames remembered, output frames measured.
FRAME_DELAY_KEEP = 300               # 20 s at 15 fps
FRAME_DELAY_SAMPLES = 9000           # 10 minutes at 15 fps
FRAME_TICKS = 90000 // PANEL_FPS     # one frame in the 90 kHz RTP clock
# A JPEG of the panel's 320x240 is 10 to 30 KB; anything past this without
# an end marker is not one.
JPEG_MAX = 1024 * 1024


def _nal_types(packet: bytes) -> list[int]:
    """The NAL types of an H.264 RTP packet, including those inside STAP-A."""
    hlen = 12 + (packet[0] & 0x0F) * 4
    if len(packet) <= hlen:
        return []
    payload = packet[hlen:]
    t = payload[0] & 0x1F
    if t == 24:
        out, i = [], 1
        while i + 2 < len(payload):
            size = int.from_bytes(payload[i:i + 2], "big")
            out.append(payload[i + 2] & 0x1F)
            i += 2 + size
        return out
    if t == 28 and len(payload) >= 2:
        return [payload[1] & 0x1F]
    return [t]


class EncodedGop:
    """The last group out of the transcoder, from the SPS on.

    The transcoder puts SPS and PPS in front of every keyframe
    (``repeat-headers``), so the group always starts with a packet that
    carries the SPS, and we restart the group there.
    """

    def __init__(self) -> None:
        self.packets: list[bytes] = []
        self._idr_open = False
        self.has_keyframe = False
        # Too long a group was dropped: nothing is kept until the next SPS.
        self._overflow = False

    def add(self, packet: bytes) -> None:
        types = _nal_types(packet)
        if 7 in types:
            self.packets = []
            self.has_keyframe = False
            self._idr_open = False
            self._overflow = False
        elif self._overflow:
            return
        elif len(self.packets) >= GOP_MAX_PACKETS:
            self.packets = []
            self.has_keyframe = False
            self._idr_open = False
            self._overflow = True
            return
        self.packets.append(packet)
        if self.has_keyframe or 5 not in types:
            return
        hlen = 12 + (packet[0] & 0x0F) * 4
        payload = packet[hlen:]
        if (payload[0] & 0x1F) == 28:
            fu = payload[1]
            if fu & 0x80:
                self._idr_open = True
            if fu & 0x40 and self._idr_open:
                self.has_keyframe = True
        else:
            self.has_keyframe = True


class Transcoder:
    """ffmpeg that decodes the panel stream and re-encodes it for the phones."""

    def __init__(self, sps: bytes | None, pps: bytes | None) -> None:
        self._sps, self._pps = sps, pps
        self._in_port = 0
        self._sdp = ""
        self._feed = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._feed.setblocking(False)
        self._out_tr: asyncio.DatagramTransport | None = None
        self._enc_sender = None
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._sinks: list = []
        self._ready = False
        self._backlog_fn = lambda: ([], [])
        self._on_ready = None
        # The panel stream (media_handler's video protocol: one per hub, the
        # same for every call) whose packets feed this transcoder. Set by
        # whoever attaches it, to detach it from the same place.
        self.video_proto = None
        # The accessory's call generation this transcoder was started for.
        # The protocol cannot tell two calls apart; the generation can.
        self.generation: int | None = None
        self.gop = EncodedGop()
        self.stats = {"in": 0, "out": 0}
        # How far behind the panel the re-encoded video runs: for each input
        # frame (RTP timestamp, relative to the first one fed) the time its
        # last packet went to ffmpeg; for each output frame, the time it came
        # back. The first output frame is the first one fed (the group starts
        # at its keyframe). The encoder rounds its timestamps to its own
        # 15 fps clock while the panel's are slightly irregular, so a later
        # output frame matches the nearest input frame within half a frame.
        # A diagnostic for the log only: if ffmpeg drops that first frame (a
        # keyframe damaged on the relay), every later sample shifts by a frame
        # or stops matching, so compare views with that in mind.
        self._in_first_ts: int | None = None
        self._in_at: OrderedDict[int, float] = OrderedDict()
        self._out_first_ts: int | None = None
        self.frame_delays: list[float] = []
        # The latest decoded picture, as JPEG (SNAPSHOT_FPS).
        self.last_jpeg: bytes | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self, backlog_fn, on_ready=None, defer: bool = False) -> bool:
        """Start ffmpeg and, as soon as it listens, give it the panel group.

        ``backlog_fn`` returns ``(prefix, group)`` of the panel packets already
        in memory. Without them the transcoder would have no keyframe to start
        from until the panel's next one, three seconds later.

        ``defer``: only start ffmpeg; begin() later gives it the group and the
        live stream. An encoder started before the call's video exists (#48)
        must not take the first packets live: the relay delivers them a little
        out of order, ffmpeg's RTP input dropped the late one as "received too
        late", the first keyframe was broken, and the view fell back to the
        panel's next keyframe (3.6 s). The group replayed in sequence order is
        what the encoder always started from.
        """
        loop = asyncio.get_running_loop()
        self._out_tr, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self._from_encoder), local_addr=("127.0.0.1", 0))
        out_port = self._out_tr.get_extra_info("sockname")[1]
        # Picked right before ffmpeg takes it, so nobody else can in between.
        self._in_port = free_udp_port()
        sprop = ""
        if self._sps and self._pps:
            sprop = (";sprop-parameter-sets="
                     f"{base64.b64encode(self._sps).decode()},"
                     f"{base64.b64encode(self._pps).decode()}")
        # The frame rate must be declared. The panel SPS carries no timing
        # info, and without it ffmpeg assumes 90000 frames per second (the RTP
        # clock). The encoder then spreads 300 kbit over ninety thousand frames:
        # three bits each, which is a grey blur. Seen on 26 September.
        sdp = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Vimar transcode\r\n"
               "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
               f"m=video {self._in_port} RTP/AVP 96\r\n"
               "a=rtpmap:96 H264/90000\r\n"
               f"a=framerate:{PANEL_FPS}\r\n"
               f"a=fmtp:96 packetization-mode=1{sprop}\r\n")
        self._sdp = await loop.run_in_executor(
            None, write_sdp, "vimar_intercom_transcode_", sdp)
        self._proc = await asyncio.create_subprocess_exec(
            ffmpeg_binary(), "-hide_banner", "-nostats", "-loglevel", "warning",
            "-protocol_whitelist", "file,udp,rtp",
            "-analyzeduration", "0", "-probesize", "32",
            # A tenth of a second to reorder the packets the relay delivers out
            # of order, then move on. More would only add delay.
            "-max_delay", "100000",
            # One decoding thread. ffmpeg's default frame threading (4 threads
            # on a Pi 5) keeps 4 frames in the decoder: measured on a 40517,
            # every re-encoded frame reached the phone 335-345 ms after it was
            # fed (p90 up to 538 ms); 69 ms with one thread on the synthetic
            # stream. 320x240 is nothing for one core. The photo and passive
            # stream decoders already run this way.
            "-threads", "1",
            *LOOPBACK_INPUT, "-i", self._sdp,
            "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-profile:v", "baseline", "-pix_fmt", "yuv420p",
            # One output frame per input frame. Without this, ffmpeg does not
            # know the panel frame rate, assumes a higher one and duplicates
            # frames to fill it: twice the bits for nothing.
            "-fps_mode", "passthrough",
            # Keyframes by time, not by count: the panel frame rate fluctuates.
            "-force_key_frames", f"expr:gte(t,n_forced*{KEYFRAME_SECONDS})",
            "-g", str(KEYFRAME_INTERVAL * 2), "-sc_threshold", "0", "-bf", "0",
            "-b:v", "300k", "-maxrate", "450k", "-bufsize", "300k",
            # One slice per frame, one thread: the usual shape for HomeKit
            # (otherwise zerolatency cuts every frame in three), and at 320x240
            # one core is more than enough.
            # The frame rate is repeated to the encoder, which uses it to set
            # the bits per frame.
            "-threads", "1",
            "-x264-params", f"repeat-headers=1:slices=1:fps={PANEL_FPS}",
            "-payload_type", "96", "-f", "rtp",
            f"rtp://127.0.0.1:{out_port}?pkt_size=1200",
            # Second output: pictures for the Home app, from the same decoder.
            "-an", "-vf", f"fps={SNAPSHOT_FPS}", "-c:v", "mjpeg", "-q:v", "5",
            "-f", "image2pipe", "pipe:1",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        # The decoder reports every lost packet it conceals. That is its job,
        # not a failure, and with the relay losing two in a hundred it would
        # flood the Home Assistant log.
        self._tasks.append(asyncio.create_task(
            log_stderr(self._proc, "ffmpeg transcode", concealment_is_normal=True)))
        if self._proc.stdout is not None:
            self._tasks.append(asyncio.create_task(self._read_pictures(self._proc.stdout)))

        # ffmpeg opens the port only after reading the SDP: anything that
        # arrives earlier is lost. We wait until the port is taken.
        waited = 0.0
        while not port_bound(self._in_port) and waited < BIND_TIMEOUT:
            if not self.running:
                return False
            await asyncio.sleep(0.02)
            waited += 0.02
        if not self.running:
            return False
        self._backlog_fn, self._on_ready = backlog_fn, on_ready
        if defer:
            _LOGGER.debug("Transcoder ready, waiting for the call's video")
        else:
            self.begin()
        return True

    @property
    def begun(self) -> bool:
        return self._ready

    def begin(self) -> None:
        """Give ffmpeg the panel group in sequence order, then the live stream.
        No await inside: the two join with no lost or duplicated packet."""
        if self._ready:
            return
        prefix, gop = self._backlog_fn()
        for pkt in list(prefix) + list(gop):
            self._send(pkt)
        self._ready = True
        if self._on_ready:
            self._on_ready()    # anchor to the live stream, at the same instant
        _LOGGER.info("Transcoding started (%d panel packets to start from)",
                     len(prefix) + len(gop))

    async def _read_pictures(self, stdout) -> None:
        """Keep the latest JPEG. Read to the end whatever happens: a full
        pipe would block ffmpeg, and the phones' video with it."""
        buf = b""
        while True:
            try:
                chunk = await stdout.read(65536)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Transcoder pictures: read failed", exc_info=True)
                return
            if not chunk:
                return
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    buf = b""
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end < 0:
                    buf = buf[start:]
                    if len(buf) > JPEG_MAX:
                        buf = b""
                    break
                self.last_jpeg = buf[start:end + 2]
                buf = buf[end + 2:]

    def feed(self, packet: bytes) -> None:
        """A live packet from the panel."""
        if self._ready:
            self._send(packet)

    def _send(self, packet: bytes) -> None:
        try:
            self._feed.sendto(packet, ("127.0.0.1", self._in_port))
            self.stats["in"] += 1
        except OSError:
            return
        if len(packet) >= 12:
            ts = struct.unpack_from("!I", packet, 4)[0]
            if self._in_first_ts is None:
                self._in_first_ts = ts
            rel = (ts - self._in_first_ts) & 0xFFFFFFFF
            self._in_at[rel] = time.monotonic()   # the frame's last packet so far
            self._in_at.move_to_end(rel)
            while len(self._in_at) > FRAME_DELAY_KEEP:
                self._in_at.popitem(last=False)

    def _from_encoder(self, data: bytes, addr) -> None:
        if len(data) < 12 or is_rtcp(data):
            return
        # Only our ffmpeg: the first sender that writes to this socket.
        if self._enc_sender is None:
            self._enc_sender = addr
        elif addr != self._enc_sender:
            return
        self.stats["out"] += 1
        if data[1] & 0x80:                        # the last packet of an output frame
            ts = struct.unpack_from("!I", data, 4)[0]
            if self._out_first_ts is None:
                self._out_first_ts = ts
            fed = self._fed_at((ts - self._out_first_ts) & 0xFFFFFFFF)
            if fed is not None and len(self.frame_delays) < FRAME_DELAY_SAMPLES:
                self.frame_delays.append(time.monotonic() - fed)
        self.gop.add(data)
        for sink in tuple(self._sinks):
            try:
                sink(data)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Forwarding the transcoded video failed")

    def add_sink(self, sink) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    async def stop(self) -> None:
        self._ready = False
        self._sinks.clear()
        # ffmpeg first: its readers drain stdout and stderr until it is gone,
        # so it never blocks on a full pipe while it shuts down.
        await stop_ffmpeg(self._proc)
        for task in self._tasks:
            task.cancel()
        if self._out_tr:
            self._out_tr.close()
        self._feed.close()
        if self._sdp:
            await asyncio.get_running_loop().run_in_executor(None, _unlink, self._sdp)
            self._sdp = ""
        _LOGGER.info("Transcoding stopped: %d panel packets in, %d transcoded packets out%s",
                     self.stats["in"], self.stats["out"], self.delay_summary())

    def _fed_at(self, rel: int) -> float | None:
        """When the input frame nearest to `rel` was fed, within half a frame."""
        rels = sorted(self._in_at)
        i = bisect.bisect_left(rels, rel)
        near = [r for r in rels[max(0, i - 1):i + 1] if abs(r - rel) <= FRAME_TICKS // 2]
        if not near:
            return None
        return self._in_at[min(near, key=lambda r: abs(r - rel))]

    def delay_summary(self) -> str:
        """", frame delay median 68 ms (p90 75 ms, 120 frames)", or "" with none."""
        d = sorted(self.frame_delays)
        if not d:
            return ""
        return (f", frame delay median {d[len(d) // 2] * 1000:.0f} ms"
                f" (p90 {d[int(len(d) * 0.9)] * 1000:.0f} ms, {len(d)} frames)")
