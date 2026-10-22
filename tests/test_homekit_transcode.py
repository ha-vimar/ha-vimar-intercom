"""The group coming out of the transcoder: where a new view starts.

A view that opens first receives the last transcoded group, from the SPS on.
It must be recognized correctly: if the group does not yet hold a whole
keyframe, the phone has nothing to decode.
"""
import struct

from harness.homekit import FakeSdpFfmpeg  # noqa: E402

from custom_components.vimar_intercom import homekit_transcode as tc


def rtp(payload: bytes, seq: int = 1) -> bytes:
    return bytes([0x80, 96]) + struct.pack("!HII", seq, 0, 1) + payload


STAP_SPS_PPS = rtp(b"\x78" + b"\x00\x02\x67\x42" + b"\x00\x02\x68\xce")
IDR_START = rtp(b"\x7c\x85" + b"\xaa" * 20)
IDR_MID = rtp(b"\x7c\x05" + b"\xbb" * 20)
IDR_END = rtp(b"\x7c\x45" + b"\xcc" * 20)
IDR_SINGLE = rtp(b"\x65" + b"\xdd" * 20)
P_FRAME = rtp(b"\x41" + b"\xee" * 20)


class TestNalTypes:
    def test_single(self):
        assert tc._nal_types(P_FRAME) == [1]

    def test_stap_a_looks_inside(self):
        assert tc._nal_types(STAP_SPS_PPS) == [7, 8]

    def test_fu_a_reports_the_fragmented_type(self):
        assert tc._nal_types(IDR_START) == [5]


class TestEncodedGop:
    def test_a_whole_fragmented_keyframe_counts(self):
        g = tc.EncodedGop()
        for p in (STAP_SPS_PPS, IDR_START, IDR_MID):
            g.add(p)
        assert not g.has_keyframe, "the end of the IDR is still missing"
        g.add(IDR_END)
        assert g.has_keyframe

    def test_an_unfragmented_keyframe_counts(self):
        g = tc.EncodedGop()
        g.add(STAP_SPS_PPS)
        g.add(IDR_SINGLE)
        assert g.has_keyframe

    def test_the_group_restarts_at_every_sps(self):
        g = tc.EncodedGop()
        for p in (STAP_SPS_PPS, IDR_SINGLE, P_FRAME, P_FRAME):
            g.add(p)
        g.add(STAP_SPS_PPS)
        assert g.packets == [STAP_SPS_PPS] and not g.has_keyframe

    def test_a_group_that_never_ends_is_capped(self):
        """An encoder that stops sending keyframes made the group grow for the
        whole call. Past the cap it is dropped until the next SPS."""
        g = tc.EncodedGop()
        g.add(STAP_SPS_PPS)
        g.add(IDR_SINGLE)
        for _ in range(tc.GOP_MAX_PACKETS + 10):
            g.add(P_FRAME)
        assert len(g.packets) <= tc.GOP_MAX_PACKETS
        assert not g.has_keyframe, "what is left has no keyframe to start from"
        g.add(STAP_SPS_PPS)
        g.add(IDR_SINGLE)
        assert g.packets == [STAP_SPS_PPS, IDR_SINGLE] and g.has_keyframe

    def test_a_tail_without_its_start_is_not_a_keyframe(self):
        """Only the end of the IDR (someone lost the start): it does not count."""
        g = tc.EncodedGop()
        g.add(STAP_SPS_PPS)
        g.add(IDR_END)
        assert not g.has_keyframe


def test_the_panel_video_is_read_on_loopback_from_a_private_sdp(monkeypatch, assert_private_mode):
    """ffmpeg 8.1 bound the RTCP port of an SDP input on 0.0.0.0, and the SDP
    had a guessable name in /tmp that open() would follow as a symlink."""
    import asyncio
    import os

    seen = {}

    async def spawn(*args, **_kw):
        path = args[args.index("-i") + 1]
        seen["args"], seen["mode"] = args, os.stat(path).st_mode & 0o777
        with open(path) as f:
            seen["sdp"] = f.read()
        return FakeSdpFfmpeg()

    monkeypatch.setattr(tc.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(tc, "port_bound", lambda _port: True)

    async def scenario():
        t = tc.Transcoder(None, None)
        assert await t.start(lambda: ([], []))
        path = seen["args"][seen["args"].index("-i") + 1]
        await t.stop()
        return t, path

    t, path = asyncio.run(scenario())
    out = seen["args"][seen["args"].index("image2pipe") - 1:]
    assert out[1:] == ("image2pipe", "pipe:1"), "pictures for the Home app on stdout"
    i = seen["args"].index("-i")
    assert seen["args"][i - 2:i] == ("-localaddr", "127.0.0.1")
    # One decoding thread, as an input option: frame threading kept every frame
    # ~340 ms behind the panel on a 40517 (#56).
    j = seen["args"].index("-threads")
    assert j < i and seen["args"][j + 1] == "1"
    assert_private_mode(seen["mode"])
    assert f"/vimar_intercom_transcode_{t._in_port}.sdp" not in path
    assert f"m=video {t._in_port} " in seen["sdp"]
    assert not os.path.exists(path)


def test_only_our_encoder_may_feed_the_phones():
    t = tc.Transcoder(None, None)
    got = []
    t.add_sink(got.append)
    pkt = rtp(b"\x41" + b"\x00" * 10)
    t._from_encoder(pkt, ("127.0.0.1", 40000))
    t._from_encoder(pkt, ("127.0.0.1", 40001))   # another local process
    t._feed.close()
    assert got == [pkt]



def test_the_latest_picture_is_kept_across_reads():
    """JPEGs split across reads, with bytes before the first one: the last
    whole picture is kept, and the pipe is read to its end."""
    import asyncio

    one = b"\xff\xd8" + b"first" + b"\xff\xd9"
    two = b"\xff\xd8" + b"second\x00\xff\x00" + b"\xff\xd9"
    stream = b"junk" + one + two + b"\xff\xd8partial"

    async def scenario():
        reader = asyncio.StreamReader()
        for i in range(0, len(stream), 5):
            reader.feed_data(stream[i:i + 5])
        reader.feed_eof()
        t = tc.Transcoder(None, None)
        await asyncio.wait_for(t._read_pictures(reader), 1)
        t._feed.close()
        return t.last_jpeg
    assert asyncio.run(scenario()) == two


def test_a_deferred_start_feeds_nothing_until_begin(monkeypatch):
    """#48: an encoder warmed up before the call's video takes no live packet;
    begin() gives it the group in sequence order first, then the live stream."""
    import asyncio

    async def spawn(*_a, **_kw):
        return FakeSdpFfmpeg()

    monkeypatch.setattr(tc.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(tc, "port_bound", lambda _port: True)
    group = [rtp(b"\x67" + b"\x00" * 4), rtp(b"\x68" + b"\x00" * 2), rtp(b"\x65" + b"\x00" * 8)]
    live = rtp(b"\x41" + b"\x00" * 6)
    attached = []

    async def scenario():
        t = tc.Transcoder(None, None)
        sent = []
        t._send = sent.append
        assert await t.start(lambda: ([], group), on_ready=lambda: attached.append(1), defer=True)
        t.feed(live)                              # a live packet before begin: ignored
        early = (list(sent), t.begun, list(attached))
        t.begin()
        t.feed(live)
        t.begin()                                 # once only
        await t.stop()
        return early, sent

    (early_sent, early_begun, early_attached), sent = asyncio.run(scenario())
    assert early_sent == [] and not early_begun and early_attached == []
    assert sent == group + [live], "the group in order first, then the live stream"
    assert attached == [1]


def test_the_frame_delay_is_measured_from_feed_to_encoder_output(monkeypatch):
    """How far behind the panel the re-encoded video runs (#56): input frames by
    their RTP timestamp relative to the first one fed, output frames likewise
    (-fps_mode passthrough keeps them), matched on the output frame's last packet."""
    import struct as st

    def pkt(ts, marker=False):
        return st.pack("!BBHII", 0x80, (0x80 if marker else 0) | 96, 1, ts, 0x1234) + b"\x41\x00"

    clock = {"t": 100.0}
    monkeypatch.setattr(tc.time, "monotonic", lambda: clock["t"])
    t = tc.Transcoder(None, None)
    t._feed.close()
    t._feed = type("F", (), {"sendto": lambda *_a: None})()
    t._send(pkt(9000))                 # frame 0 fed at 100.00
    clock["t"] = 100.066
    t._send(pkt(15000))                # frame 1 fed at 100.066
    clock["t"] = 100.30
    t._from_encoder(pkt(777, marker=True), ("127.0.0.1", 1))    # frame 0 out: 300 ms
    clock["t"] = 100.40
    t._from_encoder(pkt(777 + 6000), ("127.0.0.1", 1))          # not its last packet
    t._from_encoder(pkt(777 + 6000, marker=True), ("127.0.0.1", 1))  # frame 1 out: 334 ms
    assert [round(d * 1000) for d in t.frame_delays] == [300, 334]
    assert t.delay_summary() == ", frame delay median 334 ms (p90 334 ms, 2 frames)"
    assert tc.Transcoder(None, None).delay_summary() == ""


def test_the_frame_delay_matches_the_panels_irregular_timestamps(monkeypatch):
    """Field test (#56): the encoder rounds its timestamps to its 15 fps clock
    (exact 6000-tick steps), the panel's are a little irregular. Exact matching
    measured one frame per view; the nearest input frame within half a frame
    is the one."""
    import struct as st

    def pkt(ts, marker=False):
        return st.pack("!BBHII", 0x80, (0x80 if marker else 0) | 96, 1, ts, 0x1234) + b"\x41\x00"

    clock = {"t": 0.0}
    monkeypatch.setattr(tc.time, "monotonic", lambda: clock["t"])
    t = tc.Transcoder(None, None)
    t._feed.close()
    t._feed = type("F", (), {"sendto": lambda *_a: None})()
    panel = [0, 5940, 12090, 17950, 24060]           # irregular, about 6000 apart
    for i, ts in enumerate(panel):
        clock["t"] = i * 0.066
        t._send(pkt(50000 + ts))
    for i in range(len(panel)):
        clock["t"] = i * 0.066 + 0.3                  # each frame out 300 ms later
        t._from_encoder(pkt(9 + i * 6000, marker=True), ("127.0.0.1", 1))
    assert [round(d * 1000) for d in t.frame_delays] == [300] * 5
    t._from_encoder(pkt(9 + 40 * 6000, marker=True), ("127.0.0.1", 1))   # nothing near: no sample
    assert len(t.frame_delays) == 5
