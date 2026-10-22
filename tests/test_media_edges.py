"""media_handler on the edges: packets that are not voice or video, SRTP that
fails, a full queue, a slow WebSocket client, the depacketizer's odd cases,
and the helpers around a call (audio file decoding, keepalive, broadcast)."""
from __future__ import annotations

import asyncio
import logging
import struct

import pytest

from custom_components.vimar_intercom import av_stream
from custom_components.vimar_intercom import media_handler as mh

REMOTE = ("192.0.2.1", 4000)
SPS, PPS = b"\x67sps", b"\x68pps"
IDR, P = b"\x65idr", b"\x41p"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(mh, "request_keyframe", None)
    monkeypatch.setattr(mh, "pcm_taps", [])
    monkeypatch.setattr(mh, "ws_send_bytes", None)
    monkeypatch.setattr(mh, "_broadcast", None)


def _rtp(payload=b"\xff" * 160, pt=0, seq=1, ts=0, ssrc=7, first=0x80):
    return struct.pack("!BBHII", first, pt, seq, ts, ssrc) + payload


class _Sock:
    def __init__(self, error=None):
        self.sent = []
        self._error = error

    def sendto(self, data, addr):
        if self._error:
            raise self._error
        self.sent.append((data, addr))


class _Srtp:
    """An SRTP context whose authentication always fails."""

    replayed = 0

    def unprotect(self, data):
        return None


def _audio():
    ap = mh.RTPAudioProtocol()
    ap.remote_addr = REMOTE
    return ap


def _video():
    vp = mh.RTPVideoProtocol()
    vp.remote_addr = REMOTE
    vp.frame_sink = None
    return vp


# ─── broadcast ───────────────────────────────────────────────────────────────

def test_broadcast_after_the_listener_is_removed_reaches_nobody():
    got = []

    async def listener(t, m):
        got.append((t, m))

    mh.init(listener)
    mh.init(None)  # the hub stopped: no listener any more
    asyncio.run(mh.broadcast("log", "x"))
    assert got == []


def test_broadcast_reaches_the_listener(monkeypatch):
    got = []

    async def listener(t, m):
        got.append((t, m))

    mh.init(listener)
    asyncio.run(mh.broadcast("log", "hello"))
    assert got == [("log", "hello")]


# ─── audio in ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("packet", [
    b"\x80\x00",                          # too short
    b"\x00\x01" + b"\x00" * 18,           # STUN
    b"\x40" + b"\x00" * 20,               # not RTP
    _rtp(pt=8),                           # PCMA: not what we negotiated
    _rtp(payload=b""),                    # header only
])
def test_audio_that_is_not_pcmu_voice_is_ignored(packet):
    ap = _audio()
    ap.datagram_received(packet, REMOTE)
    assert ap.audio_buffer.empty() and ap.pkt_count == 0


def test_srtp_audio_with_wrong_keys_does_not_flood_the_log(monkeypatch):
    # Wrong keys: no packet ever authenticates, so pkt_count stays 0 and the
    # warning used to fire for every packet, 50 a second for the whole call.
    # Same limiter as video: the first five, then one every hundred. Calls are
    # counted, not caplog records: the integration's log buffer can repeat one.
    warned = []
    monkeypatch.setattr(mh._LOGGER, "warning", lambda msg, *a: warned.append(msg))
    ap = _audio()
    ap.srtp_rx = _Srtp()
    for _ in range(200):
        ap.datagram_received(_rtp(), REMOTE)
    assert warned.count("SRTP audio auth failed from %s (%dB)") == 7


def test_srtp_audio_that_fails_authentication_is_dropped_and_logged_once(caplog):
    ap = _audio()
    ap.srtp_rx = _Srtp()
    with caplog.at_level(logging.WARNING, logger=mh.__name__):
        ap.datagram_received(_rtp(), REMOTE)
        ap.pkt_count = 1  # after the first packet: no more warnings
        ap.datagram_received(_rtp(b"\xff" * 20), REMOTE)
    assert ap.audio_buffer.empty()
    # Distinct messages: the integration's log buffer can repeat a record.
    logged = {r.getMessage() for r in caplog.records if "SRTP audio auth failed" in r.getMessage()}
    assert logged == {"SRTP audio auth failed from ('192.0.2.1', 4000) (172B)"}


def _srtp_pair():
    import base64
    import os

    from custom_components.vimar_intercom.srtp import SRTPContext

    key = base64.b64encode(os.urandom(30)).decode()
    return SRTPContext(key), SRTPContext(key)


def test_srtp_audio_is_decrypted_into_voice():
    tx, rx = _srtp_pair()
    ap = _audio()
    ap.srtp_rx = rx
    ap.datagram_received(tx.protect(_rtp(b"\x00" * 160)), ("198.51.100.7", 5000))
    assert ap.audio_buffer.get_nowait() == mh.ulaw_decode(b"\x00" * 160)


def test_srtp_video_is_decrypted_and_counted():
    tx, rx = _srtp_pair()
    vp = _video()
    nals = _collect(vp)
    vp.srtp_rx = rx
    vp.datagram_received(tx.protect(_rtp(SPS, pt=96)), ("198.51.100.7", 5002))
    assert nals == [SPS] and vp._srtp_ok == 1


def test_a_duplicate_srtp_video_packet_is_not_an_auth_failure():
    tx, rx = _srtp_pair()
    vp = _video()
    nals = _collect(vp)
    vp.srtp_rx = rx
    packet = tx.protect(_rtp(SPS, pt=96))
    vp.datagram_received(packet, ("198.51.100.7", 5002))
    vp.datagram_received(packet, ("198.51.100.7", 5002))
    assert nals == [SPS] and rx.replayed == 1 and vp._srtp_fail == 0


def _replay_logs(caplog):
    # Distinct records: the integration's log buffer can repeat a record.
    return sorted({(r.levelno, r.getMessage()) for r in caplog.records if "replay" in r.getMessage()})


def test_a_sequence_restart_on_the_same_ssrc_keeps_the_video():
    """A panel or relay restarting its sequence lower on the same SSRC: #89 had
    every packet after it dropped as a replay, the media stopped for minutes.
    Now the third in a row moves the window there: two packets lost."""
    tx, rx = _srtp_pair()
    vp = _video()
    vp.srtp_rx = rx
    addr = ("198.51.100.7", 5002)
    for seq in list(range(1000, 1010)) + list(range(1, 300)):
        vp.datagram_received(tx.protect(_rtp(P, pt=96, seq=seq)), addr)
    assert vp.pkt_count == 307 and rx.replayed == 2 and vp._srtp_fail == 0


def test_a_window_jump_is_logged_once_per_call_with_its_size(caplog, monkeypatch):
    """The cloud ring that sent no media: the next one shows which it was, a lone
    packet far ahead (info) or the window moved by a run (warning, the default
    level). Each once per call and stream."""
    from custom_components.vimar_intercom import srtp

    # once a test has run log_buffer.install(), the package logger has
    # propagate=False and caplog never sees its records: use a plain logger
    monkeypatch.setattr(srtp, "_LOGGER", logging.getLogger("test_srtp_window"))
    tx, rx = _srtp_pair()
    rx.name = "video"
    vp = _video()
    vp.srtp_rx = rx
    addr = ("198.51.100.7", 5002)
    with caplog.at_level(logging.INFO, logger="test_srtp_window"):
        for seq in [1000, 1001, 5000, *range(1002, 1400), 9000, *range(10, 20), *range(2000, 2010)]:
            vp.datagram_received(tx.protect(_rtp(P, pt=96, seq=seq)), addr)
    logged = [(r.levelno, r.getMessage()) for r in caplog.records if "SRTP video" in r.getMessage()]
    assert [lvl for lvl, _ in logged] == [logging.INFO, logging.WARNING]
    assert "3999 ahead" in logged[0][1] and "went back 1387" in logged[1][1]
    assert rx.resyncs == 1 and rx.replayed == 2  # 2000.. is ahead: it passes, a run of 50 would move it


def test_a_duplicate_srtp_audio_packet_is_a_replay_not_an_auth_failure(caplog):
    tx, rx = _srtp_pair()
    ap = _audio()
    ap.srtp_rx = rx
    packet = tx.protect(_rtp(b"\x00" * 160))
    with caplog.at_level(logging.DEBUG, logger=mh.__name__):
        ap.datagram_received(packet, ("198.51.100.7", 5000))
        ap.pkt_count = 0  # the auth warning fires only before the first packet
        ap.datagram_received(packet, ("198.51.100.7", 5000))
    assert rx.replayed == 1
    assert not [r for r in caplog.records if "auth failed" in r.getMessage()]
    assert _replay_logs(caplog) == [(logging.DEBUG, "SRTP audio: dropped a packet already received (replay)")]


def test_audio_is_forwarded_to_the_av_ffmpeg_while_it_runs():
    ap = _audio()
    ap.ffmpeg_av_sock = _Sock()
    ap.forward_av = True
    ap.datagram_received(_rtp(), REMOTE)
    (data, addr), = ap.ffmpeg_av_sock.sent
    assert addr == ("127.0.0.1", av_stream.FFMPEG_AV_AUDIO_PORT) and data[12:] == b"\xff" * 160
    assert not ap.audio_buffer.empty(), "and still decoded for the card"


def test_a_send_error_towards_the_av_ffmpeg_does_not_lose_the_voice():
    ap = _audio()
    ap.ffmpeg_av_sock = _Sock(OSError("gone"))
    ap.forward_av = True
    ap.datagram_received(_rtp(), REMOTE)
    assert not ap.audio_buffer.empty()


def test_a_full_audio_buffer_keeps_the_newest_voice():
    ap = _audio()
    while not ap.audio_buffer.full():
        ap.audio_buffer.put_nowait(b"old")
    ap.datagram_received(_rtp(payload=b"\x00" * 160), REMOTE)
    items = []
    while not ap.audio_buffer.empty():
        items.append(ap.audio_buffer.get_nowait())
    assert items[-1] == mh.ulaw_decode(b"\x00" * 160)
    assert items.count(b"old") == ap.audio_buffer.maxsize - 1


def test_no_voice_goes_out_without_a_transport():
    ap = _audio()
    seq = ap.rtp_seq
    ap.send_rtp(b"\xff" * 160)
    assert ap.rtp_seq == seq


# ─── video in ────────────────────────────────────────────────────────────────

def _collect(vp):
    nals = []
    vp.frame_sink = nals.append
    return nals


@pytest.mark.parametrize("packet", [b"\x80" * 8, b"\x40" + b"\x00" * 20])
def test_video_that_is_not_rtp_is_ignored(packet):
    vp = _video()
    vp.datagram_received(packet, REMOTE)
    assert vp.pkt_count == 0


def test_srtp_video_failures_are_counted_and_the_log_is_throttled(caplog):
    vp = _video()
    vp.srtp_rx = _Srtp()
    with caplog.at_level(logging.WARNING, logger=mh.__name__):
        for _ in range(200):
            vp.datagram_received(_rtp(pt=96), REMOTE)
    assert vp._srtp_fail == 200 and vp.pkt_count == 0
    logged = {r.getMessage() for r in caplog.records if "SRTP video auth FAIL" in r.getMessage()}
    # The first five, then every hundredth.
    assert logged == {f"SRTP video auth FAIL #{n} (pkt 172B)" for n in (1, 2, 3, 4, 5, 100, 200)}


def test_a_video_header_extension_is_skipped():
    vp = _video()
    nals = _collect(vp)
    ext = struct.pack("!HH", 0xBEDE, 1) + b"\x00" * 4
    vp.datagram_received(_rtp(ext + SPS, pt=96, first=0x90), REMOTE)
    assert nals == [SPS]


@pytest.mark.parametrize("packet", [
    _rtp(b"\xbe\xde", pt=96, first=0x90),   # extension header cut short
    _rtp(b"", pt=96),                       # no payload
])
def test_a_video_packet_without_payload_is_dropped(packet):
    vp = _video()
    nals = _collect(vp)
    vp.datagram_received(packet, REMOTE)
    assert nals == []


def test_a_send_error_towards_the_av_ffmpeg_keeps_the_video():
    vp = _video()
    nals = _collect(vp)
    vp.ffmpeg_av_sock = _Sock(OSError("gone"))
    vp.forward_av = True
    vp.datagram_received(_rtp(SPS, pt=96), REMOTE)
    assert nals == [SPS]


def test_a_fu_a_start_of_an_idr_opens_a_new_gop():
    vp = _video()
    vp._gop = [b"old"]
    fu_start = bytes([0x7C, 0x85]) + b"frag"
    vp._cache_gop(_rtp(fu_start, pt=96, ts=900), fu_start)
    assert vp._gop == [_rtp(fu_start, pt=96, ts=900)]


def test_a_gop_that_never_sees_an_idr_is_given_up():
    vp = _video()
    vp._gop, vp._gop_ts = [], 1
    for i in range(1501):
        vp._cache_gop(_rtp(P, pt=96, seq=i, ts=1), P)
    assert vp._gop is None


# ─── depacketizer ────────────────────────────────────────────────────────────

def _proto_with_sink():
    vp = mh.RTPVideoProtocol()
    return vp, _collect(vp)


def test_empty_and_unknown_payloads_emit_nothing():
    vp, nals = _proto_with_sink()
    vp._depacketize(b"", 1)
    vp._depacketize(bytes([29, 0x80]) + b"x", 2)  # FU-B: not used by the panels
    vp._depacketize(bytes([28]), 3)                # FU-A without its header
    assert nals == []


def test_a_truncated_stap_a_emits_only_the_whole_nals():
    vp, nals = _proto_with_sink()
    stap = bytes([24]) + struct.pack("!H", len(SPS)) + SPS + struct.pack("!H", 50) + PPS
    vp._depacketize(stap, 1)
    assert nals == [SPS]


def test_a_new_fu_a_start_drops_the_incomplete_one():
    vp, nals = _proto_with_sink()
    vp.pkt_count = vp._nal_count = 100  # past the first packets: quieter logging
    vp._depacketize(bytes([0x5C, 0x81]) + b"lost", 1)       # P start, never ended
    vp._depacketize(bytes([0x5C, 0x81]) + b"a", 5)          # new start
    vp._depacketize(bytes([0x5C, 0x41]) + b"b", 6)          # end
    assert nals == [bytes([0x41]) + b"ab"]


def test_a_fu_a_continuation_without_its_start_is_dropped():
    vp, nals = _proto_with_sink()
    vp.pkt_count = 100
    vp._depacketize(bytes([0x5C, 0x41]) + b"tail", 1)
    assert nals == []


# ─── NAL order and the WebSocket queue ───────────────────────────────────────

def _ws_proto(monkeypatch, maxsize=500):
    async def ws(msg):
        pass

    monkeypatch.setattr(mh, "ws_send_bytes", ws)
    vp = mh.RTPVideoProtocol()
    vp._nal_queue = asyncio.Queue(maxsize=maxsize)
    return vp


def _queued(vp):
    out = []
    while not vp._nal_queue.empty():
        out.append(vp._nal_queue.get_nowait()[5:])
    return out


def test_an_idr_before_the_parameter_sets_waits_for_them(monkeypatch):
    vp = _ws_proto(monkeypatch)
    vp._emit_nal(IDR)
    vp._emit_nal(PPS)   # no SPS yet: nothing flushed
    assert _queued(vp) == []
    vp._emit_nal(SPS)
    assert _queued(vp) == [SPS, PPS, IDR]


def test_p_frames_before_the_first_idr_are_dropped_and_other_nals_pass(monkeypatch):
    vp = _ws_proto(monkeypatch)
    vp._nal_count = 50
    vp._emit_nal(P)
    sei = b"\x06sei"
    vp._emit_nal(sei)
    assert _queued(vp) == [sei]


def test_a_gop_without_its_idr_is_not_kept_for_late_clients(monkeypatch):
    vp = _ws_proto(monkeypatch, maxsize=1000)
    vp._queue_nal(P)               # no GOP yet: not kept
    assert vp._gop_msgs == []
    vp._queue_nal(IDR)
    for _ in range(150):
        vp._queue_nal(P)
    assert vp._gop_msgs == [], "an IDR that never comes (~10 s): the GOP is dropped"


def test_a_full_queue_on_an_idr_restarts_clean_from_that_idr(monkeypatch):
    vp = _ws_proto(monkeypatch, maxsize=3)
    vp._queue_nal(SPS)
    vp._queue_nal(PPS)
    vp._queue_nal(P)
    vp._queue_nal(IDR)  # full: flushed, then SPS, PPS and this IDR
    assert _queued(vp) == [SPS, PPS, IDR]
    assert vp._drop_until_idr is False


def test_a_full_queue_on_a_parameter_set_keeps_it_and_waits_for_an_idr(monkeypatch):
    vp = _ws_proto(monkeypatch, maxsize=2)
    vp._queue_nal(P)
    vp._queue_nal(P)
    vp._queue_nal(SPS)
    assert _queued(vp) == [SPS] and vp._drop_until_idr is True


def test_the_sender_survives_a_failing_client_and_skips_a_missing_one(monkeypatch):
    vp = mh.RTPVideoProtocol()
    vp._nal_queue = asyncio.Queue()
    sent = []

    async def broken(msg):
        raise ConnectionResetError

    async def ok(msg):
        sent.append(msg)

    async def run():
        vp._nal_queue.put_nowait((broken, b"a"))
        vp._nal_queue.put_nowait((None, b"b"))
        vp._nal_queue.put_nowait((ok, b"c"))
        task = asyncio.create_task(vp._nal_sender())
        while not sent:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert sent == [b"c"]


# ─── around a call ───────────────────────────────────────────────────────────

def test_rtcp_probes_that_fail_to_open_are_not_kept(monkeypatch):
    async def no_probe(*a, **k):
        return None

    monkeypatch.setattr(mh.rtcp, "debug_enabled", lambda: True)
    monkeypatch.setattr(mh.rtcp, "open_probe", no_probe)
    monkeypatch.setattr(mh, "_rtcp_probes", [])
    asyncio.run(mh._open_rtcp_probes([("audio", 40000, "192.0.2.1", 4000, None)]))
    assert mh._rtcp_probes == []


def _setup_env(monkeypatch, ap):
    for k, v in dict(audio_proto=ap, video_proto=None, _stun_task=None,
                     _audio_task=None, _tx_task=None).items():
        monkeypatch.setattr(mh, k, v)
    ap.transport = _Sock()


def test_an_encrypted_panel_without_our_key_is_a_warning(monkeypatch, caplog):
    import base64
    import os

    ap = mh.RTPAudioProtocol()
    _setup_env(monkeypatch, ap)
    key = base64.b64encode(os.urandom(30)).decode()

    async def go():
        await mh.setup_media({"conn": "192.0.2.1", "audio": {
            "port": 4000, "fmts": ["8"], "crypto_key": key}, "video": {}})
        state = (ap.srtp_rx is not None, ap.srtp_tx)
        await mh.stop_media()
        return state

    with caplog.at_level(logging.WARNING, logger=mh.__name__):
        assert asyncio.run(go()) == (True, None)
    assert "niente voce verso la targa" in caplog.text
    assert "non ha accettato PCMU (m=audio 8)" in caplog.text


def test_closing_the_transports_cancels_the_video_sender(monkeypatch):
    closed = []
    tr = type("T", (), {"close": lambda self: closed.append(1)})

    async def run():
        vp = mh.RTPVideoProtocol()
        vp.transport = tr()
        vp._nal_sender_task = asyncio.create_task(asyncio.Event().wait())
        monkeypatch.setattr(mh, "video_proto", vp)
        monkeypatch.setattr(mh, "audio_proto", None)
        mh.close_transports()
        await asyncio.sleep(0)
        return vp._nal_sender_task

    task = asyncio.run(run())
    assert task.cancelled() and closed == [1] and mh.video_proto is None


def test_closing_the_transports_closes_the_av_forwarding_sockets(monkeypatch):
    # Each protocol opens a UDP socket to forward RTP to the /av ffmpeg: left
    # to the GC (ResourceWarning) before, two more open sockets per reload.
    tr = type("T", (), {"close": lambda self: None})
    ap, vp = mh.RTPAudioProtocol(), mh.RTPVideoProtocol()
    ap.transport, vp.transport = tr(), tr()
    socks = (ap.ffmpeg_av_sock, vp.ffmpeg_av_sock)
    monkeypatch.setattr(mh, "audio_proto", ap)
    monkeypatch.setattr(mh, "video_proto", vp)
    monkeypatch.setattr(mh, "_close_rtcp_probes", lambda: None)
    mh.close_transports()
    assert [s.fileno() for s in socks] == [-1, -1]


def test_closing_the_transports_leaves_a_finished_sender_alone(monkeypatch):
    closed = []
    tr = type("T", (), {"close": lambda self: closed.append(1)})

    async def run():
        vp = mh.RTPVideoProtocol()
        vp.transport = tr()
        vp._nal_sender_task = asyncio.create_task(asyncio.sleep(0))
        await vp._nal_sender_task
        monkeypatch.setattr(mh, "video_proto", vp)
        monkeypatch.setattr(mh, "audio_proto", None)
        mh.close_transports()
        return vp._nal_sender_task

    task = asyncio.run(run())
    assert not task.cancelled() and closed == [1]


def test_a_stalled_event_loop_does_not_send_a_burst_of_voice(monkeypatch):
    """After the loop was blocked for 250 ms the pacer restarts from now: the
    12 packets it missed are not sent back to back."""
    import time

    ap = mh.RTPAudioProtocol()
    ap.transport = _Sock()
    ap.remote_addr = REMOTE
    ap.tx_enabled = True
    monkeypatch.setattr(mh, "audio_proto", ap)
    monkeypatch.setattr(mh, "_silence_limit", None)

    async def run():
        task = asyncio.create_task(mh._tx_loop())
        await asyncio.sleep(0.05)
        time.sleep(0.25)                    # the event loop is blocked
        before = len(ap.transport.sent)
        await asyncio.sleep(0.07)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return len(ap.transport.sent) - before

    assert asyncio.run(run()) <= 6, "a burst would be the ~12 packets missed"


def test_voice_outside_a_call_is_not_queued(monkeypatch):
    ap = mh.RTPAudioProtocol()
    monkeypatch.setattr(mh, "audio_proto", ap)
    mh.send_audio(b"\x10\x00" * 160)
    assert len(ap.tx_buf) == 0


def test_an_empty_mic_block_does_not_break_the_level_log(monkeypatch, caplog):
    monkeypatch.setattr(mh, "_tx_peak", 0)
    monkeypatch.setattr(mh, "_tx_last_log", 0.0)
    with caplog.at_level(logging.DEBUG, logger=mh.__name__):
        mh._note_tx_level(b"\x01")  # half a sample: no samples
        mh._note_tx_level(b"\x10\x27")  # loud, but within 2 s of the last line: not logged
    levels = [r.getMessage() for r in caplog.records if "Voce verso la targa" in r.getMessage()]
    assert set(levels) == {"Voce verso la targa: picco 0/32767 (silenzio)"}
    assert mh._tx_peak == 0x2710, "kept for the next line"


# ─── audio file decoding ─────────────────────────────────────────────────────

class _Dec:
    def __init__(self, rc=0, out=b"", err=b"", hang=False):
        self._rc, self._out, self._err, self._hang = rc, out, err, hang
        self.returncode = None
        self.killed = False

    async def communicate(self, data=None):
        if self._hang:
            await asyncio.Event().wait()
        self.returncode = self._rc
        return self._out, self._err

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _spawn(monkeypatch, proc):
    async def spawn(*a, **k):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)


def test_an_unreadable_audio_file_is_none_with_ffmpegs_reason(monkeypatch, caplog):
    _spawn(monkeypatch, _Dec(rc=1, err=b"Invalid data found when processing input\n"))
    with caplog.at_level(logging.WARNING, logger=mh.__name__):
        assert asyncio.run(mh.load_pcm("/nonexistent/away.mp3")) is None
    assert "Invalid data found" in caplog.text


def test_decoding_cancelled_halfway_kills_ffmpeg(monkeypatch):
    dec = _Dec(hang=True)
    _spawn(monkeypatch, dec)

    async def run():
        task = asyncio.create_task(mh.load_pcm(b"tts-audio"))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert dec.killed


def test_sending_pcm_stops_when_the_call_is_gone(monkeypatch):
    ap = mh.RTPAudioProtocol()
    ap.remote_addr = REMOTE
    monkeypatch.setattr(mh, "audio_proto", ap)
    alive = iter([True, False])
    asyncio.run(mh.send_pcm(b"\x10\x00" * 800, lambda: next(alive)))
    assert len(ap.tx_buf) == 160, "one 20 ms packet, then the call ended"


# ─── keepalive and broadcast loops ───────────────────────────────────────────

def _fast_sleep(monkeypatch, rounds):
    """asyncio.sleep that returns at once `rounds` times, then cancels the loop."""
    real = asyncio.sleep
    count = [0]

    async def sleep(t):
        count[0] += 1
        if count[0] > rounds:
            raise asyncio.CancelledError
        await real(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)


def test_the_stun_keepalive_pings_both_lines_during_a_call(monkeypatch):
    pings = []
    ap = type("A", (), {"remote_addr": REMOTE, "send_stun": lambda self: pings.append("a")})()
    vp = type("V", (), {"remote_addr": REMOTE, "send_stun": lambda self: pings.append("v")})()
    monkeypatch.setattr(mh, "audio_proto", ap)
    monkeypatch.setattr(mh, "video_proto", vp)
    _fast_sleep(monkeypatch, 2)
    asyncio.run(mh._stun_keepalive())
    assert pings == ["a", "v", "a", "v"]


def test_the_stun_keepalive_is_quiet_between_calls(monkeypatch):
    pings = []
    # Sockets open, but no panel address: no call, nothing to keep alive.
    ap = type("A", (), {"remote_addr": None, "send_stun": lambda self: pings.append("a")})()
    vp = type("V", (), {"remote_addr": None, "send_stun": lambda self: pings.append("v")})()
    monkeypatch.setattr(mh, "audio_proto", ap)
    monkeypatch.setattr(mh, "video_proto", vp)
    _fast_sleep(monkeypatch, 2)
    asyncio.run(mh._stun_keepalive())  # ends quietly on cancel
    assert pings == []


def test_the_audio_broadcast_waits_for_a_call_and_sends_to_the_card(monkeypatch):
    sent = []

    async def ws(data):
        sent.append(data)

    ap = mh.RTPAudioProtocol()
    ap.audio_buffer.put_nowait(b"before")
    monkeypatch.setattr(mh, "audio_proto", None)

    async def run():
        task = asyncio.create_task(mh._audio_broadcast())
        await asyncio.sleep(0)            # no call yet: it waits
        monkeypatch.setattr(mh, "audio_proto", ap)
        while not ap.audio_buffer.empty():  # no card yet: the PCM is dropped
            await asyncio.sleep(0.05)
        monkeypatch.setattr(mh, "ws_send_bytes", ws)
        ap.audio_buffer.put_nowait(b"pcm")
        while not sent:
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert sent == [b"\x01pcm"], "PCM without a card is not delivered late"


def test_an_audio_broadcast_error_is_logged(monkeypatch, caplog):
    async def broken(data):
        raise RuntimeError("socket gone")

    ap = mh.RTPAudioProtocol()
    ap.audio_buffer.put_nowait(b"pcm")
    monkeypatch.setattr(mh, "audio_proto", ap)
    monkeypatch.setattr(mh, "ws_send_bytes", broken)
    with caplog.at_level(logging.ERROR, logger=mh.__name__):
        asyncio.run(mh._audio_broadcast())
    assert "Audio broadcast error: socket gone" in caplog.text
