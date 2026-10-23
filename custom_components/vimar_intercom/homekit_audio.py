"""Two-way audio bridge between the phone and the intercom.

Kept apart from the accessory because it depends on neither Home Assistant nor
pyhap: it is tested on its own, with real ffmpeg and real SRTP packets.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
from collections import deque

from . import media_handler as media
from .const import DECODER_CONCEALMENT
from .srtp import SRTCPContext, SRTPContext

_LOGGER = logging.getLogger(__name__)

# ffmpeg stamps Opus RTP at 48 kHz whatever the real sample rate (RFC 7587).
# HomeKit expects the clock at the negotiated rate. We convert in both
# directions. Home Assistant's proxy applies the same fix.
OPUS_RTP_CLOCK = 48000
# Payload type of the phone audio as it reaches our decoder. We fix it
# ourselves, so the decoder SDP can be written before we know what the phone
# will pick.
TALK_PT = 110
# 20 ms of 8 kHz, 16-bit mono PCM: one RTP packet of the outdoor station.
PCM_FRAME = 320
PCM_FRAME_MS = 20
# A voice decoder that fails to start is tried again at most once a second,
# and after this many failures in a row not again for the same view.
TALK_RELAUNCH_S = 1.0
TALK_MAX_FAILURES = 5
# ffmpeg binds the ports of an SDP input on every address unless told
# otherwise (the RTCP port did, on ffmpeg 8.1). Our inputs only ever come
# from this host.
LOOPBACK_INPUT = ("-localaddr", "127.0.0.1")


# ─── RTP helpers ────────────────────────────────────────────────────


_MASK32 = 0xFFFFFFFF


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value & 0x80000000 else value


class TimestampScaler:
    """Moves RTP timestamps from one clock to another (× num/den).

    Relative to a base, not ``ts * num // den`` on its own: that jumps when the
    input wraps at 2^32 (every 25 hours at 48 kHz, but ffmpeg and the phone
    start from a random value, so it can come at any moment).
    """

    def __init__(self, num: int, den: int) -> None:
        self._num, self._den = num, den
        self._base_in: int | None = None
        self._base_out = 0

    def __call__(self, packet: bytes) -> bytearray:
        out = bytearray(packet)
        ts = struct.unpack_from("!I", out, 4)[0]
        if self._base_in is None:
            self._base_in, self._base_out = ts, (ts * self._num // self._den) & 0xFFFFFFFF
        delta = _signed32(ts - self._base_in)
        scaled = (self._base_out + delta * self._num // self._den) & 0xFFFFFFFF
        if abs(delta) > 1 << 30:
            # Move the base along before the difference itself wraps.
            self._base_in, self._base_out = ts, scaled
        struct.pack_into("!I", out, 4, scaled)
        return out


def set_payload_type(packet: bytearray, pt: int) -> bytearray:
    """Change the payload type and leave the marker bit alone."""
    packet[1] = (packet[1] & 0x80) | (pt & 0x7F)
    return packet


def is_rtcp(packet: bytes) -> bool:
    """RTCP multiplexed on the same port as RTP (RFC 5761)."""
    return len(packet) >= 2 and 200 <= packet[1] <= 206


# Ports handed out for an ffmpeg that may not have bound them yet. None is
# handed out again meanwhile: the transcoder's and a view's ffmpeg start within
# milliseconds of each other, and one got "bind failed: Address in use".
_RESERVED: dict[int, float] = {}
RESERVE_SECONDS = 10.0
# The audio tap picks its port in an executor thread, the transcoder and the
# talk decoder on the event loop: the probe and the reservation are one step.
_RESERVE_LOCK = threading.Lock()


def _ephemeral_port() -> int:
    """A port the kernel considers free right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def free_udp_port() -> int:
    """An even loopback UDP port with the next one free too, for an ffmpeg RTP
    input: ffmpeg binds the port and port + 1 (RTCP). Free now: we close it and
    ffmpeg reopens it, so call it right before starting that ffmpeg. A pair
    handed out in the last RESERVE_SECONDS is not handed out again."""
    with _RESERVE_LOCK:
        now = time.monotonic()
        for port, at in list(_RESERVED.items()):
            if now - at > RESERVE_SECONDS:
                del _RESERVED[port]
        for _ in range(50):
            port = _ephemeral_port() & ~1
            if port in _RESERVED:
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as a, \
                        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as b:
                    a.bind(("127.0.0.1", port))
                    b.bind(("127.0.0.1", port + 1))
            except OSError:
                continue
            _RESERVED[port] = now
            return port
        raise OSError("no free UDP port pair on loopback")


def write_sdp(prefix: str, text: str) -> str:
    """A private temporary SDP file (mode 0600, a name nobody can guess or
    plant a symlink on). Blocking: run it in an executor."""
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".sdp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def ffmpeg_binary() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


class _Datagrams(asyncio.DatagramProtocol):
    """A UDP endpoint that hands every packet to a function."""

    def __init__(self, on_packet) -> None:
        self._on_packet = on_packet

    def datagram_received(self, data: bytes, addr) -> None:
        self._on_packet(data, addr)

    def error_received(self, exc: Exception) -> None:
        # ICMP "port unreachable" and similar: on loopback, while an ffmpeg
        # starts or stops, they are normal and do not go to the log.
        pass


# ─── The audio bridge: both directions on a single port ────────────


class AudioBridge:
    """Voice in both directions, on the port we announced to the phone.

    A single socket toward the phone, because symmetric RTP requires it: the
    street audio leaves from it, and the answering person's voice arrives on it.

    - street → phone: ffmpeg encodes Opus and sends plain RTP to a local
      socket. Here we fix the clock, encrypt and send.
    - phone → street: we decrypt, set the clock back to 48 kHz and pass the
      packet to a second ffmpeg that decodes to 8 kHz PCM. The PCM goes to the
      outdoor station through ``on_pcm`` (that is, ``media.send_audio``).
    """

    def __init__(self, phone_sock: socket.socket, phone_addr: tuple[str, int],
                 srtp_key_b64: str, rate_hz: int, on_pcm, on_first_voice=None) -> None:
        self._phone_sock = phone_sock
        # Called once, at the first voice packet from the phone (Talk pressed):
        # the phone sends no audio at all while Talk is off.
        self._on_first_voice = on_first_voice
        # Called once, as the first audio packet leaves for the phone (the
        # view's timeline).
        self.on_first_audio = None
        self._voice_fired = False
        self._loud_ms = 0
        self._phone_addr = phone_addr
        self._rate = rate_hz
        self._on_pcm = on_pcm
        # Two contexts: the rollover counter is per direction.
        self._tx = SRTPContext(srtp_key_b64)
        self._rx = SRTPContext(srtp_key_b64)
        self._tx_rtcp = SRTCPContext(srtp_key_b64)
        # ffmpeg picks the SSRC (-ssrc). We learn it from the first packet.
        self._ssrc: int | None = None
        self._phone_tr: asyncio.DatagramTransport | None = None
        self._enc_tr: asyncio.DatagramTransport | None = None
        self._talk_tr: asyncio.DatagramTransport | None = None
        self._enc_sender = None
        self._talk_port = 0
        self._talk_sdp = ""
        self._talk_proc: asyncio.subprocess.Process | None = None
        self._talk_ready = False
        self._talk_starting = False
        self._talk_pending: deque[bytes] = deque(maxlen=100)
        self._talk_next_launch = 0.0
        self._talk_failures = 0
        self._stopping = False
        self._tasks: set[asyncio.Task] = set()
        # Timestamps: to the phone at the negotiated rate, to the decoder at 48 kHz.
        self._to_phone_clock = TimestampScaler(rate_hz, OPUS_RTP_CLOCK)
        self._to_decoder_clock = TimestampScaler(OPUS_RTP_CLOCK, rate_hz)
        self.encoder_port = 0
        self.stats = {"to_phone": 0, "from_phone": 0, "auth_fail": 0, "replayed": 0,
                      "pcm_frames": 0}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._phone_tr, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self._from_phone), sock=self._phone_sock)
        self._enc_tr, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self._from_encoder), local_addr=("127.0.0.1", 0))
        self.encoder_port = self._enc_tr.get_extra_info("sockname")[1]
        self._talk_tr, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(lambda *_: None), local_addr=("127.0.0.1", 0))
        # The voice decoder starts with the first voice packet, not earlier.
        # An ffmpeg that reads RTP and receives nothing for ten seconds exits
        # on its own ("Operation timed out"). Whoever opened the view and
        # waited a while before pressing "Talk" was then not heard in the street.

    def _track(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _deliver_voice(self, packet: bytes) -> None:
        """Send the voice to the decoder, (re)starting it if it is not running."""
        if self._talk_ready and self._talk_tr:
            self._talk_tr.sendto(packet, ("127.0.0.1", self._talk_port))
            return
        # Meanwhile we hold it back, so the first syllable is not lost.
        self._talk_pending.append(packet)
        if (not self._talk_starting and not self._stopping
                and self._talk_failures < TALK_MAX_FAILURES):
            self._talk_starting = True
            self._track(self._start_talk_decoder())

    async def _start_talk_decoder(self) -> None:
        """Launch the decoder, at most once a second, and give up on this view
        after TALK_MAX_FAILURES failures in a row."""
        loop = asyncio.get_running_loop()
        try:
            delay = self._talk_next_launch - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._talk_next_launch = loop.time() + TALK_RELAUNCH_S
            try:
                ok = await self._launch_talk_decoder()
            except OSError as err:
                _LOGGER.debug("HomeKit: voice decoder did not start: %s", err)
                ok = False
        finally:
            self._talk_starting = False
        if ok:
            self._talk_failures = 0
            return
        self._talk_failures += 1
        if self._talk_failures >= TALK_MAX_FAILURES and not self._stopping:
            _LOGGER.warning("HomeKit: the voice decoder failed to start %d times, "
                            "the phone's voice will not reach the intercom in this view",
                            self._talk_failures)

    async def _launch_talk_decoder(self) -> bool:
        """Start the voice decoder. True once it listens."""
        loop = asyncio.get_running_loop()
        if self._talk_sdp:
            await loop.run_in_executor(None, _unlink, self._talk_sdp)
            self._talk_sdp = ""
        # The port is picked here, right before ffmpeg takes it, not when the
        # view opened: the longer it stays free, the likelier someone else takes it.
        self._talk_port = free_udp_port()
        sdp = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 127.0.0.1\r\n"
            "s=Vimar talkback\r\n"
            "c=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\n"
            f"m=audio {self._talk_port} RTP/AVP {TALK_PT}\r\n"
            f"a=rtpmap:{TALK_PT} opus/48000/2\r\n"
        )
        # Shielded: a stop() that cancels us while the executor writes the
        # file would otherwise leave it behind, its name never assigned.
        written = loop.run_in_executor(None, write_sdp, "vimar_intercom_talk_", sdp)
        try:
            self._talk_sdp = await asyncio.shield(written)
        except asyncio.CancelledError:
            written.add_done_callback(_unlink_when_written)
            raise
        if self._stopping:
            return False
        self._talk_proc = await asyncio.create_subprocess_exec(
            ffmpeg_binary(), "-hide_banner", "-nostats", "-loglevel", "warning",
            "-protocol_whitelist", "file,udp,rtp",
            # The codec is declared in the SDP, so there is nothing to probe.
            # Every tenth of a second here is a tenth of delay on the answering voice.
            "-analyzeduration", "0", "-probesize", "32",
            # A lost packet must not stall the voice. ffmpeg waits 100 ms for
            # it by default. 40 ms is enough for packets that are only late.
            "-max_delay", "40000",
            *LOOPBACK_INPUT, "-i", self._talk_sdp,
            "-f", "s16le", "-ac", "1", "-ar", "8000", "pipe:1",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc = self._talk_proc
        self._track(self._pump_pcm())
        self._track(log_stderr(proc, "ffmpeg voice", timeout_is_normal=True))
        # Ready once it listens. Then we give it what we held back.
        waited = 0.0
        while not port_bound(self._talk_port) and waited < 3.0:
            if proc.returncode is not None or self._stopping:
                return False
            await asyncio.sleep(0.02)
            waited += 0.02
        if proc.returncode is not None or self._stopping:
            return False
        self._talk_ready = True
        while self._talk_pending and self._talk_tr:
            self._talk_tr.sendto(self._talk_pending.popleft(),
                                 ("127.0.0.1", self._talk_port))
        self._track(self._watch_talk_decoder(proc))
        return True

    async def _watch_talk_decoder(self, proc) -> None:
        """When it exits (long silence), it restarts at the next "Talk"."""
        await proc.wait()
        if self._talk_proc is proc:
            self._talk_ready = False
            self._talk_proc = None
            if not self._stopping:
                _LOGGER.debug("HomeKit: voice decoder stopped "
                              "(no voice for a while), it restarts on the next one")

    def _from_encoder(self, data: bytes, addr) -> None:
        """Street → phone: from the encoding ffmpeg to the phone, encrypted."""
        if len(data) < 12 or is_rtcp(data) or not self._phone_tr:
            return
        # Only our ffmpeg: the first sender that writes to this socket.
        if self._enc_sender is None:
            self._enc_sender = addr
        elif addr != self._enc_sender:
            return
        if self._ssrc is None:
            self._ssrc = struct.unpack_from("!I", data, 8)[0]
        packet = self._to_phone_clock(data)
        self._phone_tr.sendto(self._tx.protect(bytes(packet)), self._phone_addr)
        if not self.stats["to_phone"] and self.on_first_audio:
            try:
                self.on_first_audio()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("HomeKit: first-audio callback failed")
        self.stats["to_phone"] += 1

    def _from_phone(self, data: bytes, addr) -> None:
        """Phone → street: the answering person's voice."""
        if addr[0] != self._phone_addr[0] or len(data) < 12 or is_rtcp(data):
            return
        replayed = self._rx.replayed
        plain = self._rx.unprotect(data)
        if plain is None and self._rx.replayed != replayed:
            self.stats["replayed"] += 1  # authentic, but a packet already received
            return
        if plain is None:
            self.stats["auth_fail"] += 1
            if self.stats["auth_fail"] in (1, 50):
                _LOGGER.warning("HomeKit: audio from the phone fails to decrypt "
                                "(%d packets)", self.stats["auth_fail"])
            return
        if self.stats["from_phone"] == 0:
            _LOGGER.info("HomeKit: audio arriving from the phone (%dB)", len(data))
        self.stats["from_phone"] += 1
        packet = set_payload_type(self._to_decoder_clock(plain), TALK_PT)
        self._deliver_voice(bytes(packet))

    def _note_voice(self, frame: bytes) -> None:
        """First voice = someone is talking, not just packets from the phone.

        The first authenticated packet was the trigger before; if iOS sends
        audio with the microphone closed, opening the view would answer the
        ring. Same rule as the voice answer on /audio_ws: VOICE_ANSWER_MS of
        decoded audio above VOICE_RMS."""
        if self._voice_fired or not self._on_first_voice:
            return
        if media.rms(frame) >= media.VOICE_RMS:
            self._loud_ms += PCM_FRAME_MS
        else:
            self._loud_ms = 0
        if self._loud_ms >= media.VOICE_ANSWER_MS:
            self._voice_fired = True
            _LOGGER.info("HomeKit: voice from the phone (%d ms above the threshold)",
                         self._loud_ms)
            try:
                self._on_first_voice()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("HomeKit: first-voice callback failed")

    async def _pump_pcm(self) -> None:
        """Decoded PCM, in 20 ms packets, to the outdoor station."""
        proc = self._talk_proc
        if proc is None or not proc.stdout:
            return
        buf = b""
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    return
                buf += chunk
                while len(buf) >= PCM_FRAME:
                    frame, buf = buf[:PCM_FRAME], buf[PCM_FRAME:]
                    self._note_voice(frame)
                    self._on_pcm(frame)
                    self.stats["pcm_frames"] += 1
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.exception("HomeKit: error forwarding the voice to the intercom")

    def send_bye(self) -> None:
        """RTCP BYE on the audio: "this stream has ended"."""
        if self._phone_tr is not None and self._ssrc is not None:
            bye = struct.pack("!BBHI", 0x81, 203, 1, self._ssrc)
            self._phone_tr.sendto(self._tx_rtcp.protect(bye), self._phone_addr)

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._tasks):
            task.cancel()
        if self._talk_proc and self._talk_proc.returncode is None:
            try:
                self._talk_proc.kill()
            except ProcessLookupError:
                pass    # it exited on its own, not reaped yet
            try:
                await asyncio.wait_for(self._talk_proc.wait(), 2.0)
            except TimeoutError:
                pass
        for tr in (self._phone_tr, self._enc_tr, self._talk_tr):
            if tr:
                tr.close()
        if self._talk_sdp:
            await asyncio.get_running_loop().run_in_executor(
                None, _unlink, self._talk_sdp)
            self._talk_sdp = ""
        _LOGGER.info("HomeKit: audio bridge closed. To phone %d packets, "
                     "from phone %d (%d failed to decrypt, %d replayed), %d voice frames "
                     "to the intercom", self.stats["to_phone"], self.stats["from_phone"],
                     self.stats["auth_fail"], self.stats["replayed"], self.stats["pcm_frames"])


async def stop_ffmpeg(proc) -> None:
    """Stop ffmpeg for real, and quickly.

    Once initialised, ffmpeg ignores a single SIGTERM while it sits in the UDP
    poll (its interrupt check wants two signals), which cost three seconds per
    close. So: one SIGTERM, a short wait, a second one, then SIGKILL.
    """
    if not proc or proc.returncode is not None:
        return
    for signal_, wait in ((proc.terminate, 0.1), (proc.terminate, 2.0), (proc.kill, None)):
        try:
            signal_()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), wait)
            return
        except TimeoutError:
            continue


def port_bound(port: int) -> bool:
    """Whether someone has already opened this UDP port on loopback."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _unlink_when_written(written: asyncio.Future) -> None:
    """Remove an SDP file whose writer was cancelled while it wrote."""
    if written.cancelled() or written.exception() is not None:
        return
    asyncio.get_running_loop().run_in_executor(None, _unlink, written.result())



async def log_stderr(proc: asyncio.subprocess.Process, label: str,
                     concealment_is_normal: bool = False,
                     timeout_is_normal: bool = False) -> None:
    """Log what ffmpeg prints: at DEBUG, except real failures."""
    if not proc.stderr:
        return
    closing = False
    try:
        while line := await proc.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            low = text.lower()
            # After "immediate exit requested" we asked for the shutdown:
            # the lines that follow (muxer, trailer, file) are not failures.
            closing = closing or "immediate exit requested" in low
            benign = (closing or "exiting normally" in low
                      or (concealment_is_normal and any(c in low for c in DECODER_CONCEALMENT))
                      or (timeout_is_normal and ("operation timed out" in low
                                                 or "no filtered frames" in low
                                                 or "output file is empty" in low)))
            if not benign and any(w in low for w in ("error", "failed", "invalid", "unable")):
                _LOGGER.warning("%s: %s", label, text)
            else:
                _LOGGER.debug("%s: %s", label, text)
    except asyncio.CancelledError:
        pass
