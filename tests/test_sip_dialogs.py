"""Incoming and outgoing dialogs, driven through the real sip_client code.

Covers the state machine the review found untested: a ring answered after
early media (the 200 OK must repeat the 183 byte for byte), a second ring
during a call, a re-INVITE, a BYE for someone else's dialog, a CANCEL during
early media, and hanging up an outgoing call that is still ringing.
"""
import asyncio

import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")

OFFER = (
    "v=0\r\no=- 1 1 IN IP4 10.0.0.9\r\ns=-\r\nc=IN IP4 10.0.0.9\r\nt=0 0\r\n"
    "m=audio 4000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
    "m=video 4002 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
)

KEY = "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz\r\n"
SRTP_OFFER = (
    "v=0\r\no=- 1 1 IN IP4 10.0.0.9\r\ns=-\r\nc=IN IP4 10.0.0.9\r\nt=0 0\r\n"
    "m=audio 4000 RTP/SAVP 0\r\na=rtpmap:0 PCMU/8000\r\n" + KEY +
    "m=video 4002 RTP/SAVP 96\r\na=rtpmap:96 H264/90000\r\n" + KEY
)


def invite(cid="ring-1", cseq=1, body=OFFER):
    return (
        f"INVITE sip:60999@plant SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 63.34.36.117:7042;branch=z9hG4bK{cid}{cseq}\r\n"
        f"From: <sip:55100@plant>;tag=panel\r\n"
        f"To: <sip:60999@plant>\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq} INVITE\r\n"
        f"Contact: <sip:55100@10.0.0.9:5060>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(body)}\r\n\r\n{body}"
    )


def request(method, cid, cseq=2):
    return (
        f"{method} sip:60999@plant SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 63.34.36.117:7042;branch=z9hG4bK{method}{cid}\r\n"
        f"From: <sip:55100@plant>;tag=panel\r\nTo: <sip:60999@plant>;tag=us\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq} {method}\r\nContent-Length: 0\r\n\r\n"
    )


def first_line(msg):
    return msg.split("\r\n", 1)[0]


def body(msg):
    return msg.split("\r\n\r\n", 1)[1]


@pytest.fixture
def line(monkeypatch):
    """A fake transport and media layer; returns what was sent and broadcast."""
    sent, events, media_calls = [], [], []

    async def _send(msg):
        sent.append(msg)

    async def _broadcast(kind, _msg):
        events.append(kind)

    async def _setup_media(*_a, **_k):
        media_calls.append("setup")

    async def _stop_media(*_a, **_k):
        media_calls.append("stop")

    async def _no_keyframe():
        return None

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(sip, "broadcast", _broadcast)
    monkeypatch.setattr(sip.media, "setup_media", _setup_media)
    monkeypatch.setattr(sip.media, "stop_media", _stop_media)
    monkeypatch.setattr(sip, "send_keyframe_request", _no_keyframe)
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    monkeypatch.setattr(sip, "MY_IP", "10.0.0.2")
    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(sip.R, "VIDEO_ENABLED", True)
    monkeypatch.setattr(sip, "EARLY_MEDIA", True)
    monkeypatch.setattr(sip, "pending_incoming", dict(sip.pending_incoming, active=False))
    monkeypatch.setattr(sip, "call_state", dict(sip.call_state, **sip._DIALOG_RESET))
    return sent, events, media_calls


def run(coro):
    return asyncio.run(coro)


class TestARingWithEarlyMedia:
    def test_the_200_repeats_the_183_byte_for_byte(self, line):
        sent, events, media_calls = line
        run(sip.handle_incoming_invite(invite()))
        early = [m for m in sent if first_line(m).startswith("SIP/2.0 183")]
        assert early, "the ring asks for early media"
        run(sip.do_answer_incoming())
        ok = [m for m in sent if first_line(m) == "SIP/2.0 200 OK"]
        assert body(ok[-1]) == body(early[-1])
        assert media_calls == ["setup"], "media set up once, at the 183"
        assert events == ["ring", "call_started"]

    def test_a_cancel_during_early_media_stops_the_media(self, line):
        sent, events, media_calls = line
        run(sip.handle_incoming_invite(invite()))
        run(sip.handle_incoming_cancel(request("CANCEL", "ring-1", cseq=1)))
        assert "SIP/2.0 487 Request Terminated" in map(first_line, sent)
        assert media_calls == ["setup", "stop"]
        assert events == ["ring", "ring_ended"]

    def test_the_next_ring_does_not_inherit_the_previous_answer(self, line):
        sent, _events, _media = line
        run(sip.handle_incoming_invite(invite("ring-1", body=SRTP_OFFER)))
        run(sip.handle_incoming_cancel(request("CANCEL", "ring-1", cseq=1)))
        sip.EARLY_MEDIA = False
        run(sip.handle_incoming_invite(invite("ring-2", body=SRTP_OFFER)))
        assert sip.pending_incoming["early_sdp"] is None
        run(sip.do_answer_incoming())
        first_183 = next(m for m in sent if first_line(m).startswith("SIP/2.0 183"))
        ok = [m for m in sent if first_line(m) == "SIP/2.0 200 OK" and "ring-2" in m]
        assert body(ok[-1]) != body(first_183)


class TestDuringACall:
    def answered(self, line):
        run(sip.handle_incoming_invite(invite("ring-1")))
        run(sip.do_answer_incoming())
        line[0].clear()
        line[1].clear()
        line[2].clear()

    def test_a_second_ring_does_not_touch_the_call_media(self, line):
        sent, events, media_calls = line
        self.answered(line)
        keys = (sip._local_crypto_key, sip._local_video_crypto_key)
        run(sip.handle_incoming_invite(invite("ring-2")))
        assert not any(first_line(m).startswith("SIP/2.0 183") for m in sent)
        assert media_calls == []
        assert (sip._local_crypto_key, sip._local_video_crypto_key) == keys
        assert events == ["ring"], "the second visitor still rings"

    def test_a_reinvite_is_not_a_new_ring(self, line):
        sent, events, _media = line
        self.answered(line)
        answer = sip.call_state["local_sdp"]
        run(sip.handle_incoming_invite(invite("ring-1", cseq=2)))
        assert events == []
        assert [first_line(m) for m in sent] == ["SIP/2.0 200 OK"]
        assert body(sent[0]) == answer
        assert sip.in_call and sip.call_state["call_id"] == "ring-1"

    def test_a_bye_for_another_dialog_leaves_the_call_up(self, line):
        sent, events, media_calls = line
        self.answered(line)
        run(sip.handle_incoming_bye(request("BYE", "someone-else")))
        assert first_line(sent[0]).startswith("SIP/2.0 481")
        assert sip.in_call and events == [] and media_calls == []

    def test_the_bye_for_our_dialog_ends_it(self, line):
        sent, events, media_calls = line
        self.answered(line)
        run(sip.handle_incoming_bye(request("BYE", "ring-1")))
        assert first_line(sent[0]) == "SIP/2.0 200 OK"
        assert not sip.in_call and events == ["call_ended"] and media_calls == ["stop"]


class TestHangingUpWhileItRings:
    def test_hangup_sends_a_cancel_for_the_ringing_invite(self, line, monkeypatch):
        sent, events, _media = line
        monkeypatch.setattr(sip, "registered", True)
        monkeypatch.setattr(sip.R, "INTERCOM", "sip:55100@plant")

        async def scenario():
            call = asyncio.create_task(sip.do_call("sip:55100@plant"))
            await asyncio.sleep(0.01)
            cid = sip.call_state["call_id"]
            invite_msg = sent[0]
            await sip.pending_responses[cid].put(
                f"SIP/2.0 180 Ringing\r\nCall-ID: {cid}\r\nCSeq: 1 INVITE\r\n"
                f"To: <sip:55100@plant>;tag=p\r\nContent-Length: 0\r\n\r\n")
            await sip.do_hangup()
            cancel = next(m for m in sent if m.startswith("CANCEL "))
            # the panel answers the CANCEL, then terminates the INVITE
            q = sip.pending_responses[cid]
            await q.put(f"SIP/2.0 200 OK\r\nCall-ID: {cid}\r\nCSeq: 1 CANCEL\r\n"
                        f"Content-Length: 0\r\n\r\n")
            await q.put(f"SIP/2.0 487 Request Terminated\r\nCall-ID: {cid}\r\n"
                        f"CSeq: 1 INVITE\r\nTo: <sip:55100@plant>;tag=p\r\n"
                        f"Content-Length: 0\r\n\r\n")
            return invite_msg, cancel, await asyncio.wait_for(call, 5)

        invite_msg, cancel, result = run(scenario())
        branch = next(h for h in invite_msg.split("\r\n") if h.startswith("Via:"))
        assert branch in cancel, "the CANCEL reuses the INVITE's Via branch"
        assert "CSeq: 1 CANCEL" in cancel
        assert result[0] is False
        assert any(m.startswith("ACK ") for m in sent), "the 487 is acknowledged"
        assert not sip.in_call and not sip.calling

    def test_a_200_that_crosses_the_cancel_is_hung_up(self, line, monkeypatch):
        sent, _events, _media = line
        monkeypatch.setattr(sip, "registered", True)

        async def scenario():
            call = asyncio.create_task(sip.do_call("sip:55100@plant"))
            await asyncio.sleep(0.01)
            cid = sip.call_state["call_id"]
            await sip.do_hangup()
            await sip.pending_responses[cid].put(
                f"SIP/2.0 200 OK\r\nCall-ID: {cid}\r\nCSeq: 1 INVITE\r\n"
                f"To: <sip:55100@plant>;tag=p\r\nContact: <sip:55100@10.0.0.9>\r\n"
                f"Content-Length: 0\r\n\r\n")
            return await asyncio.wait_for(call, 10)

        result = run(scenario())
        assert result[0] is False
        assert any(m.startswith("BYE ") for m in sent), "the late answer gets a BYE"
        assert not sip.in_call


class TestAfterACancel:
    def test_a_new_call_waits_for_the_cancelled_transaction(self, line, monkeypatch):
        """The old INVITE loop must not clear the new call's state or flags."""
        sent, _events, _media = line
        monkeypatch.setattr(sip, "registered", True)
        monkeypatch.setattr(sip, "_invite_idle_event", None)

        async def scenario():
            first = asyncio.create_task(sip.do_call("sip:55100@plant"))
            await asyncio.sleep(0.01)
            old_cid = sip.call_state["call_id"]
            await sip.do_hangup()                       # CANCEL goes out
            second = asyncio.create_task(sip.do_call("sip:55100@plant"))
            await asyncio.sleep(0.05)
            assert sip.call_state["call_id"] == old_cid, "the new INVITE waited"
            await sip.pending_responses[old_cid].put(
                f"SIP/2.0 487 Request Terminated\r\nCall-ID: {old_cid}\r\n"
                f"CSeq: 1 INVITE\r\nTo: <sip:55100@plant>;tag=p\r\n"
                f"Content-Length: 0\r\n\r\n")
            assert (await asyncio.wait_for(first, 5))[0] is False
            await asyncio.sleep(0.05)
            new_cid = sip.call_state["call_id"]
            assert new_cid != old_cid and sip.calling, "the new call is ringing"
            second.cancel()
            return new_cid

        run(scenario())
        invites = [m for m in sent if m.startswith("INVITE ")]
        assert len(invites) == 2


class TestRelayCopies:
    def test_the_relay_copy_of_a_reinvite_is_answered_too(self, line):
        sent, events, _media = line
        run(sip.handle_incoming_invite(invite("ring-1")))
        run(sip.do_answer_incoming())
        sent.clear()
        raw = invite("ring-1", cseq=2)

        async def twice():
            fired = []
            await sip._process_request(raw, lambda coro, _n: fired.append(asyncio.ensure_future(coro)))
            await asyncio.gather(*fired)
            await sip._process_request(raw, lambda coro, _n: None)

        run(twice())
        assert [first_line(m) for m in sent] == ["SIP/2.0 200 OK", "SIP/2.0 200 OK"]
