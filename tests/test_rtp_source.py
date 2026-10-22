"""Plain RTP is accepted only from the other end of the call."""
import struct

import pytest

from custom_components.vimar_intercom import media_handler as media


def pcmu(seq=1):
    return struct.pack("!BBHII", 0x80, 0, seq, 160 * seq, 0x5555) + b"\xff" * 160


def test_plain_audio_from_the_call_is_taken_and_from_elsewhere_dropped():
    proto = media.RTPAudioProtocol()
    proto.remote_addr = ("198.51.100.7", 40000)
    proto.datagram_received(pcmu(1), ("198.51.100.7", 40002))   # relay, other port
    proto.datagram_received(pcmu(2), ("192.0.2.66", 40000))     # someone else
    assert proto.pkt_count == 1


def test_no_call_no_rtp():
    proto = media.RTPAudioProtocol()
    proto.datagram_received(pcmu(1), ("198.51.100.7", 40000))
    assert proto.pkt_count == 0


def h264(seq=1):
    return struct.pack("!BBHII", 0x80, 96, seq, 3000 * seq, 0x7777) + b"\x41\x9a\x00\x10"


def test_plain_video_from_the_call_is_taken_and_from_elsewhere_dropped():
    proto = media.RTPVideoProtocol()
    proto.remote_addr = ("198.51.100.7", 40002)
    proto.frame_sink = lambda nal: None
    proto.datagram_received(h264(1), ("198.51.100.7", 40010))   # relay, other port
    proto.datagram_received(h264(2), ("192.0.2.66", 40002))     # someone else
    assert proto.pkt_count == 1


def test_no_call_no_video():
    proto = media.RTPVideoProtocol()
    proto.datagram_received(h264(1), ("198.51.100.7", 40002))
    assert proto.pkt_count == 0


def test_a_dropped_source_is_logged_once_per_call(caplog):
    """At WARNING: a silent stream from a relay on another address must be
    explainable from a default log."""
    proto = media.RTPAudioProtocol()
    proto.remote_addr = ("198.51.100.7", 40000)
    def drops():
        # Distinct messages: log_buffer, once installed by another test, sends a
        # WARNING again through the root logger as a copy of the same record.
        # The two calls are counted apart (caplog.clear() below), not told apart
        # by the record's timestamp: two records can share one tick of the clock.
        return {r.getMessage() for r in caplog.records
                if r.name == media.__name__ and "192.0.2.66" in r.getMessage()}

    with caplog.at_level("WARNING", logger=media.__name__):
        for seq in range(1, 4):
            proto.datagram_received(pcmu(seq), ("192.0.2.66", 40000))
        assert len(drops()) == 1
        caplog.clear()
        proto.remote_addr = tuple(["198.51.100.7", 40000])  # the next call (new tuple)
        proto.datagram_received(pcmu(5), ("192.0.2.66", 40000))
        assert len(drops()) == 1



@pytest.mark.parametrize("local_udp, taken", [(True, 1), (False, 0)])
def test_in_local_udp_the_intercom_address_is_trusted(monkeypatch, local_udp, taken):
    """A 2-wire gateway acting as a B2BUA can send the media from its own
    address rather than the one in the SDP (review of #31)."""
    monkeypatch.setattr(media.R, "USE_LOCAL_UDP", local_udp)
    monkeypatch.setattr(media.R, "LOCAL_PROXY", "192.0.2.10")
    proto = media.RTPAudioProtocol()
    proto.remote_addr = ("192.0.2.20", 40000)
    proto.datagram_received(pcmu(1), ("192.0.2.10", 40000))   # the intercom itself
    proto.datagram_received(pcmu(2), ("192.0.2.66", 40000))   # someone else
    assert proto.pkt_count == taken


def test_the_intercom_address_is_not_trusted_without_a_call(monkeypatch):
    monkeypatch.setattr(media.R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(media.R, "LOCAL_PROXY", "192.0.2.10")
    proto = media.RTPAudioProtocol()
    proto.datagram_received(pcmu(1), ("192.0.2.10", 40000))
    assert proto.pkt_count == 0
