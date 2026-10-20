"""Entry handling in __init__.py: the identity migration, the update listener
and saving learned values, on stub HA objects."""
from __future__ import annotations

import asyncio
import types

import pytest
from harness.web import load_views


@pytest.fixture
def init(monkeypatch):
    return load_views(monkeypatch)


def _hass(init, started_with):
    reloads = []

    async def async_reload(entry_id):
        reloads.append(entry_id)

    hass = types.SimpleNamespace(
        data={init.DOMAIN: {"e1": {"applied": started_with}}},
        config_entries=types.SimpleNamespace(async_reload=async_reload))
    return hass, reloads


def _entry(data=None, options=None):
    return types.SimpleNamespace(entry_id="e1", data=data or {}, options=options or {})


def test_a_fork_entry_keeps_its_device_id_as_both_identifiers(init):
    identity = init._missing_identity({"sip_user": "1", "device_id": " ABC-123 "})
    assert identity == {"device_imei": "ABC-123", "device_uuid": "ABC-123"}


def test_an_entry_without_identity_gets_a_new_one(init):
    identity = init._missing_identity({"sip_user": "1"})
    assert identity["device_imei"] and identity["device_uuid"]


def test_an_entry_with_an_identity_is_left_alone(init):
    assert init._missing_identity(
        {"device_imei": "i", "device_uuid": "u", "device_id": "x"}) is None


def test_a_data_only_change_does_not_reload(init):
    hass, reloads = _hass(init, {"camera_target": "55100"})
    entry = _entry(data={"detected_model": "40517"}, options={"camera_target": "55100"})
    asyncio.run(init._async_update_listener(hass, entry))
    assert reloads == []


CONNECTION = {"sip_user": "60992", "sip_password": "old", "sip_domain": "plant.example.test",
              "local_proxy": "192.0.2.10", "use_local_udp": True}


def test_new_credentials_from_reconfigure_reload(init):
    """Reconfigure only saves the entry (HA 2026.12 wants the update listener to reload):
    a change of the SIP credentials must reload, or the old ones stay in use."""
    hass, reloads = _hass(init, {"camera_target": "55100"})
    hass.data[init.DOMAIN]["e1"]["connection"] = init._connection(CONNECTION)
    entry = _entry(data={**CONNECTION, "sip_password": "new"}, options={"camera_target": "55100"})
    asyncio.run(init._async_update_listener(hass, entry))
    assert reloads == ["e1"]


def test_a_learned_value_next_to_the_same_credentials_does_not_reload(init):
    hass, reloads = _hass(init, {"camera_target": "55100"})
    hass.data[init.DOMAIN]["e1"]["connection"] = init._connection(CONNECTION)
    entry = _entry(data={**CONNECTION, "detected_model": "40507"}, options={"camera_target": "55100"})
    asyncio.run(init._async_update_listener(hass, entry))
    assert reloads == []


def test_a_new_intercom_address_reloads(init):
    """zeroconf_moved saves the Tab's new address and leaves the reload to the listener."""
    hass, reloads = _hass(init, {"camera_target": "55100"})
    hass.data[init.DOMAIN]["e1"]["connection"] = init._connection(CONNECTION)
    entry = _entry(data={**CONNECTION, "local_proxy": "192.0.2.11"}, options={"camera_target": "55100"})
    asyncio.run(init._async_update_listener(hass, entry))
    assert reloads == ["e1"]


def test_an_options_change_reloads(init):
    hass, reloads = _hass(init, {"camera_target": "55100"})
    entry = _entry(options={"camera_target": "55200"})
    asyncio.run(init._async_update_listener(hass, entry))
    assert reloads == ["e1"]


def test_a_learned_value_already_saved_is_not_written_again(init):
    assert init._learned_data({"learned_camera_target": "55001"},
                              {"learned_camera_target": "55001"}) is None


def test_a_new_learned_value_is_merged_into_the_data(init):
    assert init._learned_data({"sip_user": "1"}, {"learned_camera_target": "55001"}) == {
        "sip_user": "1", "learned_camera_target": "55001"}


def test_entry_data_skips_keys_that_are_not_entries(init):
    """The HTTP views take the first value under DOMAIN: a non-entry key put
    there first (a flag, a cache) was taken for the active entry (C1)."""
    hub = object()
    hass = types.SimpleNamespace(data={init.DOMAIN: {
        "flag": True, "cache": {"x": 1}, "e1": {"hub": hub}}})
    assert init._entry_data(hass)["hub"] is hub


def test_entry_data_without_an_entry_is_empty(init):
    hass = types.SimpleNamespace(data={init.DOMAIN: {"cache": {"x": 1}}})
    assert init._entry_data(hass) == {}
