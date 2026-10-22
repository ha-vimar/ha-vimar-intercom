"""H.264: la risposta usa i parametri offerti dalla targa (RFC 6184).

Prova sul campo del 28/09 (Tab 7S 40507, 2F, targa baresip): l'INVITE dello
squillo offre `H264/90000` con `packetization-mode=0;profile-level-id=42800c`.
Rispondevamo sempre `packetization-mode=1`: nessun codec video in comune, e la
targa non mandava un solo pacchetto (audio 3602 pacchetti, video 0 in 54 s).
Alle nostre offerte con il solo mode 1 la stessa targa risponde `m=video 0`.
"""
from __future__ import annotations

from custom_components.vimar_intercom import sip_client as sip

OFFERTA_2F = (
    "v=0\r\no=- 1 1 IN IP4 192.168.1.20\r\ns=-\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
    "m=audio 15336 RTP/AVP 96 0 8 101\r\na=rtpmap:96 opus/48000/2\r\na=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\na=rtpmap:101 telephone-event/8000\r\n"
    "m=video 10876 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
    "a=fmtp:96 packetization-mode=0;profile-level-id=42800c\r\n"
)


def _video(sdp: str) -> list[str]:
    righe = sdp.split("\r\n")
    i = next(n for n, r in enumerate(righe) if r.startswith("m=video"))
    return righe[i:]


def test_risposta_alla_targa_2f_usa_packetization_mode_0():
    sdp = sip.build_sdp(sip.parse_sdp(OFFERTA_2F))
    video = _video(sdp)
    assert video[0].split()[3:] == ["96"]
    assert "a=fmtp:96 packetization-mode=0;profile-level-id=42800c" in video
    assert not any("packetization-mode=1" in r for r in video)


def test_risposta_rispetta_il_payload_type_offerto():
    offerta = OFFERTA_2F.replace("RTP/AVP 96\r\na=rtpmap:96 H264", "RTP/AVP 102\r\na=rtpmap:102 H264").replace(
        "a=fmtp:96 packetization", "a=fmtp:102 packetization")
    video = _video(sip.build_sdp(sip.parse_sdp(offerta)))
    assert video[0].split()[3:] == ["102"]
    assert "a=rtpmap:102 H264/90000" in video


def test_la_nostra_offerta_propone_entrambi_i_modi():
    video = _video(sip.build_sdp())
    assert video[0].split()[3:] == ["96", "97"]
    assert "a=fmtp:96 profile-level-id=42801F;packetization-mode=1" in video
    assert "a=fmtp:97 profile-level-id=42800c;packetization-mode=0" in video


def test_offerta_senza_h264_ripiega_sulla_nostra_offerta():
    # An offered video line with no H.264 in it: our own H.264 list.
    senza_h264 = OFFERTA_2F.replace("a=rtpmap:96 H264/90000", "a=rtpmap:96 VP8/90000")
    video = _video(sip.build_sdp(sip.parse_sdp(senza_h264)))
    assert video[0].split()[3:] == ["96", "97"]
