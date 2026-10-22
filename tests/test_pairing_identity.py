"""The identity and domain a paired entry registers with.

A local pairing binds (identifier, MyName) to the credential and refuses any
other pair with 503. Entries saved by the m4r1k fork keep the cloud domain
under "sip_cloud_domain": without it cloud mode registers on the wrong domain.
"""
import asyncio
import importlib
import socket
import threading

import pytest

from custom_components.vimar_intercom import runtime as R

BASE = {"sip_user": "60999", "sip_password": "pw", "sip_domain": "127.0.0.1"}


def test_the_fork_cloud_domain_is_used_in_cloud_mode():
    R.configure({**BASE, "use_local_udp": False, "sip_cloud_domain": "plant.example.cloud"})
    assert R.SIP_DOMAIN == "plant.example.cloud"


def test_cloud_mode_without_a_cloud_domain_warns(caplog):
    with caplog.at_level("WARNING"):
        R.configure({**BASE, "use_local_udp": False})
    assert "without a cloud domain" in caplog.text


def test_ha1_is_recomputed_even_when_the_domain_matches():
    import hashlib
    R.configure({**BASE, "sip_ha1": "stale"})
    assert R.SIP_HA1 == hashlib.md5(b"60999:127.0.0.1:pw").hexdigest()


def test_the_paired_name_is_kept():
    R.configure({**BASE, "device_name": "Test"})
    assert R.DEVICE_NAME == "Test"
    R.configure(dict(BASE))
    assert R.DEVICE_NAME == R._const.MY_NAME


@pytest.fixture(scope="module")
def cf():
    # conftest stubs the HA modules config_flow imports, and a ConfigFlow that
    # takes the class argument.
    return importlib.import_module("custom_components.vimar_intercom.config_flow")


@pytest.fixture(scope="module")
def of(cf):
    return importlib.import_module("custom_components.vimar_intercom.options_flow")


def test_a_challenge_with_commas_in_quotes_keeps_its_realm(cf):
    challenge = cf._parse_challenge(
        'SIP/2.0 401 Unauthorized\r\n'
        'WWW-Authenticate: Digest realm="a, b", nonce="n1", qop="auth,auth-int", opaque="o"\r\n')
    assert challenge == {"realm": "a, b", "nonce": "n1", "qop": "auth,auth-int", "opaque": "o"}


def fake_intercom(final="SIP/2.0 200 OK"):
    """A UDP intercom: 100 Trying, then 407, then ``final``. Records requests."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(5)
    seen = []

    def serve():
        try:
            for reply in ("SIP/2.0 407 Proxy Authentication Required\r\n"
                          'Proxy-Authenticate: Digest realm="r", nonce="n"\r\n\r\n',
                          final + "\r\n\r\n"):
                data, addr = sock.recvfrom(65535)
                seen.append(data.decode())
                sock.sendto(b"SIP/2.0 100 Trying\r\n\r\n", addr)
                sock.sendto(reply.encode(), addr)
        except OSError:
            pass
        finally:
            sock.close()

    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1], seen


def register(cf, monkeypatch, port):
    monkeypatch.setattr(cf, "DEFAULT_LOCAL_SIP_PORT", port)
    return asyncio.run(cf._test_sip_registration(
        sip_user="60999", sip_password="pw", sip_domain="127.0.0.1",
        local_proxy="127.0.0.1", local_udp_port=0,
        device_imei="351234567890123", device_uuid="uuid-1", device_name="Test",
        timeout=3))


def test_the_local_test_registers_with_the_paired_identity(cf, monkeypatch):
    port, seen = fake_intercom()
    ok, msg = register(cf, monkeypatch, port)
    assert ok, msg
    assert len(seen) == 2
    for request in seen:
        assert "MyName: Test\r\n" in request
        assert "Mobile-IMEI: 351234567890123\r\n" in request
        assert '+sip.instance="<urn:uuid:uuid-1>"' in request
    assert 'realm="r"' in seen[1]


def test_a_refused_identity_is_explained(cf, monkeypatch):
    port, _ = fake_intercom("SIP/2.0 503 You're not allowed to make this operation")
    ok, msg = register(cf, monkeypatch, port)
    assert not ok and "refuses this identity" in msg


def fake_intercom_3():
    """Like fake_intercom, and answers a third REGISTER (the unregister) with 200."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(5)
    seen = []

    def serve():
        try:
            for reply in ("SIP/2.0 401 Unauthorized\r\n"
                          'WWW-Authenticate: Digest realm="r", nonce="n"\r\n\r\n',
                          "SIP/2.0 200 OK\r\n\r\n", "SIP/2.0 200 OK\r\n\r\n"):
                data, addr = sock.recvfrom(65535)
                seen.append(data.decode())
                sock.sendto(reply.encode(), addr)
        except OSError:
            pass
        finally:
            sock.close()

    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1], seen


@pytest.mark.parametrize("unregister", [True, False])
def test_the_options_test_removes_its_own_binding(cf, monkeypatch, unregister):
    """With the integration running, the test binding (same +sip.instance)
    replaces the live one and points it at a socket closed right after."""
    port, seen = fake_intercom_3()
    monkeypatch.setattr(cf, "DEFAULT_LOCAL_SIP_PORT", port)
    ok, msg = asyncio.run(cf._test_sip_registration(
        sip_user="60999", sip_password="pw", sip_domain="127.0.0.1",
        local_proxy="127.0.0.1", local_udp_port=0, device_uuid="uuid-1",
        device_name="Test", timeout=3, unregister=unregister))
    assert ok, msg
    if not unregister:
        assert len(seen) == 2
        return
    assert len(seen) == 3
    contact = [h for h in seen[1].split("\r\n") if h.startswith("Contact:")]
    assert "Expires: 0\r\n" in seen[2] and contact[0] + "\r\n" in seen[2]
    assert "Authorization: Digest" in seen[2]


def test_the_options_flow_asks_for_the_unregister(of, monkeypatch):
    seen = {}

    async def _test(**kw):
        seen.update(kw)
        return True, "ok"

    monkeypatch.setattr(of, "_test_sip_registration", _test)
    from tests.test_config_flow_camera_target import _base_entry_data, _flow
    flow = _flow(of, _base_entry_data())
    asyncio.run(flow.async_step_settings({"local_proxy": "192.0.2.9", "use_local_udp": True}))
    assert seen.get("unregister") is True
