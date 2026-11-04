"""SRTCP replay protection (RFC 3711 §3.3.2), as libsrtp does it.

Without it, anyone on the path could send one captured NACK from the phone
again and again, and every copy made the doorbell resend its packets.
"""
import base64
import struct

import pytest

from custom_components.vimar_intercom.srtp import SRTCPContext, _truncated_hmac

KEY = base64.b64encode(bytes(range(30))).decode()


def rr(n: int) -> bytes:
    return struct.pack("!BBHI", 0x80, 201, 1, 0x5555) + struct.pack("!I", n)


def sender_and_receiver():
    return SRTCPContext(KEY), SRTCPContext(KEY)


def test_each_packet_is_accepted_once():
    tx, rx = sender_and_receiver()
    pkt = tx.protect(rr(1))
    assert rx.unprotect(pkt) == rr(1)
    assert rx.unprotect(pkt) is None


def test_packets_out_of_order_inside_the_window_are_accepted():
    tx, rx = sender_and_receiver()
    pkts = [tx.protect(rr(n)) for n in range(5)]
    for i in (0, 3, 1, 4, 2):
        assert rx.unprotect(pkts[i]) == rr(i)
    assert all(rx.unprotect(p) is None for p in pkts)


def test_too_old_is_rejected():
    tx, rx = sender_and_receiver()
    old = tx.protect(rr(0))
    for n in range(1, SRTCPContext.REPLAY_WINDOW + 2):
        assert rx.unprotect(tx.protect(rr(n))) is not None
    assert rx.unprotect(old) is None, "never seen, but behind the window"


def test_a_forged_packet_does_not_move_the_window():
    tx, rx = sender_and_receiver()
    first = tx.protect(rr(0))
    forged = bytearray(tx.protect(rr(1)))
    struct.pack_into("!I", forged, len(forged) - 14, 0x80000000 | 5000)   # index, unsigned
    assert rx.unprotect(bytes(forged)) is None
    assert rx.unprotect(first) == rr(0), "the window did not jump to 5000"


def test_the_index_wraps():
    tx, rx = sender_and_receiver()
    tx.index = 0x7FFFFFFE
    a, b, c = (tx.protect(rr(n)) for n in range(3))    # 0x7FFFFFFF, 0, 1
    assert rx.unprotect(a) is not None
    assert rx.unprotect(c) is not None and rx.unprotect(b) is not None
    assert rx.unprotect(a) is None


def test_a_huge_jump_forgets_the_window_without_a_huge_integer():
    """The window was shifted by the jump itself: a jump of 2^30 indices built
    a 128 MB integer before masking it back to 128 bits."""
    import tracemalloc

    tx, rx = sender_and_receiver()
    assert rx.unprotect(tx.protect(rr(0))) is not None
    tx.index = 0x3FFFFFF0
    far = tx.protect(rr(1))
    tracemalloc.start()
    try:
        assert rx.unprotect(far) == rr(1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1 << 20, peak
    assert rx._rx_seen == 1
    assert rx.unprotect(far) is None, "and it is still a replay"


def test_a_key_shorter_than_30_bytes_is_refused():
    with pytest.raises(ValueError, match="SRTCP key too short: 20 bytes"):
        SRTCPContext(base64.b64encode(bytes(20)).decode())


@pytest.mark.parametrize("length", [0, 8, 21])
def test_a_packet_too_short_for_header_index_and_tag_is_dropped(length):
    _, rx = sender_and_receiver()
    assert rx.unprotect(bytes(length)) is None
    assert rx._rx_max is None, "a runt must not touch the replay window"


def test_an_unencrypted_packet_is_authenticated_and_returned_as_is():
    # E bit clear (RFC 3711 §3.4): the payload travels in clear, still signed.
    tx, rx = sender_and_receiver()
    body = rr(7) + struct.pack("!I", 3)
    pkt = body + _truncated_hmac(tx._hmac, body, SRTCPContext.AUTH_TAG_LEN)
    assert rx.unprotect(pkt) == rr(7)
    assert rx.unprotect(pkt) is None, "the replay check applies to it too"


def test_an_unencrypted_packet_with_a_bad_tag_is_dropped():
    tx, rx = sender_and_receiver()
    body = rr(7) + struct.pack("!I", 3)
    tag = bytearray(_truncated_hmac(tx._hmac, body, SRTCPContext.AUTH_TAG_LEN))
    tag[0] ^= 1
    assert rx.unprotect(body + bytes(tag)) is None
    assert rx._rx_max is None
