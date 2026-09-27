"""Setup must establish which transport works, not assume it.

The QR's plant type only suggests a starting point. On 2FV2 the local UDP path
is refused with 503 while the cloud relay works; on 2F it is the other way
round. Whichever answers is what gets stored.
"""
import asyncio

import pytest

cf = pytest.importorskip("custom_components.vimar_intercom.config_flow")

CREDENTIALS = {
    "sip_user": "60999",
    "sip_password": "secret",
    "sip_domain": "127.0.0.1",
    "cloud_domain": "plant.ipvdes.vimar.cloud",
    "cloud_proxy": "ipvdes.vimar.cloud",
}


def install(monkeypatch, *, local_ok, cloud_ok):
    calls = []

    async def fake_local(**kwargs):
        calls.append("local")
        return local_ok, "OK" if local_ok else "503 You're not allowed to make this operation"

    async def fake_cloud(**kwargs):
        calls.append("cloud")
        return cloud_ok, "OK" if cloud_ok else "relay refused"

    monkeypatch.setattr(cf, "_test_sip_registration", fake_local)
    monkeypatch.setattr(cf, "_test_cloud_registration", fake_cloud)
    return calls


def probe(prefer_local):
    return asyncio.run(cf._probe_transport(
        CREDENTIALS, local_proxy="192.168.1.2", local_udp_port=5060,
        device_imei="351234567890123", device_uuid="abc123", device_name="Home Assistant", prefer_local=prefer_local,
    ))


def test_the_preferred_transport_is_tried_first_and_kept(monkeypatch):
    calls = install(monkeypatch, local_ok=True, cloud_ok=True)
    use_local, ok, _ = probe(prefer_local=True)
    assert (use_local, ok) == (True, True)
    assert calls == ["local"], "no need to try the other path once one works"


def test_a_refused_local_path_falls_back_to_the_cloud(monkeypatch):
    """Exactly the 2FV2 case: local UDP answers 503, the relay works."""
    calls = install(monkeypatch, local_ok=False, cloud_ok=True)
    use_local, ok, msg = probe(prefer_local=True)
    assert (use_local, ok) == (False, True)
    assert calls == ["local", "cloud"]
    assert "cloud TLS" in msg


def test_a_cloud_first_plant_falls_back_to_local(monkeypatch):
    calls = install(monkeypatch, local_ok=True, cloud_ok=False)
    use_local, ok, msg = probe(prefer_local=False)
    assert (use_local, ok) == (True, True)
    assert calls == ["cloud", "local"]
    assert "UDP locale" in msg


def test_when_neither_answers_both_reasons_are_reported(monkeypatch):
    install(monkeypatch, local_ok=False, cloud_ok=False)
    use_local, ok, msg = probe(prefer_local=True)
    assert ok is False
    assert use_local is True, "keep what was asked for so the form stays consistent"
    assert "503" in msg and "relay refused" in msg
