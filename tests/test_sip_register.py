"""Registration lifetime and challenge handling in sip_client.

Both behaviours were observed live on a Tab 7S Up: the registrar grants a
shorter lifetime than requested, and it rotates its nonce between renewals
while answering 401 without stale=true.
"""
import asyncio

import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")


def response(code, reason="OK", **headers):
    lines = [f"SIP/2.0 {code} {reason}"]
    lines += [f"{name.replace('_', '-')}: {value}" for name, value in headers.items()]
    lines += ["Call-ID: reg-test", "CSeq: 1 REGISTER", "Content-Length: 0", "", ""]
    return "\r\n".join(lines)


class FakeSocket:
    def getsockname(self):
        return ("0.0.0.0", 5060)


class FakeRegistrar:
    """Answers REGISTER with a scripted sequence, recording what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    async def send_request(self, message, cid, timeout=15):
        self.sent.append(message)
        return [self.replies.pop(0)] if self.replies else []


@pytest.fixture
def registrar(monkeypatch):
    def install(replies):
        fake = FakeRegistrar(replies)
        monkeypatch.setattr(sip, "send", fake.send)
        monkeypatch.setattr(sip, "_send_request", fake.send_request)
        monkeypatch.setattr(sip, "connect", lambda: asyncio.sleep(0))
        monkeypatch.setattr(sip, "_set_registered", lambda value: None)
        monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", True)
        monkeypatch.setattr(sip, "_udp_sock", FakeSocket())
        return fake
    return install


def challenge(nonce):
    return response(401, "Unauthorized", WWW_Authenticate=f'Digest realm="127.0.0.1", nonce="{nonce}"')


def test_nonce_rotation_is_retried(registrar):
    fake = registrar([challenge("first"), challenge("second"), response(200)])
    assert asyncio.run(sip.do_register()) is True
    assert len(fake.sent) == 3, "the rotated nonce must be retried"


def test_repeated_nonce_is_treated_as_refused_credentials(registrar):
    fake = registrar([challenge("same"), challenge("same"), response(200)])
    assert asyncio.run(sip.do_register()) is False
    assert len(fake.sent) == 2, "a repeated nonce must not be retried"


def test_rejection_other_than_a_challenge_stops_immediately(registrar):
    fake = registrar([response(503, "You're not allowed to make this operation")])
    assert asyncio.run(sip.do_register()) is False
    assert len(fake.sent) == 1


def test_granted_lifetime_comes_from_our_own_contact(registrar, monkeypatch):
    """Our binding is identified by the instance id we registered with."""
    monkeypatch.setattr(sip.R, "DEVICE_UUID", "abc123")
    registrar([response(
        200,
        Contact=(
            '<sip:1@10.0.0.9;transport=tls>;expires=30, '
            '<sip:1@10.0.0.1;transport=tls>;+sip.instance="<urn:uuid:abc123>";expires=120'
        ),
    )])
    assert asyncio.run(sip.do_register()) is True
    assert sip.granted_expiry == 120, "ours wins even when another binding is shorter"


def test_several_contact_headers_are_all_considered(registrar, monkeypatch):
    """A registrar lists one Contact line per binding; keeping only the last
    one made the choice depend on which binding happened to come last."""
    monkeypatch.setattr(sip.R, "DEVICE_UUID", "abc123")
    reply = response(200).replace(
        "Content-Length: 0",
        "Contact: <sip:1@10.0.0.9>;expires=900\r\n"
        'Contact: <sip:1@10.0.0.1>;+sip.instance="<urn:uuid:abc123>";expires=150\r\n'
        "Content-Length: 0",
    )
    registrar([reply])
    assert asyncio.run(sip.do_register()) is True
    assert sip.granted_expiry == 150


@pytest.mark.parametrize(
    "contact",
    [
        "<sip:1@10.0.0.9;transport=tls>;expires=30, <sip:1@10.0.0.8;transport=tls>;expires=900",
        "<sip:1@10.0.0.8;transport=tls>;expires=900, <sip:1@10.0.0.9;transport=tls>;expires=30",
    ],
)
def test_other_devices_leases_are_never_taken_as_ours(registrar, monkeypatch, contact):
    """Several devices share this SIP account, and their remaining time says
    nothing about ours.

    Adopting the shortest one is how a registration storm starts: on a busy
    account some binding is always seconds from expiry, so the renewal delay
    collapses to its floor and we re-register every few seconds forever.
    """
    monkeypatch.setattr(sip.R, "DEVICE_UUID", "not-in-this-response")
    monkeypatch.setattr(sip, "MY_IP", "10.0.0.7")
    registrar([response(200, Contact=contact, Expires="600")])
    assert asyncio.run(sip.do_register()) is True
    assert sip.granted_expiry == 600, "fall back to the Expires header, not to a stranger"


def test_our_binding_is_recognised_by_address_when_the_instance_is_stripped(
    registrar, monkeypatch,
):
    """Relays often rewrite Contact parameters, dropping +sip.instance."""
    monkeypatch.setattr(sip.R, "DEVICE_UUID", "abc123")
    monkeypatch.setattr(sip, "MY_IP", "10.0.0.7")
    monkeypatch.setattr(sip, "_my_port", lambda: 5070)
    registrar([response(
        200,
        Contact="<sip:1@10.0.0.9:5060;transport=tls>;expires=30, "
                "<sip:1@10.0.0.7:5070;transport=tls>;expires=450",
    )])
    assert asyncio.run(sip.do_register()) is True
    assert sip.granted_expiry == 450


def test_with_nothing_identifiable_at_all_an_hour_is_assumed(registrar, monkeypatch):
    monkeypatch.setattr(sip.R, "DEVICE_UUID", "not-in-this-response")
    monkeypatch.setattr(sip, "MY_IP", "10.0.0.7")
    registrar([response(200, Contact="<sip:1@10.0.0.9;transport=tls>;expires=30")])
    assert asyncio.run(sip.do_register()) is True
    assert sip.granted_expiry == 3600


def test_lifetime_falls_back_to_expires_then_to_an_hour(registrar):
    registrar([response(200, Expires="240")])
    assert asyncio.run(sip.do_register()) is True
    assert sip.granted_expiry == 240

    registrar([response(200)])
    assert asyncio.run(sip.do_register()) is True
    assert sip.granted_expiry == 3600


@pytest.mark.parametrize(
    "granted,expected",
    [
        (3600, 3540),   # long lease: a fixed 60 s of margin
        (300, 240),
        (90, 72),       # short lease: the margin shrinks with it
        (60, 48),
        (0, 3540),      # nothing granted yet: assume an hour
    ],
)
def test_renewal_always_lands_before_the_lease_expires(monkeypatch, granted, expected):
    monkeypatch.setattr(sip, "granted_expiry", granted)
    delay = sip._renew_delay()
    assert delay == expected
    if granted:
        assert delay < granted, "renewing at or after expiry loses the binding"


def test_a_comma_inside_a_quoted_parameter_does_not_split_a_binding():
    """Splitting blindly on commas would tear one binding into two."""
    values = sip._split_contacts({
        "_contact_all": ['<sip:1@10.0.0.1>;+sip.instance="<urn:uuid:a,b>";expires=77'],
    })
    assert values == ['<sip:1@10.0.0.1>;+sip.instance="<urn:uuid:a,b>";expires=77']


def test_registration_gives_up_after_three_distinct_challenges(registrar):
    """The retry loop must be bounded even when the nonce keeps changing."""
    fake = registrar([challenge("one"), challenge("two"), challenge("three"), response(200)])
    assert asyncio.run(sip.do_register()) is False
    assert len(fake.sent) == 3
