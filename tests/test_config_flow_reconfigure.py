"""Reconfigure: a new pairing QR for the entry that is already set up.

Until now the only way to use a new QR (the intercom refusing the old one with
403) was to delete the integration and add it again: a new entry id, new
entities, automations and dashboards to fix. Reconfigure changes only the SIP
credentials and keeps the entry, its identity, network settings and options.
"""
from __future__ import annotations

import asyncio
import importlib
import types

import pytest


@pytest.fixture(scope="module")
def cf():
    return importlib.import_module("custom_components.vimar_intercom.config_flow")


async def _job(fn, *args):
    return fn(*args)


ENTRY_DATA = {
    "sip_user": "60902", "sip_password": "old", "sip_domain": "plant.example.test",
    "device_imei": "123456789012345", "device_uuid": "uuid-1", "device_name": "HA",
    "local_proxy": "192.0.2.10", "use_local_udp": True, "local_udp_port": 5070,
}
NEW_QR = {"id": "60904", "pwd": "new", "domain": "plant.example.test", "planttype": "2F"}


def _flow(cf, monkeypatch, probe_ok=True):
    entry = types.SimpleNamespace(entry_id="e1", data=dict(ENTRY_DATA), options={"camera_target": "55100"},
                                  state=types.SimpleNamespace(value="loaded"))
    flow = cf.VimarIntercomConfigFlow()
    flow.context = {"source": "reconfigure", "entry_id": "e1"}
    flow.hass = types.SimpleNamespace(async_add_executor_job=_job)
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow._get_reconfigure_entry = lambda: entry
    flow.updates = []

    def update_reload_and_abort(e, **kw):
        flow.updates.append((e, kw))
        return {"type": "abort", "reason": "reconfigure_successful"}

    flow.async_update_and_abort = update_reload_and_abort
    flow.reloaded = []

    def reload_and_abort(e, **kw):
        flow.reloaded.append(e)
        return update_reload_and_abort(e, **kw)

    flow.async_update_reload_and_abort = reload_and_abort
    flow.probes = []

    async def probe(credentials, **kw):
        flow.probes.append((credentials, kw))
        return kw["prefer_local"], probe_ok, "200 OK" if probe_ok else "403 Forbidden"

    monkeypatch.setattr(cf, "_probe_transport", probe)
    monkeypatch.setattr(cf.qr_decoder, "decode", lambda text: NEW_QR if text == "QR" else
                        (_ for _ in ()).throw(cf.qr_decoder.QRDecodeError("Testo QR vuoto")))
    return flow, entry


def test_reconfigure_asks_for_the_new_qr(cf, monkeypatch):
    flow, _ = _flow(cf, monkeypatch)
    result = asyncio.run(flow.async_step_reconfigure())
    assert result["type"] == "form" and result["step_id"] == "reconfigure"


def test_an_invalid_qr_stays_on_the_form(cf, monkeypatch):
    flow, _ = _flow(cf, monkeypatch)
    result = asyncio.run(flow.async_step_reconfigure({"qr_text": " "}))
    assert result["step_id"] == "reconfigure" and result["errors"] == {"qr_text": "qr_invalid"}
    assert flow.probes == [] and flow.updates == []


def test_a_new_qr_updates_only_the_credentials_of_the_same_entry(cf, monkeypatch):
    flow, entry = _flow(cf, monkeypatch)
    result = asyncio.run(flow.async_step_reconfigure({"qr_text": " QR "}))
    assert result == {"type": "abort", "reason": "reconfigure_successful"}
    (updated, kw), = flow.updates
    assert updated is entry
    assert kw["unique_id"] == "60904@plant.example.test"
    data = kw["data_updates"]
    assert data["sip_user"] == "60904" and data["sip_password"] == "new"
    # identity, network and options are not part of the update: they stay as they are
    for key in ("device_imei", "device_uuid", "device_name", "local_proxy", "local_udp_port"):
        assert key not in data
    assert "options" not in kw


def test_the_new_qr_is_tried_with_the_entry_identity_and_network(cf, monkeypatch):
    flow, _ = _flow(cf, monkeypatch)
    asyncio.run(flow.async_step_reconfigure({"qr_text": "QR"}))
    (credentials, kw), = flow.probes
    assert credentials["sip_password"] == "new"
    assert kw["local_proxy"] == "192.0.2.10" and kw["local_udp_port"] == 5070
    assert kw["prefer_local"] is True
    assert kw["identity"] == {"device_imei": "123456789012345", "device_uuid": "uuid-1", "device_name": "HA"}


def test_a_qr_the_intercom_refuses_changes_nothing(cf, monkeypatch):
    flow, _ = _flow(cf, monkeypatch, probe_ok=False)
    result = asyncio.run(flow.async_step_reconfigure({"qr_text": "QR"}))
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"qr_text": "sip_registration_failed"}
    assert result["description_placeholders"]["error_detail"] == "403 Forbidden"
    assert flow.updates == []


def test_a_qr_with_a_loopback_domain_keeps_the_local_domain_learned_at_setup(cf, monkeypatch):
    """On a 40515 the QR carries domain=127.0.0.1; setup took the Tab's own address
    from mDNS. The new QR must not throw that away (the local probe would get 503)."""
    flow, entry = _flow(cf, monkeypatch)
    entry.data["local_domain"] = "192.0.2.10"
    monkeypatch.setattr(cf.qr_decoder, "decode", lambda text: {
        "id": "60904", "pwd": "new", "domain": "127.0.0.1", "cdomain": "plant.example.test",
        "planttype": "2FV2"})
    asyncio.run(flow.async_step_reconfigure({"qr_text": "QR"}))
    (_, kw), = flow.probes
    assert kw["local_domain"] == "192.0.2.10"
    (_, update), = flow.updates
    assert update["data_updates"]["local_domain"] == "192.0.2.10"


def test_a_qr_without_a_mac_keeps_the_stored_one(cf, monkeypatch):
    flow, entry = _flow(cf, monkeypatch)
    entry.data["mac"] = "00:00:5E:00:53:01"
    asyncio.run(flow.async_step_reconfigure({"qr_text": "QR"}))
    (_, update), = flow.updates
    assert update["data_updates"]["mac"] == "00:00:5E:00:53:01"


def test_a_qr_that_works_only_on_the_other_transport_says_so(cf, monkeypatch):
    flow, _ = _flow(cf, monkeypatch)

    async def probe(credentials, **kw):
        return not kw["prefer_local"], True, "local UDP not available (503); registered via cloud TLS"

    monkeypatch.setattr(cf, "_probe_transport", probe)
    result = asyncio.run(flow.async_step_reconfigure({"qr_text": "QR"}))
    (_, update), = flow.updates
    assert update["reason"] == "reconfigure_transport_changed"
    assert update["data_updates"]["use_local_udp"] is False
    assert "cloud TLS" in update["data_updates"]["setup_note"]
    assert result["reason"] == "reconfigure_successful"  # what the stub returns


def test_the_same_transport_clears_an_old_fallback_note(cf, monkeypatch):
    flow, entry = _flow(cf, monkeypatch)
    entry.data["setup_note"] = "an old fallback"
    asyncio.run(flow.async_step_reconfigure({"qr_text": "QR"}))
    (_, update), = flow.updates
    assert update["data_updates"]["setup_note"] == ""
    assert update["reason"] == "reconfigure_successful"


def test_a_loaded_entry_is_saved_and_left_to_the_update_listener(cf, monkeypatch):
    flow, _ = _flow(cf, monkeypatch)
    asyncio.run(flow.async_step_reconfigure({"qr_text": "QR"}))
    assert len(flow.updates) == 1 and flow.reloaded == []


def test_an_entry_that_is_not_set_up_is_reloaded_by_reconfigure(cf, monkeypatch):
    """After ConfigEntryNotReady or a setup error there is no update listener: without
    a reload the new credentials would be saved and never used."""
    flow, entry = _flow(cf, monkeypatch)
    entry.state = types.SimpleNamespace(value="setup_retry")
    asyncio.run(flow.async_step_reconfigure({"qr_text": "QR"}))
    assert flow.reloaded == [entry]


def test_a_qr_already_used_by_another_entry_is_refused_before_any_registration(cf, monkeypatch):
    """The new QR's user@domain is the unique id: if another entry has it, Reconfigure stops
    before the probe (which would register on that entry's slot)."""
    flow, _ = _flow(cf, monkeypatch)
    other = types.SimpleNamespace(entry_id="e2", unique_id="60904@plant.example.test")
    flow._async_current_entries = lambda include_ignore=True: [other]
    result = asyncio.run(flow.async_step_reconfigure({"qr_text": "QR"}))
    assert result == {"type": "abort", "reason": "already_configured"}
    assert flow.probes == [] and flow.updates == []
