"""sip_client at its edges, without a network: helpers, SDP corners, model
detection, TLS context and SRV lookup, the UDP and TLS readers, OPTIONS and
the push profile, the request loop."""
from __future__ import annotations

import asyncio
import logging
import ssl
import sys
import types

import pytest

from custom_components.vimar_intercom import const as C
from custom_components.vimar_intercom import plant_state as S
from custom_components.vimar_intercom import runtime as R
from custom_components.vimar_intercom import sip_client as sip
from custom_components.vimar_intercom import sip_message

# The real one: the `net` fixture replaces it with a no-op for the calls.
_KEYFRAME = sip.send_keyframe_request


# ─── Broadcast and state callbacks ───────────────────────────────────────────

def test_broadcasts_are_muted_inside_silenced_and_come_back_after_an_error(monkeypatch):
    sent = []

    async def _bc(kind, msg):
        sent.append(kind)

    monkeypatch.setattr(sip, "_broadcast", _bc)

    async def _run():
        with pytest.raises(RuntimeError):
            with sip.silenced():
                await sip.broadcast("call_ended", "switch")
                raise RuntimeError("cancelled switch")
        await sip.broadcast("call_ended", "real")

    asyncio.run(_run())
    assert sent == ["call_ended"], "only the broadcast after the block goes out"


def test_a_failing_state_callback_does_not_stop_the_state_change(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("entity gone")

    monkeypatch.setattr(sip, "registered", False)
    sip.set_state_callback(_boom)
    with caplog.at_level(logging.ERROR, logger=sip.__name__):
        sip._set_registered(True)
    assert sip.registered is True
    assert "State change callback error" in caplog.text


# ─── Local address and ports ─────────────────────────────────────────────────

def test_the_local_ip_falls_back_to_any_when_there_is_no_route(monkeypatch):
    closed = []

    class _Sock:
        def connect(self, target):
            raise OSError("network unreachable")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(sip.socket, "socket", lambda *a: _Sock())
    assert sip.get_local_ip() == "0.0.0.0"
    assert closed == [True]


def test_a_socket_that_cannot_tell_its_port_gives_the_configured_one(monkeypatch):
    class _Sock:
        def getsockname(self):
            raise OSError("closed")

    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(R, "LOCAL_UDP_PORT", 5099)
    monkeypatch.setattr(sip, "_udp_sock", _Sock())
    assert sip._my_port() == 5099


def test_the_cloud_contact_carries_the_push_parameters_with_a_token(monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(R, "SIP_USER", "7001")
    monkeypatch.setattr(R, "SIP_DOMAIN", "plant.example")
    monkeypatch.setattr(sip, "MY_IP", "192.0.2.10")
    monkeypatch.setattr(C, "PN_TOKEN", "tok123")
    contact = sip._contact_hdr()
    assert "pn-tok=tok123" in contact and "domain-name=plant.example" in contact
    assert contact.endswith("expires=5184000")
    assert "pn-tok" not in sip._contact_hdr(include_pn=False)


# ─── Challenges, TLS, SRV ─────────────────────────────────────────────────────

def test_a_challenge_without_a_scheme_name_is_read_as_parameters():
    assert sip._challenge_params('realm="a, b", nonce=n1') == {"realm": "a, b", "nonce": "n1"}


def test_a_bundled_ca_is_loaded_into_the_tls_context(monkeypatch):
    certifi = pytest.importorskip("certifi")
    monkeypatch.setattr(C, "CA_PATH", certifi.where())
    ctx = sip._create_ssl_context(verify=False)
    assert ctx.verify_mode == ssl.CERT_REQUIRED, "with a CA the proxy is always verified"
    assert ctx.cert_store_stats()["x509_ca"] > 0


def test_srv_records_come_first_by_priority_then_weight(monkeypatch):
    def _rec(prio, weight, target, port):
        return types.SimpleNamespace(priority=prio, weight=weight, target=target, port=port)

    asked = []

    def _resolve(name, rtype):
        asked.append((name, rtype))
        return [_rec(20, 5, "c.example.", 7043), _rec(10, 1, "b.example.", 7042),
                _rec(10, 9, "a.example.", 7041)]

    resolver = types.SimpleNamespace(resolve=_resolve)
    monkeypatch.setitem(sys.modules, "dns", types.SimpleNamespace(resolver=resolver))
    monkeypatch.setitem(sys.modules, "dns.resolver", resolver)
    targets = sip._resolve_sip_targets("proxy.example", 5061)
    assert asked == [("_sips._tcp.proxy.example", "SRV")]
    assert targets == [("a.example", 7041), ("b.example", 7042), ("c.example", 7043),
                       ("proxy.example", 5061)]


# ─── Parsing corners ─────────────────────────────────────────────────────────

def test_empty_contact_entries_are_dropped():
    assert sip._split_contacts({"_contact_all": ["<sip:1@192.0.2.1>,", ""]}) == ["<sip:1@192.0.2.1>"]


def test_the_h264_answer_takes_the_fmtp_of_the_offered_payload_type():
    offer = sip.parse_sdp(
        "v=0\r\nc=IN IP4 192.0.2.5\r\nm=video 5000 RTP/AVP 98\r\n"
        "a=rtpmap:98 H264/90000\r\n"
        "a=fmtp:99 packetization-mode=1\r\n"
        "a=fmtp:98 profile-level-id=42800c;packetization-mode=0;max-br=512\r\n")
    assert sip.SDP._h264_answer(offer) == (("98", "profile-level-id=42800c;packetization-mode=0"),)


def test_sdp_corners_bad_port_keyless_crypto_and_media_level_address():
    parsed = sip.parse_sdp(
        "v=0\r\nc=IN IP4 192.0.2.5\r\n"
        "m=audio 4000 RTP/SAVP 0\r\nc=IN IP4 192.0.2.6\r\n"
        "a=crypto:1 AES_CM_128_HMAC_SHA1_80\r\n"
        "a=crypto:2 AES_CM_128_HMAC_SHA1_80 inline:QUJD\r\n"
        "m=video x RTP/AVP 96\r\n")
    assert parsed["audio"]["ip"] == "192.0.2.6", "the line's own c= wins"
    assert parsed["audio"]["crypto"] == [{"tag": "2", "suite": "AES_CM_128_HMAC_SHA1_80"}]
    assert parsed["audio"]["crypto_key"] == "QUJD"
    assert parsed["video"]["port"] == 0, "a port that is not a number is a refused line"


def test_an_old_answer_fits_a_reinvite_without_sdp():
    assert sip._answer_fits("v=0\r\nm=audio 4000 RTP/AVP 0\r\n", None) is True


# ─── Registration helpers ────────────────────────────────────────────────────

def test_the_renewal_follows_a_short_grant(monkeypatch):
    monkeypatch.setattr(sip, "granted_expiry", None)
    assert sip.renew_delay() == sip.REGISTER_INTERVAL
    monkeypatch.setattr(sip, "granted_expiry", 60)
    assert sip.renew_delay() == 48.0, "60 s minus a fifth"
    monkeypatch.setattr(sip, "granted_expiry", 3)
    assert sip.renew_delay() == sip.MIN_REGISTER_INTERVAL
    monkeypatch.setattr(sip, "granted_expiry", 3600)
    assert sip.renew_delay() == sip.REGISTER_INTERVAL


def test_our_contact_without_expires_falls_back_to_the_header(monkeypatch):
    monkeypatch.setattr(R, "DEVICE_UUID", "0000-uuid")
    hdrs = {"_contact_all": ['<sip:1@192.0.2.9>;+sip.instance="<urn:uuid:0000-uuid>"'],
            "expires": "300"}
    assert sip._granted_expires(hdrs) == 300


def test_a_broken_binding_list_does_not_break_the_registration(monkeypatch, caplog):
    class _Inv:
        def forget_bindings(self):
            pass

        def note_binding(self, *a, **k):
            raise RuntimeError("inventory broken")

    monkeypatch.setattr(sip, "DEVICES", _Inv())
    with caplog.at_level(logging.DEBUG, logger=sip.__name__):
        sip._record_bindings({"contact": "<sip:1@192.0.2.9>"})
    assert "Device inventory (bindings) skipped" in caplog.text


# ─── Model detection ─────────────────────────────────────────────────────────

def test_the_model_is_learned_once_and_a_less_specific_match_does_not_replace_it(monkeypatch, caplog):
    seen = []
    monkeypatch.setattr(S, "DETECTED_MODEL", "")
    monkeypatch.setattr(S, "DETECTED_FW", "")
    monkeypatch.setattr(S, "DETECTED_PRIORITY", 999)
    monkeypatch.setattr(sip, "_model_callback", None)
    sip._apply_ua("Elvox Tab IP")                # no callback yet: only remembered
    assert S.DETECTED_MODEL == "Elvox Tab IP"
    monkeypatch.setattr(sip, "_model_callback", lambda *a: seen.append(a))
    sip._apply_ua("Elvox Tab 7S/2.1.0")
    sip._apply_ua("Elvox Tab 7S/2.1.0")          # same model and firmware: nothing
    sip._apply_ua("Generic Tab")                 # less specific: ignored
    assert [s[0] for s in seen] == ["Elvox Tab 7S"]
    assert S.DETECTED_FW == "2.1.0"

    def _boom(*a):
        raise RuntimeError("registry gone")

    monkeypatch.setattr(sip, "_model_callback", _boom)
    with caplog.at_level(logging.ERROR, logger=sip.__name__):
        sip._apply_ua("Elvox Tab 7S Plus/3.0")
    assert S.DETECTED_MODEL == "Elvox Tab 7S Plus"
    assert "Model callback error" in caplog.text


# ─── Keepalive OPTIONS ───────────────────────────────────────────────────────

def test_the_keepalive_ping_goes_only_when_registered_and_survives_a_send_error(monkeypatch, caplog):
    sent = []

    async def _send(msg):
        sent.append(msg)
        raise OSError("socket closed")

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(sip, "registered", False)
    asyncio.run(sip._send_options_ping())
    assert sent == []
    monkeypatch.setattr(sip, "registered", True)
    with caplog.at_level(logging.DEBUG, logger=sip.__name__):
        asyncio.run(sip._send_options_ping())
    assert sent[0].startswith("OPTIONS ") and "Call-ID: ping-" in sent[0]
    assert "OPTIONS ping failed" in caplog.text


def test_the_answer_to_a_keepalive_ping_is_not_queued(monkeypatch, caplog):
    monkeypatch.setattr(sip, "pending_responses", {})
    raw = ("SIP/2.0 407 Proxy Authentication Required\r\nVia: SIP/2.0/UDP 192.0.2.2:5060\r\n"
           "Call-ID: ping-abc\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n")
    with caplog.at_level(logging.DEBUG, logger=sip.__name__):
        asyncio.run(sip._dispatch_message(raw))
    assert "Keepalive response 407" in caplog.text
    assert sip.pending_responses == {}


# ─── A scripted network ──────────────────────────────────────────────────────

def _hdrs(msg: str) -> dict:
    return dict(line.split(": ", 1) for line in msg.split("\r\n\r\n")[0].split("\r\n")[1:] if ": " in line)


def _reply(req: str, code: int, reason: str = "X", method: str | None = None, tag: str | None = "tg",
           extra: str = "", body: str = "") -> str:
    h = _hdrs(req)
    cseq = h["CSeq"] if method is None else f"{h['CSeq'].split()[0]} {method}"
    to = h["To"] + (f";tag={tag}" if tag else "")
    ctype = "Content-Type: application/sdp\r\n" if body else ""
    return (f"SIP/2.0 {code} {reason}\r\nVia: {h['Via']}\r\nFrom: {h['From']}\r\nTo: {to}\r\n"
            f"Call-ID: {h['Call-ID']}\r\nCSeq: {cseq}\r\nContact: <sip:55001@192.0.2.50>\r\n"
            f"{extra}{ctype}Content-Length: {len(body)}\r\n\r\n{body}")


CHALLENGE = 'Proxy-Authenticate: Digest realm="r", nonce="n1"\r\n'


@pytest.fixture
def net(monkeypatch):
    """`send` answers from `script`: one list of replies per request sent, in order
    (a callable gets the request). Requests go to `sent`."""
    sent, script, media_calls = [], [], []

    async def _send(msg):
        sent.append(msg)
        if msg.startswith(("SIP/2.0", "ACK", "CANCEL")) or not script:
            return
        step = script.pop(0)
        replies = step(msg) if callable(step) else step
        cid = _hdrs(msg)["Call-ID"]
        q = sip.pending_responses.setdefault(cid, asyncio.Queue())
        for r in replies:
            q.put_nowait(r(msg) if callable(r) else r)

    async def _nop(*a, **k):
        pass

    async def _setup_media(remote, *a, **k):
        media_calls.append("setup")

    async def _stop_media():
        media_calls.append("stop")

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(sip, "broadcast", _nop)
    monkeypatch.setattr(sip, "send_keyframe_request", _nop)
    monkeypatch.setattr(sip.media, "setup_media", _setup_media)
    monkeypatch.setattr(sip.media, "stop_media", _stop_media)
    monkeypatch.setattr(sip, "build_sdp", lambda offer=None, reuse_keys=False: "v=0\r\nSDP-OURS\r\n")
    monkeypatch.setattr(sip, "registered", True)
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    monkeypatch.setattr(sip, "MY_IP", "192.0.2.2")
    monkeypatch.setattr(sip, "pending_responses", {})
    monkeypatch.setattr(sip, "_proxy_challenge", None)
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(R, "INTERCOM", "sip:55001@plant.example")
    for k in sip.call_state:
        monkeypatch.setitem(sip.call_state, k, None)
    return types.SimpleNamespace(sent=sent, script=script, media=media_calls)


# ─── OPTIONS probe ───────────────────────────────────────────────────────────

def test_options_needs_a_registration(net, monkeypatch):
    monkeypatch.setattr(sip, "registered", False)
    assert asyncio.run(sip.do_options()) == (False, "Non registrato")


@pytest.mark.parametrize("script, expected", [
    ([[lambda m: _reply(m, 100), lambda m: _reply(m, 200)]], (True, "OK: 200")),
    ([[lambda m: _reply(m, 404)]], (False, "Errore: 404")),
    ([[lambda m: _reply(m, 407)]], (False, "Auth vuoto")),
    ([[lambda m: _reply(m, 407, extra=CHALLENGE)], [lambda m: _reply(m, 200)]], (True, "OK: 200")),
    ([[lambda m: _reply(m, 407, extra=CHALLENGE)], [lambda m: _reply(m, 403)]], (False, "Errore: 403")),
])
def test_options_reports_the_final_answer(net, script, expected):
    net.script.extend(script)
    assert asyncio.run(sip.do_options("sip:55002@plant.example")) == expected
    assert net.sent[0].startswith("OPTIONS sip:55002@plant.example ")
    if len(script) == 2:
        assert "Proxy-Authorization: Digest" in net.sent[1]


class _Clock:
    """time.time() that jumps 20 s per call: every wait is over at once."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        self.now += 20
        return self.now

    def monotonic(self):
        return self.now


def test_options_without_any_answer_times_out(net, monkeypatch):
    monkeypatch.setattr(sip, "time", _Clock())
    assert asyncio.run(sip.do_options()) == (False, "Timeout")


def test_options_after_a_challenge_without_a_second_answer_times_out(net, monkeypatch):
    real_wait_final = sip._wait_final
    calls = []

    async def _wait_final(cid, timeout=15):
        calls.append(cid)
        if len(calls) == 2:
            return []
        return await real_wait_final(cid, timeout)

    monkeypatch.setattr(sip, "_wait_final", _wait_final)
    net.script.append([lambda m: _reply(m, 407, extra=CHALLENGE)])
    assert asyncio.run(sip.do_options()) == (False, "Timeout")


def test_a_reset_wakes_whoever_waits_for_a_final_answer(net):
    async def _run():
        waiter = asyncio.create_task(sip._wait_final("cid-x", timeout=5))
        await asyncio.sleep(0)
        sip.pending_responses["cid-x"].put_nowait(sip._CANCEL)
        return await waiter

    assert asyncio.run(_run()) == []


# ─── MESSAGE ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("script, expected", [
    ([[lambda m: _reply(m, 100), lambda m: _reply(m, 202)]], (True, "OK (202)")),
    ([[lambda m: _reply(m, 404)]], (False, "Errore: 404")),
    ([[lambda m: _reply(m, 407)]], (False, "Auth vuoto (407)")),
    ([[lambda m: _reply(m, 407, extra=CHALLENGE)], [lambda m: _reply(m, 403)]], (False, "Errore: 403")),
    ([[lambda m: _reply(m, 407, extra=CHALLENGE)], []], (False, "Timeout")),
    ([[]], (False, "Timeout")),
])
def test_a_message_reports_the_final_answer(net, script, expected):
    net.script.extend(script)
    result = asyncio.run(sip.do_system_message("sip:55001@plant.example", "STATUS", timeout=0.05))
    assert result == expected
    assert "Content-Type: text/plain" in net.sent[0]


def test_a_message_answer_for_another_cseq_is_skipped(net):
    def _stale_then_ok(m):
        stale = _reply(m, 500).replace(f"CSeq: {_hdrs(m)['CSeq']}", "CSeq: 999 MESSAGE")
        return [stale, _reply(m, 200)]

    net.script.append(_stale_then_ok)
    assert asyncio.run(sip.do_system_message("sip:55001@plant.example", "X")) == (True, "OK (200)")


# ─── Outgoing call corners ───────────────────────────────────────────────────

def test_a_call_needs_a_registration_and_no_other_call(net, monkeypatch):
    monkeypatch.setattr(sip, "registered", False)
    assert asyncio.run(sip.do_call()) == (False, "Non registrato")
    monkeypatch.setattr(sip, "registered", True)
    monkeypatch.setattr(sip, "in_call", True)
    assert asyncio.run(sip.do_call()) == (False, "Già in chiamata")


def test_a_challenge_without_a_header_ends_the_call_and_acks_without_to_tag(net):
    net.script.append([lambda m: _reply(m, 407, tag=None)])
    assert asyncio.run(sip.do_call()) == (False, "Auth vuoto (407)")
    ack = next(m for m in net.sent if m.startswith("ACK "))
    assert "To: <sip:55001@plant.example>\r\n" in ack
    assert not sip.calling


def test_early_sdp_in_a_183_and_odd_provisional_codes_do_not_end_the_call(net):
    sdp = "v=0\r\nc=IN IP4 192.0.2.50\r\nm=audio 4000 RTP/AVP 0\r\n"
    net.script.append([lambda m: _reply(m, 183, body=sdp), lambda m: _reply(m, 181),
                       lambda m: _reply(m, 200, body=sdp)])
    assert asyncio.run(sip.do_call()) == (True, "Connesso!")
    assert sip.call_state["remote_sdp"]["audio"]["port"] == 4000
    sip._set_in_call(False)


def test_a_hangup_before_the_200_is_processed_closes_the_dialog(net, monkeypatch):
    """`calling` cleared (do_hangup) while the 200 OK waits in the queue: the
    dialog it opened is ACKed and closed with a BYE, and no call starts."""
    def _then_hang_up(m):
        sip._set_calling(False)
        return [_reply(m, 200)]

    closed = []

    async def _close_orphan(code, hdrs, ack=True):
        closed.append((code, ack))

    monkeypatch.setattr(sip, "_close_orphan_invite", _close_orphan)
    net.script.append(_then_hang_up)
    assert asyncio.run(sip.do_call()) == (False, "Annullata")
    assert closed == [(200, True)] and not sip.in_call
    assert sip.call_state["call_id"] is None


def test_a_hangup_during_the_ack_closes_the_dialog_without_a_second_ack(net, monkeypatch):
    closed = []

    async def _close_orphan(code, hdrs, ack=True):
        closed.append((code, ack))

    real_send = sip.send

    async def _send(msg):
        await real_send(msg)
        if msg.startswith("ACK "):
            sip._set_calling(False)
            sip.call_state["call_id"] = "someone-else"

    monkeypatch.setattr(sip, "_close_orphan_invite", _close_orphan)
    monkeypatch.setattr(sip, "send", _send)
    net.script.append([lambda m: _reply(m, 200)])
    assert asyncio.run(sip.do_call()) == (False, "Annullata")
    assert closed == [(200, False)]
    assert sip.call_state["call_id"] == "someone-else", "another dialog's state is left alone"


# ─── Keyframe INFO corners ───────────────────────────────────────────────────

def _in_call(monkeypatch, cid="dlg-1"):
    monkeypatch.setattr(sip, "in_call", True)
    sip.call_state.update(call_id=cid, to_tag="tt", from_tag="ff",
                          remote_contact="<sip:55001@192.0.2.50>", original_target="sip:55001@plant.example")


def test_a_provisional_answer_to_the_keyframe_info_is_waited_through(net, monkeypatch):
    _in_call(monkeypatch)
    net.script.append([lambda m: _reply(m, 100), lambda m: _reply(m, 200)])

    async def _run():
        await _KEYFRAME()
        return sip.pending_responses["dlg-1"].qsize()

    assert asyncio.run(_run()) == 0, "both answers consumed, none left behind"


def test_a_repeated_challenge_to_the_keyframe_info_is_not_answered_again(net, monkeypatch):
    _in_call(monkeypatch)
    monkeypatch.setattr(sip, "_proxy_challenge", CHALLENGE.split(": ", 1)[1].strip())
    net.script.append([lambda m: _reply(m, 407, extra=CHALLENGE)])
    net.script.append([lambda m: _reply(m, 407, extra=CHALLENGE)])
    asyncio.run(_KEYFRAME())
    infos = [m for m in net.sent if m.startswith("INFO ")]
    assert len(infos) == 2, "one retry with the same nonce, then stop"


def test_foreign_answers_are_put_back_until_the_queue_is_full(net, monkeypatch):
    """Answers that are not for the INFO (a BYE's 200 on the shared dialog queue)
    go back to the queue after the wait; a full queue keeps what it has."""
    _in_call(monkeypatch)
    bye_ok = ("SIP/2.0 200 OK\r\nVia: SIP/2.0/TLS 192.0.2.2\r\nCall-ID: dlg-1\r\n"
              "CSeq: {} BYE\r\nContent-Length: 0\r\n\r\n")

    async def _run():
        q = sip.pending_responses["dlg-1"] = asyncio.Queue(maxsize=1)
        q.put_nowait(bye_ok.format(1))
        loop = asyncio.get_running_loop()

        def _info_ok(m):
            loop.call_later(0.01, q.put_nowait, bye_ok.format(2))
            loop.call_later(0.02, q.put_nowait, _reply(m, 200))
            return []

        net.script.append(_info_ok)
        await _KEYFRAME()
        return [q.get_nowait() for _ in range(q.qsize())]

    assert asyncio.run(_run()) == [bye_ok.format(1)]


def test_no_keyframe_info_in_the_ring_preview(net, monkeypatch):
    """In the early dialog of our 183 the 40515 answers every INFO 491 or 501."""
    monkeypatch.setattr(sip, "in_call", False)
    sip.pending_incoming.update(active=True, early=True, cid="ring-1", caller_uri="sip:55001@plant.example",
                                caller_tag="abc", my_tag="me", contact="<sip:55001@192.0.2.50>",
                                route_set=[])
    assert sip.early_media()
    asyncio.run(_KEYFRAME())
    assert net.sent == []


def test_one_keyframe_info_in_flight_at_a_time(net, monkeypatch):
    """Concurrent INFO read the same dialog queue and took each other's 407. A request
    made while one waits for its answer is dropped; the next one goes out."""
    _in_call(monkeypatch)

    async def _run():
        loop = asyncio.get_running_loop()

        def _late_ok(m):
            loop.call_later(0.05, sip.pending_responses["dlg-1"].put_nowait, _reply(m, 200))
            return []

        net.script.append(_late_ok)
        await asyncio.gather(_KEYFRAME(), _KEYFRAME(), _KEYFRAME())
        assert len([m for m in net.sent if m.startswith("INFO ")]) == 1
        net.script.append([lambda m: _reply(m, 200)])
        await _KEYFRAME()

    asyncio.run(_run())
    assert len([m for m in net.sent if m.startswith("INFO ")]) == 2
    assert not sip._keyframe_lock.locked()


def test_the_cached_challenge_counts_its_uses():
    """Every keyframe INFO is signed with the cached challenge: with nc fixed at 1 a
    proxy checking it sees a replay. A full table drops its oldest nonce only."""
    ch = 'Digest realm="r", nonce="n9", qop="auth"'
    assert "nc=00000001" in sip._make_auth("INFO", "sip:a", ch)
    assert "nc=00000002" in sip._make_auth("INFO", "sip:a", ch)
    assert "nc=00000001" in sip._make_auth("INFO", "sip:a", ch.replace("n9", "n10"))
    for i in range(14):                             # 16 nonces with n9 and n10
        sip._make_auth("INFO", "sip:a", ch.replace("n9", f"old{i}"))
    sip._make_auth("INFO", "sip:a", ch.replace("n9", "new"))   # evicts n9, the oldest
    assert len(sip.SM._nonce_count) == 16 and "n9" not in sip.SM._nonce_count
    assert "nc=00000002" in sip._make_auth("INFO", "sip:a", ch.replace("n9", "n10"))


# ─── Incoming ring corners ───────────────────────────────────────────────────

INVITE = (
    "INVITE sip:7001@plant.example SIP/2.0\r\n"
    "Via: SIP/2.0/TLS 192.0.2.50;branch=z9hG4bKring\r\n"
    "From: <sip:55001@plant.example>;tag=abc\r\n"
    "To: <sip:7001@plant.example>\r\n"
    "Call-ID: ring-1\r\n"
    "CSeq: 1 INVITE\r\n"
    "Content-Type: application/sdp\r\n"
    "\r\n"
    "v=0\r\nc=IN IP4 192.0.2.50\r\nm=audio 4000 RTP/AVP 0\r\n"
)


@pytest.fixture
def ring(net, monkeypatch):
    monkeypatch.setitem(sip.pending_incoming, "active", False)
    monkeypatch.setitem(sip.pending_incoming, "cid", None)
    monkeypatch.setitem(sip.pending_incoming, "resp", None)
    return net


def test_a_retransmitted_invite_after_our_200_of_another_call_is_not_answered(ring):
    async def _run():
        await sip.handle_incoming_invite(INVITE)
        sip._close_ring()           # the ring ended, the 180 stays as the last answer
        sip.pending_incoming["resp"] = "SIP/2.0 180 Ringing\r\n"
        ring.sent.clear()
        await sip.handle_incoming_invite(INVITE)

    asyncio.run(_run())
    assert ring.sent == [], "a provisional answer is not repeated once the ring is over"


def test_a_cancel_while_the_preview_starts_closes_the_preview(ring, monkeypatch):
    async def _setup_then_cancel(*a, **k):
        sip._close_ring()

    monkeypatch.setattr(sip.media, "setup_media", _setup_then_cancel)
    asyncio.run(sip.handle_incoming_invite(INVITE))
    assert ring.media == ["stop"]
    assert "timer" not in sip.pending_incoming, "no ring timer for a cancelled ring"


def test_answering_or_timing_out_without_a_ring_does_nothing(ring):
    assert asyncio.run(sip.do_answer_incoming()) == (False, "Nessuna chiamata in arrivo")
    asyncio.run(sip._ring_timeout("ring-1"))
    assert ring.sent == []


def test_an_answer_that_cannot_be_sent_ends_the_ring(ring, monkeypatch):
    events = []

    async def _bc(kind, msg):
        events.append(kind)

    async def _run():
        await sip.handle_incoming_invite(INVITE.split("\r\n\r\n")[0] + "\r\n\r\n")  # no SDP: 180

        async def _down(msg):
            raise OSError("connection lost")

        monkeypatch.setattr(sip, "send", _down)
        return await sip.do_answer_incoming()

    monkeypatch.setattr(sip, "broadcast", _bc)
    ok, msg = asyncio.run(_run())
    assert not ok and msg.startswith("Risposta non inviata")
    assert events[-1] == "ring_ended" and ring.media == []
    assert not sip.in_call


def test_answering_a_ring_without_preview_opens_the_media(ring, monkeypatch):
    async def _run():
        await sip.handle_incoming_invite(INVITE)
        sip.pending_incoming["early"] = False  # the preview did not start
        return await sip.do_answer_incoming()

    assert asyncio.run(_run()) == (True, "Risposto!")
    assert ring.media == ["setup", "setup"]
    sip._set_in_call(False)


def test_declining_the_echo_of_our_own_call_ends_the_ring_quietly(ring, monkeypatch):
    events = []

    async def _bc(kind, msg):
        events.append(kind)

    async def _run():
        await sip.handle_incoming_invite(INVITE)
        monkeypatch.setattr(sip, "in_call", True)
        return await sip.do_decline_incoming()

    monkeypatch.setattr(sip, "broadcast", _bc)
    assert asyncio.run(_run()) is True
    assert "ring_ended" not in events
    assert ring.media == ["setup"], "our call's media stays open"
    assert ring.sent[-1].startswith("SIP/2.0 603 Decline")


@pytest.fixture
def preview(ring, monkeypatch):
    """A ring with early media whose RTP the test feeds by hand (#60)."""
    events = []

    async def _bc(kind, msg):
        events.append(kind)

    audio = types.SimpleNamespace(pkt_count=0, remote_addr=("192.0.2.50", 4000))
    monkeypatch.setattr(sip, "broadcast", _bc)
    monkeypatch.setattr(sip.media, "audio_proto", audio)
    monkeypatch.setattr(sip.media, "video_proto", None)
    monkeypatch.setattr(sip, "RING_MEDIA_POLL_S", 0.01, raising=False)
    monkeypatch.setattr(sip, "RING_MEDIA_GAP_S", 0.05, raising=False)
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    return types.SimpleNamespace(events=events, audio=audio, net=ring)


async def _rtp_for(audio, seconds):
    for _ in range(int(seconds / 0.01)):
        audio.pkt_count += 1
        await asyncio.sleep(0.01)


def test_a_ring_ends_when_the_panel_stops_its_preview(preview):
    """40507 on local UDP: an unanswered ring gets no CANCEL, the panel just stops
    its early media after ~30 s. The ring ends there, not at RING_MAX_S, and a
    late answer finds no ring instead of a dead dialog."""
    async def _run():
        await sip.handle_incoming_invite(INVITE)
        await _rtp_for(preview.audio, 0.2)
        assert sip.ringing("ring-1"), "the preview is flowing: still ringing"
        await asyncio.sleep(0.2)
        return await sip.do_answer_incoming()

    assert asyncio.run(_run()) == (False, "Nessuna chiamata in arrivo")
    assert preview.events == ["ring", "ring_ended"]
    assert preview.net.sent[-1].startswith("SIP/2.0 480 ")
    assert preview.net.media == ["setup", "stop"]
    assert not sip.in_call


def test_a_cloud_ring_is_not_ended_by_a_gap_in_its_preview(preview, monkeypatch):
    """The cloud relay sends CANCEL; a preview gap over Wi-Fi or the relay must
    not end a ring the user can still answer."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)

    async def _run():
        await sip.handle_incoming_invite(INVITE)
        await _rtp_for(preview.audio, 0.05)
        await asyncio.sleep(0.2)
        return sip.ringing("ring-1")

    assert asyncio.run(_run()) is True
    assert preview.events == ["ring"]


def test_a_ring_whose_preview_never_starts_keeps_the_long_timeout(preview):
    async def _run():
        await sip.handle_incoming_invite(INVITE)
        await asyncio.sleep(0.2)
        return sip.ringing("ring-1")

    assert asyncio.run(_run()) is True
    assert preview.events == ["ring"]


def test_an_answered_ring_is_not_ended_by_the_preview_watch(preview, monkeypatch):
    async def _run():
        await sip.handle_incoming_invite(INVITE)
        await _rtp_for(preview.audio, 0.05)
        assert await sip.do_answer_incoming() == (True, "Risposto!")
        await asyncio.sleep(0.2)  # no RTP now: the call is the media's business

    monkeypatch.setattr(sip.media, "enable_tx", lambda: None)
    asyncio.run(_run())
    assert sip.in_call and "ring_ended" not in preview.events
    sip._set_in_call(False)


# ─── Request loop ────────────────────────────────────────────────────────────

def test_a_request_that_fails_does_not_stop_the_request_loop(monkeypatch, caplog):
    handled = []

    async def _process(raw, fire):
        handled.append(raw)
        if raw == "bad":
            raise OSError("reply not sent")
        if raw == "stop":
            raise asyncio.CancelledError

    monkeypatch.setattr(sip, "_process_request", _process)

    async def _run():
        monkeypatch.setattr(sip, "incoming_requests", asyncio.Queue())
        for raw in ("bad", "good", "stop"):
            sip.incoming_requests.put_nowait(raw)
        with pytest.raises(asyncio.CancelledError):
            await sip.request_processor()

    with caplog.at_level(logging.WARNING, logger=sip.__name__):
        asyncio.run(_run())
    assert handled == ["bad", "good", "stop"]
    assert "SIP request not handled: reply not sent" in caplog.text


def test_a_failing_handler_task_is_logged(monkeypatch, caplog):
    async def _boom(raw):
        raise RuntimeError("handler failed")

    monkeypatch.setattr(sip, "handle_incoming_options", _boom)

    async def _run():
        monkeypatch.setattr(sip, "incoming_requests", asyncio.Queue())
        sip.incoming_requests.put_nowait("OPTIONS sip:7001@x SIP/2.0\r\nCall-ID: o1\r\nCSeq: 1 OPTIONS\r\n\r\n")
        task = asyncio.create_task(sip.request_processor())
        for _ in range(50):
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level(logging.ERROR, logger=sip.__name__):
        asyncio.run(_run())
    assert "OPTIONS handler error: handler failed" in caplog.text


def test_a_request_without_call_id_is_never_a_duplicate():
    assert sip._is_duplicate("MESSAGE", {"cseq": "1 MESSAGE"}) is False
    assert sip._is_duplicate("MESSAGE", {"cseq": "1 MESSAGE"}) is False


def test_old_requests_leave_the_duplicate_window(monkeypatch):
    clock = types.SimpleNamespace(monotonic=lambda: 1000.0, time=lambda: 0.0)
    monkeypatch.setattr(sip, "time", clock)
    monkeypatch.setattr(sip, "_last_sweep", 0.0)
    hdrs = {"call-id": "m1", "cseq": "1 MESSAGE"}
    assert sip._is_duplicate("MESSAGE", hdrs) is False
    assert sip._is_duplicate("MESSAGE", hdrs) is True
    clock.monotonic = lambda: 1000.0 + sip.DUPLICATE_WINDOW - 1
    assert sip._is_duplicate("MESSAGE", {"call-id": "m3", "cseq": "1 MESSAGE"}) is False
    assert ("MESSAGE", "m1", "1 MESSAGE") in sip._seen_requests, "still inside the window"
    clock.monotonic = lambda: 1000.0 + sip.DUPLICATE_WINDOW + 5
    assert sip._is_duplicate("MESSAGE", {"call-id": "m2", "cseq": "1 MESSAGE"}) is False
    assert ("MESSAGE", "m1", "1 MESSAGE") not in sip._seen_requests, "swept"
    assert sip._is_duplicate("MESSAGE", hdrs) is False


def test_a_broken_device_list_does_not_drop_the_request(monkeypatch, caplog):
    fired = []

    class _Inv:
        def note_peer(self, *a, **k):
            raise RuntimeError("inventory broken")

        def forget_bindings(self):
            pass

    monkeypatch.setattr(sip, "DEVICES", _Inv())

    async def _opt(raw):
        return raw

    monkeypatch.setattr(sip, "handle_incoming_options", _opt)
    with caplog.at_level(logging.DEBUG, logger=sip.__name__):
        asyncio.run(sip._process_request(
            "OPTIONS sip:7001@x SIP/2.0\r\nCall-ID: o1\r\nCSeq: 1 OPTIONS\r\n\r\n",
            lambda coro, name: (fired.append(name), coro.close())))
    assert fired == ["OPTIONS"]
    assert "Device inventory (peer) skipped" in caplog.text


# ─── Push profile (cloud) ────────────────────────────────────────────────────

def test_the_push_profile_needs_a_token(monkeypatch):
    monkeypatch.setattr(C, "PN_TOKEN", "")
    assert asyncio.run(sip.do_connect_profiles()) == (False, "No FCM token")


@pytest.mark.parametrize("codes, expected", [
    ([200], (True, "Profilo connesso")),
    ([500], (False, "connectProfiles: 500")),
    ([403, 200, 200], (True, "Profilo connesso")),
    ([403, 200, 403], (False, "connectProfiles: 403")),
])
def test_the_push_profile_is_reconnected_after_a_403(monkeypatch, codes, expected):
    requests = pytest.importorskip("requests")
    posted = []

    def _post(url, **kw):
        posted.append(url.rsplit("/", 1)[1])
        return types.SimpleNamespace(status_code=codes[len(posted) - 1])

    monkeypatch.setattr(C, "PN_TOKEN", "tok")
    monkeypatch.setattr(requests, "post", _post)
    assert asyncio.run(sip.do_connect_profiles()) == expected
    if codes[0] == 403:
        assert posted == ["connectProfiles", "disconnectProfiles", "connectProfiles"]


def test_a_push_profile_error_is_reported(monkeypatch):
    requests = pytest.importorskip("requests")

    def _post(url, **kw):
        raise requests.ConnectionError("cloud down")

    monkeypatch.setattr(C, "PN_TOKEN", "tok")
    monkeypatch.setattr(requests, "post", _post)
    ok, msg = asyncio.run(sip.do_connect_profiles())
    assert not ok and "cloud down" in msg


# ─── Readers ─────────────────────────────────────────────────────────────────

async def _no_sleep(*a, **k):
    return None


def test_the_udp_reader_filters_senders_pings_and_survives_errors(monkeypatch, caplog):
    """Datagrams from outside the plant are dropped (one WARNING per sender), a
    quiet socket sends the keepalive, an error does not end the loop."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(R, "LOCAL_PROXY", "192.0.2.1")
    monkeypatch.setattr(sip, "registered", True)
    monkeypatch.setattr(sip, "time", _Clock())
    pings, dispatched = [], []

    async def _ping():
        pings.append(True)

    async def _dispatch(raw):
        dispatched.append(raw)

    monkeypatch.setattr(sip, "_send_options_ping", _ping)
    monkeypatch.setattr(sip, "_dispatch_message", _dispatch)
    steps = [
        (b"INVITE spoofed", ("198.51.100.7", 5060)),
        (b"INVITE spoofed", ("198.51.100.7", 5060)),
        TimeoutError(),
        RuntimeError("socket error"),
        (b"OPTIONS from the panel", ("192.0.2.1", 5060)),
        asyncio.CancelledError(),
    ]

    async def _wait_for(coro, timeout):
        coro.close()
        step = steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    async def _run():
        loop = asyncio.get_running_loop()

        async def _no_dns(*a, **k):
            raise OSError("no resolver")

        monkeypatch.setattr(loop, "getaddrinfo", _no_dns)
        monkeypatch.setattr(asyncio, "wait_for", _wait_for)
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        with pytest.raises(asyncio.CancelledError):
            await sip.reader_task()

    with caplog.at_level(logging.DEBUG, logger=sip.__name__):
        asyncio.run(_run())
    assert dispatched == ["OPTIONS from the panel"]
    assert pings == [True]
    # With log_buffer installed each forwarded record is seen twice: compare the ends.
    levels = [r.levelno for r in caplog.records if "ignorato" in r.getMessage()]
    assert levels[0] == logging.WARNING and levels[-1] == logging.DEBUG
    assert levels.count(logging.DEBUG) == 1
    assert "SIP UDP reader error: socket error" in caplog.text


class _Reader:
    def __init__(self, steps):
        self.steps = steps

    async def read(self, n):
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class _Writer:
    def __init__(self, fail=False):
        self.fail, self.written = fail, []

    def write(self, data):
        self.written.append(data)

    async def drain(self):
        if self.fail:
            raise ConnectionResetError("gone")


def test_the_tls_reader_keeps_alive_and_reconnects_on_every_failure(monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(sip_message, "MAX_SIP_BODY", 200)
    good, bad = _Writer(), _Writer(fail=True)
    reasons, dispatched = [], []
    ok_msg = b"OPTIONS sip:7001@x SIP/2.0\r\nCall-ID: a\r\nContent-Length: 0\r\n\r\n"

    async def _reconnect():
        reasons.append("reconnect")
        monkeypatch.setattr(sip, "writer", bad if sip.writer is good else good)

    async def _framing():
        reasons.append("framing")

    async def _dispatch(raw):
        dispatched.append(raw.split(" ", 1)[0])

    monkeypatch.setattr(sip, "_reconnect_from_reader", _reconnect)
    monkeypatch.setattr(sip, "_reconnect_after_framing_error", _framing)
    monkeypatch.setattr(sip, "_dispatch_message", _dispatch)
    monkeypatch.setattr(sip, "reader", _Reader([
        TimeoutError(),          # CRLF keepalive on the good writer
        b"x" * 300,                      # over MAX_SIP_BODY
        TimeoutError(),          # keepalive fails on the bad writer
        RuntimeError("tls error"),
        ok_msg + b"SIP/2.0 200 OK\r\nContent-Length: abc\r\n\r\n",  # broken framing
        asyncio.CancelledError(),
    ]))
    monkeypatch.setattr(sip, "writer", good)

    async def _run():
        monkeypatch.setattr(sip, "lock", asyncio.Lock())
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        with pytest.raises(asyncio.CancelledError):
            await sip.reader_task()

    asyncio.run(_run())
    assert good.written == [b"\r\n\r\n"]
    assert reasons == ["reconnect", "reconnect", "reconnect", "framing"]
    assert dispatched == ["OPTIONS"]


def test_in_udp_mode_the_reader_is_the_udp_one(monkeypatch):
    ran = []

    async def _udp():
        ran.append(True)

    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(sip, "_udp_reader_task", _udp)
    asyncio.run(sip.reader_task())
    assert ran == [True]


# ─── Connect and reconnect ───────────────────────────────────────────────────

def test_a_busy_udp_port_falls_back_to_an_ephemeral_one(monkeypatch, caplog):
    import socket
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("0.0.0.0", 0))
    busy = holder.getsockname()[1]
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(R, "LOCAL_PROXY", "127.0.0.1")
    monkeypatch.setattr(R, "LOCAL_UDP_PORT", busy)
    try:
        with caplog.at_level(logging.WARNING, logger=sip.__name__):
            asyncio.run(sip.connect())
        assert sip._my_port() not in (busy, 0)
        assert f"Porta UDP {busy} occupata" in caplog.text
    finally:
        holder.close()
        if sip._udp_sock is not None:
            sip._udp_sock.close()


def test_a_certificate_refused_with_and_without_verification_is_a_connection_error(monkeypatch, caplog):
    attempts = []

    async def _open(host, port, ssl=None, server_hostname=None):
        attempts.append((host, ssl.verify_mode))
        raise sip.ssl.SSLCertVerificationError("self-signed")

    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(R, "SIP_PROXY", "proxy.example")
    monkeypatch.setattr(C, "CA_PATH", "/nonexistent/ca.pem")
    monkeypatch.setattr(sip, "get_local_ip", lambda: "192.0.2.2")
    monkeypatch.setattr(sip, "_resolve_sip_targets", lambda p, port: [("127.0.0.1", 9)])
    monkeypatch.setattr(asyncio, "open_connection", _open)
    with caplog.at_level(logging.WARNING, logger=sip.__name__), \
            pytest.raises(ConnectionError, match="self-signed"):
        asyncio.run(sip.connect())
    assert [mode for _, mode in attempts] == [ssl.CERT_REQUIRED, ssl.CERT_NONE]
    assert "Riprovo SENZA verifica" in caplog.text
    assert sip.writer is None


def test_reconnect_over_udp_only_registers_again(monkeypatch):
    results = [False, True]
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(sip, "registered", True)

    async def _register():
        return results.pop(0)

    async def _connect():
        raise AssertionError("no connect over UDP")

    monkeypatch.setattr(sip, "do_register", _register)
    monkeypatch.setattr(sip, "connect", _connect)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    assert asyncio.run(sip._reconnect()) is True
    assert results == []


def test_reconnect_over_tls_survives_a_failing_close_and_profile(monkeypatch):
    calls = []

    class _W:
        def close(self):
            calls.append("close")
            raise OSError("already closed")

    async def _connect():
        calls.append("connect")

    async def _register():
        calls.append("register")
        return True

    async def _profiles():
        calls.append("profiles")
        raise RuntimeError("cloud down")

    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(sip, "writer", _W())
    monkeypatch.setattr(sip, "connect", _connect)
    monkeypatch.setattr(sip, "do_register", _register)
    monkeypatch.setattr(sip, "do_connect_profiles", _profiles)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    assert asyncio.run(sip._reconnect()) is True
    assert calls == ["close", "connect", "register", "profiles"]


def test_reconnect_gives_up_after_five_attempts(monkeypatch, caplog):
    tries = []

    async def _register():
        tries.append(True)
        raise OSError("unreachable")

    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(sip, "do_register", _register)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    with caplog.at_level(logging.ERROR, logger=sip.__name__):
        assert asyncio.run(sip._reconnect()) is False
    assert len(tries) == 5
    assert "All reconnect attempts failed" in caplog.text


def test_register_over_udp_opens_the_socket_first(monkeypatch):
    opened = []

    async def _connect():
        opened.append(True)

    async def _send_request(msg, cid, timeout=15, on_sent=None):
        return []

    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(sip, "_udp_sock", None)
    monkeypatch.setattr(sip, "connect", _connect)
    monkeypatch.setattr(sip, "_send_request", _send_request)
    assert asyncio.run(sip.do_register()) is False
    assert opened == [True]


def test_without_a_hub_a_broadcast_goes_nowhere(monkeypatch):
    monkeypatch.setattr(sip, "_broadcast", None)
    sip.init(None)
    assert asyncio.run(sip.broadcast("ring", "x")) is None


def test_a_quiet_udp_socket_sends_no_ping_when_not_registered(monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(R, "LOCAL_PROXY", "127.0.0.1")
    monkeypatch.setattr(sip, "registered", False)
    pings = []

    async def _ping():
        pings.append(True)

    steps = [TimeoutError(), asyncio.CancelledError()]

    async def _wait_for(coro, timeout):
        coro.close()
        raise steps.pop(0)

    async def _run():
        monkeypatch.setattr(asyncio, "wait_for", _wait_for)
        with pytest.raises(asyncio.CancelledError):
            await sip._udp_reader_task()

    monkeypatch.setattr(sip, "_send_options_ping", _ping)
    asyncio.run(_run())
    assert pings == [] and steps == []


def test_the_ring_hosts_are_its_via_hosts_when_it_has_no_contact(monkeypatch):
    monkeypatch.setitem(sip.pending_incoming, "active", True)
    monkeypatch.setitem(sip.pending_incoming, "via_block", "Via: SIP/2.0/UDP 192.0.2.60:5060;branch=z\r\n")
    monkeypatch.setitem(sip.pending_incoming, "contact", None)
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    assert sip._dialog_hosts() == {"192.0.2.60"}


def test_waiting_for_a_final_answer_gives_up_at_the_deadline(monkeypatch):
    class _Slow:
        def __init__(self):
            self.now = 0.0

        def time(self):
            self.now += 5
            return self.now

    waits = []

    async def _wait_for(coro, timeout):
        coro.close()
        waits.append(timeout)
        raise TimeoutError

    async def _run():
        monkeypatch.setattr(asyncio, "wait_for", _wait_for)
        return await sip._wait_final("cid-slow", timeout=15)

    monkeypatch.setattr(sip, "time", _Slow())
    monkeypatch.setattr(sip, "pending_responses", {})
    assert asyncio.run(_run()) == []
    assert len(waits) == 2 and "cid-slow" not in sip.pending_responses


def test_reconnect_over_tls_without_a_writer_retries_a_failed_register(monkeypatch):
    results, calls = [False, True], []

    async def _connect():
        calls.append("connect")

    async def _register():
        calls.append("register")
        return results.pop(0)

    async def _profiles():
        calls.append("profiles")

    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(sip, "writer", None)
    monkeypatch.setattr(sip, "connect", _connect)
    monkeypatch.setattr(sip, "do_register", _register)
    monkeypatch.setattr(sip, "do_connect_profiles", _profiles)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    assert asyncio.run(sip._reconnect()) is True
    assert calls == ["connect", "register", "connect", "register", "profiles"]


def test_a_short_grant_is_accepted_with_a_warning(monkeypatch, caplog):
    async def _send_request(msg, cid, timeout=15, on_sent=None):
        return [f"SIP/2.0 100 Trying\r\nCall-ID: {cid}\r\nCSeq: 1 REGISTER\r\n\r\n",
                f"SIP/2.0 200 OK\r\nCall-ID: {cid}\r\nCSeq: 1 REGISTER\r\nExpires: 60\r\n\r\n"]

    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(sip, "writer", types.SimpleNamespace(is_closing=lambda: False))
    monkeypatch.setattr(sip, "registered", False)
    monkeypatch.setattr(sip, "_send_request", _send_request)
    with caplog.at_level(logging.WARNING, logger=sip.__name__):
        assert asyncio.run(sip.do_register()) is True
    assert sip.registered and sip.granted_expiry == 60
    assert "granted only 60 s" in caplog.text


def test_a_message_whose_authenticated_retry_gets_only_a_provisional_times_out(net):
    net.script.extend([[lambda m: _reply(m, 407, extra=CHALLENGE)], [lambda m: _reply(m, 100)]])
    result = asyncio.run(sip.do_system_message("sip:55001@plant.example", "X", timeout=0.05))
    assert result == (False, "Timeout")


def test_options_waits_past_a_provisional_answer_after_the_challenge(net):
    """The authenticated OPTIONS got 100 Trying then 200 OK: the probe reported
    "Errore: 100" from the provisional answer instead of the 200."""
    net.script.extend([[lambda m: _reply(m, 407, extra=CHALLENGE)],
                       [lambda m: _reply(m, 100), lambda m: _reply(m, 200)]])
    assert asyncio.run(sip.do_options()) == (True, "OK: 200")
