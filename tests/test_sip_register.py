"""REGISTER and a registrar that rotates its nonce.

Seen on a Tab 7S Up 40517 over the cloud relay: at renewal the registrar can
answer the authenticated REGISTER with a new 401 and a new nonce, without
stale=true. A single retry then failed the registration until the next
keepalive, about two minutes without rings.
"""
import asyncio

import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")


def response(code, reason="OK", **headers):
    lines = [f"SIP/2.0 {code} {reason}"]
    lines += [f"{k.replace('_', '-')}: {v}" for k, v in headers.items()]
    lines += ["Call-ID: reg-test", "CSeq: 1 REGISTER", "Content-Length: 0", "", ""]
    return "\r\n".join(lines)


def challenge(nonce):
    return response(401, "Unauthorized",
                    WWW_Authenticate=f'Digest realm="plant", nonce="{nonce}"')


@pytest.fixture
def registrar(monkeypatch):
    def install(replies):
        sent = []

        async def send_request(msg, cid, timeout=15):
            sent.append(msg)
            return [replies.pop(0)] if replies else []

        monkeypatch.setattr(sip, "_send_request", send_request)
        monkeypatch.setattr(sip, "connect", lambda: asyncio.sleep(0))
        monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", True)
        monkeypatch.setattr(sip, "_udp_sock", object())
        monkeypatch.setattr(sip, "_set_registered", lambda _v: None)
        monkeypatch.setattr(sip, "_contact_hdr", lambda *a, **k: "<sip:1@10.0.0.2>")
        monkeypatch.setattr(sip, "_via_line", lambda *_a: "Via: SIP/2.0/UDP 10.0.0.2\r\n")
        return sent
    return install


def test_a_rotated_nonce_is_retried(registrar):
    sent = registrar([challenge("first"), challenge("second"), response(200)])
    assert asyncio.run(sip.do_register()) is True
    assert len(sent) == 3


def test_the_same_nonce_twice_means_refused_credentials(registrar):
    sent = registrar([challenge("same"), challenge("same"), response(200)])
    assert asyncio.run(sip.do_register()) is False
    assert len(sent) == 2


def test_a_rejection_that_is_not_a_challenge_stops_at_once(registrar):
    sent = registrar([response(503, "Service Unavailable")])
    assert asyncio.run(sip.do_register()) is False
    assert len(sent) == 1


@pytest.mark.parametrize("code", [403, 404])
def test_a_refused_register_keeps_its_code(registrar, monkeypatch, code):
    """The hub turns a 403/404 into a repair: it needs to know which code came back."""
    monkeypatch.setattr(sip, "last_register_code", None, raising=False)
    registrar([response(code, "Forbidden")])
    assert asyncio.run(sip.do_register()) is False
    assert sip.last_register_code == code


def test_a_successful_register_replaces_the_refused_code(registrar, monkeypatch):
    monkeypatch.setattr(sip, "last_register_code", 403, raising=False)
    registrar([challenge("n"), response(200)])
    assert asyncio.run(sip.do_register()) is True
    assert sip.last_register_code == 200


def test_it_gives_up_after_three_distinct_challenges(registrar):
    sent = registrar([challenge("a"), challenge("b"), challenge("c"), response(200)])
    assert asyncio.run(sip.do_register()) is False
    assert len(sent) == 3


def test_a_proxy_challenge_is_answered_with_proxy_authorization(registrar):
    """RFC 3261 section 22.3: a 407 wants Proxy-Authorization, not Authorization."""
    sent = registrar([
        response(407, "Proxy Authentication Required",
                 Proxy_Authenticate='Digest realm="plant", nonce="p1"'),
        response(200),
    ])
    assert asyncio.run(sip.do_register()) is True
    assert len(sent) == 2
    assert "\r\nProxy-Authorization: Digest " in sent[1]
    assert "\r\nAuthorization:" not in sent[1]


def test_a_401_is_answered_with_authorization(registrar):
    sent = registrar([challenge("n1"), response(200)])
    assert asyncio.run(sip.do_register()) is True
    assert "\r\nAuthorization: Digest " in sent[1]
    assert "Proxy-Authorization" not in sent[1]


def test_three_rotated_nonces_are_reported_as_refused_credentials(registrar, caplog):
    registrar([challenge("a"), challenge("b"), challenge("c")])
    with caplog.at_level("WARNING"):
        assert asyncio.run(sip.do_register()) is False
    assert "credentials refused (the registrar rotated its nonce each time)" in caplog.text
    assert "nessuna risposta finale" not in caplog.text


def test_the_contacts_of_a_200_reach_the_device_list(registrar, monkeypatch):
    """The registrar lists every binding of the shared SIP user in its 200."""
    from custom_components.vimar_intercom.inventory import DeviceInventory
    monkeypatch.setattr(sip, "DEVICES", DeviceInventory())
    monkeypatch.setattr(sip.R, "DEVICE_UUID", "ours")
    ok = response(200, Contact='<sip:60999@192.0.2.5:5070>;+sip.instance="<urn:uuid:ours>";expires=900, '
                               '<sip:60999@198.51.100.9:5071>;+sip.instance="<urn:uuid:other>";expires=600')
    registrar([ok])
    assert asyncio.run(sip.do_register()) is True
    devices = {d["device_id"]: d for d in sip.DEVICES.snapshot()}
    assert set(devices) == {"ours", "other"}
    assert devices["other"]["registered"] is True
