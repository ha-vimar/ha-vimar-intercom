"""Test di build_sdp/parse_sdp: RTP in chiaro (default) vs SRTP (media_enc=True).

Verifica sul campo (20/08/2026): la targa baresip di questo impianto NON accetta
SRTP, quindi build_sdp deve offrire RTP/AVP senza a=crypto quando MEDIA_ENC=False.
"""
import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")
from custom_components.vimar_intercom import plant_state as S  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_media_enc():
    prev = getattr(S, "MEDIA_ENC", False)
    yield
    S.MEDIA_ENC = prev


def test_build_sdp_plain_rtp_default():
    """MEDIA_ENC=False → RTP/AVP, nessuna riga a=crypto, chiavi locali None."""
    S.MEDIA_ENC = False
    sdp = sip.build_sdp()
    assert "m=audio" in sdp and "RTP/AVP" in sdp
    assert "m=video" in sdp
    assert "RTP/SAVP" not in sdp
    assert "a=crypto" not in sdp
    # audio offre PCMU/PCMA/telephone-event
    assert "m=audio" in sdp
    assert "RTP/AVP 0 8 101" in sdp
    assert "RTP/AVP 96" in sdp  # video H.264
    # nessuna chiave SRTP generata → setup_media non creerà i contesti
    assert sip.SDP._local_crypto_key is None
    assert sip.SDP._local_video_crypto_key is None


@pytest.mark.parametrize("option, video, session", [
    (None, 256, 512),          # the default: what the integration asked before 1.0.19
    ("256", 256, 512),
    ("2048", 2048, 2200),      # the 40515's sharper picture, on request
    ("bogus", 256, 512),
])
def test_build_sdp_asks_for_the_chosen_video_bandwidth(monkeypatch, option, video, session):
    """2048 kbit/s made a 40517 send ten times the packets through a cloud relay
    that loses them (smeared, frozen, laggy video): it is an option, 256 by default."""
    from custom_components.vimar_intercom import runtime as R
    S.MEDIA_ENC = False
    data = {"sip_user": "1001", "sip_domain": "example.invalid"}
    if option is not None:
        data["video_bandwidth"] = option
    R.configure(data)
    offer = sip.parse_sdp(sip.build_sdp())
    for sdp in (sip.build_sdp(), sip.build_sdp(offer)):
        assert f"b=AS:{session}\r\nt=0 0" in sdp
        assert "m=video" in sdp and f"b=AS:{video}\r\n" in sdp.split("m=video")[1]


def test_build_sdp_srtp_when_enabled():
    """MEDIA_ENC=True → RTP/SAVP con a=crypto e chiavi base64 generate."""
    S.MEDIA_ENC = True
    sdp = sip.build_sdp()
    assert "RTP/SAVP 0 8 101" in sdp
    assert "RTP/SAVP 96" in sdp
    assert "RTP/AVP" not in sdp
    assert sdp.count("a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:") == 2
    assert sip.SDP._local_crypto_key is not None
    assert sip.SDP._local_video_crypto_key is not None


def test_parse_sdp_plain_answer_no_crypto():
    """SDP di risposta della targa (RTP/AVP, senza crypto) → nessuna crypto_key."""
    answer = (
        "v=0\r\n"
        "o=- 0 0 IN IP4 192.0.2.60\r\n"
        "s=baresip\r\n"
        "c=IN IP4 192.0.2.60\r\n"
        "t=0 0\r\n"
        "m=audio 53304 RTP/AVP 0 8 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
        "a=ptime:20\r\n"
        "m=video 9300 RTP/AVP 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
    )
    r = sip.parse_sdp(answer)
    assert r["audio"]["port"] == 53304
    assert r["audio"]["ip"] == "192.0.2.60"
    assert "crypto_key" not in r["audio"]
    assert r["video"]["port"] == 9300
    assert "crypto_key" not in r["video"]


def test_parse_sdp_srtp_answer_extracts_key():
    answer = (
        "v=0\r\n"
        "c=IN IP4 192.0.2.60\r\n"
        "m=audio 53304 RTP/SAVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:AbCdEf0123456789AbCdEf0123456789AbCdEf01\r\n"
    )
    r = sip.parse_sdp(answer)
    assert r["audio"]["crypto_key"] == "AbCdEf0123456789AbCdEf0123456789AbCdEf01"
