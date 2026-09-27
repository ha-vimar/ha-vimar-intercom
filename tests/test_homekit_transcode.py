"""Il gruppo in uscita dal ricodificatore: da dove si parte con una vista nuova.

Una vista che si apre riceve per primo l'ultimo gruppo ricodificato, dall'SPS
in poi. Va riconosciuto bene: se il gruppo non contiene ancora un keyframe
intero, il telefono non ha di che decodificare.
"""
import struct

import pytest

tc = pytest.importorskip("custom_components.vimar_intercom.homekit_transcode")


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
        assert not g.has_keyframe, "manca ancora la fine dell'IDR"
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

    def test_a_tail_without_its_start_is_not_a_keyframe(self):
        """Solo la fine dell'IDR (l'inizio l'ha perso qualcuno): non vale."""
        g = tc.EncodedGop()
        g.add(STAP_SPS_PPS)
        g.add(IDR_END)
        assert not g.has_keyframe
