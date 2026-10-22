"""Voice in sequence order (#53): the cloud relay loses and reorders packets."""
from __future__ import annotations

import array
import struct

from custom_components.vimar_intercom import media_handler as mh


def _pkt(seq: int, level: int, ssrc: int = 7) -> bytes:
    # PCMU: 0xFF is silence; a different byte per packet tells them apart once decoded.
    return struct.pack("!BBHII", 0x80, 0, seq, seq * 160, ssrc) + bytes([level]) * 160


def _proto():
    p = mh.RTPAudioProtocol()
    p.remote_addr = ("192.0.2.1", 4000)
    return p


def _played(p):
    out = []
    while not p.audio_buffer.empty():
        out.append(p.audio_buffer.get_nowait())
    return out


def test_reordered_packets_come_out_in_order():
    p = _proto()
    for seq in (10, 12, 11, 13):
        p.datagram_received(_pkt(seq, 0x10 + seq), ("192.0.2.1", 4000))
    assert _played(p) == [mh.ulaw_decode(bytes([0x10 + s]) * 160) for s in (10, 11, 12, 13)]


def test_duplicates_and_late_packets_are_dropped():
    p = _proto()
    for seq in (1, 2, 2, 3, 1):
        p.datagram_received(_pkt(seq, 0x20 + seq), ("192.0.2.1", 4000))
    assert len(_played(p)) == 3


def test_a_lost_packet_is_concealed_at_half_volume():
    p = _proto()
    p.datagram_received(_pkt(1, 0x20), ("192.0.2.1", 4000))
    for seq in (3, 4, 5, 6):  # 2 never comes: after 3 held packets it is given up
        p.datagram_received(_pkt(seq, 0x20), ("192.0.2.1", 4000))
    out = _played(p)
    assert len(out) == 6  # 1, the concealed 2, 3..6
    first = array.array("h", out[0])
    assert array.array("h", out[1]).tolist() == [x // 2 for x in first]


def test_a_long_gap_is_skipped_not_filled():
    p = _proto()
    p.datagram_received(_pkt(1, 0x30), ("192.0.2.1", 4000))
    for seq in range(100, 104):
        p.datagram_received(_pkt(seq, 0x30), ("192.0.2.1", 4000))
    assert len(_played(p)) == 1 + mh.RTPAudioProtocol.CONCEAL_MAX + 4


def test_a_new_ssrc_starts_over():
    p = _proto()
    p.datagram_received(_pkt(500, 0x40, ssrc=1), ("192.0.2.1", 4000))
    p.datagram_received(_pkt(3, 0x40, ssrc=2), ("192.0.2.1", 4000))  # would be "late"
    assert len(_played(p)) == 2


def test_rtp_padding_is_not_voice():
    p = _proto()
    pkt = bytearray(_pkt(1, 0x50) + b"\x00\x00\x03")
    pkt[0] |= 0x20
    p.datagram_received(bytes(pkt), ("192.0.2.1", 4000))
    assert len(_played(p)[0]) == 320


def test_a_packet_of_padding_only_is_ignored():
    p = _proto()
    pkt = bytearray(struct.pack("!BBHII", 0x80, 0, 1, 160, 7) + b"\x00\x02")
    pkt[0] |= 0x20
    p.datagram_received(bytes(pkt), ("192.0.2.1", 4000))
    assert _played(p) == [] and p.pkt_count == 0


def test_a_loss_before_any_voice_is_filled_with_silence():
    p = _proto()
    p._a_ssrc, p._a_next = 7, 1  # packet 1 expected, never arrives
    for seq in (2, 3, 4, 5):
        p.datagram_received(_pkt(seq, 0x60), ("192.0.2.1", 4000))
    assert _played(p)[0] == bytes(320)


def test_a_sequence_restart_with_the_same_ssrc_is_followed():
    """A B2BUA or relay can restart the numbers and keep the SSRC: without a
    resync every packet looked already played, silence for minutes (#54 review)."""
    p = _proto()
    for seq in range(5000, 5010):
        p.datagram_received(_pkt(seq, 0x70), ("192.0.2.1", 4000))
    _played(p)
    for seq in range(100, 120):
        p.datagram_received(_pkt(seq, 0x71), ("192.0.2.1", 4000))
    out = _played(p)
    late = mh.RTPAudioProtocol.RESYNC_LATE - 1
    assert len(out) == 20 - late  # the first few are dropped, then it follows


def test_a_few_late_packets_do_not_resync():
    p = _proto()
    for seq in range(1, 11):
        p.datagram_received(_pkt(seq, 0x72), ("192.0.2.1", 4000))
    _played(p)
    for seq in (3, 4, 11, 12):  # two stragglers, then the stream goes on
        p.datagram_received(_pkt(seq, 0x72), ("192.0.2.1", 4000))
    assert len(_played(p)) == 2


def test_the_queue_holds_a_second_of_voice():
    """A 200 ms queue dropped voice whenever the loop stalled for longer (#54)."""
    assert mh.RTPAudioProtocol().audio_buffer.maxsize == 50


def test_a_duplicate_of_a_waiting_packet_is_dropped():
    p = _proto()
    for seq in (1, 3, 3, 2):  # 3 waits for 2, and comes twice
        p.datagram_received(_pkt(seq, 0x74), ("192.0.2.1", 4000))
    assert len(_played(p)) == 3
