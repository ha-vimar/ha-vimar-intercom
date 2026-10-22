"""Direct video from the door panel to the phone, with real SRTP and sockets.

Through ffmpeg, the opening keyframe reached the phone inside a burst with a
presentation time already in the past, and the phone dropped it: two and a
half seconds of black until the next natural keyframe. These tests check that
what we send is a consistent RTP stream (continuous sequence, initial group
squeezed in time, live stream continuing at its own rate) and that the phone
can decrypt it packet by packet.
"""
import asyncio
import base64
import socket
import struct

import pytest

from custom_components.vimar_intercom import homekit_video as video
from custom_components.vimar_intercom import srtp

KEY = base64.b64encode(bytes(range(1, 31))).decode()
SSRC, PT = 0xABCDEF, 99


def panel(seq: int, ts: int, payload: bytes, marker: bool = False,
          ssrc: int = 0x11111111) -> bytes:
    """A packet as the panel sends it (PT 96, its own SSRC)."""
    return (bytes([0x80, (0x80 if marker else 0) | 96])
            + struct.pack("!HII", seq, ts, ssrc) + payload)


SPS = b"\x67\x42\x80\x1f"
PPS = b"\x68\xce\x3c\x80"
IDR_A = b"\x7c\x85" + b"\xaa" * 50        # FU-A, IDR start
IDR_B = b"\x7c\x45" + b"\xbb" * 30        # FU-A, IDR end
P1 = b"\x41" + b"\xcc" * 40
P2 = b"\x41" + b"\xdd" * 40


@pytest.fixture
def rig():
    loop = asyncio.new_event_loop()
    phone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    phone.bind(("127.0.0.1", 0))
    phone.settimeout(1)
    ours = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ours.bind(("127.0.0.1", 0))
    ours.setblocking(False)
    dv = video.DirectVideo(ours, phone.getsockname(), KEY, SSRC, PT)
    loop.run_until_complete(dv.open())
    rtcp_rx = srtp.SRTCPContext(KEY)
    # The phone's own RTCP sender: its index grows with every packet.
    phone_rtcp = srtp.SRTCPContext(KEY)

    def received():
        """(plain rtp) and (plain rtcp) that reached the phone."""
        loop.run_until_complete(asyncio.sleep(0.05))
        rtp, rtcp = [], []
        phone.settimeout(0.2)
        rx = srtp.SRTPContext(KEY)  # a resend is the same packet again: this phone lost it
        while True:
            try:
                data, _ = phone.recvfrom(2048)
            except OSError:
                break
            if 200 <= data[1] <= 206:
                rtcp.append(rtcp_rx.unprotect(data))
            else:
                rtp.append(rx.unprotect(data))
        return rtp, rtcp

    def run(fn, *args):
        async def call():
            fn(*args)
        loop.run_until_complete(call())

    dv.phone_rtcp = phone_rtcp
    yield dv, received, run, phone, ours.getsockname(), loop
    loop.run_until_complete(dv.stop())
    phone.close()
    loop.close()


def seq(p): return struct.unpack_from("!H", p, 2)[0]
def ts(p): return struct.unpack_from("!I", p, 4)[0]
def ssrc(p): return struct.unpack_from("!I", p, 8)[0]


BACKLOG = [
    panel(100, 9000, SPS), panel(101, 9000, PPS),
    panel(102, 9000, IDR_A), panel(103, 9000, IDR_B, marker=True),
    panel(104, 15000, P1, marker=True), panel(105, 21000, P2, marker=True),
]


class TestTheOpeningGroup:
    def test_every_packet_decrypts_and_is_restamped_for_the_phone(self, rig):
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])
        rtp, _ = received()
        assert len(rtp) == 6 and all(p is not None for p in rtp)
        assert all(ssrc(p) == SSRC and p[1] & 0x7F == PT for p in rtp)
        assert [p[12:] for p in rtp] == [b[12:] for b in BACKLOG], "payload intact"
        assert [bool(p[1] & 0x80) for p in rtp] == [False, False, False, True, True, True]

    def test_the_first_packet_to_the_phone_is_reported_once(self, rig):
        """The view's timeline marks when the phone gets its first packet."""
        dv, received, run, *_ = rig
        calls = []
        dv.on_first_packet = lambda: calls.append(dv.stats["packets"])
        run(dv.begin, BACKLOG, [])
        received()
        assert calls == [0]

    def test_sequence_is_continuous(self, rig):
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])
        rtp, _ = received()
        s = [seq(p) for p in rtp]
        assert all((b - a) & 0xFFFF == 1 for a, b in zip(s, s[1:], strict=False))

    def test_the_backlog_is_squeezed_so_it_is_not_late(self, rig):
        """Three frames, 133 ms of panel video: three ticks on our side."""
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])
        rtp, _ = received()
        t = [ts(p) for p in rtp]
        base = t[0]
        assert t == [base] * 4 + [(base + 1) & 0xFFFFFFFF, (base + 2) & 0xFFFFFFFF]

    def test_a_sender_report_goes_out_with_it(self, rig):
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])
        _, rtcp = received()
        assert rtcp and rtcp[0] is not None and rtcp[0][1] == 200
        # In RTCP the SSRC sits at byte 4, not at byte 8 as in RTP.
        assert struct.unpack_from("!I", rtcp[0], 4)[0] == SSRC

    def test_the_added_parameter_sets_come_just_before(self, rig):
        """The SPS+PPS we add takes the slot right before the group."""
        dv, received, run, *_ = rig
        stap = panel(102, 9000, b"\x78" + b"\x00\x04" + SPS + b"\x00\x04" + PPS)
        backlog = BACKLOG[2:]
        run(dv.begin, backlog, [stap])
        rtp, _ = received()
        s = [seq(p) for p in rtp]
        assert all((b - a) & 0xFFFF == 1 for a, b in zip(s, s[1:], strict=False)), s
        assert rtp[0][12] & 0x1F == 24 and ts(rtp[0]) == ts(rtp[1])


class TestTheLiveFlow:
    def test_continues_at_its_own_pace_from_the_last_frame(self, rig):
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])
        run(dv.on_live, panel(106, 27000, P1, marker=True))
        rtp, _ = received()
        # 6000 ticks after the last frame of the group, as for the panel
        assert (ts(rtp[-1]) - ts(rtp[-2])) & 0xFFFFFFFF == 6000
        assert (seq(rtp[-1]) - seq(rtp[-2])) & 0xFFFF == 1

    def test_a_late_fragment_of_a_sent_frame_keeps_its_time(self, rig):
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])
        run(dv.on_live, panel(99, 9000, SPS))
        rtp, _ = received()
        assert ts(rtp[-1]) == ts(rtp[0])

    def test_anything_older_than_the_group_is_dropped(self, rig):
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])
        run(dv.on_live, panel(90, 3000, P1, marker=True))
        rtp, _ = received()
        assert len(rtp) == 6 and dv.stats["late_dropped"] == 1

    def test_without_a_group_the_live_flow_just_starts(self, rig):
        dv, received, run, *_ = rig
        run(dv.begin, [], [])
        run(dv.on_live, panel(500, 1000, P1, marker=True))
        run(dv.on_live, panel(501, 7000, P2, marker=True))
        rtp, _ = received()
        assert len(rtp) == 2 and (ts(rtp[1]) - ts(rtp[0])) & 0xFFFFFFFF == 6000


    def test_a_new_panel_stream_is_followed(self, rig):
        """media_handler resyncs on a new SSRC and keeps forwarding. Against the
        old anchor a new stream whose clock starts lower was all "late" and
        dropped for good (or, higher, jumped hours ahead)."""
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])
        run(dv.on_live, panel(106, 27000, P1, marker=True))
        new = 0x22222222
        run(dv.on_live, panel(7, 500, P1, marker=True, ssrc=new))     # below the old anchor
        run(dv.on_live, panel(8, 500 + 6000, P2, marker=True, ssrc=new))
        run(dv.on_live, panel(9, 500 + 12000, P2, marker=True, ssrc=new))
        rtp, _ = received()
        assert dv.stats["late_dropped"] == 0
        assert len(rtp) == 10
        s = [seq(p) for p in rtp]
        assert all((b - a) & 0xFFFF == 1 for a, b in zip(s, s[1:], strict=False)), s
        t = [ts(p) for p in rtp[6:]]
        assert [(b - a) & 0xFFFFFFFF for a, b in zip(t, t[1:], strict=False)] == [6000, 6000, 6000]

    def test_a_new_stream_does_not_count_as_upstream_loss(self, rig):
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])
        run(dv.on_live, panel(20000, 500, P1, marker=True, ssrc=0x22222222))
        run(dv.on_live, panel(20001, 6500, P1, marker=True, ssrc=0x22222222))
        assert dv.stats["upstream_missing"] == 0


class TestTheSenderReport:
    def test_without_a_group_it_waits_for_the_first_packet(self, rig):
        """A report with no packet sent tied our clock's start to 'now', and
        the first packet, stamped with that start later, looked late."""
        dv, received, run, *_ = rig
        run(dv.begin, [], [])
        rtp, rtcp = received()
        assert rtp == [] and rtcp == []
        run(dv.on_live, panel(500, 1000, P1, marker=True))
        rtp, rtcp = received()
        assert len(rtp) == 1 and len(rtcp) == 1 and rtcp[0][1] == 200
        sr_rtp = struct.unpack_from("!I", rtcp[0], 16)[0]
        assert (sr_rtp - ts(rtp[0])) & 0xFFFFFFFF < 90000 // 10, "tied to the packet just sent"


class TestWhatThePhoneTellsUs:
    def test_a_keyframe_request_is_counted(self, rig):
        """PLI (RFC 4585): until now we had no way to see it."""
        dv, received, run, phone, ours, loop = rig
        pli = struct.pack("!BBHII", 0x81, 206, 2, 0x5555, SSRC)
        phone.sendto(dv.phone_rtcp.protect(pli), ours)
        loop.run_until_complete(asyncio.sleep(0.05))
        assert dv.stats["keyframe_requests"] == 1
        assert dv.stats["rtcp_in"] == {"PLI": 1}


class TestWhereThePacketsGoMissing:
    """On 26 September: 13 PLIs in one view, the passing car moving in jerks.

    A lost packet breaks its frame and every frame built on it, until the
    panel's next keyframe (three seconds). If the Wi-Fi lost it, we have it and
    resend it. If the relay lost it, there is no remedy.
    """

    def nack(self, pid: int, blp: int = 0) -> bytes:
        return struct.pack("!BBHIIHH", 0x81, 205, 3, 0x5555, SSRC, pid, blp)

    def test_a_packet_we_sent_is_sent_again(self, rig):
        dv, received, run, phone, ours, loop = rig
        run(dv.begin, BACKLOG, [])
        first, _ = received()
        lost = seq(first[2])
        phone.sendto(dv.phone_rtcp.protect(self.nack(lost)), ours)
        again, _ = received()
        assert [seq(p) for p in again] == [lost], "resent, identical"
        assert again[0] == first[2]
        assert dv.stats["nack_we_had"] == 1 and dv.stats["retransmitted"] == 1

    def test_the_bitmask_covers_the_following_packets(self, rig):
        dv, received, run, phone, ours, loop = rig
        run(dv.begin, BACKLOG, [])
        first, _ = received()
        base = seq(first[1])
        phone.sendto(dv.phone_rtcp.protect(self.nack(base, 0b101)), ours)
        again, _ = received()
        assert sorted(seq(p) for p in again) == sorted([base, (base + 1) & 0xFFFF, (base + 3) & 0xFFFF])

    def test_a_packet_we_never_had_is_counted_as_upstream(self, rig):
        dv, received, run, phone, ours, loop = rig
        run(dv.begin, BACKLOG, [])
        received()
        phone.sendto(dv.phone_rtcp.protect(self.nack(1)), ours)
        received()
        assert dv.stats["nack_never_had"] == 1 and dv.stats["retransmitted"] == 0

    def test_gaps_in_the_panel_stream_are_counted(self, rig):
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])              # last from the panel: 105
        run(dv.on_live, panel(108, 27000, P1, marker=True))   # 106, 107 missing
        assert dv.stats["upstream_missing"] == 2
        run(dv.on_live, panel(106, 24000, P2, marker=True))   # 106 arrives late
        assert dv.stats["upstream_missing"] == 1 and dv.stats["upstream_late"] == 1

    def test_the_phone_loss_report_is_read(self, rig):
        dv, received, run, phone, ours, loop = rig
        block = struct.pack("!IB", SSRC, 12) + (7).to_bytes(3, "big") + struct.pack("!IIII", 100, 450, 0, 0)
        rr = struct.pack("!BBHI", 0x81, 201, 7, 0x5555) + block
        phone.sendto(dv.phone_rtcp.protect(rr), ours)
        loop.run_until_complete(asyncio.sleep(0.05))
        assert dv.stats["rr_cumulative_lost"] == 7 and dv.stats["rr_jitter_max"] == 450


def test_a_replayed_nack_is_not_answered_again(rig):
    """Anyone on the path could resend a captured NACK over and over, and each
    copy made us resend up to seventeen packets."""
    dv, received, run, phone, ours, loop = rig
    run(dv.begin, BACKLOG, [])
    first, _ = received()
    nack = dv.phone_rtcp.protect(struct.pack("!BBHIIHH", 0x81, 205, 3, 0x5555, SSRC,
                                             seq(first[2]), 0))
    for _ in range(3):
        phone.sendto(nack, ours)
    again, _ = received()
    assert len(again) == 1 and dv.stats["retransmitted"] == 1


def test_late_packets_of_the_old_stream_are_dropped_not_followed_back(rig):
    """After a switch to a new SSRC, a packet of the old stream still in flight
    re-anchored our clock to the old stream, and the next packet of the new
    one re-anchored it back: two jumps per straggler."""
    dv, received, run, *_ = rig
    old, new = 0x11111111, 0x22222222
    run(dv.begin, BACKLOG, [])
    run(dv.on_live, panel(7, 500, P1, marker=True, ssrc=new))
    run(dv.on_live, panel(106, 27000, P1, marker=True, ssrc=old))     # late, old stream
    run(dv.on_live, panel(8, 500 + 6000, P2, marker=True, ssrc=new))
    rtp, _ = received()
    assert dv._panel_ssrc == new and dv.stats["late_dropped"] == 1
    assert len(rtp) == 8
    t = [ts(p) for p in rtp[6:]]
    assert (t[1] - t[0]) & 0xFFFFFFFF == 6000, "the new stream's clock did not jump"
    # Once the grace is over the old SSRC is a stream like any other.
    dv._old_until = 0.0
    run(dv.on_live, panel(200, 90000, P1, marker=True, ssrc=old))
    assert dv._panel_ssrc == old
