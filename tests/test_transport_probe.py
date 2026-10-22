"""Setup must establish which transport works, not assume it.

The QR's plant type only suggests a starting point. On 2FV2 the local UDP path
is refused with 503 while the cloud relay works; on 2F it is the other way
round. Whichever answers is what gets stored.
"""
import asyncio
import importlib

import pytest


@pytest.fixture(scope="module")
def cf():
    # conftest stubs the HA modules config_flow imports, and a ConfigFlow that
    # takes the class argument.
    return importlib.import_module("custom_components.vimar_intercom.config_flow")


CREDENTIALS = {
    "sip_user": "60999",
    "sip_password": "secret",
    "sip_domain": "127.0.0.1",
    "cloud_domain": "plant.ipvdes.vimar.cloud",
    "cloud_proxy": "ipvdes.vimar.cloud",
}


def install(cf, monkeypatch, *, local_ok, cloud_ok):
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


def probe(cf, prefer_local):
    return asyncio.run(cf._probe_transport(
        CREDENTIALS, local_proxy="192.168.1.2", local_udp_port=5060,
        identity={"device_imei": "351234567890123", "device_uuid": "abc123"}, prefer_local=prefer_local,
    ))


def test_the_preferred_transport_is_tried_first_and_kept(cf, monkeypatch):
    calls = install(cf, monkeypatch, local_ok=True, cloud_ok=True)
    use_local, ok, _ = probe(cf, prefer_local=True)
    assert (use_local, ok) == (True, True)
    assert calls == ["local"], "no need to try the other path once one works"


def test_a_refused_local_path_falls_back_to_the_cloud(cf, monkeypatch):
    """Exactly the 2FV2 case: local UDP answers 503, the relay works."""
    calls = install(cf, monkeypatch, local_ok=False, cloud_ok=True)
    use_local, ok, msg = probe(cf, prefer_local=True)
    assert (use_local, ok) == (False, True)
    assert calls == ["local", "cloud"]
    assert "cloud TLS" in msg


def test_a_cloud_first_plant_falls_back_to_local(cf, monkeypatch):
    calls = install(cf, monkeypatch, local_ok=True, cloud_ok=False)
    use_local, ok, msg = probe(cf, prefer_local=False)
    assert (use_local, ok) == (True, True)
    assert calls == ["cloud", "local"]
    assert "local UDP" in msg


def test_when_neither_answers_both_reasons_are_reported(cf, monkeypatch):
    install(cf, monkeypatch, local_ok=False, cloud_ok=False)
    use_local, ok, msg = probe(cf, prefer_local=True)
    assert ok is False
    assert use_local is True, "keep what was asked for so the form stays consistent"
    assert "503" in msg and "relay refused" in msg


def network_step(cf, monkeypatch, credentials, *, local_ok, cloud_ok, user_input=None):
    calls = install(cf, monkeypatch, local_ok=local_ok, cloud_ok=cloud_ok)
    flow = cf.VimarIntercomConfigFlow()
    flow._credentials = dict(credentials)
    order = []

    async def set_unique_id(uid):
        order.append("unique_id")

    flow.async_set_unique_id = set_unique_id
    flow._abort_if_unique_id_configured = lambda: order.append("dedupe")
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    result = asyncio.run(flow.async_step_network(
        user_input or {"local_proxy": "192.168.1.2"}))
    return result, calls, order


def test_a_2fv2_plant_is_stored_on_the_cloud(cf, monkeypatch):
    creds = {**CREDENTIALS, "plant_type": "2FV2"}
    result, calls, order = network_step(cf, monkeypatch, creds, local_ok=False, cloud_ok=True)
    assert result["type"] == "create_entry"
    data = result["data"]
    assert calls == ["cloud"], "the profile puts the cloud first on 2FV2"
    assert data["use_local_udp"] is False
    # Media encryption is not forced from the profile: "auto" learns it from
    # the plant's own GET_INIT_STATUS reply.
    assert "media_enc" not in data
    assert data["device_imei"] and data["device_uuid"], "the identity the probe used is kept"


def test_the_duplicate_check_runs_before_the_probe(cf, monkeypatch):
    result, calls, order = network_step(cf, monkeypatch, CREDENTIALS, local_ok=True, cloud_ok=True)
    assert order == ["unique_id", "dedupe"]
    assert result["data"]["use_local_udp"] is True


def test_a_failed_probe_shows_both_reasons_on_the_form(cf, monkeypatch):
    result, _, _ = network_step(cf, monkeypatch, CREDENTIALS, local_ok=False, cloud_ok=False)
    assert result["type"] == "form"
    assert result["errors"] == {"local_proxy": "sip_registration_failed"}
    detail = result["description_placeholders"]["error_detail"]
    assert "503" in detail and "relay refused" in detail


@pytest.mark.parametrize("name", ["Kitchen\r\nVia: evil", "x" * 65, "tab\there"])
def test_an_unusable_device_name_is_refused_before_the_probe(cf, monkeypatch, name):
    result, calls, _ = network_step(cf, monkeypatch, CREDENTIALS, local_ok=True, cloud_ok=True,
                                    user_input={"local_proxy": "192.168.1.2",
                                                "device_name": name})
    assert result["type"] == "form"
    assert result["errors"] == {"device_name": "invalid_device_name"}
    assert calls == [], "no registration with a name the header cannot carry"


def test_a_plain_device_name_is_kept(cf, monkeypatch):
    result, _, _ = network_step(cf, monkeypatch, CREDENTIALS, local_ok=True, cloud_ok=True,
                                user_input={"local_proxy": "192.168.1.2",
                                            "device_name": "Casa Rossi"})
    assert result["data"]["device_name"] == "Casa Rossi"


def test_a_transport_fallback_is_shown_and_kept(cf, monkeypatch, caplog):
    """Asked for local UDP, registered on the cloud: the entry works, but the
    user must see that it is not the transport they chose."""
    with caplog.at_level("WARNING"):
        result, calls, _ = network_step(cf, monkeypatch, CREDENTIALS, local_ok=False, cloud_ok=True,
                                        user_input={"local_proxy": "192.168.1.2",
                                                    "use_local_udp": True})
    assert result["type"] == "create_entry" and calls == ["local", "cloud"]
    assert result["data"]["use_local_udp"] is False
    assert "503" in result["data"]["setup_note"]
    assert result["description"] == "transport_fallback"
    assert result["description_placeholders"]["tried"] == "local UDP"
    assert result["description_placeholders"]["used"] == "cloud TLS"
    assert "registered via cloud TLS" in caplog.text


def test_no_fallback_no_note(cf, monkeypatch):
    result, _, _ = network_step(cf, monkeypatch, CREDENTIALS, local_ok=True, cloud_ok=True)
    assert "setup_note" not in result["data"] and "description" not in result
