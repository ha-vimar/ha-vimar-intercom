"""The cloud TLS connection and its unverified fallback.

The fallback stays (the live plant uses this path and its certificate chain
cannot be checked from here), but every connection made through it is logged
at WARNING, and only a certificate error leads to it.
"""
import asyncio
import ssl

import pytest

from custom_components.vimar_intercom import sip_client as sip


class _Writer:
    def is_closing(self):
        return False


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(sip.R, "SIP_PROXY", "proxy.example")
    monkeypatch.setattr(sip, "_resolve_sip_targets", lambda proxy, port: [("192.0.2.1", 7042)])
    monkeypatch.setattr(sip, "_create_ssl_context", lambda verify: verify)
    monkeypatch.setattr(sip, "get_local_ip", lambda: "10.0.0.2")
    for name in ("reader", "writer", "lock", "MY_IP"):
        monkeypatch.setattr(sip, name, None, raising=False)
    attempts = []

    def install(outcomes):
        async def open_connection(host, port, ssl=None, server_hostname=None):
            attempts.append(ssl)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return object(), _Writer()
        monkeypatch.setattr(sip.asyncio, "open_connection", open_connection)
        return attempts
    return install


def test_a_verified_connection_needs_no_fallback(cloud, caplog):
    attempts = cloud(["ok"])
    with caplog.at_level("WARNING"):
        asyncio.run(sip.connect())
    assert attempts == [True]
    assert "WITHOUT certificate verification" not in caplog.text


def test_the_unverified_fallback_is_logged_at_warning(cloud, caplog):
    attempts = cloud([ssl.SSLCertVerificationError("self signed"), "ok"])
    with caplog.at_level("WARNING"):
        asyncio.run(sip.connect())
    assert attempts == [True, False]
    assert "WITHOUT certificate verification" in caplog.text


def test_a_network_error_does_not_lead_to_the_unverified_fallback(cloud):
    attempts = cloud([OSError("unreachable")])
    with pytest.raises(ConnectionError):
        asyncio.run(sip.connect())
    assert attempts == [True]
