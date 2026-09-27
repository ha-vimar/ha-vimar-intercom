"""Il video diretto dalla targa al telefono, con SRTP e socket veri.

Passando da ffmpeg, il keyframe di apertura arrivava al telefono dentro una
raffica con il tempo di presentazione già nel passato, e il telefono lo
scartava: due secondi e mezzo di nero fino al keyframe naturale successivo.
Qui si prova che quello che mandiamo è un flusso RTP coerente — sequenza
continua, gruppo iniziale schiacciato nel tempo, dal vivo che prosegue alla
sua cadenza — e che il telefono può decifrarlo pacchetto per pacchetto.
"""
import asyncio
import base64
import socket
import struct

import pytest

video = pytest.importorskip("custom_components.vimar_intercom.homekit_video")
srtp = pytest.importorskip("custom_components.vimar_intercom.srtp")

KEY = base64.b64encode(bytes(range(1, 31))).decode()
SSRC, PT = 0xABCDEF, 99


def panel(seq: int, ts: int, payload: bytes, marker: bool = False) -> bytes:
    """Un pacchetto come lo manda la targa (PT 96, SSRC suo)."""
    return (bytes([0x80, (0x80 if marker else 0) | 96])
            + struct.pack("!HII", seq, ts, 0x11111111) + payload)


SPS = b"\x67\x42\x80\x1f"
PPS = b"\x68\xce\x3c\x80"
IDR_A = b"\x7c\x85" + b"\xaa" * 50        # FU-A, inizio IDR
IDR_B = b"\x7c\x45" + b"\xbb" * 30        # FU-A, fine IDR
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
    rx = srtp.SRTPContext(KEY)
    rtcp_rx = srtp.SRTCPContext(KEY)

    def received():
        """(rtp in chiaro) e (rtcp in chiaro) arrivati al telefono."""
        loop.run_until_complete(asyncio.sleep(0.05))
        rtp, rtcp = [], []
        phone.settimeout(0.2)
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
        assert [p[12:] for p in rtp] == [b[12:] for b in BACKLOG], "payload intatto"
        assert [bool(p[1] & 0x80) for p in rtp] == [False, False, False, True, True, True]

    def test_sequence_is_continuous(self, rig):
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])
        rtp, _ = received()
        s = [seq(p) for p in rtp]
        assert all((b - a) & 0xFFFF == 1 for a, b in zip(s, s[1:]))

    def test_the_backlog_is_squeezed_so_it_is_not_late(self, rig):
        """Tre fotogrammi, 133 ms di video della targa: da noi tre tick."""
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
        # Nell'RTCP l'SSRC sta al byte 4, non all'8 come nell'RTP.
        assert struct.unpack_from("!I", rtcp[0], 4)[0] == SSRC

    def test_the_added_parameter_sets_come_just_before(self, rig):
        """L'SPS+PPS aggiunto da noi prende il posto subito prima del gruppo."""
        dv, received, run, *_ = rig
        stap = panel(102, 9000, b"\x78" + b"\x00\x04" + SPS + b"\x00\x04" + PPS)
        backlog = BACKLOG[2:]
        run(dv.begin, backlog, [stap])
        rtp, _ = received()
        s = [seq(p) for p in rtp]
        assert all((b - a) & 0xFFFF == 1 for a, b in zip(s, s[1:])), s
        assert rtp[0][12] & 0x1F == 24 and ts(rtp[0]) == ts(rtp[1])


class TestTheLiveFlow:
    def test_continues_at_its_own_pace_from_the_last_frame(self, rig):
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])
        run(dv.on_live, panel(106, 27000, P1, marker=True))
        rtp, _ = received()
        # 6000 tick dopo l'ultimo fotogramma del gruppo, come per la targa
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


class TestWhatThePhoneTellsUs:
    def test_a_keyframe_request_is_counted(self, rig):
        """PLI (RFC 4585): finora non avevamo modo di vederlo."""
        dv, received, run, phone, ours, loop = rig
        pli = struct.pack("!BBHII", 0x81, 206, 2, 0x5555, SSRC)
        phone.sendto(srtp.SRTCPContext(KEY).protect(pli), ours)
        loop.run_until_complete(asyncio.sleep(0.05))
        assert dv.stats["keyframe_requests"] == 1
        assert dv.stats["rtcp_in"] == {"PLI": 1}


class TestWhereThePacketsGoMissing:
    """Il 26 settembre: 13 PLI in un'apertura, la macchina che passava a scatti.

    Un pacchetto perso rompe il fotogramma e tutti quelli costruiti sopra, fino
    al keyframe successivo della targa — tre secondi. Se l'ha perso il Wi-Fi,
    noi lo abbiamo e lo si rimanda; se l'ha perso il relay, non c'è rimedio.
    """

    def nack(self, pid: int, blp: int = 0) -> bytes:
        return struct.pack("!BBHIIHH", 0x81, 205, 3, 0x5555, SSRC, pid, blp)

    def test_a_packet_we_sent_is_sent_again(self, rig):
        dv, received, run, phone, ours, loop = rig
        run(dv.begin, BACKLOG, [])
        first, _ = received()
        lost = seq(first[2])
        phone.sendto(srtp.SRTCPContext(KEY).protect(self.nack(lost)), ours)
        again, _ = received()
        assert [seq(p) for p in again] == [lost], "rimandato, identico"
        assert again[0] == first[2]
        assert dv.stats["nack_we_had"] == 1 and dv.stats["retransmitted"] == 1

    def test_the_bitmask_covers_the_following_packets(self, rig):
        dv, received, run, phone, ours, loop = rig
        run(dv.begin, BACKLOG, [])
        first, _ = received()
        base = seq(first[1])
        phone.sendto(srtp.SRTCPContext(KEY).protect(self.nack(base, 0b101)), ours)
        again, _ = received()
        assert sorted(seq(p) for p in again) == sorted([base, (base + 1) & 0xFFFF, (base + 3) & 0xFFFF])

    def test_a_packet_we_never_had_is_counted_as_upstream(self, rig):
        dv, received, run, phone, ours, loop = rig
        run(dv.begin, BACKLOG, [])
        received()
        phone.sendto(srtp.SRTCPContext(KEY).protect(self.nack(1)), ours)
        received()
        assert dv.stats["nack_never_had"] == 1 and dv.stats["retransmitted"] == 0

    def test_gaps_in_the_panel_stream_are_counted(self, rig):
        dv, received, run, *_ = rig
        run(dv.begin, BACKLOG, [])              # ultimo della targa: 105
        run(dv.on_live, panel(108, 27000, P1, marker=True))   # mancano 106, 107
        assert dv.stats["upstream_missing"] == 2
        run(dv.on_live, panel(106, 24000, P2, marker=True))   # 106 arriva tardi
        assert dv.stats["upstream_missing"] == 1 and dv.stats["upstream_late"] == 1

    def test_the_phone_loss_report_is_read(self, rig):
        dv, received, run, phone, ours, loop = rig
        block = struct.pack("!IB", SSRC, 12) + (7).to_bytes(3, "big") + struct.pack("!IIII", 100, 450, 0, 0)
        rr = struct.pack("!BBHI", 0x81, 201, 7, 0x5555) + block
        phone.sendto(srtp.SRTCPContext(KEY).protect(rr), ours)
        loop.run_until_complete(asyncio.sleep(0.05))
        assert dv.stats["rr_cumulative_lost"] == 7 and dv.stats["rr_jitter_max"] == 450
