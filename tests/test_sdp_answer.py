"""An answer must use the media profile the offer proposed.

Plants differ: a 2F entrance panel refuses SRTP outright, while the 2FV2 cloud
path offers RTP/SAVP with a=crypto. Answering with a different profile claims
to accept something we will not use — here it meant receiving encrypted while
transmitting in the clear.
"""
import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")

SRTP_OFFER = (
    "v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\nc=IN IP4 10.0.0.1\r\nt=0 0\r\n"
    "m=audio 55730 RTP/SAVP 0 8 101\r\n"
    "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:vhqadOgRjI2PcBRJkrIfnU3EvNU4TdJ5v76/mFo2\r\n"
    "m=video 57100 RTP/SAVP 96\r\na=rtpmap:96 H264/90000\r\n"
    "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:ALAyk5Pmr5m1/T4eg0yAf5pGYuSS6Ra0lbDLitGA\r\n"
)

PLAIN_OFFER = (
    "v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\nc=IN IP4 10.0.0.1\r\nt=0 0\r\n"
    "m=audio 55730 RTP/AVP 0 8 101\r\n"
    "m=video 57100 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
)


def test_an_srtp_offer_is_answered_with_srtp(monkeypatch):
    monkeypatch.setattr(sip.R, "MEDIA_ENC", False)  # our own preference must not win
    offer = sip.parse_sdp(SRTP_OFFER)
    assert offer["audio"]["crypto_key"]
    answer = sip.build_sdp(enc=True)
    assert "RTP/SAVP" in answer
    assert "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:" in answer


def test_a_plaintext_offer_is_answered_in_the_clear(monkeypatch):
    monkeypatch.setattr(sip.R, "MEDIA_ENC", True)  # nor must it win the other way
    offer = sip.parse_sdp(PLAIN_OFFER)
    assert not offer["audio"].get("crypto_key")
    answer = sip.build_sdp(enc=False)
    assert "RTP/AVP" in answer and "RTP/SAVP" not in answer
    assert "a=crypto" not in answer


def test_our_own_offers_still_follow_the_plant_setting(monkeypatch):
    monkeypatch.setattr(sip.R, "MEDIA_ENC", True)
    assert "RTP/SAVP" in sip.build_sdp()
    monkeypatch.setattr(sip.R, "MEDIA_ENC", False)
    assert "RTP/AVP" in sip.build_sdp()


def test_encryption_keys_are_fresh_for_each_answer(monkeypatch):
    monkeypatch.setattr(sip.R, "MEDIA_ENC", False)
    first = sip.build_sdp(enc=True)
    second = sip.build_sdp(enc=True)
    assert first != second, "a reused key would break the security it claims"


def test_plaintext_clears_the_keys_so_no_srtp_context_is_built(monkeypatch):
    monkeypatch.setattr(sip.R, "MEDIA_ENC", False)
    sip.build_sdp(enc=True)
    assert sip._local_crypto_key
    sip.build_sdp(enc=False)
    assert sip._local_crypto_key is None
    assert sip._local_video_crypto_key is None
