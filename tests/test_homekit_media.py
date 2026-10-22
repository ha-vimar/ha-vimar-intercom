"""The adapter between the HomeKit doorbell and media_handler (v1.0.9)."""
import asyncio
import os
import socket
import struct
import time

import pytest

from custom_components.vimar_intercom import homekit_media as hkm

media = hkm.media
from harness.homekit import PPS, SPS, FakeVideoProto, Recorder  # noqa: E402


def rtp(seq, payload, ts=1000, ssrc=0x1234):
    return struct.pack("!BBHII", 0x80, 96, seq, ts, ssrc) + payload


def test_a_group_without_sps_gets_it_prepended(monkeypatch):
    """The panel does not always put the SPS in the keyframe's group."""
    idr = rtp(10, b"\x65" + b"\xaa" * 50)
    monkeypatch.setattr(media, "video_proto", FakeVideoProto([idr]))
    prefix, gop = hkm.gop_for_direct_video()
    assert gop == [idr]
    assert len(prefix) == 1 and hkm._nal_types(prefix[0]) == [7, 8]
    assert prefix[0][:12][2:] == idr[:12][2:], "same seq, timestamp and SSRC"
    assert hkm.gop_has_keyframe()


def test_a_group_with_its_own_sps_is_left_alone(monkeypatch):
    gop = [rtp(9, SPS), rtp(10, PPS), rtp(11, b"\x65" + b"\xaa" * 50)]
    monkeypatch.setattr(media, "video_proto", FakeVideoProto(gop))
    assert hkm.gop_for_direct_video() == ([], gop)


def test_an_idr_split_in_fu_a_counts_once_it_is_whole(monkeypatch):
    fu_start = rtp(10, bytes([0x7C, 0x85]) + b"\xaa" * 20)  # FU-A, start, type 5
    fu_mid = rtp(11, bytes([0x7C, 0x05]) + b"\xaa" * 20)
    fu_end = rtp(12, bytes([0x7C, 0x45]) + b"\xaa" * 20)    # FU-A, end, type 5
    monkeypatch.setattr(media, "video_proto", FakeVideoProto([fu_start, fu_mid]))
    assert not hkm.gop_has_keyframe(), "a truncated IDR leaves the phone black"
    monkeypatch.setattr(media, "video_proto", FakeVideoProto([fu_start, fu_mid, fu_end]))
    assert hkm.gop_has_keyframe()


def test_the_tail_of_an_idr_without_its_start_is_not_a_keyframe(monkeypatch):
    fu_end = rtp(12, bytes([0x7C, 0x45]) + b"\xaa" * 20)
    p_frame = rtp(13, b"\x41" + b"\xaa" * 20)
    monkeypatch.setattr(media, "video_proto", FakeVideoProto([rtp(9, SPS), fu_end, p_frame]))
    assert not hkm.gop_has_keyframe()


def test_an_out_of_order_group_is_replayed_in_sequence_order(monkeypatch):
    """The cache is in arrival order: replayed as is, the phone dropped the
    packets that came before the first one and stayed black ~2.8 s."""
    p9, p10, p11 = (rtp(9, b"\x41" + b"\xbb" * 10), rtp(10, b"\x65" + b"\xaa" * 50),
                    rtp(11, b"\x41" + b"\xcc" * 10))
    monkeypatch.setattr(media, "video_proto", FakeVideoProto([p10, p11, p9]))
    prefix, gop = hkm.gop_for_direct_video()
    assert gop == [p9, p10, p11]
    assert prefix[0][2:12] == p9[2:12], "the SPS/PPS goes right before the first packet"


def test_the_sequence_order_survives_a_wrap(monkeypatch):
    a, b, c, d = (rtp(s, b"\x41" + bytes([s & 0xFF]) * 10) for s in (65534, 65535, 0, 1))
    monkeypatch.setattr(media, "video_proto", FakeVideoProto([c, a, d, b]))
    assert hkm.gop_for_direct_video()[1] == [a, b, c, d]
    monkeypatch.setattr(media, "video_proto", FakeVideoProto([b, d, a, c]))
    assert hkm.gop_for_direct_video()[1] == [a, b, c, d]


def test_a_whole_idr_that_arrived_out_of_order_is_a_keyframe(monkeypatch):
    fu_start = rtp(10, bytes([0x7C, 0x85]) + b"\xaa" * 20)
    fu_mid = rtp(11, bytes([0x7C, 0x05]) + b"\xaa" * 20)
    fu_end = rtp(12, bytes([0x7C, 0x45]) + b"\xaa" * 20)
    monkeypatch.setattr(media, "video_proto", FakeVideoProto([fu_end, fu_start, fu_mid]))
    assert hkm.gop_has_keyframe()


def test_the_video_tap_sees_packets_after_the_late_filter(monkeypatch):
    """The hook sits behind v1.0.9's duplicate/late filter, next to its GOP cache."""
    proto = media.RTPVideoProtocol()
    seen = []
    proto.rtp_sinks.append(seen.append)
    first = rtp(100, b"\x65" + b"\x00" * 10)
    proto._reorder(100, 0x1234, first[12:], first)
    late = rtp(99, b"\x41" + b"\x00" * 10)
    proto._reorder(99, 0x1234, late[12:], late)      # older than expected: dropped
    assert seen == [first]


def test_the_video_tap_skips_duplicates_still_in_the_reorder_buffer():
    """A packet waiting for a gap to fill, received twice, reached the phone twice."""
    proto = media.RTPVideoProtocol()
    seen = []
    proto.rtp_sinks.append(seen.append)
    first = rtp(100, b"\x65" + b"\x00" * 10)
    proto._reorder(100, 0x1234, first[12:], first)
    ahead = rtp(102, b"\x41" + b"\x00" * 10)          # 101 is missing: 102 waits
    proto._reorder(102, 0x1234, ahead[12:], ahead)
    proto._reorder(102, 0x1234, ahead[12:], ahead)      # the relay sends it again
    assert seen == [first, ahead]


def test_the_audio_tap_delivers_the_call_audio_to_its_port(monkeypatch):
    class Proto:
        rtp_sinks: list = []

    monkeypatch.setattr(media, "audio_proto", Proto())
    tap = hkm.AudioTap()
    try:
        with open(tap.sdp_path) as f:
            sdp = f.read()
        assert f"m=audio {tap.port} RTP/AVP 0" in sdp and tap.port % 2 == 0
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", tap.port))
        rx.settimeout(1)
        tap.attach()
        voice = bytes(range(160))
        for seq in (1, 2):
            for sink in media.audio_proto.rtp_sinks:
                sink(struct.pack("!BBHII", 0x80, 0, seq, 160 * seq, 0x1234) + voice)
        first, second = rx.recv(2048), rx.recv(2048)
        assert first[12:] == voice and first[1] == 0, "the payload as sent, PT 0"
        seq1, ts1 = struct.unpack_from("!HI", first, 2)
        seq2, ts2 = struct.unpack_from("!HI", second, 2)
        assert (seq2 - seq1) & 0xFFFF == 1 and (ts2 - ts1) & 0xFFFFFFFF == 160
        for sink in media.audio_proto.rtp_sinks:
            sink(struct.pack("!BBHII", 0x80, 101, 3, 0, 1) + b"\x01\x02\x03\x04")
        rx.settimeout(0.1)
        with pytest.raises(socket.timeout):
            rx.recv(2048)  # DTMF is not audio for ffmpeg
        rx.close()
    finally:
        tap.close()
    assert media.audio_proto.rtp_sinks == []
    assert not os.path.exists(tap.sdp_path)


def test_without_panel_audio_the_tap_sends_silence(monkeypatch):
    """The ring preview carries video only. Without audio coming in, the phone
    sends nothing when Talk is pressed, so answer-on-Talk never fired."""
    class Proto:
        rtp_sinks: list = []

    monkeypatch.setattr(media, "audio_proto", Proto())

    async def scenario():
        tap = hkm.AudioTap()
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", tap.port))
        rx.setblocking(False)
        tap.attach()
        await asyncio.sleep(0.5)
        got = []
        while True:
            try:
                got.append(rx.recv(2048))
            except BlockingIOError:
                break
        rx.close()
        tap.close()
        return got

    got = asyncio.run(scenario())
    assert len(got) >= 10
    assert all(p[12:] == b"\xff" * 160 for p in got)
    stamps = [struct.unpack_from("!HI", p, 2) for p in got]
    # The silence advances with the clock: whole 160-sample slots, one per
    # 20 ms, more when the event loop was late (a busy Pi can be).
    gaps = [((b[0] - a[0]) & 0xFFFF, (b[1] - a[1]) & 0xFFFFFFFF)
            for a, b in zip(stamps, stamps[1:], strict=False)]
    assert all(seq == 1 and gap > 0 and gap % 160 == 0 for seq, gap in gaps), gaps


# ─── the audio tap's clock ──────────────────────────────────────────────────

def _tap(monkeypatch):
    class Proto:
        rtp_sinks: list = []

    monkeypatch.setattr(media, "audio_proto", Proto())
    tap = hkm.AudioTap()
    tap._sock.close()
    tap._sock = Recorder()
    return tap


def _voice(seq, ts, ssrc=0x1234):
    return struct.pack("!BBHII", 0x80, 0, seq, ts & 0xFFFFFFFF, ssrc) + bytes(range(160))


def _stamps(tap, voice_only=False):
    return [(at, struct.unpack_from("!I", p, 4)[0]) for at, p in tap._sock.sent
            if not voice_only or p[12:] != b"\xff" * 160]


async def _live(tap, first_seq, count, ts0=0, ssrc=0x1234):
    """The panel's audio as it arrives: one packet every 20 ms."""
    loop = asyncio.get_running_loop()
    tick = loop.time()
    for n in range(count):
        tap._send(_voice(first_seq + n, ts0 + (first_seq + n) * 160, ssrc))
        tick += 0.02
        await asyncio.sleep(max(0.0, tick - loop.time()))


def test_a_live_call_is_never_padded(monkeypatch):
    """Replacing the 'no audio for FILL_AFTER' test with 'always' made every
    other test pass; this one would not."""
    tap = _tap(monkeypatch)

    async def scenario():
        tap.attach()
        await _live(tap, 0, 15)
        tap.close()

    asyncio.run(scenario())
    assert tap.filled == 0
    assert all(p[12:] == bytes(range(160)) for _at, p in tap._sock.sent)


def test_after_a_stall_silence_resumes_after_fill_after(monkeypatch):
    tap = _tap(monkeypatch)

    async def scenario():
        tap.attach()
        await _live(tap, 0, 5)
        stalled_at = time.monotonic()
        for _ in range(100):                     # at most 2 s, on a slow Pi
            if tap.filled:
                break
            await asyncio.sleep(0.02)
        tap.close()
        return stalled_at

    stalled_at = asyncio.run(scenario())
    assert tap.filled
    voice, silence = tap._sock.sent[4], tap._sock.sent[5]
    assert silence[1][12:] == b"\xff" * 160
    assert silence[0] - stalled_at >= hkm.AudioTap.FILL_AFTER - 0.03
    gap = (struct.unpack_from("!I", silence[1], 4)[0]
           - struct.unpack_from("!I", voice[1], 4)[0]) & 0xFFFFFFFF
    # The silence is stamped where it goes out, not right after the voice.
    assert gap >= int((silence[0] - voice[0]) * 8000) - 320


def test_a_burst_after_a_stall_does_not_add_delay(monkeypatch):
    """The relay stalls, the tap fills, then the relay delivers what it held
    all at once. Stamped back to back after the silence, the burst pushed the
    stream's clock ahead of the wall clock by the length of the stall, and the
    phone's playout delay with it."""
    tap = _tap(monkeypatch)

    async def scenario():
        tap.attach()
        await _live(tap, 0, 10)
        await asyncio.sleep(0.5)                 # the stall: the relay holds 25 packets
        for n in range(10, 35):                  # the burst
            tap._send(_voice(n, n * 160))
        await _live(tap, 35, 10)
        tap.close()

    asyncio.run(scenario())
    assert tap.filled
    stamps = _stamps(tap)

    def lag(at, ts):
        return at * 8000 - _unwrap(ts, stamps[0][1])

    before = lag(*stamps[5])
    after = lag(*stamps[-1])
    # Where the stream's clock stands against the wall clock, before and after:
    # within 0.1 s (a Pi's scheduling), not the half second of the stall.
    assert abs(after - before) < 800, (before, after)


def test_a_packet_lost_upstream_keeps_its_gap(monkeypatch):
    tap = _tap(monkeypatch)
    for seq in (0, 1, 3, 4):                     # 2 never arrives
        tap._send(_voice(seq, seq * 160))
    ts = [t for _at, t in _stamps(tap)]
    assert [(b - a) & 0xFFFFFFFF for a, b in zip(ts, ts[1:], strict=False)] == [160, 320, 160]


def test_the_panel_clock_may_wrap(monkeypatch):
    tap = _tap(monkeypatch)
    for n in range(4):
        tap._send(_voice(n, (1 << 32) - 320 + n * 160))
    ts = [t for _at, t in _stamps(tap)]
    assert [(b - a) & 0xFFFFFFFF for a, b in zip(ts, ts[1:], strict=False)] == [160, 160, 160]


def test_a_restarted_panel_stream_is_followed(monkeypatch):
    tap = _tap(monkeypatch)
    for n in range(3):
        tap._send(_voice(n, 1_000_000 + n * 160))
    for n in range(3):
        tap._send(_voice(n, 5 + n * 160, ssrc=0x9999))   # new SSRC, clock far below
    assert tap.late_dropped == 0 and len(tap._sock.sent) == 6
    ts = [t for _at, t in _stamps(tap)]
    assert all(0 < (b - a) & 0xFFFFFFFF <= 480 for a, b in zip(ts, ts[1:], strict=False))


def _unwrap(ts, ref):
    return ref + ((ts - ref) & 0xFFFFFFFF)


def test_the_previous_calls_group_is_never_replayed(monkeypatch):
    """The video protocol lives as long as the hub. The end of a call resets
    its packet count but keeps the cached group until the next call's setup:
    a view opened in between got the previous visitor's keyframe."""
    idr = rtp(10, b"\x65" + b"\xaa" * 50)
    proto = FakeVideoProto([idr])
    proto.pkt_count = 0                          # the call ended
    monkeypatch.setattr(media, "video_proto", proto)
    assert hkm.gop_for_direct_video() == ([], [])
    assert not hkm.gop_has_keyframe()


# ─── the audio tap, more ────────────────────────────────────────────────────

def test_the_late_burst_is_dropped_behind_the_silence(monkeypatch):
    """After a stall the relay delivers what it held. What falls inside the
    silence already sent is dropped, and nothing is stamped before the end
    of that silence."""
    tap = _tap(monkeypatch)

    async def scenario():
        tap.attach()
        await _live(tap, 0, 10)
        await asyncio.sleep(0.5)
        for n in range(10, 35):
            tap._send(_voice(n, n * 160))
        await _live(tap, 35, 10)
        tap.close()

    asyncio.run(scenario())
    assert tap.filled and tap.late_dropped > 0
    sent = tap._sock.sent
    assert len(sent) == (45 - tap.late_dropped) + tap.filled
    silence_end = None
    for _at, p in sent:
        stamp = struct.unpack_from("!I", p, 4)[0]
        if p[12:] == b"\xff" * 160:
            silence_end = (stamp + 160) & 0xFFFFFFFF
        elif silence_end is not None:
            assert hkm._signed32(stamp - silence_end) >= 0, "voice stamped inside silence sent"


def test_a_new_ssrc_with_a_nearby_clock_is_restamped(monkeypatch):
    """A restarted stream whose clock lands next to the old one's: mapped with
    the old offset, its timestamps went backwards."""
    tap = _tap(monkeypatch)
    for n in range(3):
        tap._send(_voice(n, 1000 + n * 160))
    for n in range(3):
        tap._send(_voice(n, 1000 + n * 160, ssrc=0x9999))
    ts = [t for _at, t in _stamps(tap)]
    assert tap.late_dropped == 0 and len(ts) == 6
    assert all(hkm._signed32(b - a) > 0 for a, b in zip(ts, ts[1:], strict=False)), ts


def test_after_a_loop_stall_the_silence_does_not_catch_up_in_a_burst(monkeypatch):
    """The fill loop runs on a fixed tick. After the event loop stalled, a
    tick left behind made it fire every missed slot back to back."""
    tap = _tap(monkeypatch)

    async def scenario():
        tap.attach()
        await asyncio.sleep(0.1)
        time.sleep(0.6)                          # the event loop stalls
        stalled = time.monotonic()
        await asyncio.sleep(0.1)
        tap.close()
        return stalled

    stalled = asyncio.run(scenario())
    burst = [at for at, _p in tap._sock.sent if stalled <= at < stalled + 0.03]
    assert len(burst) <= 3, len(burst)


def test_after_a_restart_the_tile_shows_the_last_ring_photo(tmp_path, monkeypatch):
    """The frame grabber starts empty after a restart: the Home tile used to
    show the placeholder until the next call."""
    import json
    rl = hkm.ring_log
    photo = b"\xff\xd8photo\xff\xd9"
    (tmp_path / "squillo_20260929_101010_000.jpg").write_bytes(photo)
    (tmp_path / rl.RING_LOG).write_text(json.dumps(
        [{"time": "t", "photo": "squillo_20260929_101010_000.jpg"}]))
    monkeypatch.setattr(hkm.R, "SNAPSHOT_DIR", str(tmp_path))
    assert hkm.last_ring_photo() == photo


def test_no_photo_folder_or_a_file_outside_it_gives_nothing(tmp_path, monkeypatch):
    import json
    rl = hkm.ring_log
    monkeypatch.setattr(hkm.R, "SNAPSHOT_DIR", "")
    assert hkm.last_ring_photo() is None
    (tmp_path / rl.RING_LOG).write_text(json.dumps([{"photo": "../../etc/passwd"}]))
    monkeypatch.setattr(hkm.R, "SNAPSHOT_DIR", str(tmp_path))
    assert hkm.last_ring_photo() is None


def test_parameter_sets_never_borrow_another_panels(monkeypatch):
    """On a plant with several panels, another panel's SPS/PPS would garble
    the first frames: HomeKit waits for the calling panel's own instead."""
    vp = media.RTPVideoProtocol()
    vp._ps_by_panel = {"55002": (SPS, PPS)}
    vp.set_panel("55001")
    monkeypatch.setattr(media, "video_proto", vp)
    assert hkm.parameter_sets() == (None, None)
    vp.set_panel("55002")
    assert hkm.parameter_sets() == (SPS, PPS)


def test_parameter_sets_of_the_panel_about_to_be_called(monkeypatch):
    """Before a view's call has set its panel (after a restart: none; after a ring:
    another panel), the early encoder asks for the called panel's own."""
    vp = media.RTPVideoProtocol()
    vp._ps_by_panel = {"55100": (SPS, PPS), "55001": (b"other-sps", b"other-pps")}
    monkeypatch.setattr(media, "video_proto", vp)
    vp.set_panel(None)                                      # after a restart
    assert hkm.parameter_sets() == (None, None)
    assert hkm.parameter_sets("sip:55100@plant.example.test;transport=tls") == (SPS, PPS)
    vp.set_panel("55001")                                   # the last call was a ring
    assert hkm.parameter_sets("sip:55100@plant.example.test") == (SPS, PPS)
    assert hkm.parameter_sets("sip:55999@plant.example.test") == (None, None)
