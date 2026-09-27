"""Depacketizer H.264 (RTP) e codec μ-law — funzioni pure di media_handler.

Fixture RTP sintetiche: verifichiamo che single-NAL, STAP-A e FU-A vengano
riassemblati nei NAL corretti e inviati nell'ordine SPS→PPS→IDR. Nessuna
dipendenza da Home Assistant: usiamo direttamente RTPVideoProtocol e
intercettiamo i NAL prodotti tramite _queue_nal.
"""
from __future__ import annotations

import struct

from custom_components.vimar_intercom import media_handler as mh
from custom_components.vimar_intercom.media_handler import (
    RTPVideoProtocol, ulaw_decode, ulaw_encode,
)

START_CODE = b"\x00\x00\x00\x01"


def _mk_proto():
    """RTPVideoProtocol che raccoglie i NAL emessi (bypassa asyncio queue/WS)."""
    p = RTPVideoProtocol.__new__(RTPVideoProtocol)
    # Stato minimo necessario a _depacketize/_emit_nal.
    p._fua_buf = bytearray()
    p._fua_started = False
    p._fua_expected_seq = None
    p._last_sps = None
    p._last_pps = None
    p._sps_pps_sent = False
    p._pending_idr = None
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
    for orig, rt in zip(samples, out):
        # Tolleranza μ-law: errore relativo entro ~la banda del segmento.
        assert abs(orig - rt) <= max(256, abs(orig) * 0.10)


def test_ulaw_decode_table_len():
    from custom_components.vimar_intercom import g711
    assert len(g711._ULAW_DECODE) == 256
