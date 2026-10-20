"""Binary sensors and doorbell events: what they show, and that they follow the
hub's callbacks for as long as they are in Home Assistant."""
from __future__ import annotations

import asyncio
import types

import pytest

from custom_components.vimar_intercom import binary_sensor as bs
from custom_components.vimar_intercom import event as ev
from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom.const import DOMAIN, EVENT_FUORIPORTA


class _Hub:
    def __init__(self):
        self.registered = False
        self.in_call = False
        self.is_ringing = False
        self.calling = False
        self.stats = {}
        self.state_cbs, self.ring_cbs, self.event_cbs = [], [], []

    def register_state_callback(self, cb):
        self.state_cbs.append(cb)

    def unregister_state_callback(self, cb):
        self.state_cbs.remove(cb)

    def register_ring_callback(self, cb):
        self.ring_cbs.append(cb)

    def unregister_ring_callback(self, cb):
        self.ring_cbs.remove(cb)

    def register_event_callback(self, cb):
        self.event_cbs.append(cb)

    def unregister_event_callback(self, cb):
        self.event_cbs.remove(cb)


def _setup(module, hub):
    added = []
    hass = types.SimpleNamespace(data={DOMAIN: {"e1": {"hub": hub}}})
    asyncio.run(module.async_setup_entry(hass, types.SimpleNamespace(entry_id="e1"), added.extend))
    return {e._attr_unique_id: e for e in added}


def _recording(entity):
    writes = []
    entity.async_write_ha_state = lambda: writes.append(True)
    return writes


# ─── binary_sensor ───────────────────────────────────────────────────────────

def test_binary_sensors_are_created_for_the_entry():
    entities = _setup(bs, _Hub())
    assert set(entities) == {"e1_sip_registered", "e1_in_call", "e1_ringing", "e1_calling",
                             "e1_new_videomessage"}


def test_binary_sensors_follow_the_hub_state():
    hub = _Hub()
    e = _setup(bs, hub)
    assert [e[k].is_on for k in ("e1_sip_registered", "e1_in_call", "e1_ringing", "e1_calling",
                                 "e1_new_videomessage")] == [False] * 5
    hub.registered = hub.in_call = hub.is_ringing = hub.calling = True
    hub.stats["new_videomessage"] = "NEW"
    assert [e[k].is_on for k in ("e1_sip_registered", "e1_in_call", "e1_ringing", "e1_calling",
                                 "e1_new_videomessage")] == [True] * 5


def test_ringing_sensor_names_the_caller(monkeypatch):
    monkeypatch.setitem(hub_mod.SIP_ID_NAMES["en"], "55999", "Test panel")
    hub = _Hub()
    sensor = _setup(bs, hub)["e1_ringing"]
    assert sensor.extra_state_attributes == {"chiamante": None, "chiamante_id": None}
    hub.stats["last_caller_id"] = "55999"
    assert sensor.extra_state_attributes == {"chiamante": "Test panel", "chiamante_id": "55999"}


def test_videomessage_sensor_reports_when_and_how_much_space():
    hub = _Hub()
    hub.stats.update(last_videomessage="2026-09-30T10:00:00", vm_level=40)
    attrs = _setup(bs, hub)["e1_new_videomessage"].extra_state_attributes
    assert attrs == {"ultimo_cambio": "2026-09-30T10:00:00", "spazio": 40}


@pytest.mark.parametrize("key", ["e1_sip_registered", "e1_in_call", "e1_ringing", "e1_calling",
                                 "e1_new_videomessage"])
def test_binary_sensor_writes_its_state_on_hub_changes_until_removed(key):
    hub = _Hub()
    entity = _setup(bs, hub)[key]
    writes = _recording(entity)
    asyncio.run(entity.async_added_to_hass())
    for cb in list(hub.state_cbs):
        cb()
    assert writes == [True]
    asyncio.run(entity.async_will_remove_from_hass())
    assert hub.state_cbs == []


# ─── event ───────────────────────────────────────────────────────────────────

def test_doorbell_and_fuoriporta_events_are_created_for_the_entry():
    assert set(_setup(ev, _Hub())) == {"e1_doorbell", "e1_fuoriporta"}


def test_a_ring_fires_the_doorbell_event_until_the_entity_is_removed():
    hub = _Hub()
    doorbell = _setup(ev, hub)["e1_doorbell"]
    fired = []
    doorbell._trigger_event = lambda event_type, *a: fired.append(event_type)
    writes = _recording(doorbell)
    asyncio.run(doorbell.async_added_to_hass())
    for cb in list(hub.ring_cbs):
        cb()
    assert fired == ["ring"] and writes == [True]
    asyncio.run(doorbell.async_will_remove_from_hass())
    assert hub.ring_cbs == []


def test_fuoriporta_event_fires_only_for_fuoriporta_messages():
    hub = _Hub()
    fp = _setup(ev, hub)["e1_fuoriporta"]
    fired = []
    fp._trigger_event = lambda event_type, data=None: fired.append((event_type, data))
    writes = _recording(fp)
    asyncio.run(fp.async_added_to_hass())
    (cb,) = hub.event_cbs
    cb("vimar_intercom_something_else", {"x": 1})
    cb(EVENT_FUORIPORTA, {"floor": "1"})
    cb(EVENT_FUORIPORTA, None)
    assert fired == [("fuoriporta", {"floor": "1"}), ("fuoriporta", {})]
    assert len(writes) == 2
    asyncio.run(fp.async_will_remove_from_hass())
    assert hub.event_cbs == []
