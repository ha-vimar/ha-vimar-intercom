"""Vimar Intercom — Media: RTP transport, STUN, G.711 codec, video capture, audio."""

import array
import asyncio
import base64
import contextlib
import logging
import math
import os
import random
import socket
import struct
import subprocess
import sys
import time

from . import av_stream, frame_grabber, rtcp
from . import runtime as R
from .const import RTP_AUDIO_PORT, RTP_VIDEO_PORT
from .srtp import SRTPContext

_LOGGER = logging.getLogger(__name__)

# ─── Broadcast callback (set by main.py) ────────────────────────────
_broadcast = None


def init(broadcast_fn):
    global _broadcast
    _broadcast = broadcast_fn


async def broadcast(msg_type, msg):
    if _broadcast:
        await _broadcast(msg_type, msg)


# ─── G.711 μ-law codec ──────────────────────────────────────────────

def _build_ulaw_decode_table():
    table = []
    for byte_val in range(256):
        b = ~byte_val & 0xFF
        sign = b & 0x80
        exponent = (b >> 4) & 0x07
        mantissa = b & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        table.append(-sample if sign else sample)
    return table

_ULAW_DECODE = _build_ulaw_decode_table()
# The two bytes of each 16-bit sample, so decoding is two bytes.translate().
_ULAW_LO = bytes(struct.pack("<h", value)[0] for value in _ULAW_DECODE)
_ULAW_HI = bytes(struct.pack("<h", value)[1] for value in _ULAW_DECODE)


def ulaw_decode(data: bytes) -> bytes:
    """μ-law bytes → 16-bit signed LE PCM.

    Two translate() calls over byte tables instead of a struct.pack_into loop:
    measured 1.5 µs against 27.9 µs per packet, byte-identical output. It is
    time taken off the event loop for every audio packet.
    """
    out = bytearray(len(data) * 2)
    out[0::2] = data.translate(_ULAW_LO)
    out[1::2] = data.translate(_ULAW_HI)
    return bytes(out)


def _ulaw_encode_sample(sample: int) -> int:
    """One 16-bit signed sample → its μ-law code (the reference algorithm)."""
    BIAS = 0x84
    CLIP = 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, CLIP) + BIAS
    exp = 7
    mask = 0x4000
    while exp > 0 and not (sample & mask):
        exp -= 1
        mask >>= 1
    mantissa = (sample >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mantissa)) & 0xFF


def _build_ulaw_encode_table() -> bytes:
    """The μ-law code of every 16-bit sample, indexed by its unsigned value.

    Built from the shape of the code rather than one sample at a time (that
    took 80 ms at import): after the bias, the magnitudes whose top bit is
    bit ``exp + 7`` share the exponent, and inside that range each of the 16
    mantissa values covers a run of ``2 ** (exp + 3)`` consecutive magnitudes.
    The negative half is the positive one mirrored with the sign bit flipped
    (the complement at the end turns ``sign | code`` into ``code ^ 0x80``).
    """
    BIAS = 0x84
    CLIP = 32635
    runs = []
    for exp in range(8):
        for mantissa in range(16):
            code = (~((exp << 4) | mantissa)) & 0xFF
            runs.append(bytes([code]) * (1 << (exp + 3)))
    # Index: the biased magnitude minus 0x80 (the first run starts at the
    # smallest magnitude with bit 7 set; the bias alone is above it).
    by_magnitude = b"".join(runs)
    first, last = BIAS - 0x80, CLIP + BIAS - 0x80
    clipped = by_magnitude[last]
    # Samples 0 .. 32767 (unsigned 0 .. 32767): magnitude = sample.
    positive = by_magnitude[first:last + 1] + bytes([clipped]) * (32768 - CLIP - 1)
    # Samples -32768 .. -1 (unsigned 32768 .. 65535): magnitude 32768 .. 1.
    flip_sign = bytes(b ^ 0x80 for b in range(256))
    negative = (positive[1:] + bytes([clipped]))[::-1].translate(flip_sign)
    return positive + negative


_ULAW_ENCODE = _build_ulaw_encode_table()


def ulaw_encode(pcm_data: bytes) -> bytes:
    """16-bit signed LE PCM → μ-law bytes.

    One table lookup per sample instead of the exponent loop: 13 µs against
    113 µs for a 20 ms packet (160 samples) on a Raspberry Pi 5, the same
    bytes out. The talk path encodes one packet every 20 ms, on the event
    loop, for as long as someone talks (the card's microphone, HomeKit's
    Talk, the away message).
    """
    n = len(pcm_data) // 2
    samples = array.array("H")
    samples.frombytes(pcm_data[:n * 2])
    if sys.byteorder != "little":  # pragma: no cover - PCM is little-endian, the host may not be
        samples.byteswap()
    return bytes(map(_ULAW_ENCODE.__getitem__, samples))


# Voce sul WS mentre squilla = «Rispondi»: RMS del PCM16 sopra VOICE_RMS per almeno
# VOICE_ANSWER_MS di fila (il rumore del microfono sta sotto i 300, la voce a 8 kHz
# sopra i 2000). Serve a chi risponde parlando da Echo Show o HomeKit via Scrypted.
VOICE_RMS = 800
VOICE_ANSWER_MS = 200


def rms(pcm: bytes) -> float:
    a = array.array("h", pcm[:len(pcm) & ~1])
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


# ─── RTP Protocols ──────────────────────────────────────────────────
# Le porte dell'ffmpeg di /av si leggono da av_stream (non da const) a ogni
# pacchetto: il banco di prova (tests/harness/rig.py) le sostituisce lì.

def _replay_dropped(proto, kind: str) -> None:
    """An authentic packet the SRTP context refused as a replay: debug on the call's first one."""
    if proto.srtp_rx.replayed == 1:
        _LOGGER.debug("SRTP %s: dropped a packet already received (replay)", kind)


def _from_the_call(proto, addr) -> bool:
    """Plain RTP only from the other end of the current call.

    SRTP is authenticated by its key. Plain RTP is not: without this check any
    host that can reach the port could inject audio or video into the call, and
    an injected SPS would be kept for the next calls. Only the IP is compared:
    the relay sends from other ports than the ones in the SDP.
    """
    remote = proto.remote_addr
    if bool(remote) and addr[0] == remote[0]:
        return True
    # In local UDP mode the intercom's own address is trusted too: on a 2-wire
    # plant the SIP gateway can act as a B2BUA and send the media from its
    # own address rather than the one in the SDP.
    if bool(remote) and R.USE_LOCAL_UDP and R.LOCAL_PROXY and addr[0] == R.LOCAL_PROXY:
        return True
    # Once per call (setup_media assigns a new remote_addr tuple): enough to
    # explain a silent stream without flooding the log. A WARNING, since the
    # symptom is a call with no audio or video.
    if remote and getattr(proto, "_foreign_logged", None) is not remote:
        proto._foreign_logged = remote
        _LOGGER.warning(
            "Plain RTP from %s dropped: the call's media is at %s. If this "
            "address is your intercom or its gateway, please report it.",
            addr[0], remote[0])
    return False


class RTPAudioProtocol(asyncio.DatagramProtocol):
    """Audio: receive (S)RTP PCMU → [decrypt] → decode → buffer. Send as (S)RTP.

    SRTP è opzionale: i contesti srtp_rx/srtp_tx vengono creati in setup_media
    solo se il remoto negozia a=crypto (media_enc). Quando sono None il traffico
    è RTP in chiaro (caso di default su questo impianto).
    Also forwards RTP to a secondary port for AV ffmpeg."""

    def __init__(self):
        self.transport = None
        self.remote_addr = None
        # 1 s: a backlog longer than that is dropped, not played late (was 4 s).
        # Not less: the event loop can stall 200-500 ms, and a shorter queue
        # dropped that voice instead of delivering it a little late (#54).
        self.audio_buffer = asyncio.Queue(maxsize=50)
        # Receive order (#53): the relay loses and reorders a few packets in a
        # hundred, and a 20 ms block played out of place, or skipped, is a click.
        self._a_ssrc = None
        self._a_next = None
        self._a_buf: dict[int, bytes] = {}
        self._a_last: bytes | None = None
        self._a_late = 0  # "already played" packets in a row
        self.rtp_seq = random.randint(0, 65535)
        self.rtp_ts = random.randint(0, 2**32 - 1)
        self.rtp_ssrc = random.randint(0, 2**32 - 1)
        self.pkt_count = 0
        self.srtp_rx: SRTPContext | None = None
        self.srtp_tx: SRTPContext | None = None
        # Voce in uscita: μ-law in attesa del pacer (_tx_loop, 160 B ogni 20 ms).
        self.tx_buf = bytearray()
        self.tx_enabled = False   # False durante l'anteprima dello squillo
        self.tx_count = 0
        # Forward decrypted RTP to the AV ffmpeg, only while it's running.
        self.ffmpeg_av_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.forward_av = False
        self.av_rtp = av_stream.AvRtp(160)
        # Callables that receive every decrypted audio RTP packet of the call
        # (the HomeKit doorbell feeds its own ffmpeg from here).
        self.rtp_sinks: list = []

    def connection_made(self, transport):
        self.transport = transport
        _LOGGER.info("RTP Audio ready on :%d", RTP_AUDIO_PORT)

    def datagram_received(self, data, addr):
        if not self.remote_addr:  # media chiuso (stop_media): RTP in ritardo dalla targa, via
            return
        if len(data) < 4:
            return
        if (data[0] & 0xC0) == 0x00:  # STUN
            return
        if (data[0] & 0xC0) != 0x80:  # not RTP/SRTP
            return

        # Decrypt SRTP → RTP
        if self.srtp_rx:
            replayed = self.srtp_rx.replayed
            rtp = self.srtp_rx.unprotect(data)
            if rtp is None:
                if self.srtp_rx.replayed != replayed:
                    _replay_dropped(self, "audio")
                    return
                if self.pkt_count == 0:
                    _LOGGER.warning("SRTP audio auth failed from %s (%dB)", addr, len(data))
                return
        elif _from_the_call(self, addr):
            rtp = data
        else:
            return

        if (rtp[1] & 0x7F) != 0:  # not PCMU
            return
        cc = rtp[0] & 0x0F
        hlen = 12 + cc * 4
        if rtp[0] & 0x10 and len(rtp) >= hlen + 4:  # header extension: non è voce
            hlen += 4 + struct.unpack_from('!H', rtp, hlen + 2)[0] * 4
        if len(rtp) <= hlen:
            return
        # Forward decrypted RTP to AV ffmpeg port (only while ffmpeg is up)
        if self.forward_av:
            try:
                self.ffmpeg_av_sock.sendto(self.av_rtp.fix(rtp, 0),
                                           ('127.0.0.1', av_stream.FFMPEG_AV_AUDIO_PORT))
            except OSError:
                pass
        for sink in tuple(self.rtp_sinks):
            try:
                sink(rtp)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Audio RTP sink failed")
        end = len(rtp) - (rtp[-1] if rtp[0] & 0x20 else 0)  # RTP padding is not voice
        payload = rtp[hlen:end]
        if not payload:
            return
        self.pkt_count += 1
        if self.pkt_count == 1:
            _LOGGER.info("First %s audio from %s (%dB)",
                         "SRTP" if self.srtp_rx else "RTP", addr, len(payload))
        self._reorder(struct.unpack_from('!H', rtp, 2)[0], struct.unpack_from('!I', rtp, 8)[0], payload)

    REORDER_MAX = 3   # packets held while one is missing (60 ms): then it is lost
    CONCEAL_MAX = 3   # lost packets filled in a row; a longer gap is skipped
    RESYNC_LATE = 5   # "late" packets in a row (100 ms): the source restarted its numbers

    def _reorder(self, seq: int, ssrc: int, payload: bytes) -> None:
        """Voice in sequence order: late and duplicate packets dropped, a lost one
        replaced by the previous 20 ms at half volume (fading on repeated losses),
        so the listener hears a dip instead of a click and the timing holds."""
        if ssrc != self._a_ssrc or self._a_next is None:
            self._a_ssrc, self._a_next = ssrc, seq
            self._a_buf.clear()
        back = (self._a_next - seq) & 0xFFFF
        if 0 < back < 0x8000:
            # Already played, or the source (a B2BUA switching legs, a relay)
            # jumped its sequence numbers with the same SSRC: after a few in a
            # row, follow the new numbers instead of dropping them for minutes.
            self._a_late += 1
            if self._a_late < self.RESYNC_LATE:
                return
            self._a_next = seq
            self._a_buf.clear()
        self._a_late = 0
        if seq in self._a_buf:
            return  # already waiting
        self._a_buf[seq] = payload
        while self._a_buf:
            nxt = self._a_buf.pop(self._a_next, None)
            if nxt is not None:
                self._emit(ulaw_decode(nxt))
                self._a_next = (self._a_next + 1) & 0xFFFF
                continue
            if len(self._a_buf) <= self.REORDER_MAX:
                return  # the missing one may still come
            gap = min((s - self._a_next) & 0xFFFF for s in self._a_buf)
            for _ in range(min(gap, self.CONCEAL_MAX)):
                self._emit(self._conceal())
            self._a_next = (self._a_next + gap) & 0xFFFF

    def _conceal(self) -> bytes:
        if not self._a_last:
            return bytes(320)
        return array.array("h", (x // 2 for x in array.array("h", self._a_last))).tobytes()

    def _emit(self, pcm: bytes) -> None:
        self._a_last = pcm
        for tap in pcm_taps:
            tap(pcm)
        try:
            self.audio_buffer.put_nowait(pcm)
        except asyncio.QueueFull:
            try:
                self.audio_buffer.get_nowait()
                self.audio_buffer.put_nowait(pcm)
            except Exception:
                pass

    def send_rtp(self, ulaw_payload: bytes):
        if not self.transport or not self.remote_addr:
            return
        if self.srtp_rx and not self.srtp_tx:
            return  # la targa cifra e noi non abbiamo chiavi: mai RTP in chiaro dentro SRTP
        self.rtp_seq = (self.rtp_seq + 1) & 0xFFFF
        self.rtp_ts = (self.rtp_ts + len(ulaw_payload)) & 0xFFFFFFFF
        header = struct.pack('!BBHII',
            0x80, 0, self.rtp_seq, self.rtp_ts, self.rtp_ssrc)
        rtp = header + ulaw_payload
        if self.srtp_tx:
            rtp = self.srtp_tx.protect(rtp)
        self.transport.sendto(rtp, self.remote_addr)
        self.tx_count += 1
        if self.tx_count == 1:
            _LOGGER.info("First %s audio TX → %s (PT 0, %dB)",
                         "SRTP" if self.srtp_tx else "RTP", self.remote_addr, len(ulaw_payload))

    def send_stun(self):
        if not self.transport or not self.remote_addr:
            return
        stun = struct.pack('!HHI', 0x0001, 0, 0x2112A442) + os.urandom(12)
        self.transport.sendto(stun, self.remote_addr)
        _LOGGER.debug("STUN Audio → %s", self.remote_addr)


class RTPVideoProtocol(asyncio.DatagramProtocol):
    """Video: [decrypt] → depacketize RTP H.264 → send NALs via WebSocket.

    SRTP (srtp_rx) è opzionale: creato in setup_media solo se il remoto
    negozia a=crypto. Quando è None si depacketizza RTP in chiaro (default).
    No ffmpeg — direct pipeline like the official Vimar app."""

    # Riceve ogni NAL riassemblato (frame grabber delle foto), se impostato.
    frame_sink = None
    # Chiamato quando la targa ne manda di diversi: restore_sps_pps salva
    # _ps_by_panel nello storage di HA.
    on_sps_pps = None

    # A keyframe arrives as a burst of ~40 packets (30-58 KB at 1.5 Mbit/s) that the
    # cloud relay reorders by up to 15 places and 34 ms (40515, measured): counting 5
    # packets gave up on packets that were still coming, and a cap of 64 filled in
    # 34 ms. No packet waits behind a gap more than REORDER_WAIT (0.3 s, /av's ffmpeg
    # -max_delay: at 80 ms most packets given up on came later), or past REORDER_BUF_SIZE.
    REORDER_BUF_SIZE = 512
    REORDER_WAIT = 0.3
    KEYFRAME_RETRY = 1.0  # while waiting for an IDR, ask again at most this often
    # The cloud relay also sends isolated packets hundreds ahead on the same SSRC.
    # Forwarded, ffmpeg (/av) takes one as the new head and drops the live stream
    # as "too late" until it catches up, and the phone's libsrtp (HomeKit) refuses
    # everything more than its 128-packet replay window behind it. So a packet more
    # than FAR_AHEAD past the newest one is held back, unless RESYNC_FAR come in a
    # row, FAR_AHEAD end to end at most (the relay reorders them too): then
    # the source really jumped and we follow it from the first of them. RESYNC_BACK
    # in a row back where we jumped from, before the new numbers go on, undo it.
    FAR_AHEAD = 128
    RESYNC_FAR = 50
    RESYNC_BACK = 5
    BACK_REORDER = 16  # the relay reorders by up to 15 places

    def __init__(self):
        self.transport = None
        self.remote_addr = None
        self.pkt_count = 0
        self.srtp_rx: SRTPContext | None = None
        # Forward plain RTP (H.264) to the AV ffmpeg only while it's running.
        # Enabled by av_stream.av_subscribe(), disabled by stop_av_ffmpeg() so we
        # never blast packets at a closed/absent socket.
        self.ffmpeg_av_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.forward_av = False
        self.av_rtp = av_stream.AvRtp(3000)
        # Callables that receive every decrypted video RTP packet once, as it
        # arrives: after the late and far-ahead filters, and without the
        # duplicates of packets still waiting in the reorder buffer (the HomeKit
        # doorbell sends these to the phone).
        self.rtp_sinks: list = []
        # RTP dell'ultimo GOP (da SPS/PPS/IDR in poi): l'ffmpeg di /av parte dopo
        # il 200 OK (poll, avvio, 0,3 s) e l'RTP arrivato prima era perso; con
        # l'IDR ogni 3 s della targa il video partiva al secondo. replay_gop()
        # glielo rimanda quando l'inoltro si accende.
        self._gop: list[bytes] | None = None
        self._gop_ts = None
        # FU-A reassembly buffer
        self._fua_buf = bytearray()
        self._fua_started = False
        self._fua_expected_seq = None  # Track RTP seq for FU-A continuity
        # Ordered NAL send queue — preserves SPS→PPS→IDR order
        self._nal_queue: asyncio.Queue | None = None
        self._nal_sender_task: asyncio.Task | None = None
        # SPS/PPS reorder buffer — hold IDR until SPS+PPS received.
        # Restano fra le chiamate (non cambiano): la 40515 dopo il 200 OK manda
        # PPS+IDR subito ma l'SPS solo ogni ~6 s, e chiude dopo ~10 s. Con quelli
        # della chiamata prima /av (sprop) e le foto partono dal primo IDR.
        # Una coppia per targa (id SIP): due targhe (portone e cancello) hanno
        # risoluzioni diverse, e con l'SPS dell'altra ffmpeg decodifica spazzatura.
        self._last_sps = None
        self._last_pps = None
        self.panel: str | None = None                      # targa di questa chiamata
        self._ps_by_panel: dict[str, tuple[bytes, bytes]] = {}
        self._sps_pps_sent = False  # True after first SPS+PPS pair sent
        self._pending_idr = None   # IDR waiting for SPS+PPS
        # GOP corrente già in forma di messaggi WS (SPS, PPS, IDR e i P dopo): a un
        # client che si collega a video in corso (la card allo squillo, l'app) lo si
        # rimanda subito, così decodifica ora e non al prossimo IDR (fino a 3 s).
        self._gop_msgs: list[bytes] = []
        self._gop_hdr: list[bytes] = []   # SPS+PPS in attesa dell'IDR che li segue
        self._drop_until_idr = False      # buco (coda WS piena, pacchetto perso): via i P fino all'IDR
        self._keyframe_at = 0.0           # ultimo keyframe chiesto per un pacchetto perso
        # RTP reorder buffer — fixes out-of-order UDP packets
        self._reorder_buf = {}  # seq -> (payload, arrival time), in arrival order
        self._next_seq = None   # next expected sequence number
        self._top = None        # newest sequence number accepted
        self._far_pkts = []     # packets far ahead in a row (_reorder args), held
        self._back_to = None    # (_next_seq before the last jump ahead, _top after it),
                                # until _top is FAR_AHEAD past the latter
        self._undone = False    # a jump ahead undone on this SSRC
        self._ssrc = None       # SSRC del flusso che stiamo riordinando
        # Diagnostics
        self._srtp_fail = 0
        self._srtp_ok = 0
        self._nal_count = 0
        self._nal_types = {}  # type -> count

    def sps_pps(self, own_only: bool = False) -> tuple[bytes, bytes] | None:
        """Gli ultimi SPS e PPS di questa targa (anche dalla chiamata prima), se ci sono
        entrambi: con quelli ffmpeg (/av, foto) decodifica dal primo IDR. Se questa targa
        non ne ha ancora mai mandati (mai chiamata prima, o cache persa a un aggiornamento),
        quelli di un'altra: la risoluzione cambia raramente, meglio una foto con quelli
        che nessuna foto ad aspettare l'SPS in banda (fino a 6 s sulla 40515).
        own_only: mai quelli di un'altra targa (il clip li userebbe per tutta la durata)."""
        if self._last_sps and self._last_pps:
            return self._last_sps, self._last_pps
        return None if own_only else next((ps for ps in self._ps_by_panel.values() if all(ps)), None)

    def sps_pps_of(self, panel: str | None) -> tuple[bytes, bytes] | None:
        """The stored SPS and PPS of this panel, when both are known."""
        ps = self._ps_by_panel.get(panel or "")
        return ps if ps and all(ps) else None

    def set_panel(self, panel: str | None) -> None:
        """Chiamata (o anteprima) con questa targa: si riparte dai suoi SPS/PPS."""
        self.panel = panel
        self._last_sps, self._last_pps = self._ps_by_panel.get(panel or "", (None, None))

    def connection_made(self, transport):
        self.transport = transport
        _LOGGER.info("RTP Video ready on :%d", RTP_VIDEO_PORT)
        # Start ordered NAL sender
        loop = asyncio.get_event_loop()
        self._nal_queue = asyncio.Queue(maxsize=500)
        self._nal_sender_task = loop.create_task(self._nal_sender())

    def datagram_received(self, data, addr):
        # Media chiuso (stop_media) e non ancora riaperto (setup_media): l'RTP che la
        # targa manda ancora dopo il nostro BYE (dal cloud ~100 ms, «First video RTP»
        # ricontato da zero) non va depacketizzato né messo nel GOP per il replay né
        # mandato ai WS: la card della chiamata dopo riceverebbe pezzi di quella prima.
        if not self.remote_addr:
            return
        if len(data) < 12:
            return
        if (data[0] & 0xC0) != 0x80:  # not RTP/SRTP
            return

        # Decrypt SRTP → plain RTP
        if self.srtp_rx:
            replayed = self.srtp_rx.replayed
            rtp = self.srtp_rx.unprotect(data)
            if rtp is None:
                if self.srtp_rx.replayed != replayed:  # already received, not an auth failure
                    _replay_dropped(self, "video")
                    return
                self._srtp_fail += 1
                if self._srtp_fail <= 5 or self._srtp_fail % 100 == 0:
                    _LOGGER.warning("SRTP video auth FAIL #%d (pkt %dB)", self._srtp_fail, len(data))
                return
            self._srtp_ok += 1
        elif _from_the_call(self, addr):
            rtp = data
        else:
            return

        self.pkt_count += 1
        if self.pkt_count == 1:
            _LOGGER.info("First video RTP from %s (%dB)", addr, len(rtp))
        if self.pkt_count <= 3 or self.pkt_count % 200 == 0:
            _LOGGER.debug("Video pkt #%d: %dB, srtp_ok=%d fail=%d nals=%d types=%s",
                          self.pkt_count, len(rtp), self._srtp_ok, self._srtp_fail,
                          self._nal_count, self._nal_types)

        # Parse RTP header
        cc = rtp[0] & 0x0F
        hlen = 12 + cc * 4
        seq = struct.unpack_from('!H', rtp, 2)[0]
        # Check for extension header
        if rtp[0] & 0x10:
            if len(rtp) < hlen + 4:
                return
            ext_len = struct.unpack_from('!H', rtp, hlen + 2)[0]
            hlen += 4 + ext_len * 4
        if len(rtp) <= hlen:
            return
        self._reorder(seq, struct.unpack_from('!I', rtp, 8)[0], rtp[hlen:], rtp)

    def _forward_av(self, rtp):
        try:
            # L'SDP di ffmpeg dice PT 96, che la targa può non usare (allo
            # squillo l'offerta è sua): ffmpeg scarta ogni altro PT → /av nero.
            self.ffmpeg_av_sock.sendto(self.av_rtp.fix(rtp, 96),
                                       ('127.0.0.1', av_stream.FFMPEG_AV_VIDEO_PORT))
        except OSError:
            pass

    def _cache_gop(self, rtp, payload):
        t = payload[0] & 0x1F
        if t == 28:  # FU-A: conta solo lo start
            t = payload[1] & 0x1F if len(payload) > 1 and payload[1] & 0x80 else 0
        if t in (5, 7, 8, 24):  # IDR, SPS, PPS, STAP-A (SPS+PPS): stesso timestamp = stesso AU
            ts = struct.unpack_from('!I', rtp, 4)[0]
            if ts != self._gop_ts:
                self._gop, self._gop_ts = [], ts
        if self._gop is not None:
            self._gop.append(rtp)
            if len(self._gop) > 1500:  # mai un IDR: inutile tenerlo (~30 s)
                self._gop = None

    def gop_in_sequence_order(self) -> list[bytes]:
        """The cached GOP in RTP sequence order.

        _cache_gop runs after the late and far-ahead filters but before the
        reorder buffer, so _gop is in arrival order. Replayed as is, ffmpeg
        takes the first packet as its reference and drops every one before it
        ("RTP: dropping old packet received too late"): an incomplete IDR and
        nothing decodable until the next keyframe. The order is rebuilt around
        the first packet with a signed 16-bit distance, so a sequence wrap
        inside the group does not upset it.
        """
        packets = [p for p in self._gop or () if len(p) >= 12]
        if len(packets) < 2:
            return packets
        base = struct.unpack_from("!H", packets[0], 2)[0]

        def distance(pkt: bytes) -> int:
            delta = (struct.unpack_from("!H", pkt, 2)[0] - base) & 0xFFFF
            return delta - 0x10000 if delta >= 0x8000 else delta

        return sorted(packets, key=distance)

    def replay_gop(self):
        """Rimanda a ffmpeg il GOP corrente, dall'IDR in poi: decodifica subito."""
        for rtp in self.gop_in_sequence_order():
            self._forward_av(rtp)

    @staticmethod
    def _ahead(a, b):
        """How far sequence number a is ahead of b, in -0x7FFF..0x8000."""
        return 0x8000 - ((b - a + 0x8000) & 0xFFFF)

    def _reorder(self, seq, ssrc, payload, rtp=b""):
        """Riordina i pacchetti UDP e li passa al depacketizer in sequenza.

        Fino alla 1.0.9 un pacchetto più vecchio di _next_seq (duplicato, in
        ritardo, o l'inizio di un flusso ripartito più indietro) restava nel
        buffer per sempre; al primo buco il flush avanzava di uno in uno per
        ~65 000 giri fino a raggiungerlo e lo emetteva fuori ordine: il
        «FU-A seq gap: expected 37 got 23 (gap=65522)» visto a inizio chiamata.
        Il GOP per il replay (`_cache_gop`) si riempie qui, DOPO lo stesso filtro:
        prima veniva prima, e un keyframe vecchio rimandato dalla 40515 azzerava
        `_gop`, che a ffmpeg arrivava «IDR vecchio a pezzi + P nuovi».
        Lo stesso vale per un pacchetto più di FAR_AHEAD avanti al più nuovo (/av,
        HomeKit e GOP non lo vedono), finché non ne arrivano RESYNC_FAR di fila.
        """
        if self._next_seq is not None:
            back = (self._next_seq - seq) & 0xFFFF  # 0 = atteso, 1..0x8000 = già passato
            same = ssrc == self._ssrc
            behind = same and 0 < back <= 0x8000
            # The live stream goes on from where we left it (give or take the
            # relay's reordering): older packets are late ones of a real jump.
            # Once the new numbers went on past a burst's reach, the jump was real.
            if self._back_to is not None and self._ahead(self._top, self._back_to[1]) > self.FAR_AHEAD:
                self._back_to = None
            back_again = (behind and self._back_to is not None
                          and -self.BACK_REORDER <= self._ahead(seq, self._back_to[0]) <= self.FAR_AHEAD)
            far = back_again or (same and self._ahead(seq, self._top) > self.FAR_AHEAD)
            # Ripartito (encoder o relay riavviato) se cambia l'SSRC o dopo RESYNC_FAR
            # pacchetti di fila molto avanti. La seq che torna indietro non basta: la
            # 40515 rimanda pacchetti vecchi di un keyframe (103, 2, 104...); presi per
            # un riavvio spostavano seq e timestamp di ~3 s verso ffmpeg e il muxer di
            # HA falliva: video bianco.
            if behind and not back_again:
                # Duplicato o arrivato dopo che l'abbiamo dato per perso. ffmpeg (/av)
                # aspetta fino a -max_delay (300 ms) dal suo primo pacchetto in coda:
                # a lui serve ancora (un duplicato quasi sempre lo scarta). Più
                # indietro di FAR_AHEAD lo scarterebbe comunque.
                if self.forward_av and rtp and back <= self.FAR_AHEAD:
                    self._forward_av(rtp)
                return
            if far:
                held = self._far_pkts
                if any(p[0] == seq for p in held):
                    return  # a copy: still one packet
                held.append((seq, ssrc, payload, rtp))
                # FAR_AHEAD end to end at most: replayed from the lowest, none is far
                # ahead of the newest before it, so none is held again.
                span = [self._ahead(p[0], seq) for p in held]
                while max(span) - min(span) > self.FAR_AHEAD:
                    del held[0], span[0]  # scattered strays, not one stream that jumped
                # Strays come alone or a few at a time (3 seen) and once followed /av
                # and HomeKit keep them, hence many in a row (~0.5 s of video, replayed,
                # not lost). Coming back is cheap; once we did, every later jump on
                # this SSRC needs twice as many (once, not more): two legs on one SSRC
                # flip us once per 2 * RESYNC_FAR packets at most.
                if back_again:
                    need = self.RESYNC_BACK
                else:
                    need = self.RESYNC_FAR * (2 if self._undone else 1)
                if len(held) < need:
                    return  # isolated, see FAR_AHEAD
                self._lost(f"seq saltata da {self._next_seq} a {seq}")
            elif not same:  # new stream: its P frames do not refer to our last IDR
                self._lost(f"ssrc nuovo {ssrc:08x}")
            if not same or far:
                _LOGGER.info("Video RTP ripartito (%s: ssrc %08x→%08x, seq %d→%d): "
                             "risincronizzo", "seq saltata" if far else "ssrc nuovo",
                             self._ssrc or 0, ssrc, self._next_seq, seq)
                if not same:
                    self._back_to, self._undone = None, False
                self._reorder_buf.clear()
                self._fua_buf = bytearray()
                self._fua_started = False
                self._fua_expected_seq = None
                jumped_from, self._next_seq = self._next_seq, None
                if far:  # the packets held so far start the new stream (an IDR's first fragments)
                    self._far_pkts = []
                    held.sort(key=lambda p: self._ahead(p[0], seq))
                    self.av_rtp.follow(held[0][0])  # /av: one continuous sequence
                    self._gop = self._gop_ts = None  # no strays in a later /av's replay
                    for pkt in held:
                        self._reorder(*pkt)
                    if back_again:
                        self._back_to = None
                        self._undone = True
                    else:
                        self._back_to = (jumped_from, self._top)
                    return
        self._far_pkts.clear()
        if self._next_seq is None:
            self._next_seq = self._top = seq
            self._ssrc = ssrc
        elif (seq - self._top) & 0xFFFF < 0x8000:
            self._top = seq
        # Every consumer gets a packet once, after the filters above and as it
        # arrives (no reorder delay; /av also gets the late ones, see above): a
        # duplicate of one still in the buffer was sent the first time.
        if rtp and seq not in self._reorder_buf:
            if self.forward_av:  # ffmpeg is up and listening (/av)
                self._forward_av(rtp)
            self._cache_gop(rtp, payload)
            for sink in tuple(self.rtp_sinks):
                try:
                    sink(rtp)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Video RTP sink failed")
        now = time.monotonic()
        self._reorder_buf.setdefault(seq, (payload, now))

        while True:
            while self._next_seq in self._reorder_buf:
                self._depacketize(self._reorder_buf.pop(self._next_seq)[0], self._next_seq)
                self._next_seq = (self._next_seq + 1) & 0xFFFF
            if not self._reorder_buf:
                return
            # The clock is the oldest packet waiting, as in ffmpeg, not the gap: losses in
            # a row cost REORDER_WAIT once, not once each.
            oldest = next(iter(self._reorder_buf.values()))[1]
            if len(self._reorder_buf) <= self.REORDER_BUF_SIZE and now - oldest < self.REORDER_WAIT:
                return
            # Buco che non si riempie più: salta al primo pacchetto disponibile.
            lost = self._next_seq
            self._next_seq = min(self._reorder_buf,
                                 key=lambda s: (s - self._next_seq) & 0xFFFF)
            self._lost(f"seq {lost} persa")

    def _lost(self, why):
        """Un pacchetto video perso (via cloud capita). Dal campo: un solo pacchetto
        perso dentro un FU-A («seq gap ... gap=1, continuing») dava un NAL col buco e
        il video smerigliato fino all'IDR dopo (~3 s). Via i P fino al prossimo IDR
        (WS, foto, GOP) e keyframe chiesto subito (_ask_keyframe)."""
        _LOGGER.debug("Video RTP: %s: via i P fino al prossimo IDR", why)
        self._drop_until_idr = True
        self._ask_keyframe()

    def _ask_keyframe(self):
        """Keyframe request, at most one per KEYFRAME_RETRY; also for every P dropped
        while waiting, so a request skipped or unanswered is repeated until the IDR."""
        now = time.monotonic()
        if request_keyframe and now - self._keyframe_at >= self.KEYFRAME_RETRY:
            self._keyframe_at = now
            request_keyframe()

    def _depacketize(self, payload, seq):
        """Depacketize RTP H.264 payload → send NAL units via WebSocket."""
        if len(payload) < 1:
            return
        nal_type = payload[0] & 0x1F

        if 1 <= nal_type <= 23:
            # Single NAL unit — send directly with Annex B start code
            self._emit_nal(payload)

        elif nal_type == 24:  # STAP-A
            # Aggregation: multiple NALs packed together
            off = 1
            while off + 2 <= len(payload):
                nalu_size = struct.unpack_from('!H', payload, off)[0]
                off += 2
                if off + nalu_size > len(payload):
                    break
                self._emit_nal(payload[off:off + nalu_size])
                off += nalu_size

        elif nal_type == 28:  # FU-A
            # Fragmentation: one NAL split across packets
            if len(payload) < 2:
                return
            fu_header = payload[1]
            start = bool(fu_header & 0x80)
            end = bool(fu_header & 0x40)
            nal_unit_type = fu_header & 0x1F
            fragment = payload[2:]

            if start:
                # Reconstruct NAL header: F|NRI from original + type from FU
                nal_header = (payload[0] & 0xE0) | nal_unit_type
                if self._fua_started:
                    _LOGGER.debug("FU-A new start while prev incomplete (type=%d buf=%d)",
                                  nal_unit_type, len(self._fua_buf))
                self._fua_buf = bytearray([nal_header])
                self._fua_buf.extend(fragment)
                self._fua_started = True
                self._fua_expected_seq = (seq + 1) & 0xFFFF
                if nal_unit_type in (5, 7, 8) or self.pkt_count <= 20:
                    _LOGGER.debug("FU-A START seq=%d nalType=%d fragSize=%d",
                                  seq, nal_unit_type, len(fragment))
            elif not self._fua_started:
                # FU-A continuation without start — dropped start packet
                if self.pkt_count <= 20:
                    # DEBUG: a lost start packet at the beginning of a call is
                    # routine on the relay, and the NAL is dropped anyway.
                    _LOGGER.debug("FU-A middle/end without start: seq=%d nalType=%d end=%s",
                                    seq, nal_unit_type, end)
                return
            else:
                # Un frammento mancante = NAL col buco: mai emetterlo (vedi _lost).
                if self._fua_expected_seq is not None and seq != self._fua_expected_seq:
                    self._lost(f"FU-A: attesa seq {self._fua_expected_seq}, arrivata {seq}")
                    self._fua_buf = bytearray()
                    self._fua_started = False
                    self._fua_expected_seq = None
                    return
                self._fua_buf.extend(fragment)
                self._fua_expected_seq = (seq + 1) & 0xFFFF

            if end and self._fua_started:
                completed_type = self._fua_buf[0] & 0x1F if self._fua_buf else 0
                if completed_type in (5, 7, 8) or self._nal_count <= 20:
                    _LOGGER.debug("FU-A END seq=%d nalType=%d totalSize=%d",
                                  seq, completed_type, len(self._fua_buf))
                self._emit_nal(bytes(self._fua_buf))
                self._fua_buf = bytearray()
                self._fua_started = False
                self._fua_expected_seq = None

    def _emit_nal(self, nal_data):
        """Queue a complete NAL unit for ordered sending via WebSocket.

        Ensures SPS→PPS→IDR ordering: if IDR arrives before SPS+PPS,
        buffer it and emit after both parameter sets are received.
        """
        t = nal_data[0] & 0x1F if nal_data else 0
        if t == 5:
            self._drop_until_idr = False
        elif t == 1 and self._drop_until_idr:
            self._ask_keyframe()
            return  # riferisce un fotogramma perso: né WS, né foto, né GOP
        # SPS/PPS servono anche a /av (sprop nell'SDP di ffmpeg): memorizzali
        # anche quando non c'è nessun client WebSocket.
        if t in (7, 8):
            old = (self._last_sps, self._last_pps)
            if t == 7:
                self._last_sps = nal_data
            else:
                self._last_pps = nal_data
            new = (self._last_sps, self._last_pps)
            if new != old and all(new):
                self._ps_by_panel[self.panel or ""] = new
                if self.on_sps_pps:
                    self.on_sps_pps()
        if nal_data and self.frame_sink:
            self.frame_sink(nal_data)
        if not nal_data or not ws_send_bytes or not self._nal_queue:
            return
        nal_type = nal_data[0] & 0x1F if nal_data else 0
        self._nal_count += 1
        self._nal_types[nal_type] = self._nal_types.get(nal_type, 0) + 1
        if self._nal_count <= 10 or nal_type in (7, 8, 5):
            _LOGGER.debug("NAL #%d type=%d size=%d (first4: %s)",
                          self._nal_count, nal_type, len(nal_data),
                          nal_data[:4].hex() if len(nal_data) >= 4 else nal_data.hex())

        # Reorder: ensure SPS+PPS always precede IDR
        if nal_type == 7:  # SPS (già memorizzato in cima)
            # If we have both SPS+PPS now, emit them + any pending IDR
            if self._last_pps is not None:
                self._flush_params_and_idr()
            return
        elif nal_type == 8:  # PPS (già memorizzato in cima)
            if self._last_sps is not None:
                self._flush_params_and_idr()
            return
        elif nal_type == 5:  # IDR
            if not self._sps_pps_sent:
                # No SPS+PPS sent yet — buffer IDR
                _LOGGER.debug("IDR buffered — waiting for SPS+PPS")
                self._pending_idr = nal_data
                return
            else:
                # Re-emit latest SPS+PPS before each IDR for robustness
                if self._last_sps:
                    self._queue_nal(self._last_sps)
                if self._last_pps:
                    self._queue_nal(self._last_pps)
        elif nal_type == 1:  # P-frame
            if not self._sps_pps_sent:
                return  # Drop P-frames before first IDR

        self._queue_nal(nal_data)

    def _flush_params_and_idr(self):
        """Emit SPS→PPS→(pending IDR) in correct order."""
        _LOGGER.debug("Flushing SPS→PPS→IDR (pending_idr=%s)",
                      "yes" if self._pending_idr else "no")
        if self._last_sps:
            self._queue_nal(self._last_sps)
        if self._last_pps:
            self._queue_nal(self._last_pps)
        self._sps_pps_sent = True
        if self._pending_idr:
            self._queue_nal(self._pending_idr)
            self._pending_idr = None

    def _queue_nal(self, nal_data):
        """Low-level: queue NAL bytes to WebSocket send queue."""
        msg = b'\x03\x00\x00\x00\x01' + nal_data
        t = nal_data[0] & 0x1F
        if t == 7:
            self._gop_hdr = [msg]
        elif t == 8:
            self._gop_hdr.append(msg)
        elif t == 5:
            self._gop_msgs = self._gop_hdr + [msg]
            self._drop_until_idr = False
        elif self._gop_msgs:
            self._gop_msgs.append(msg)
            if len(self._gop_msgs) > 150:  # IDR che non arriva (~10 s): inutile
                self._gop_msgs = []
        try:
            self._nal_queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Client lento (il sender aspetta il suo socket): un buco a metà GOP
            # farebbe solo artefatti fino al prossimo IDR. Si svuota e si riparte
            # pulito dal prossimo IDR, tenendo l'IDR corrente se è lui in coda.
            while not self._nal_queue.empty():
                self._nal_queue.get_nowait()
            self._drop_until_idr = t != 5
            if t == 5:
                for m in self._gop_msgs:  # SPS, PPS e questo IDR
                    self._nal_queue.put_nowait(m)
            else:
                self._gop_msgs = []
                if t in (7, 8):
                    self._nal_queue.put_nowait(msg)

    def replay_gop_ws(self, send):
        """Nuovo client WS a video in corso: gli manda il GOP corrente, dall'IDR in
        poi. Passa dalla coda del sender: resta in ordine coi NAL dal vivo."""
        for msg in self._gop_msgs:
            try:
                self._nal_queue.put_nowait((send, msg))
            except asyncio.QueueFull:
                return

    async def _nal_sender(self):
        """Drain NAL queue and send via WebSocket in order (a tutti, o a uno solo)."""
        try:
            while True:
                item = await self._nal_queue.get()
                send, msg = item if isinstance(item, tuple) else (ws_send_bytes, item)
                if send:
                    try:
                        await send(msg)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    def send_stun(self):
        if not self.transport or not self.remote_addr:
            return
        stun = struct.pack('!HHI', 0x0001, 0, 0x2112A442) + os.urandom(12)
        self.transport.sendto(stun, self.remote_addr)
        _LOGGER.debug("STUN Video → %s", self.remote_addr)


# ─── State ──────────────────────────────────────────────────────────

audio_proto: RTPAudioProtocol | None = None
video_proto: RTPVideoProtocol | None = None
_stun_task = None
_audio_task = None
_tx_task = None
# RTCP probes of the current call (rtcp.py): only with the logger at DEBUG.
_rtcp_probes: list[rtcp.RTCPProbe] = []


def _close_rtcp_probes() -> None:
    for probe in _rtcp_probes:
        probe.close()
    _rtcp_probes.clear()


async def _open_rtcp_probes(lines) -> None:
    """lines: (label, our RTP port, remote ip, remote RTP port, remote key)."""
    _close_rtcp_probes()
    if not rtcp.debug_enabled():
        return
    for label, port, ip, remote_port, key in lines:
        probe = await rtcp.open_probe(label, port + 1, (ip, remote_port),
                                      encrypted=bool(key), key=key)
        if probe:
            _rtcp_probes.append(probe)


# ─── Transport setup ────────────────────────────────────────────────

async def setup_transports():
    global audio_proto, video_proto
    loop = asyncio.get_event_loop()

    # Use SO_REUSEADDR to avoid "Address in use" on HA restart/reload
    audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    audio_sock.bind(('0.0.0.0', RTP_AUDIO_PORT))

    video_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    video_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # A keyframe arrives as a burst of FU-A packets: with the default buffer
    # the kernel drops part of it, and one lost fragment costs the whole group
    # until the next keyframe.
    for sock in (audio_sock, video_sock):
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    video_sock.bind(('0.0.0.0', RTP_VIDEO_PORT))

    _, audio_proto = await loop.create_datagram_endpoint(
        RTPAudioProtocol, sock=audio_sock)
    _, video_proto = await loop.create_datagram_endpoint(
        RTPVideoProtocol, sock=video_sock)


async def restore_sps_pps(store) -> None:
    """SPS/PPS dell'ultima chiamata dallo storage di HA (Store, uno per entry) al
    protocollo video, e da qui in poi ogni coppia diversa che la targa manda viene
    salvata. Senza, la prima /av dopo un riavvio di HA non ha sprop nell'SDP e aspetta
    l'SPS in banda (~6 s sulla 40515 se la richiesta di keyframe tarda)."""
    saved = await store.async_load()
    try:  # {"panels": {"55001": {"sps": b64, "pps": b64}, ...}}
        video_proto._ps_by_panel = {
            str(k): (base64.b64decode(v["sps"]), base64.b64decode(v["pps"]))
            for k, v in saved["panels"].items()}
    except (TypeError, KeyError, ValueError, AttributeError):
        try:  # formato di prima di 787bb87 (una sola coppia, non per targa): tenuta
            # come fallback generico invece di perderla al primo riavvio dopo
            # l'aggiornamento (altrimenti la prima targa a chiamare non ha nulla,
            # né sua né di un'altra, finché non manda l'SPS in banda).
            video_proto._ps_by_panel = {
                "": (base64.b64decode(saved["sps"]), base64.b64decode(saved["pps"]))}
        except (TypeError, KeyError, ValueError):
            pass  # niente di salvato (o rotto): si aspetta la targa come prima
    video_proto.on_sps_pps = lambda: store.async_delay_save(
        lambda: {"panels": {k: {"sps": base64.b64encode(s).decode(), "pps": base64.b64encode(p).decode()}
                            for k, (s, p) in video_proto._ps_by_panel.items()}}, 5)


def panel_id(uri: str | None) -> str | None:
    """The SIP id of a panel's URI (sip:55100@domain;x=y -> 55100)."""
    return uri.split(":")[-1].split("@")[0].split(";")[0] if uri else None


def _current_panel() -> str | None:
    """Id SIP della targa di questa chiamata: chi suona (anche in anteprima), o chi
    abbiamo chiamato. Import in ritardo: sip_client importa questo modulo."""
    from . import sip_client as sip
    pi = sip.pending_incoming
    uri = (pi.get("caller_uri") if pi.get("active") else None) or sip.call_state.get("original_target")
    return panel_id(uri)


async def setup_media(remote_sdp, local_crypto_key=None, local_video_crypto_key=None,
                      early=False, silence_limit=None):
    """Start media after SIP call established. Called by sip.py.

    early: anteprima dello squillo (183): si riceve soltanto, la voce parte
    con enable_tx() alla risposta.
    silence_limit: secondi di silenzio PCMU dopo cui il pacer smette di mandarlo
    (vista in uscita "Vedi esterno", 0 = mai silenzio); None = senza limite, come
    in ogni chiamata risposta (la targa chiude se non riceve RTP)."""
    global _stun_task, _audio_task, _tx_task, _silence_limit
    _silence_limit = silence_limit
    audio = remote_sdp.get("audio", {})
    video = remote_sdp.get("video", {})
    remote_ip = remote_sdp.get("conn", "")

    # parse_sdp sets crypto_key only on an RTP/SAVP line with a supported
    # suite; our answer echoes that suite (sdp._line_security), so both
    # directions of a line use it.
    remote_audio_key = audio.get("crypto_key")
    remote_video_key = video.get("crypto_key")
    rtcp_lines = []
    audio_suite = audio.get("crypto_suite") or "AES_CM_128_HMAC_SHA1_80"
    video_suite = video.get("crypto_suite") or "AES_CM_128_HMAC_SHA1_80"

    # A line our answer refused (RTP/SAVP without a usable suite) carries no media.
    if audio.get("port") and not audio.get("refused") and audio_proto:
        aip = audio.get("ip", remote_ip)
        audio_proto.remote_addr = (aip, audio["port"])
        audio_proto.pkt_count = 0
        audio_proto._a_ssrc = audio_proto._a_next = audio_proto._a_last = None
        audio_proto._a_buf.clear()
        audio_proto._a_late = 0
        # SRTP solo se il remoto negozia crypto E abbiamo una chiave locale.
        # Se il remoto risponde in chiaro (nessun a=crypto) restano None → RTP puro.
        audio_proto.srtp_rx = None
        audio_proto.srtp_tx = None
        if remote_audio_key:
            audio_proto.srtp_rx = SRTPContext(remote_audio_key, audio_suite, "audio")
            _LOGGER.info("SRTP Audio RX context created")
        if local_crypto_key and remote_audio_key:
            audio_proto.srtp_tx = SRTPContext(local_crypto_key, audio_suite)
            _LOGGER.info("SRTP Audio TX context created")
        elif remote_audio_key:
            _LOGGER.warning("La targa cifra l'audio (a=crypto) ma noi non abbiamo offerto "
                            "SRTP: niente voce verso la targa. Attiva «Cifra il media».")
        fmts = audio.get("fmts")
        if fmts is not None and "0" not in fmts:
            _LOGGER.warning("La targa non ha accettato PCMU (m=audio %s): la voce non passerà",
                            " ".join(fmts))
        audio_proto.tx_buf.clear()
        audio_proto.tx_count = 0
        # La targa dice sendonly/inactive: non vuole ricevere la nostra voce.
        audio_proto.tx_enabled = not early and audio.get("dir") not in ("sendonly", "inactive")
        _LOGGER.info("Audio: dir=%s fmts=%s tx=%s", audio.get("dir", "sendrecv"),
                     fmts, audio_proto.tx_enabled)
        audio_proto.send_stun()
        rtcp_lines.append(("audio", RTP_AUDIO_PORT, aip, audio["port"], remote_audio_key))
        _mode = "SRTP" if audio_proto.srtp_rx else "RTP"
        await broadcast("log", f"Audio {_mode} → {aip}:{audio['port']}")

    if video.get("port") and not video.get("refused") and video_proto:
        vip = video.get("ip", remote_ip)
        video_proto.remote_addr = (vip, video["port"])
        # Reset ALL state for new call (tranne SPS/PPS: vedi __init__, per targa)
        video_proto.set_panel(_current_panel())
        video_proto.pkt_count = 0
        video_proto._fua_buf = bytearray()
        video_proto._fua_started = False
        video_proto._fua_expected_seq = None
        video_proto._sps_pps_sent = False
        video_proto._pending_idr = None
        video_proto._gop_msgs, video_proto._gop_hdr = [], []
        # A new stream (or the same call re-negotiated): no P frame until an IDR of
        # this one, even when a PPS with the cached SPS lets _sps_pps_sent through
        # before it. The SIP side asks for the first keyframe; retries a second later.
        video_proto._drop_until_idr = True
        video_proto._keyframe_at = time.monotonic()
        video_proto._reorder_buf = {}
        video_proto._next_seq = None
        video_proto._back_to, video_proto._undone = None, False
        video_proto._gop = video_proto._gop_ts = None
        video_proto._srtp_fail = 0
        video_proto._srtp_ok = 0
        video_proto._nal_count = 0
        video_proto._nal_types = {}
        video_proto.srtp_rx = None
        if remote_video_key:
            video_proto.srtp_rx = SRTPContext(remote_video_key, video_suite, "video")
            _LOGGER.info("SRTP Video RX — direct H.264 depacketization (no ffmpeg)")
        frame_grabber.start(video_proto)
        video_proto.send_stun()
        rtcp_lines.append(("video", RTP_VIDEO_PORT, vip, video["port"], remote_video_key))
        _vmode = "SRTP" if video_proto.srtp_rx else "RTP"
        await broadcast("log", f"Video {_vmode} → {vip}:{video['port']} (direct)")
    elif video_proto:
        if video_proto.remote_addr:
            # No video in this session (an audio-only panel, or a re-INVITE
            # that declined it): stop sending STUN and keyframe requests to
            # the old one.
            video_proto.remote_addr = None
            frame_grabber.stop(video_proto)
        # Nothing of an earlier call's video may be replayed in this one, and
        # the keyframe loop must not see video "flowing".
        _forget_video(video_proto)

    # Debugging only: with the logger at DEBUG, listen on the RTCP ports.
    await _open_rtcp_probes(rtcp_lines)

    if _stun_task:
        _stun_task.cancel()
    _stun_task = asyncio.create_task(_stun_keepalive())

    if _audio_task:
        _audio_task.cancel()
    _audio_task = asyncio.create_task(_audio_broadcast())

    if _tx_task:
        _tx_task.cancel()
    _tx_task = asyncio.create_task(_tx_loop())


def claim_voice() -> None:
    """Parla una persona: la chiamata non è più una semplice vista, silenzio senza limite."""
    global _silence_limit
    _silence_limit = None
def _forget_video(vp) -> None:
    """Drop the call's cached keyframe group (RTP for /av, WS messages for the
    cards) and its packet count. The transports live as long as the hub: left
    in place, the next call's viewer got the previous call's picture."""
    vp.pkt_count = 0
    vp._gop = vp._gop_ts = None
    vp._gop_msgs, vp._gop_hdr = [], []


def enable_tx():
    """Risposta a uno squillo con anteprima: da qui si manda anche la voce."""
    if audio_proto and audio_proto.remote_addr:
        audio_proto.tx_enabled = True


async def stop_media():
    """Stop all media. Called on hangup/bye."""
    global _stun_task, _audio_task, _tx_task
    if _stun_task:
        _stun_task.cancel()
        _stun_task = None
    if _audio_task:
        _audio_task.cancel()
        _audio_task = None
    if _tx_task:
        _tx_task.cancel()
        _tx_task = None
    if audio_proto and audio_proto.remote_addr:
        _LOGGER.info("Media chiuso: audio rx=%d tx=%d, video rx=%d (srtp fail %d)",
                     audio_proto.pkt_count, audio_proto.tx_count,
                     video_proto.pkt_count if video_proto else 0,
                     video_proto._srtp_fail if video_proto else 0)
    if audio_proto:
        audio_proto.tx_enabled = False
        audio_proto.tx_buf.clear()
        audio_proto.remote_addr = None
        audio_proto.pkt_count = 0
        audio_proto._a_ssrc = audio_proto._a_next = audio_proto._a_last = None
        audio_proto._a_buf.clear()
        audio_proto._a_late = 0
        audio_proto.srtp_rx = None
        audio_proto.srtp_tx = None
        while not audio_proto.audio_buffer.empty():
            try:
                audio_proto.audio_buffer.get_nowait()
            except Exception:
                break
    if video_proto:
        video_proto.remote_addr = None
        video_proto.pkt_count = 0
        video_proto.srtp_rx = None
        video_proto._fua_buf = bytearray()
        video_proto._fua_started = False
        video_proto._fua_expected_seq = None
        _forget_video(video_proto)  # video finito: niente replay a chi si collega dopo
    frame_grabber.stop(video_proto)
    _close_rtcp_probes()
    await av_stream.stop_av_ffmpeg()


def close_transports():
    """Close UDP transports — called on integration unload."""
    global audio_proto, video_proto
    _close_rtcp_probes()
    if audio_proto and audio_proto.transport:
        audio_proto.transport.close()
        audio_proto = None
    if video_proto and video_proto.transport:
        # Its sender task would otherwise be destroyed pending on reload.
        task = getattr(video_proto, "_nal_sender_task", None)
        if task and not task.done():
            task.cancel()
        video_proto.transport.close()
        video_proto = None


# Cap on queued voice. The pacer sends in real time, so everything queued is
# delay the person at the panel hears, and it never shrinks back: after one
# network hiccup a 1 s queue kept the whole rest of the call 1 s late. 80 ms
# (four packets) holds one of the browser's 43-46 ms bursts plus what is
# left of the previous one; the oldest audio is dropped first. A frame goes
# out as soon as 160 bytes are queued.
#
# Tried and rejected: a 200 ms cap with a 40 ms pre-buffer after each
# underrun. The pre-buffer adds 20-40 ms at the start of every talk spurt,
# and the larger cap lets a standing queue of up to 200 ms build when the
# phone sends in bursts, which never drains because we send in real time.
# What it buys is smoother input, but the relay and the panel drop packets
# anyway (measured while working on the video re-encode), so little of it
# can be heard. Latency matters more here than an occasional silence frame
# on an underrun.
_TX_MAX = 640  # 80 ms of μ-law

# 20 ms di silenzio PCMU: keepalive quando non c'è voce in coda. La targa
# chiude un "Vedi esterno" se non riceve RTP per ~10 s, anche se il video
# continua ad arrivare — verificato sul campo il 2026-09-29: l'app VIEW
# ufficiale (linphone) manda audio in continuo, muto o no, e la chiamata
# dura i 20+ s configurati; senza RTP in uscita HA veniva chiuso dalla
# targa a ~10 s indipendentemente dal timer di autoaccensione.
SILENCE_ULAW = ulaw_encode(bytes(320))
_silence_limit: float | None = None  # vedi setup_media


def send_audio(pcm_data: bytes):
    """PCM16LE 8 kHz (microfono della card, messaggio di assenza) → coda del pacer.

    Fino alla 1.0.9 ogni blocco del browser diventava un pacchetto: 341-371
    campioni (43-46 ms) a raffiche, contro l'a=ptime:20 che offriamo."""
    if audio_proto and audio_proto.remote_addr:
        _note_tx_level(pcm_data)
        buf = audio_proto.tx_buf
        buf += ulaw_encode(pcm_data)
        del buf[:max(0, len(buf) - _TX_MAX)]


# Livello della voce in uscita, in DEBUG ogni 2 s. Il conteggio `tx=` dice che i
# pacchetti partono, non che dentro c'è una voce: così un microfono muto (permesso
# negato, dispositivo sbagliato) si distingue da una targa che non riproduce.
_TX_SILENCE = 300  # picco sotto cui è solo rumore di fondo (su 32767)
_tx_peak = 0
_tx_last_log = 0.0


def _note_tx_level(pcm_data: bytes) -> None:
    global _tx_peak, _tx_last_log
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    samples = array.array("h")
    samples.frombytes(pcm_data[:len(pcm_data) // 2 * 2])
    if samples:
        _tx_peak = max(_tx_peak, max(samples), -min(samples))
    now = time.monotonic()
    if now - _tx_last_log >= 2.0:
        _LOGGER.debug("Voce verso la targa: picco %d/32767 (%s)", _tx_peak,
                      "silenzio" if _tx_peak < _TX_SILENCE else "voce")
        _tx_peak = 0
        _tx_last_log = now


async def _tx_loop():
    """Un pacchetto da 20 ms (160 B) ogni 20 ms: voce in coda (microfono o
    messaggio di assenza) se c'è, altrimenti silenzio PCMU (SILENCE_ULAW)
    come keepalive — come fa l'app ufficiale, che non lascia mai il canale
    audio muto durante una chiamata."""
    loop = asyncio.get_running_loop()
    nxt = loop.time()
    t_view = None  # da quando si può trasmettere: il silenzio dura _silence_limit s da qui
    try:
        while True:
            nxt += 0.02
            await asyncio.sleep(max(0.0, nxt - loop.time()))
            if nxt < loop.time() - 0.2:
                nxt = loop.time()  # event loop rimasto fermo: niente raffica di recupero
            ap = audio_proto
            if not ap or not ap.remote_addr or not ap.tx_enabled:
                continue
            if t_view is None:
                t_view = loop.time()
            if len(ap.tx_buf) >= 160:
                frame = bytes(ap.tx_buf[:160])
                del ap.tx_buf[:160]
            else:
                if _silence_limit is not None and loop.time() - t_view >= _silence_limit:
                    continue  # solo la vista: 0 = mai silenzio; poi si chiude da sola, come prima
                frame = SILENCE_ULAW
            ap.send_rtp(frame)
    except asyncio.CancelledError:
        pass


async def load_pcm(path: str | bytes, max_seconds: int = 30) -> bytes | None:
    """Decodifica un file audio (mp3, wav, ...) in PCM 8 kHz mono 16 bit.
    `path` può essere anche l'audio stesso (bytes, es. dal TTS): va a ffmpeg da stdin.

    Si fa PRIMA di rispondere: un file sparito o illeggibile non deve
    trasformarsi in una risposta muta. Tetto di durata: la linea è occupata.
    """
    data = path if isinstance(path, bytes) else None
    try:
        # `file:`: un nome che comincia con «-» non diventa un'opzione di ffmpeg.
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", "pipe:0" if data else f"file:{path}", "-t", str(max_seconds),
            "-f", "s16le", "-ac", "1", "-ar", "8000", "pipe:1",
            stdin=subprocess.PIPE if data else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        _LOGGER.warning("Messaggio audio: ffmpeg non avviabile (%s)", e)
        return None
    try:
        pcm, err = await proc.communicate(data)
    finally:
        if proc.returncode is None:  # annullato a metà (unload, squillo finito)
            proc.kill()
            await proc.wait()
    if proc.returncode or not pcm:
        _LOGGER.warning("Messaggio audio non leggibile (%s): %s",
                        "tts" if data else path, err.decode(errors="replace").strip()[-200:])
        return None
    return pcm


async def send_pcm(pcm: bytes, alive) -> None:
    """Manda il PCM alla targa a pacchetti da 20 ms (160 campioni) al ritmo
    reale, finché alive() è vero (la chiamata che lo sta ascoltando)."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    for n, i in enumerate(range(0, len(pcm), 320)):
        if not alive():
            return
        chunk = pcm[i:i + 320]
        # The tail is padded with silence to a whole 20 ms packet: the pacer
        # only sends whole packets, and a short tail would never go out.
        send_audio(chunk + bytes(320 - len(chunk)))
        await asyncio.sleep(max(0.0, start + (n + 1) * 0.02 - loop.time()))
    await asyncio.sleep(0.1)  # l'ultimo pezzo esce dal pacer prima del BYE


# ─── STUN keepalive ─────────────────────────────────────────────────

async def _stun_keepalive():
    try:
        while True:
            await asyncio.sleep(15)
            if audio_proto and audio_proto.remote_addr:
                audio_proto.send_stun()
            if video_proto and video_proto.remote_addr:
                video_proto.send_stun()
    except asyncio.CancelledError:
        pass


# ─── Audio broadcast ────────────────────────────────────────────────

# ws_send_bytes: set by main.py — async fn(data) to send binary to all clients
ws_send_bytes = None
# pcm_taps: lista di fn(pcm), una per ogni pacchetto PCM della targa (av_passive: audio
# nello stream continuo; frame_grabber: audio del clip dello squillo), in più rispetto
# alla coda per il WS. Una lista invece di un solo slot: più tap possono essere agganciati
# insieme (es. squillo e stream passivo continuo in parallelo) senza incatenarsi a vicenda.
pcm_taps: list = []


def add_pcm_tap(fn) -> None:
    """Aggancia un tap PCM (chiamato ad ogni pacchetto, oltre a quelli già agganciati)."""
    pcm_taps.append(fn)


def remove_pcm_tap(fn) -> None:
    """Sgancia un tap PCM aggiunto con add_pcm_tap; gli altri restano agganciati."""
    pcm_taps.remove(fn)


# request_keyframe: set by hub — fn() che chiede subito un keyframe alla targa (INFO SIP)
request_keyframe = None


async def _audio_broadcast():
    """Forward decoded PCM to browser via WebSocket."""
    try:
        while True:
            if not audio_proto:
                await asyncio.sleep(0.5)
                continue
            try:
                pcm = await asyncio.wait_for(
                    audio_proto.audio_buffer.get(), timeout=0.5)
            except TimeoutError:
                continue
            if ws_send_bytes:
                await ws_send_bytes(b'\x01' + pcm)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _LOGGER.error("Audio broadcast error: %s", e)
