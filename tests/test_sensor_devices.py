"""The "Intercom Dispositivi" sensor: its list survives a restart, and what it
shows in Home Assistant hides the full identifiers and ports."""
from __future__ import annotations

import asyncio
import types

import pytest

from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import inventory, sensor

sip = hub_mod.sip

PHONE_ID = "C0FFEE00-1111-2222-3333-444455556666"


@pytest.fixture
def devices(monkeypatch):
    fresh = inventory.DeviceInventory()
    monkeypatch.setattr(sip, "DEVICES", fresh)
    return fresh


def _description():
    return next(d for d in sensor.SENSORS if d.key == "devices")


def _entity(hub, attributes):
    desc = _description()
    entity = sensor.sensor_class(desc)(hub, "e1", desc)
    entity.entity_id = "sensor.intercom_dispositivi"

    async def last_state():
        return types.SimpleNamespace(state="1", attributes=attributes)

    entity.async_get_last_state = last_state
    return entity


def test_the_saved_list_is_given_back_to_the_hub(devices):
    hub = hub_mod.VimarIntercomHub()
    entity = _entity(hub, {"dispositivi": [
        {"sip_id": "60901", "device_id": "***556666", "name": "Kitchen iPhone",
         "address": "192.0.2.97", "registered": True},
    ], "riepilogo": ["Kitchen iPhone (60901)"]})
    asyncio.run(entity.async_added_to_hass())
    restored = hub.devices
    assert [d["name"] for d in restored] == ["Kitchen iPhone"]
    assert restored[0]["registered"] is False, "the next REGISTER tells that again"
    assert entity.native_value == 1


def test_the_attribute_masks_the_identifier_and_drops_the_address(devices):
    hub = hub_mod.VimarIntercomHub()
    devices.note_peer({"from": "<sip:60901@d>", "mobile-imei": PHONE_ID,
                       "myname": "Kitchen iPhone",
                       "via": "SIP/2.0/TLS 10.0.0.1;received=192.0.2.97;rport=64406"})
    attrs = _entity(hub, {}).extra_state_attributes
    shown = attrs["dispositivi"][0]
    assert shown["device_id"] == "***556666"
    assert "address" not in shown
    assert PHONE_ID not in str(attrs) and "192.0.2.97" not in str(attrs)
    assert "64406" not in str(attrs)
    assert hub.devices[0]["device_id"] == PHONE_ID, "the hub keeps the full value"


def test_a_restored_masked_device_merges_with_the_live_one(devices):
    hub = hub_mod.VimarIntercomHub()
    asyncio.run(_entity(hub, {"dispositivi": [
        {"sip_id": "60901", "device_id": "***556666", "name": "Kitchen iPhone"}]})
        .async_added_to_hass())
    devices.note_peer({"from": "<sip:60901@d>", "mobile-imei": PHONE_ID.lower()})
    assert len(devices) == 1
    assert hub.devices[0]["device_id"] == PHONE_ID.lower()
    assert hub.devices[0]["name"] == "Kitchen iPhone"


def test_the_device_list_is_kept_out_of_the_recorder():
    cls = sensor.sensor_class(_description())
    assert {"dispositivi", "riepilogo"} <= cls._unrecorded_attributes
    other = next(d for d in sensor.SENSORS if d.key == "status")
    assert "dispositivi" not in getattr(sensor.sensor_class(other),
                                        "_unrecorded_attributes", frozenset())
