"""Depacketizer H.264 (RTP) e codec μ-law — funzioni pure di media_handler.

Fixture RTP sintetiche: verifichiamo che single-NAL, STAP-A e FU-A vengano
riassemblati nei NAL corretti e inviati nell'ordine SPS→PPS→IDR. Nessuna
dipendenza da Home Assistant: usiamo direttamente RTPVideoProtocol e
intercettiamo i NAL prodotti tramite _queue_nal.
"""
from __future__ import annotations

import asyncio
import shutil
import socket
import struct
import subprocess

import pytest

from custom_components.vimar_intercom import media_handler as mh
from custom_components.vimar_intercom.media_handler import (
    RTPVideoProtocol,
    ulaw_decode,
    ulaw_encode,
)

START_CODE = b"\x00\x00\x00\x01"


@pytest.fixture(autouse=True)
def _senza_hub(monkeypatch):
    # Un hub creato da un test prima lascia il suo request_keyframe (asyncio.create_task):
    # qui non c'è un loop.
    monkeypatch.setattr(mh, "request_keyframe", None)


def _mk_proto():
    """RTPVideoProtocol che raccoglie i NAL emessi (bypassa asyncio queue/WS)."""
    p = RTPVideoProtocol.__new__(RTPVideoProtocol)
    # Stato minimo necessario a _depacketize/_emit_nal.
    p._fua_buf = bytearray()
    p._fua_started = False
    p._fua_expected_seq = None
    p._last_sps = None
    p._last_pps = None
    p.panel = None
    p._ps_by_panel = {}
    p._sps_pps_sent = False
    p._pending_idr = None
    p._drop_until_idr = False
    p._nal_count = 0
    p._nal_types = {}
    p.pkt_count = 0
    # _emit_nal fa un guard su ws_send_bytes/_nal_queue: rendili truthy.
    p._nal_queue = object()
    mh.ws_send_bytes = (lambda *_a, **_k: None)
    emitted: list[bytes] = []
    # _queue_nal è il punto unico da cui passano tutti i NAL ordinati.
    p._queue_nal = lambda nal, _out=emitted: _out.append(nal)
    return p, emitted


def _rtp(payload: bytes, seq: int) -> bytes:
    hdr = struct.pack("!BBHII", 0x80, 96, seq, 0, 0x1234)
    return hdr + payload


def test_single_nal_emitted():
    p, out = _mk_proto()
    p._sps_pps_sent = True  # consenti P-frame diretti
    nal = bytes([0x41, 0xAA, 0xBB])  # type=1 (P-frame)
    p._depacketize(nal, 10)
    assert out == [nal]


def test_stap_a_splits_sps_pps():
    p, out = _mk_proto()
    sps = bytes([0x67, 0x42, 0x80, 0x1F])  # type 7
    pps = bytes([0x68, 0xCE, 0x3C])         # type 8
    stap = bytes([0x78])  # STAP-A header (type 24)
    stap += struct.pack("!H", len(sps)) + sps
    stap += struct.pack("!H", len(pps)) + pps
    p._depacketize(stap, 20)
    # SPS+PPS devono uscire (in ordine) dopo l'aggregazione
    assert sps in out and pps in out
    assert out.index(sps) < out.index(pps)


def test_fua_reassembly():
    p, out = _mk_proto()
    p._sps_pps_sent = True  # permetti l'emissione dell'IDR ricostruito
    # IDR (type 5) frammentato in 2 pacchetti FU-A.
    # FU indicator: F|NRI da NAL orig (0x60) | type 28
    frag_payload = bytes(range(20))
    half = len(frag_payload) // 2
    # start
    pkt1 = bytes([0x7C, 0x80 | 5]) + frag_payload[:half]  # S=1, type=5
    # end
    pkt2 = bytes([0x7C, 0x40 | 5]) + frag_payload[half:]  # E=1, type=5
    p._depacketize(pkt1, 30)
    p._depacketize(pkt2, 31)
    # Un solo NAL ricostruito: header 0x65 (F|NRI 0x60 | type 5) + payload
    assert len(out) == 1
    assert out[0][0] == 0x65
    assert out[0][1:] == frag_payload


def test_ulaw_roundtrip_is_close():
    # μ-law è lossy; verifichiamo che decode(encode(x)) resti vicino a x.
    import random
    random.seed(1)
    samples = [random.randint(-20000, 20000) for _ in range(64)]
    pcm = b"".join(struct.pack("<h", s) for s in samples)
    encoded = ulaw_encode(pcm)
    decoded = ulaw_decode(encoded)
    out = [struct.unpack_from("<h", decoded, i * 2)[0] for i in range(len(samples))]
    for orig, rt in zip(samples, out, strict=False):
        # Tolleranza μ-law: errore relativo entro ~la banda del segmento.
        assert abs(orig - rt) <= max(256, abs(orig) * 0.10)


def test_ulaw_decode_table_len():
    assert len(mh._ULAW_DECODE) == 256


def _video_rx():
    """RTPVideoProtocol vero: i NAL escono da frame_sink (nessun WebSocket)."""
    p = RTPVideoProtocol()
    p.remote_addr = ("192.0.2.1", 4002)  # media aperto (setup_media)
    got: list[int] = []
    p.frame_sink = lambda nal: got.append(int.from_bytes(nal[1:3], "big"))
    calls = []
    orig = p._depacketize
    p._depacketize = lambda payload, seq: (calls.append(seq), orig(payload, seq))
    return p, got, calls


def _pkt(seq: int, ssrc: int = 0x1234, nal: int = 0x41) -> bytes:
    # NAL singolo (tipo 1, o `nal`) che porta il proprio numero di sequenza
    return struct.pack("!BBHII", 0x80, 96, seq, 0, ssrc) + bytes([nal]) + seq.to_bytes(2, "big")


def test_sequenza_che_torna_indietro_non_esce_fuori_ordine():
    """Campo: «FU-A seq gap: expected 37 got 23 (gap=65522)» a inizio chiamata."""
    p, got, calls = _video_rx()
    for seq in range(20, 37):
        p.datagram_received(_pkt(seq), p.remote_addr)
    for seq in range(23, 41):          # stessi numeri di nuovo (duplicati/replay)
        p.datagram_received(_pkt(seq), p.remote_addr)
    assert got == list(range(20, 41))  # in ordine, niente duplicati
    assert len(calls) < 100            # niente giro di 65 000 sequenze


def test_nuovo_ssrc_risincronizza_dal_suo_idr():
    """A new SSRC is a new stream: its P frames refer to an IDR we never had, and on
    the card they smeared until the next IDR. Nothing goes out until its first IDR."""
    p, got, _ = _video_rx()
    for seq in range(40000, 40010):
        p.datagram_received(_pkt(seq, 1), p.remote_addr)
    for seq in range(5, 8):
        p.datagram_received(_pkt(seq, 2), p.remote_addr)
    p.datagram_received(_pkt(8, 2, nal=0x65), p.remote_addr)
    p.datagram_received(_pkt(9, 2), p.remote_addr)
    assert got == list(range(40000, 40010)) + [8, 9]


def test_buco_nella_sequenza_si_salta():
    p, got, _ = _video_rx()
    p.REORDER_WAIT = 0                 # il tempo d'attesa del buco è nel test sotto
    p.datagram_received(_pkt(20), p.remote_addr)
    for seq in range(30, 40):          # 21..29 persi; IDR: i P dopo un buco si scartano
        p.datagram_received(_pkt(seq, nal=0x65), p.remote_addr)
    assert got == [20] + list(range(30, 40))[:len(got) - 1]
    assert got[-1] >= 34               # al più REORDER_BUF_SIZE pacchetti trattenuti


def _fua(seq: int, first=False, last=False) -> bytes:
    hdr = 0x65 & 0x1F | (0x80 if first else 0) | (0x40 if last else 0)
    return (struct.pack("!BBHII", 0x80, 96, seq, 0, 0x1234)
            + bytes([0x7C, hdr]) + seq.to_bytes(2, "big") + b"\xaa" * 20)


def test_idr_burst_reordered_by_the_relay_is_not_lost(monkeypatch):
    """40515 over the cloud: a keyframe's packets arrive up to 12 places late, ms later."""
    now = [100.0]
    monkeypatch.setattr(mh.time, "monotonic", lambda: now[0])
    p, _, calls = _video_rx()
    nals = []
    p.frame_sink = nals.append
    p._lost = lambda why: pytest.fail(why)
    order = [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 1]  # seq 1 arrives 10 places late
    for i in order:
        now[0] += 0.001
        p.datagram_received(_fua(500 + i, first=i == 0, last=i == 11), p.remote_addr)
    assert not p._drop_until_idr
    assert calls == list(range(500, 512))
    assert len(nals) == 1 and len(nals[0]) > 12 * 20


def test_a_40_packet_keyframe_burst_reordered_by_the_relay_is_not_lost(monkeypatch):
    """At 1.5 Mbit/s a keyframe is ~40 packets and the P frames follow at once:
    packet 1 turns up 34 ms late, after more than 64 others had piled up behind it."""
    now = [100.0]
    monkeypatch.setattr(mh.time, "monotonic", lambda: now[0])
    p, _, calls = _video_rx()
    nals = []
    p.frame_sink = nals.append
    p._lost = lambda why: pytest.fail(why)
    idr = [_fua(500 + i, first=i == 0, last=i == 39) for i in range(40)]
    p_frames = [_pkt(540 + i) for i in range(60)]
    burst = idr[:1] + idr[2:] + p_frames + idr[1:2]   # seq 501 arrives 99 places late
    for data in burst:
        now[0] += 0.0003                               # 100 packets in 30 ms, inside REORDER_WAIT
        p.datagram_received(data, p.remote_addr)
    assert not p._drop_until_idr
    assert calls == list(range(500, 600))
    assert len(nals) == 1 + 60


def test_a_real_gap_is_declared_lost_after_the_reorder_wait(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(mh.time, "monotonic", lambda: now[0])
    p, _, _ = _video_rx()
    p.datagram_received(_pkt(20), p.remote_addr)
    p.datagram_received(_pkt(22), p.remote_addr)   # 21 never comes
    assert not p._drop_until_idr
    now[0] += p.REORDER_WAIT + 0.01
    p.datagram_received(_pkt(23), p.remote_addr)
    assert p._drop_until_idr


def _consumers(p):
    """What /av's ffmpeg and the HomeKit sinks receive, by sequence number."""
    fwd, sunk = [], []
    p.forward_av = True
    p._forward_av = lambda rtp: fwd.append(_seq_of(rtp))
    p.rtp_sinks.append(lambda rtp: sunk.append(_seq_of(rtp)))
    return fwd, sunk


@pytest.mark.parametrize("ahead", [600, 20000])
def test_a_lone_packet_far_ahead_reaches_no_consumer(ahead):
    """The 40515 cloud relay sends isolated packets far ahead on the same SSRC.
    /av's ffmpeg took one as the new head and dropped the live stream as too
    late (600 ahead: ~20 s frozen), and the phone's libsrtp (HomeKit) refused
    everything behind it. The GOP replayed to a later /av carried it too."""
    p, _, calls = _video_rx()
    fwd, sunk = _consumers(p)
    for seq in range(1000, 1010):
        p.datagram_received(_pkt(seq, nal=0x65), p.remote_addr)
    p.datagram_received(_pkt(1009 + ahead), p.remote_addr)
    for seq in range(1010, 1020):
        p.datagram_received(_pkt(seq, nal=0x65), p.remote_addr)
    live = list(range(1000, 1020))
    assert fwd == sunk == calls == live
    assert [_seq_of(r) for r in p.gop_in_sequence_order()] == live
    assert not p._drop_until_idr


def test_a_real_jump_on_the_same_ssrc_is_followed_after_a_few_packets():
    """A relay switching legs (or a long loss) jumps the numbers for good: after
    RESYNC_FAR packets in a row the stream follows them from the first one, so
    the first fragments of the keyframe after the jump are not lost."""
    p, _, calls = _video_rx()
    fwd, sunk = _consumers(p)
    for seq in range(1000, 1010):
        p.datagram_received(_pkt(seq), p.remote_addr)
    jump = list(range(5000, 5010 + p.RESYNC_FAR))
    for seq in jump:
        p.datagram_received(_pkt(seq), p.remote_addr)
    assert fwd == sunk == calls == list(range(1000, 1010)) + jump
    assert p._drop_until_idr  # the packets in between are lost: wait for a keyframe


def test_a_real_jump_reordered_by_the_relay_is_followed_in_order():
    """The relay keeps swapping neighbours after the jump, across the wrap: the
    run is not numbered one after the other but it is one stream."""
    p, _, calls = _video_rx()
    fwd, sunk = _consumers(p)
    for seq in range(64800, 64810):
        p.datagram_received(_pkt(seq), p.remote_addr)
    after = [(65530 + i) & 0xFFFF for i in range(p.RESYNC_FAR + 10)]
    for i in range(0, len(after), 2):
        p.datagram_received(_pkt(after[i + 1]), p.remote_addr)
        p.datagram_received(_pkt(after[i]), p.remote_addr)
    assert calls == list(range(64800, 64810)) + after
    assert sorted(fwd[10:]) == sorted(sunk[10:]) == sorted(after)


def test_a_jump_whose_first_packet_is_its_newest_is_replayed_whole():
    """The run is replayed from its lowest packet: one FAR_AHEAD past it is
    still not far ahead of the newest before it, so nothing is held again."""
    p, _, calls = _video_rx()
    for seq in range(1000, 1010):
        p.datagram_received(_pkt(seq), p.remote_addr)
    run = list(range(5000, 5001 + p.FAR_AHEAD))
    p.datagram_received(_pkt(run[-1]), p.remote_addr)
    for seq in run[:-1]:
        p.datagram_received(_pkt(seq), p.remote_addr)
    assert calls == list(range(1000, 1010)) + run


def test_scattered_strays_in_a_row_do_not_move_the_stream():
    """Strays far from each other are not one stream that jumped, even
    RESYNC_FAR of them in a row."""
    p, _, calls = _video_rx()
    fwd, sunk = _consumers(p)
    for seq in range(1000, 1010):
        p.datagram_received(_pkt(seq), p.remote_addr)
    for k in range(p.RESYNC_FAR + 1):
        p.datagram_received(_pkt(2000 + 1000 * k), p.remote_addr)
    for seq in range(1010, 1020):
        p.datagram_received(_pkt(seq), p.remote_addr)
    assert fwd == sunk == calls == list(range(1000, 1020))
    assert not p._drop_until_idr


@pytest.mark.parametrize("ahead", [1000, 0x7F00])
@pytest.mark.parametrize("n", [5, mh.RTPVideoProtocol.RESYNC_FAR])  # 5 used to pass for a jump
def test_a_burst_of_strays_does_not_freeze_the_video(n, ahead):
    """Strays come a few at a time (3 seen): five in a row are held back like one.
    RESYNC_FAR look like a jump and we follow them, but the live stream keeps
    coming from where it was: RESYNC_BACK of it in a row and we go back, instead
    of dropping it all as late (video frozen for seconds, or the whole call)."""
    p, _, calls = _video_rx()
    p._lost = lambda why: None
    fwd, sunk = _consumers(p)
    for seq in range(100, 150):
        p.datagram_received(_pkt(seq), p.remote_addr)
    strays = [(149 + ahead + k) & 0xFFFF for k in range(n)]
    for seq in strays:
        p.datagram_received(_pkt(seq), p.remote_addr)
    for seq in range(150, 400):
        p.datagram_received(_pkt(seq), p.remote_addr)
    followed = strays if n >= p.RESYNC_FAR else []
    assert calls == fwd == sunk == list(range(100, 150)) + followed + list(range(150, 400))


def test_a_sparse_burst_followed_leaves_room_to_go_back():
    """A burst spread over FAR_AHEAD, one more stray, then the live stream: the
    way back stays open for FAR_AHEAD packets past the newest stray, not past
    the first."""
    p, _, calls = _video_rx()
    p._lost = lambda why: None
    for seq in range(100, 150):
        p.datagram_received(_pkt(seq), p.remote_addr)
    n = p.RESYNC_FAR
    strays = [749 + round(k * p.FAR_AHEAD / (n - 1)) for k in range(n)]
    for seq in strays + [strays[-1] + 1]:
        p.datagram_received(_pkt(seq), p.remote_addr)
    for seq in range(150, 400):
        p.datagram_received(_pkt(seq), p.remote_addr)
    assert calls[-250:] == list(range(150, 400))  # all of the live stream, back in order


def test_two_legs_on_one_ssrc_do_not_flip_the_stream_forever():
    """Two legs on one SSRC, in runs long enough to move us once: after a jump we
    undo, the next one needs twice as many in a row, so the flipping stops."""
    p, _, calls = _video_rx()
    p._lost = lambda why: None
    run = p.RESYNC_FAR + 20
    a, b = 1000, 9000
    on_b = []
    for _ in range(10):
        for _ in range(run):
            p.datagram_received(_pkt(a), p.remote_addr)
            a += 1
        for _ in range(run):
            p.datagram_received(_pkt(b), p.remote_addr)
            b += 1
        on_b.append(calls[-1] == b - 1)
    assert on_b == [True] + [False] * 9  # followed once, undone, never again
    assert calls[-run:] == list(range(a - run, a))


def test_old_packets_after_a_real_jump_do_not_take_us_back():
    """Only the stream going on from where we jumped, before the new numbers go
    on too, takes us back: late packets from before the jump, or the old numbers
    once the new ones went on past a burst's reach, do not."""
    p, _, calls = _video_rx()
    p._lost = lambda why: None
    for seq in range(100, 150):
        p.datagram_received(_pkt(seq), p.remote_addr)
    jump = list(range(5000, 5000 + p.RESYNC_FAR))
    for seq in jump:
        p.datagram_received(_pkt(seq), p.remote_addr)
    for seq in range(120, 120 + p.RESYNC_BACK):  # stragglers from before the jump
        p.datagram_received(_pkt(seq), p.remote_addr)
    more = list(range(jump[-1] + 1, jump[-1] + p.FAR_AHEAD + 2))
    for seq in more:
        p.datagram_received(_pkt(seq), p.remote_addr)
    for seq in range(150, 150 + p.RESYNC_BACK):  # the old numbers, now too late
        p.datagram_received(_pkt(seq), p.remote_addr)
    p.datagram_received(_pkt(more[-1] + 1), p.remote_addr)
    assert calls == list(range(100, 150)) + jump + more + [more[-1] + 1]


def test_av_goes_on_from_its_last_packet_across_a_jump():
    """/av's ffmpeg drops anything behind the newest it saw: across a jump we
    follow, or undo, it gets one continuous sequence. The panel's clock goes on
    on the same SSRC, so the timestamps (audio sync) are left alone. A later
    /av's GOP replay gets none of the strays."""
    p, _, _ = _video_rx()
    p._lost = lambda why: None
    out = []
    p.forward_av = True
    p.ffmpeg_av_sock = type("S", (), {"sendto": lambda self, d, a: out.append(d)})()

    def send(seq, nal=0x41):
        rtp = _pkt(seq, nal=nal)
        p.datagram_received(rtp[:4] + struct.pack("!I", 3000 * seq) + rtp[8:], p.remote_addr)
    send(100, nal=0x65)
    for seq in range(101, 150):
        send(seq)
    for seq in range(5000, 5000 + p.RESYNC_FAR):
        send(seq)
    for seq in range(150, 200):
        send(seq)
    assert [_seq_of(d) for d in out] == list(range(100, 100 + len(out)))
    assert [struct.unpack_from("!I", d, 4)[0] for d in out][-50:] == [3000 * s for s in range(150, 200)]
    assert all(_seq_of(d) < 5000 for d in p.gop_in_sequence_order())


def test_av_goes_on_from_its_newest_packet_not_a_late_one(monkeypatch):
    """A late packet still goes to /av (see below): the jump carries on from the
    newest one ffmpeg got, or the whole replayed run would be "too late"."""
    now = [100.0]
    monkeypatch.setattr(mh.time, "monotonic", lambda: now[0])
    p, _, _ = _video_rx()
    p._lost = lambda why: None
    fwd = []
    p.forward_av = True
    p.ffmpeg_av_sock = type("S", (), {"sendto": lambda self, d, a: fwd.append(_seq_of(d))})()
    for seq in (1000, 1001, 1003):
        p.datagram_received(_pkt(seq), p.remote_addr)
    now[0] += p.REORDER_WAIT + 0.01
    for seq in range(1004, 1100):
        p.datagram_received(_pkt(seq), p.remote_addr)
    p.datagram_received(_pkt(1002), p.remote_addr)  # late, still forwarded
    for seq in range(1400, 1400 + p.RESYNC_FAR):
        p.datagram_received(_pkt(seq), p.remote_addr)
    assert fwd[-p.RESYNC_FAR :] == list(range(1100, 1100 + p.RESYNC_FAR))


def test_copies_of_one_stray_do_not_move_the_stream():
    """The relay repeats packets too: RESYNC_FAR copies of one stray are still
    one packet, not a run."""
    p, _, calls = _video_rx()
    for seq in range(1000, 1010):
        p.datagram_received(_pkt(seq), p.remote_addr)
    for _ in range(p.RESYNC_FAR + 1):
        p.datagram_received(_pkt(1600), p.remote_addr)
    p.datagram_received(_pkt(1010), p.remote_addr)
    assert calls == list(range(1000, 1011))


def test_a_packet_late_for_the_reorder_buffer_still_reaches_av(monkeypatch):
    """/av's ffmpeg waits up to -max_delay (300 ms) for a late packet, on its own
    clock: it still gets one we gave up on. The sinks do not.
    One older than FAR_AHEAD (an old keyframe resent) ffmpeg drops anyway."""
    now = [100.0]
    monkeypatch.setattr(mh.time, "monotonic", lambda: now[0])
    p, _, _ = _video_rx()
    fwd, sunk = _consumers(p)
    p.datagram_received(_pkt(1000), p.remote_addr)
    p.datagram_received(_pkt(1002), p.remote_addr)
    now[0] += p.REORDER_WAIT + 0.01
    p.datagram_received(_pkt(1003), p.remote_addr)   # 1001 given up
    p.datagram_received(_pkt(1001), p.remote_addr)   # ...and here it is, 90 ms late
    p.datagram_received(_pkt(700), p.remote_addr)
    assert fwd == [1000, 1002, 1003, 1001]
    assert sunk == [1000, 1002, 1003]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg non installato")
def test_rtp_h264_vero_di_ffmpeg_con_pacchetti_scambiati_e_ripetuti(tmp_path):
    """RTP H.264 prodotto da ffmpeg (FU-A), con due pacchetti scambiati e un
    tratto ripetuto: i NAL ricostruiti devono decodificare senza errori."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(3)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x480:rate=15",
         "-t", "2", "-g", "15", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "baseline",
         "-f", "rtp", "-payload_type", "96", f"rtp://127.0.0.1:{sock.getsockname()[1]}"],
        check=True)
    pkts = []
    try:
        while True:
            pkts.append(sock.recv(2000))
    except TimeoutError:
        pass
    assert len(pkts) > 30  # STAP-A (SPS+PPS), FU-A e NAL singoli
    pkts[10], pkts[11] = pkts[11], pkts[10]
    pkts[30:30] = pkts[20:27]              # replay all'indietro, come sul campo

    p = RTPVideoProtocol()
    p.remote_addr = ("192.0.2.1", 4002)
    nals: list[bytes] = []
    p.frame_sink = nals.append
    for d in pkts:
        p.datagram_received(d, p.remote_addr)
    out = tmp_path / "out.h264"
    out.write_bytes(b"".join(START_CODE + n for n in nals))
    res = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"],
                         capture_output=True, text=True)
    assert res.returncode == 0 and res.stderr == ""
    frames = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
                            capture_output=True, text=True).stdout.strip()
    assert int(frames) == 30


def test_coda_ws_gop_corrente_e_client_lento():
    """La coda dei NAL verso il WebSocket: tiene il GOP corrente (SPS, PPS, IDR e i P
    dopo) per chi si collega a video in corso, mandato in coda a lui solo, in ordine
    coi NAL dal vivo; con un client lento (coda piena) si butta tutto e si riparte
    puliti dal prossimo IDR, senza P orfani in mezzo."""
    import asyncio

    async def s():
        p = RTPVideoProtocol()
        p._nal_queue = asyncio.Queue(maxsize=4)
        mh.ws_send_bytes = (lambda *_a, **_k: None)
        sps, pps, idr, pf = bytes([0x67, 1]), bytes([0x68, 1]), bytes([0x65, 1]), bytes([0x41, 1])
        for n in (sps, pps, idr, pf):
            p._queue_nal(n)
        assert [m[5:] for m in p._gop_msgs] == [sps, pps, idr, pf]
        sent = []

        async def send(m):
            sent.append(m[5:])
        p.replay_gop_ws(send)  # coda già piena (4): il replay non entra, ma non rompe
        assert p._nal_queue.qsize() == 4
        p._queue_nal(pf)  # coda piena con un P: si svuota, via i P fino al prossimo IDR
        assert p._nal_queue.qsize() == 0 and p._drop_until_idr and p._gop_msgs == []
        p._sps_pps_sent = True
        p._emit_nal(pf)
        assert p._nal_queue.qsize() == 0, "P orfano dopo il buco"
        p._last_sps, p._last_pps = sps, pps
        p._emit_nal(idr)  # SPS+PPS+IDR: si riparte
        assert not p._drop_until_idr and [m[5:] for m in p._gop_msgs] == [sps, pps, idr]
        p.replay_gop_ws(send)
        p.connection_made(None)  # avvia il sender sulla coda (ne fa una nuova)
        p._nal_queue.put_nowait((send, b"\x03\x00\x00\x00\x01" + pf))
        await asyncio.sleep(0.05)
        assert sent == [pf]
        p._nal_sender_task.cancel()
    asyncio.run(s())


def test_dopo_stop_media_l_rtp_in_ritardo_si_scarta_e_il_gop_parte_solo_dall_idr(monkeypatch):
    """Dal campo: dopo il nostro BYE la targa manda ancora RTP (~100 ms dal cloud). Fino a
    qui veniva depacketizzato («First video RTP» ricontato), mandato ai WS e, con un IDR,
    messo nel GOP che si rimanda alla card della chiamata dopo. Ora fra stop_media e
    setup_media si scarta tutto (video e audio); il GOP per il replay comincia solo con
    SPS→PPS→IDR, mai con un P."""
    import asyncio

    sps, pps, idr, pf = bytes([0x67, 1]), bytes([0x68, 1]), bytes([0x65, 1]), bytes([0x41, 1])

    async def s():
        vp, ap = RTPVideoProtocol(), mh.RTPAudioProtocol()
        vp._nal_queue = asyncio.Queue()
        mh.ws_send_bytes = (lambda *_a, **_k: None)
        for k, v in dict(video_proto=vp, audio_proto=ap, _stun_task=None, _audio_task=None, _tx_task=None).items():
            monkeypatch.setattr(mh, k, v)
        monkeypatch.setattr(mh.frame_grabber, "start", lambda vp: None)
        monkeypatch.setattr(mh.frame_grabber, "stop", lambda vp: None)
        sdp = {"conn": "192.0.2.1", "audio": {"port": 4000}, "video": {"port": 4002}}
        seq = 0

        def feed(*nals, ssrc=7):
            # Always from the panel's address, also after stop_media: what is
            # dropped then is dropped by the "media closed" guard.
            nonlocal seq
            for n in nals:
                seq += 1
                vp.datagram_received(struct.pack("!BBHII", 0x80, 96, seq, 0, ssrc) + n,
                                     ("192.0.2.1", 4002))

        await mh.setup_media(sdp)
        feed(pf, pf)                         # P prima dell'IDR: non entrano nel GOP
        assert vp._gop_msgs == [] and vp.pkt_count == 2
        feed(sps, pps, idr, pf)
        assert [m[5:] for m in vp._gop_msgs] == [sps, pps, idr, pf]
        await mh.stop_media()
        assert vp.remote_addr is None and vp._gop_msgs == []
        queued = vp._nal_queue.qsize()       # i NAL della chiamata, già in coda per i WS
        feed(pf, sps, pps, idr, pf)          # in ritardo dalla targa: via
        ap.datagram_received(struct.pack("!BBHII", 0x80, 0, 1, 0, 5) + b"\xff" * 160,
                             ("192.0.2.1", 4000))
        assert vp.pkt_count == 0 and vp._gop_msgs == [] and vp._nal_queue.qsize() == queued
        assert ap.pkt_count == 0 and ap.audio_buffer.empty()
        await mh.setup_media(sdp)           # chiamata nuova: si riparte, dal suo IDR
        feed(pf, ssrc=8)
        assert vp.pkt_count == 1 and vp._gop_msgs == [] and vp._nal_queue.qsize() == queued
        feed(sps, pps, idr, ssrc=8)
        assert [m[5:] for m in vp._gop_msgs] == [sps, pps, idr]
        await mh.stop_media()
    asyncio.run(s())


def test_pacchetto_perso_niente_nal_col_buco_e_p_scartati_fino_all_idr(monkeypatch):
    """Dal campo (40515 via cloud, 2026-09-28): «FU-A seq gap: expected 91 got 92 (gap=1),
    continuing» e video smerigliato fino all'IDR dopo. Un frammento perso dentro un FU-A:
    quel NAL non esce mai; dopo una perdita i P (WS e foto) si scartano fino al prossimo
    IDR, che riparte; il keyframe si chiede subito, al più uno al secondo."""
    p, out = _mk_proto()
    p._sps_pps_sent = True
    p._keyframe_at = 0.0
    foto: list[bytes] = []
    p.frame_sink = foto.append
    asked: list[int] = []
    monkeypatch.setattr(mh, "request_keyframe", lambda: asked.append(1))
    mono = [1000.0]
    monkeypatch.setattr(mh.time, "monotonic", lambda: mono[0])
    pf = bytes([0x41, 0xAA])
    p._depacketize(pf, 89)
    p._depacketize(bytes([0x7C, 0x80 | 5]) + bytes(10), 90)   # IDR FU-A: start
    p._depacketize(bytes([0x7C, 5]) + bytes(10), 92)          # 91 perso
    p._depacketize(bytes([0x7C, 0x40 | 5]) + bytes(10), 93)   # end
    assert out == [pf] and foto == [pf], "NAL col buco emesso"
    assert asked == [1] and p._drop_until_idr
    p._depacketize(pf, 94)                                    # P che riferisce il buco
    p._depacketize(pf, 95)
    assert out == [pf] and foto == [pf]
    p._depacketize(bytes([0x7C, 0x80 | 5]) + bytes(4), 96)
    p._depacketize(bytes([0x7C, 0x40 | 5]) + bytes(4), 97)    # IDR intero: si riparte
    p._depacketize(pf, 98)
    assert [n[0] & 0x1F for n in out] == [1, 5, 1] and [n[0] & 0x1F for n in foto] == [1, 5, 1]
    assert not p._drop_until_idr
    # Seconda perdita entro 1 s: niente secondo INFO; dopo 1 s sì.
    p._depacketize(bytes([0x7C, 0x80 | 1]) + bytes(4), 99)
    p._depacketize(bytes([0x7C, 0x40 | 1]) + bytes(4), 101)   # 100 perso
    assert asked == [1]
    mono[0] += 1.0
    p._depacketize(bytes([0x7C, 0x80 | 1]) + bytes(4), 102)
    p._depacketize(bytes([0x7C, 0x40 | 1]) + bytes(4), 104)   # 103 perso
    assert asked == [1, 1] and len(out) == 3


def test_pacchetto_singolo_perso_fra_due_nal_scarta_i_p_fino_all_idr(monkeypatch):
    """Perso un pacchetto intero (NAL singolo, non FU-A): il depacketizer da solo non se
    ne accorge, ma il buco in uscita dal riordino sì: P via fino al prossimo IDR."""
    p, got, _ = _video_rx()
    p.REORDER_WAIT = 0
    asked: list[int] = []
    monkeypatch.setattr(mh, "request_keyframe", lambda: asked.append(1))
    for seq in (20, 21, 23, 24, 25, 26, 27, 28, 29):        # 22 perso
        p.datagram_received(_pkt(seq), p.remote_addr)
    assert got == [20, 21] and asked == [1]                  # i P dopo il buco: via
    p.datagram_received(_pkt(30, nal=0x65), p.remote_addr)
    p.datagram_received(_pkt(31), p.remote_addr)
    assert got == [20, 21, 30, 31]


def test_keyframe_vecchio_rimandato_non_azzera_il_gop_per_il_replay(monkeypatch):
    """La 40515 rimanda pacchetti vecchi di un keyframe (seq 103, 2, 104: stesso SSRC). Il
    GOP per il replay a ffmpeg si riempiva PRIMA del filtro di riordino: l'IDR vecchio
    azzerava `_gop`, e replay_gop mandava «IDR vecchio a pezzi + P nuovi»."""
    import asyncio

    sps, pps, idr, pf = bytes([0x67, 1]), bytes([0x68, 1]), bytes([0x65, 1]), bytes([0x41, 1])

    async def s():
        vp = RTPVideoProtocol()
        vp._nal_queue = asyncio.Queue()
        for k, v in dict(video_proto=vp, audio_proto=None, _stun_task=None, _audio_task=None, _tx_task=None).items():
            monkeypatch.setattr(mh, k, v)
        monkeypatch.setattr(mh.frame_grabber, "start", lambda vp: None)
        monkeypatch.setattr(mh.frame_grabber, "stop", lambda vp: None)
        await mh.setup_media({"conn": "192.0.2.1", "audio": {}, "video": {"port": 4002}})

        def pkt(seq, ts, nal):
            return struct.pack("!BBHII", 0x80, 96, seq, ts, 7) + nal

        for p in (pkt(1, 0, sps), pkt(2, 0, pps), pkt(3, 0, idr), pkt(4, 3000, pf), pkt(5, 6000, pf)):
            vp.datagram_received(p, vp.remote_addr)
        gop = list(vp._gop)
        assert [g[12:] for g in gop] == [sps, pps, idr, pf, pf]
        vp.datagram_received(pkt(3, 0, idr), vp.remote_addr)     # l'IDR di prima, rimandato
        vp.datagram_received(pkt(1, 0, sps), vp.remote_addr)
        assert vp._gop == gop, "il keyframe vecchio ha azzerato il GOP"
        vp.datagram_received(pkt(6, 9000, pf), vp.remote_addr)  # il flusso continua da dov'era
        assert [g[12:] for g in vp._gop] == [sps, pps, idr, pf, pf, pf]
        await mh.stop_media()
    asyncio.run(s())


def _seq_of(rtp: bytes) -> int:
    return struct.unpack_from("!H", rtp, 2)[0]


def test_the_gop_is_replayed_in_sequence_order():
    """_gop fills in arrival order; ffmpeg drops every packet older than the
    first one it sees ("RTP: dropping old packet received too late")."""
    p = RTPVideoProtocol()
    p._gop = [_pkt(seq) for seq in (101, 100, 103, 102)]
    forwarded = []
    p._forward_av = forwarded.append
    p.replay_gop()
    assert [_seq_of(r) for r in forwarded] == [100, 101, 102, 103]


def test_the_gop_order_survives_a_sequence_wrap():
    p = RTPVideoProtocol()
    p._gop = [_pkt(seq) for seq in (65535, 1, 65534, 0)]
    assert [_seq_of(r) for r in p.gop_in_sequence_order()] == [65534, 65535, 0, 1]


def test_an_empty_gop_replays_nothing():
    p = RTPVideoProtocol()
    p._gop = None
    assert p.gop_in_sequence_order() == []


def _media_rig(monkeypatch):
    vp, ap = RTPVideoProtocol(), mh.RTPAudioProtocol()
    vp._nal_queue = asyncio.Queue()
    monkeypatch.setattr(mh, "ws_send_bytes", lambda *_a, **_k: None, raising=False)
    for k, v in dict(video_proto=vp, audio_proto=ap, _stun_task=None, _audio_task=None,
                     _tx_task=None).items():
        monkeypatch.setattr(mh, k, v)
    monkeypatch.setattr(mh.frame_grabber, "start", lambda vp: None)
    monkeypatch.setattr(mh.frame_grabber, "stop", lambda vp: None)
    return vp, ap


_SPS, _PPS, _IDR = bytes([0x67, 1]), bytes([0x68, 1]), bytes([0x65, 1])


def _feed_keyframe(vp):
    for seq, nal in enumerate((_SPS, _PPS, _IDR), 1):
        vp.datagram_received(struct.pack("!BBHII", 0x80, 96, seq, 0, 7) + nal, ("192.0.2.1", 4002))


def _stop_tasks():
    for name in ("_stun_task", "_audio_task", "_tx_task"):
        task = getattr(mh, name)
        if task:
            task.cancel()


def test_an_audio_only_call_after_a_video_call_has_no_cached_keyframe(monkeypatch):
    """The transports live as long as the hub: a keyframe group left from the
    last call was replayed to the next call's viewer."""
    async def s():
        vp, _ = _media_rig(monkeypatch)
        await mh.setup_media({"conn": "192.0.2.1", "audio": {"port": 4000}, "video": {"port": 4002}})
        _feed_keyframe(vp)
        assert vp._gop and vp._gop_msgs and vp.pkt_count
        await mh.stop_media()
        await mh.setup_media({"conn": "192.0.2.1", "audio": {"port": 4000}, "video": {}})
        assert vp._gop is None and vp._gop_ts is None and vp._gop_msgs == []
        assert vp.gop_in_sequence_order() == [] and vp.pkt_count == 0
        _stop_tasks()
    asyncio.run(s())


def test_a_reinvite_that_drops_the_video_forgets_its_keyframe(monkeypatch):
    async def s():
        vp, _ = _media_rig(monkeypatch)
        await mh.setup_media({"conn": "192.0.2.1", "audio": {"port": 4000}, "video": {"port": 4002}})
        _feed_keyframe(vp)
        await mh.setup_media({"conn": "192.0.2.1", "audio": {"port": 4000}, "video": {"port": 0}})
        assert vp._gop is None and vp._gop_msgs == [] and vp.pkt_count == 0
        _stop_tasks()
    asyncio.run(s())


def test_a_duplicate_waiting_in_the_reorder_buffer_is_cached_once():
    p, _, _ = _video_rx()
    p._gop, p._gop_ts = [], 0
    p.datagram_received(_pkt(20, nal=0x65), p.remote_addr)
    p.datagram_received(_pkt(22), p.remote_addr)      # 21 late: 22 waits
    p.datagram_received(_pkt(22), p.remote_addr)      # a copy of 22, still waiting
    assert [_seq_of(r) for r in p._gop].count(22) == 1


def test_a_packet_200_ms_late_on_the_cloud_is_waited_for_not_lost(monkeypatch):
    """40515 over the cloud: a packet given up after 80 ms came later, and the P
    frames were dropped until the next IDR (5 s once). A gap waits as long as ffmpeg."""
    now = [100.0]
    monkeypatch.setattr(mh.time, "monotonic", lambda: now[0])
    p, got, _ = _video_rx()
    p.datagram_received(_pkt(20), p.remote_addr)
    for seq in range(22, 30):                       # 21 is late
        now[0] += 0.025
        p.datagram_received(_pkt(seq), p.remote_addr)
    p.datagram_received(_pkt(21), p.remote_addr)    # 200 ms after the gap opened
    assert not p._drop_until_idr
    assert got == list(range(20, 30))


def test_while_waiting_for_an_idr_the_keyframe_is_asked_again_every_second(monkeypatch):
    """A loss within a second of the last request was never asked for again (2 s
    frozen in the field). Every P dropped while waiting repeats it once the second is
    up; the IDR ends it."""
    p, out = _mk_proto()
    p._sps_pps_sent = True
    p._keyframe_at = 0.0
    asked: list[int] = []
    monkeypatch.setattr(mh, "request_keyframe", lambda: asked.append(1))
    mono = [1000.0]
    monkeypatch.setattr(mh.time, "monotonic", lambda: mono[0])
    pf = bytes([0x41, 0xAA])
    p._lost("seq 1 persa")
    mono[0] += 0.5
    p._emit_nal(pf)
    assert asked == [1] and out == []
    mono[0] += 0.5
    p._emit_nal(pf)
    assert asked == [1, 1] and out == []
    p._emit_nal(bytes([0x65, 1]))                   # the IDR
    mono[0] += 5
    p._emit_nal(pf)
    assert asked == [1, 1] and [n[0] & 0x1F for n in out] == [5, 1]


def test_after_setup_media_no_p_frame_goes_out_before_an_idr_of_this_stream(monkeypatch):
    """A PPS in band with the SPS cached from the call before let the P frames through
    when the call's first IDR was lost: P frames on a reference the card never had,
    smeared until the next IDR. Now only SPS/PPS until an IDR of this stream."""
    async def s():
        vp, _ = _media_rig(monkeypatch)
        vp._ps_by_panel[""] = (_SPS, _PPS)          # from the call before
        await mh.setup_media({"conn": "192.0.2.1", "audio": {"port": 4000}, "video": {"port": 4002}})
        pf = bytes([0x41, 1])
        for seq, nal in enumerate((_PPS, pf, pf, _IDR, pf), 1):
            vp.datagram_received(struct.pack("!BBHII", 0x80, 96, seq, 0, 7) + nal, ("192.0.2.1", 4002))
        sent = [vp._nal_queue.get_nowait()[5:] for _ in range(vp._nal_queue.qsize())]
        assert sent == [_SPS, _PPS, _SPS, _PPS, _IDR, pf]
        _stop_tasks()
    asyncio.run(s())


def test_a_packet_350_ms_late_is_given_up_on(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(mh.time, "monotonic", lambda: now[0])
    p, got, _ = _video_rx()
    p.datagram_received(_pkt(20), p.remote_addr)
    for seq in range(22, 36):                       # 21 is late
        now[0] += 0.025
        p.datagram_received(_pkt(seq), p.remote_addr)
    p.datagram_received(_pkt(21), p.remote_addr)    # 350 ms after the gap opened
    assert p._drop_until_idr and got == [20]        # lost: P frames dropped until an IDR


def test_losses_in_a_row_hold_a_packet_reorder_wait_once_not_once_each(monkeypatch):
    """The wait restarted at each gap: ten real losses 60 ms apart held packets 1.7 s.
    The clock is the oldest packet waiting, as in ffmpeg's -max_delay."""
    now = [100.0]
    monkeypatch.setattr(mh.time, "monotonic", lambda: now[0])
    p, got, _ = _video_rx()
    emitted_at = {}
    p.frame_sink = lambda nal: emitted_at.setdefault(int.from_bytes(nal[1:3], "big"), now[0])
    arrived = {}
    lost = set(range(30, 130, 10))
    for seq in range(20, 400):
        now[0] += 0.006                             # ~170 packets/s, 1.9 Mbit/s
        if seq not in lost:
            arrived[seq] = now[0]
            p.datagram_received(_pkt(seq, nal=0x65), p.remote_addr)
    held = max(emitted_at[s] - arrived[s] for s in emitted_at)
    assert held <= p.REORDER_WAIT + 0.007, held
    assert len(emitted_at) == len(arrived)
