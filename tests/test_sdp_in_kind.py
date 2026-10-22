"""An SDP answer mirrors the offer's media profile.

A 2F panel offers plain RTP and ignores SRTP; a 2FV2 relay offers RTP/SAVP
with a=crypto. Answering with the other profile claims to accept something we
will not use, so the plant setting only decides when we make the offer.
"""
import importlib

import pytest

from custom_components.vimar_intercom import sip_client as sip

SRTP_OFFER = (
    "v=0\r\nc=IN IP4 192.168.1.20\r\n"
    "m=audio 7078 RTP/SAVP 0\r\n"
    "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz\r\n"
    "m=video 9078 RTP/SAVP 96\r\na=rtpmap:96 H264/90000\r\n"
    "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz\r\n"
)
PLAIN_OFFER = (
    "v=0\r\nc=IN IP4 192.168.1.20\r\n"
    "m=audio 7078 RTP/AVP 0\r\n"
    "m=video 9078 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
)


@pytest.mark.parametrize("plant_setting", [False, True])
def test_an_srtp_offer_gets_an_srtp_answer(monkeypatch, plant_setting):
    monkeypatch.setattr(sip.S, "MEDIA_ENC", plant_setting)
    answer = sip.build_sdp(sip.parse_sdp(SRTP_OFFER))
    assert "m=audio" in answer and "RTP/SAVP" in answer
    assert answer.count("a=crypto:") == 2


@pytest.mark.parametrize("plant_setting", [False, True])
def test_a_plain_offer_gets_a_plain_answer(monkeypatch, plant_setting):
    monkeypatch.setattr(sip.S, "MEDIA_ENC", plant_setting)
    answer = sip.build_sdp(sip.parse_sdp(PLAIN_OFFER))
    assert "RTP/SAVP" not in answer and "a=crypto:" not in answer


@pytest.mark.parametrize("plant_setting", [False, True])
def test_our_own_offer_follows_the_plant_setting(monkeypatch, plant_setting):
    monkeypatch.setattr(sip.S, "MEDIA_ENC", plant_setting)
    answer = sip.build_sdp()
    assert ("RTP/SAVP" in answer) is plant_setting


KEY_A = "WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz"
KEY_B = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIz"
MIXED_OFFER = (
    "v=0\r\nc=IN IP4 192.168.1.20\r\n"
    "m=audio 7078 RTP/SAVP 0\r\n"
    f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{KEY_A}\r\n"
    "m=video 9078 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
)


def _mline(answer, kind):
    """The m= line and its attributes, up to the next m= line."""
    block = answer[answer.index(f"m={kind} "):]
    nxt = block.find("\r\nm=", 1)
    return block if nxt < 0 else block[:nxt + 2]


@pytest.mark.parametrize("plant_setting", [False, True])
def test_each_line_mirrors_its_own_profile(monkeypatch, plant_setting):
    monkeypatch.setattr(sip.S, "MEDIA_ENC", plant_setting)
    answer = sip.build_sdp(sip.parse_sdp(MIXED_OFFER))
    audio, video = _mline(answer, "audio"), _mline(answer, "video")
    assert "RTP/SAVP" in audio and "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:" in audio
    assert "RTP/AVP" in video and "a=crypto" not in video
    assert sip.SDP._local_crypto_key is not None
    assert sip.SDP._local_video_crypto_key is None


def test_the_answer_echoes_the_chosen_tag_and_suite():
    offer = (
        "v=0\r\nc=IN IP4 192.168.1.20\r\n"
        "m=audio 7078 RTP/SAVP 0\r\n"
        f"a=crypto:3 F8_128_HMAC_SHA1_80 inline:{KEY_B}\r\n"
        f"a=crypto:5 AES_CM_128_HMAC_SHA1_32 inline:{KEY_A}\r\n"
        f"a=crypto:6 AES_CM_128_HMAC_SHA1_80 inline:{KEY_B}\r\n"
        "m=video 9078 RTP/SAVP 96\r\na=rtpmap:96 H264/90000\r\n"
        f"a=crypto:2 AES_CM_128_HMAC_SHA1_80 inline:{KEY_B}|2^20|1:4\r\n"
    )
    parsed = sip.parse_sdp(offer)
    # The first supported suite wins, and its own key is the one used for RX.
    assert parsed["audio"]["crypto_suite"] == "AES_CM_128_HMAC_SHA1_32"
    assert parsed["audio"]["crypto_tag"] == "5"
    assert parsed["audio"]["crypto_key"] == KEY_A
    assert parsed["video"]["crypto_key"] == KEY_B
    assert len(parsed["audio"]["crypto"]) == 3

    answer = sip.build_sdp(parsed)
    audio, video = _mline(answer, "audio"), _mline(answer, "video")
    assert "a=crypto:5 AES_CM_128_HMAC_SHA1_32 inline:" in audio
    assert audio.count("a=crypto:") == 1
    assert "a=crypto:2 AES_CM_128_HMAC_SHA1_80 inline:" in video
    # Our own key, never the offer's.
    assert KEY_A not in answer and KEY_B not in answer


def test_a_crypto_line_on_a_plain_rtp_line_is_ignored():
    offer = ("v=0\r\nc=IN IP4 192.168.1.20\r\nm=audio 7078 RTP/AVP 0\r\n"
             f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{KEY_A}\r\n")
    parsed = sip.parse_sdp(offer)
    assert "crypto_key" not in parsed["audio"]
    assert "a=crypto" not in _mline(sip.build_sdp(parsed), "audio")


def test_an_srtp_line_without_a_supported_suite_is_refused():
    offer = ("v=0\r\nc=IN IP4 192.168.1.20\r\n"
             "m=audio 7078 RTP/SAVP 0\r\n"
             f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{KEY_A}\r\n"
             "m=video 9078 RTP/SAVP 96\r\na=rtpmap:96 H264/90000\r\n"
             f"a=crypto:1 AEAD_AES_256_GCM inline:{KEY_B}\r\n")
    answer = sip.build_sdp(sip.parse_sdp(offer))
    assert _mline(answer, "video").startswith("m=video 0 ")
    assert "a=crypto" not in _mline(answer, "video")
    assert "a=crypto:1 AES_CM_128_HMAC_SHA1_80" in _mline(answer, "audio")


def test_the_32_bit_suite_protects_and_unprotects():
    import base64
    import os
    import struct

    from custom_components.vimar_intercom.srtp import SRTPContext

    key = base64.b64encode(os.urandom(30)).decode()
    tx = SRTPContext(key, "AES_CM_128_HMAC_SHA1_32")
    rx = SRTPContext(key, "AES_CM_128_HMAC_SHA1_32")
    rtp = struct.pack("!BBHII", 0x80, 0, 1, 160, 0x1234) + b"\x55" * 160
    srtp = tx.protect(rtp)
    assert len(srtp) == len(rtp) + 4
    assert rx.unprotect(srtp) == rtp
    assert SRTPContext(key).unprotect(srtp) is None


def test_a_parsed_offer_keeps_no_master_key_in_the_crypto_list():
    """The parsed SDP is logged at INFO: only the chosen key survives, under
    crypto_key, and the log sites drop it."""
    offer = sip.parse_sdp(MIXED_OFFER)
    assert offer["audio"]["crypto"] == [{"tag": "1", "suite": "AES_CM_128_HMAC_SHA1_80"}]
    assert offer["audio"]["crypto_key"] == KEY_A
    assert KEY_A not in str(sip._loggable(offer["audio"]))


def test_a_parsed_offer_logged_through_redact_shows_no_key():
    lr = importlib.import_module("custom_components.vimar_intercom.log_redact")
    offer = (
        "v=0\r\nc=IN IP4 192.168.1.20\r\n"
        "m=audio 7078 RTP/SAVP 0\r\n"
        f"a=crypto:3 F8_128_HMAC_SHA1_80 inline:{KEY_B}\r\n"
        f"a=crypto:5 AES_CM_128_HMAC_SHA1_32 inline:{KEY_A}\r\n"
    )
    out = lr.redact(f"SDP: audio={sip.parse_sdp(offer)}")
    assert KEY_A not in out and KEY_B not in out
    assert "AES_CM_128_HMAC_SHA1_32" in out, "the rest stays readable"


def _reinvite(body):
    return ("INVITE sip:60999@x SIP/2.0\r\nVia: SIP/2.0/TLS 192.0.2.4;branch=z9hG4bKre\r\n"
            "From: <sip:55001@x>;tag=abc\r\nTo: <sip:60999@x>;tag=ours\r\n"
            "Call-ID: call-1\r\nCSeq: 2 INVITE\r\nContent-Type: application/sdp\r\n"
            f"Content-Length: {len(body)}\r\n\r\n{body}")


def test_a_rebuilt_answer_in_a_dialog_keeps_the_running_srtp_key(monkeypatch):
    """Our offer had audio and video; the panel's re-INVITE repeats its
    audio-only answer, so our old SDP no longer fits and a new answer is built.
    The media keeps running with the old key: the new answer must carry it."""
    import asyncio

    from custom_components.vimar_intercom import media_handler as mh

    monkeypatch.setattr(sip.S, "MEDIA_ENC", True)
    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", False)
    ours = sip.build_sdp()                       # our offer, with SRTP keys
    running_key = sip.SDP._local_crypto_key
    panel = ("v=0\r\nc=IN IP4 192.0.2.20\r\n"
             f"m=audio 7078 RTP/SAVP 0\r\na=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{KEY_A}\r\n")
    sent, setups = [], []

    async def _send(msg):
        sent.append(msg)

    async def _setup(remote, akey=None, vkey=None, **k):
        setups.append(akey)

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(mh, "setup_media", _setup)
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setitem(sip.call_state, "call_id", "call-1")
    monkeypatch.setitem(sip.call_state, "local_sdp", ours)
    monkeypatch.setitem(sip.call_state, "remote_sdp", sip.parse_sdp(panel))

    asyncio.run(sip.handle_incoming_invite(_reinvite(panel)))

    answer = sent[0].split("\r\n\r\n", 1)[1]
    assert answer.count("m=") == 1, "the new answer mirrors the audio-only offer"
    assert f"inline:{running_key}" in answer
    assert all(key == running_key for key in setups)


def test_an_srtp_line_we_refuse_gets_no_media(monkeypatch):
    """Answered with port 0, the line must not be set up either: we would
    send plain RTP into an encrypted session."""
    import asyncio

    from custom_components.vimar_intercom import media_handler as mh
    offer = sip.parse_sdp(
        "v=0\r\nc=IN IP4 192.0.2.20\r\n"
        f"m=audio 7078 RTP/SAVP 0\r\na=crypto:1 F8_128_HMAC_SHA1_80 inline:{KEY_A}\r\n"
        "m=video 9078 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n")
    assert offer["audio"]["refused"] is True and "refused" not in offer["video"]
    assert _mline(sip.build_sdp(offer), "audio").startswith("m=audio 0 ")

    ap, vp = mh.RTPAudioProtocol(), mh.RTPVideoProtocol()
    for k, v in dict(audio_proto=ap, video_proto=vp, _stun_task=None, _audio_task=None,
                     _tx_task=None).items():
        monkeypatch.setattr(mh, k, v)
    monkeypatch.setattr(mh.frame_grabber, "start", lambda vp: None)
    monkeypatch.setattr(mh.frame_grabber, "stop", lambda vp: None)

    async def go():
        await mh.setup_media(offer)
        for name in ("_stun_task", "_audio_task", "_tx_task"):
            getattr(mh, name).cancel()
    asyncio.run(go())
    assert ap.remote_addr is None, "no media on the refused line"
    assert vp.remote_addr == ("192.0.2.20", 9078)


UNKNOWN_OFFER = (
    "v=0\r\nc=IN IP4 192.0.2.20\r\n"
    "m=audio 7078 RTP/AVP 0\r\n"
    "m=text 11000 RTP/AVP 98\r\nc=IN IP4 192.0.2.99\r\na=rtpmap:98 t140/1000\r\na=sendonly\r\n"
    "m=video 9078 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
)


def test_an_unknown_line_does_not_leak_into_the_line_before():
    offer = sip.parse_sdp(UNKNOWN_OFFER)
    assert offer["audio"]["ip"] == "192.0.2.20"
    assert "dir" not in offer["audio"] or offer["audio"]["dir"] != "sendonly"
    assert all("t140" not in r for r in offer["audio"].get("rtpmap", []))
    assert offer["order"] == ["audio", "m1", "video"]


def test_an_unknown_line_is_echoed_with_port_0_in_its_place():
    answer = sip.build_sdp(sip.parse_sdp(UNKNOWN_OFFER))
    mlines = [line for line in answer.split("\r\n") if line.startswith("m=")]
    assert [m.split()[0] for m in mlines] == ["m=audio", "m=text", "m=video"]
    assert mlines[1] == "m=text 0 RTP/AVP 98"
    assert sip._answer_fits(answer, sip.parse_sdp(UNKNOWN_OFFER))


def test_a_second_audio_line_does_not_replace_the_first():
    offer = sip.parse_sdp("v=0\r\nc=IN IP4 192.0.2.20\r\n"
                          "m=audio 7078 RTP/AVP 0\r\nm=audio 7080 RTP/AVP 8\r\n")
    assert offer["audio"]["port"] == 7078
    answer = sip.build_sdp(offer)
    assert [m for m in answer.split("\r\n") if m.startswith("m=")][1] == "m=audio 0 RTP/AVP 8"
