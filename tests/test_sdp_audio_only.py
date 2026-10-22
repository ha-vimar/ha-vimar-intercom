"""The SDP answer has exactly the offer's m-lines (RFC 3264 section 6).

Not every entrance has a camera. parse_sdp used to give the video section the
session's c= address even when the offer had no m=video, so the answer carried
a live m=video 9200 the panel never asked for. An offered line with port 0 was
answered live too.
"""
import asyncio

from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import sip_client as sip

AUDIO_ONLY_OFFER = (
    "v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\nc=IN IP4 10.0.0.1\r\nt=0 0\r\n"
    "m=audio 55730 RTP/AVP 0 8 101\r\na=rtpmap:0 PCMU/8000\r\n"
)
VIDEO_DECLINED_OFFER = AUDIO_ONLY_OFFER + "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
VIDEO_FIRST_OFFER = (
    "v=0\r\nc=IN IP4 10.0.0.1\r\nt=0 0\r\n"
    "m=video 57100 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
    "m=audio 55730 RTP/AVP 0\r\n"
)


def _mlines(sdp: str) -> list[str]:
    return [line for line in sdp.split("\r\n") if line.startswith("m=")]


def test_an_audio_only_offer_parses_without_a_video_section():
    offer = sip.parse_sdp(AUDIO_ONLY_OFFER)
    assert offer["audio"]["port"] == 55730
    assert offer["video"] == {}
    assert offer["order"] == ["audio"]


def test_an_audio_only_offer_gets_an_audio_only_answer():
    answer = sip.build_sdp(sip.parse_sdp(AUDIO_ONLY_OFFER))
    assert [m.split()[0] for m in _mlines(answer)] == ["m=audio"]
    assert f"m=audio {sip.C.RTP_AUDIO_PORT} " in answer
    assert sip.SDP._local_video_crypto_key is None


def test_an_audio_only_srtp_offer_adds_no_video(monkeypatch):
    monkeypatch.setattr(sip.S, "MEDIA_ENC", True)
    offer = AUDIO_ONLY_OFFER.replace("RTP/AVP", "RTP/SAVP") + (
        "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz\r\n")
    answer = sip.build_sdp(sip.parse_sdp(offer))
    assert "m=video" not in answer
    assert answer.count("a=crypto:") == 1


def test_a_declined_video_line_stays_declined():
    answer = sip.build_sdp(sip.parse_sdp(VIDEO_DECLINED_OFFER))
    assert [m.split()[:2] for m in _mlines(answer)] == [
        ["m=audio", str(sip.C.RTP_AUDIO_PORT)], ["m=video", "0"]]


def test_the_answer_keeps_the_offer_order():
    answer = sip.build_sdp(sip.parse_sdp(VIDEO_FIRST_OFFER))
    assert [m.split()[0] for m in _mlines(answer)] == ["m=video", "m=audio"]


def test_our_own_offer_keeps_both_lines():
    answer = sip.build_sdp()
    assert [m.split()[:2] for m in _mlines(answer)] == [
        ["m=audio", str(sip.C.RTP_AUDIO_PORT)], ["m=video", str(sip.C.RTP_VIDEO_PORT)]]


def test_a_reinvite_that_drops_video_gets_a_new_answer(monkeypatch):
    """The answer kept from the call had a live video line the re-offer lacks."""
    sent = []

    async def _send(msg):
        sent.append(msg)

    async def _setup(*a, **k):
        pass

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(sip.media, "setup_media", _setup)
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setitem(sip.call_state, "call_id", "c1")
    monkeypatch.setitem(sip.call_state, "local_sdp", sip.build_sdp())
    monkeypatch.setitem(sip.call_state, "remote_sdp", None)
    reinvite = ("INVITE sip:60902@x SIP/2.0\r\nVia: SIP/2.0/TLS 1.2.3.4;branch=z9hG4bKr\r\n"
                "From: <sip:55001@x>;tag=abc\r\nTo: <sip:60902@x>;tag=t\r\nCall-ID: c1\r\n"
                "CSeq: 5 INVITE\r\nContent-Type: application/sdp\r\n\r\n" + AUDIO_ONLY_OFFER)
    asyncio.run(sip.handle_incoming_invite(reinvite))
    assert len(sent) == 1 and sent[0].startswith("SIP/2.0 200 OK")
    assert "m=audio" in sent[0] and "m=video" not in sent[0]
    assert "m=video" not in sip.call_state["local_sdp"]


def test_setup_media_without_a_video_section_leaves_video_closed(monkeypatch):
    class _Proto:
        remote_addr = ("10.0.0.1", 57100)

    proto = _Proto()
    stopped = []
    monkeypatch.setattr(media, "audio_proto", None)
    monkeypatch.setattr(media, "video_proto", proto)
    monkeypatch.setattr(media.frame_grabber, "stop", lambda p: stopped.append(p))

    async def _noop():
        pass

    monkeypatch.setattr(media, "_stun_keepalive", _noop)
    monkeypatch.setattr(media, "_audio_broadcast", _noop)
    monkeypatch.setattr(media, "_tx_loop", _noop)

    async def _run():
        await media.setup_media(sip.parse_sdp(AUDIO_ONLY_OFFER))
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert proto.remote_addr is None and stopped == [proto]
