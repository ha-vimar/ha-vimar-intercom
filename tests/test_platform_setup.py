"""Each platform's async_setup_entry adds the entities the entry should have,
and the entities stay hooked to the hub only while they are in Home Assistant."""
from __future__ import annotations

import asyncio
import json
import logging
import types
from pathlib import Path

import pytest
from homeassistant.exceptions import HomeAssistantError as _HAError

from custom_components.vimar_intercom import (
    binary_sensor,
    button,
    camera,
    device,
    event,
    lock,
    select,
    sensor,
    switch,
    text,
)
from custom_components.vimar_intercom import plant_state as S
from custom_components.vimar_intercom import runtime as R
from custom_components.vimar_intercom.const import DOMAIN, MODEL


class _Hub:
    def __init__(self, ok=True, **stats):
        self.ok = ok
        self.registered = True
        self.in_call = self.is_ringing = self.calling = self.video_active = False
        self.sip_user, self.sip_domain, self.proxy = "100", "example.invalid", "192.0.2.10"
        self.transport, self.local_ip = "udp", "192.0.2.20"
        self.stats = dict(stats)
        self.state_cbs, self.video_end_cbs = [], []
        self.pressed = []

    def register_state_callback(self, cb):
        self.state_cbs.append(cb)

    def unregister_state_callback(self, cb):
        if cb in self.state_cbs:
            self.state_cbs.remove(cb)

    def register_video_end_callback(self, cb):
        self.video_end_cbs.append(cb)
        return lambda: self.video_end_cbs.remove(cb)

    async def async_call(self, target=None):
        self.pressed.append(("call", target))
        return self.ok, "200"

    async def async_answer(self):
        self.pressed.append(("answer",))
        return self.ok, "200"

    async def async_decline(self):
        self.pressed.append(("decline",))
        return self.ok, "603"

    async def async_hangup(self):
        self.pressed.append(("hangup",))

    async def async_door(self, target=None, command=None):
        self.pressed.append(("door", target))
        return self.ok, "opened", 200

    async def async_send_command(self, **kw):
        self.pressed.append(("command", kw["body"], kw["target"]))
        return self.ok, "200"

    def simulate_ring(self, duration):
        self.pressed.append(("sim_ring", duration))
        return self.ok

    async def async_set_apt_param(self, name, value):
        self.pressed.append(("apt", name, value))
        return self.ok, "200" if self.ok else "Timeout"


def _entry(options=None, data=None):
    unload = []
    return types.SimpleNamespace(entry_id="e1", options=options or {}, data=data or {},
                                 async_on_unload=unload.append, unloads=unload)


def _hass(hub, tmp_path=None):
    async def executor(f, *a):
        return f(*a)

    media = {"local": str(tmp_path)} if tmp_path else {}
    return types.SimpleNamespace(data={DOMAIN: {"e1": {"hub": hub}}}, async_add_executor_job=executor,
                                 config=types.SimpleNamespace(media_dirs=media))


def _setup(module, hub, entry=None, hass=None):
    added = []

    def add(entities, update_before_add=False):
        added.extend(entities)

    asyncio.run(module.async_setup_entry(hass or _hass(hub), entry or _entry(), add))
    return added


def _ids(entities):
    return [e._attr_unique_id for e in entities]


# ─── device ──────────────────────────────────────────────────────────────────

def test_device_info_uses_the_detected_model_and_firmware(monkeypatch):
    monkeypatch.setattr(S, "DETECTED_MODEL", None)
    monkeypatch.setattr(S, "DETECTED_FW", None)
    info = device.device_info("e1")
    assert info["model"] == MODEL and "sw_version" not in info
    assert info["identifiers"] == {(DOMAIN, "e1")}
    monkeypatch.setattr(S, "DETECTED_MODEL", "Elvox Tab 7S")
    monkeypatch.setattr(S, "DETECTED_FW", "1.2.3")
    info = device.device_info("e1")
    assert info["model"] == "Elvox Tab 7S" and info["sw_version"] == "1.2.3"


# ─── button ──────────────────────────────────────────────────────────────────

@pytest.fixture
def ha_error(monkeypatch):
    for mod in (button, select):
        monkeypatch.setattr(mod, "HomeAssistantError", _HAError)


def test_buttons_include_the_actuators_from_the_options_and_skip_broken_ones(caplog):
    entry = _entry(options={"actuators": [
        {"name": "Luce scala", "msg": "OPEN_3", "target": "55001", "icon": "light"},
        {"msg": "no name"},  # broken: logged and skipped, the others still load
    ]}, data={"actuators": [{"name": "ignored", "msg": "x", "target": "1", "icon": "door"}]})
    with caplog.at_level(logging.ERROR):
        entities = _setup(button, _Hub(), entry)
    assert _ids(entities) == ["e1_call", "e1_call_ext", "e1_call_int", "e1_answer", "e1_decline",
                              "e1_hangup", "e1_door_street", "e1_test_ring", "e1_act_luce_scala_open_3"]
    assert "Attuatore ignorato" in caplog.text


def test_buttons_take_the_actuators_from_the_data_without_options():
    entry = _entry(data={"actuators": [{"name": "Cancello", "msg": "OPEN_1", "target": "AUTO",
                                        "icon": "unknown"}]})
    act = _setup(button, _Hub(), entry)[-1]
    assert act._target == R.DOOR_TARGET and act._attr_icon == "mdi:gesture-tap-button"


def test_buttons_send_what_they_say(ha_error, monkeypatch):
    monkeypatch.setattr(R, "CAMERA_TARGET", "55100")
    monkeypatch.setattr(R, "INTERNAL_PANEL_TARGET", "55200")
    hub = _Hub()
    hub.is_ringing = True
    entry = _entry(options={"actuators": [{"name": "Luce", "msg": "OPEN_3", "target": "55009",
                                           "icon": "light"}]})
    for entity in _setup(button, hub, entry):
        entity._context = None  # HA's default: no user (the Test ring button checks it)
        asyncio.run(entity.async_press())
    assert hub.pressed == [("call", None), ("call", "55100"), ("call", "55200"), ("answer",),
                           ("decline",), ("hangup",), ("door", None), ("sim_ring", 20),
                           ("command", "OPEN_3", "55009")]


def test_test_ring_button_simulates_a_ring_and_complains_when_busy(ha_error):
    hub = _Hub()
    b = button.VimarTestRingButton(hub, "e1")
    b._context = None  # no user, as from an automation
    assert b._attr_entity_category == "diagnostic"
    asyncio.run(b.async_press())
    assert hub.pressed == [("sim_ring", 20)]
    hub.ok = False  # a real call or ring in progress
    with pytest.raises(_HAError):
        asyncio.run(b.async_press())


@pytest.mark.parametrize("user_id, is_admin, allowed", [
    ("u1", True, True),       # administrator
    ("u1", False, False),     # regular user, even one in allowed_users
    ("ghost", True, False),   # id that no longer resolves to a user
    (None, False, True),      # no user: automation or system call, like the admin service
])
def test_test_ring_button_is_admin_only_like_the_service(user_id, is_admin, allowed):
    from homeassistant.exceptions import Unauthorized

    async def get_user(uid):
        return {"u1": types.SimpleNamespace(is_admin=is_admin)}.get(uid)

    hub = _Hub()
    b = button.VimarTestRingButton(hub, "e1")
    b.hass = types.SimpleNamespace(auth=types.SimpleNamespace(async_get_user=get_user))
    b._context = types.SimpleNamespace(user_id=user_id)
    if allowed:
        asyncio.run(b.async_press())
    else:
        with pytest.raises(Unauthorized):
            asyncio.run(b.async_press())
    assert hub.pressed == ([("sim_ring", 20)] if allowed else [])  # no fake ring when refused


def test_answer_button_stops_following_the_hub_when_removed():
    hub = _Hub()
    b = button.VimarAnswerButton(hub, "e1")
    asyncio.run(b.async_added_to_hass())
    assert hub.state_cbs == [b._on_state_change]
    asyncio.run(b.async_will_remove_from_hass())
    assert hub.state_cbs == []


def test_actuator_slug_never_empty():
    assert button._slug("", "!!!") == "act"


# ─── camera ──────────────────────────────────────────────────────────────────

def test_camera_is_added_and_stops_the_stream_when_the_video_ends(monkeypatch):
    hub = _Hub()
    (cam,) = _setup(camera, hub)
    assert cam._attr_unique_id == "e1_camera" and cam.available is True
    # Camera's own async_added_to_hass: the stub base has none, add one for the test.
    base_added = []

    async def base_added_to_hass(self):
        base_added.append(self)

    monkeypatch.setattr(camera.Camera, "async_added_to_hass", base_added_to_hass, raising=False)
    removers = []
    cam.async_on_remove = removers.append
    asyncio.run(cam.async_added_to_hass())
    assert base_added == [cam] and hub.video_end_cbs == [cam._stop_stream]
    removers[0]()  # entity removed: the callback goes away with it
    assert hub.video_end_cbs == []


def test_camera_leaves_a_missing_or_recording_stream_alone():
    cam = camera.VimarIntercomCamera(_Hub(), "e1", types.SimpleNamespace())
    tasks = []
    cam.hass = types.SimpleNamespace(async_create_task=tasks.append)
    cam.stream = None
    cam._stop_stream()
    cam.stream = types.SimpleNamespace(outputs=lambda: {"recorder": object()})
    cam._stop_stream()
    assert tasks == []


def test_camera_image_during_a_call_is_the_next_frame(monkeypatch):
    hub = _Hub()
    hub.video_active = True
    asked = []

    async def wait_frame(timeout=6, after=0):
        asked.append(after)
        return b"JPEG"

    monkeypatch.setattr(camera.frame_grabber, "wait_frame", wait_frame)
    cam = camera.VimarIntercomCamera(hub, "e1", types.SimpleNamespace())
    assert cam.is_streaming is True
    assert asyncio.run(cam.async_camera_image()) == b"JPEG"
    assert asked == [1]  # not the first, dark IDR


# ─── sensor ──────────────────────────────────────────────────────────────────

def test_one_sensor_per_description():
    entities = _setup(sensor, _Hub())
    assert _ids(entities) == [f"e1_{d.key}" for d in sensor.SENSORS]


def _desc(key):
    return next(d for d in sensor.SENSORS if d.key == key)


def test_status_sensor_attributes_describe_the_connection():
    hub = _Hub(register_failures=2)
    s = sensor.VimarStatSensor(hub, "e1", _desc("status"))
    attrs = s.extra_state_attributes
    assert attrs["registrato"] is True and attrs["proxy"] == "192.0.2.10"
    assert attrs["transport"] == "udp" and attrs["registrazioni_fallite"] == 2


def test_a_sensor_whose_value_or_attributes_fail_shows_nothing():
    def boom(hub):
        raise KeyError("missing")

    desc = sensor.VimarSensorDescription(key="x", name="X", value_fn=boom, attrs_fn=boom)
    s = sensor.VimarStatSensor(_Hub(), "e1", desc)
    assert s.native_value is None and s.extra_state_attributes is None
    plain = sensor.VimarStatSensor(_Hub(), "e1", sensor.VimarSensorDescription(
        key="y", name="Y", value_fn=lambda hub: 3))
    assert plain.native_value == 3 and plain.extra_state_attributes is None


def _restoring(desc, hub, *, data=None, last=None):
    s = sensor.VimarStatSensor(hub, "e1", desc)
    s.entity_id = f"sensor.{desc.key}"

    async def last_data():
        return data

    async def last_state():
        return last

    s.async_get_last_sensor_data = last_data
    s.async_get_last_state = last_state
    return s


def test_nothing_saved_means_nothing_restored():
    hub = _Hub()
    s = _restoring(_desc("last_door"), hub, data=None)
    asyncio.run(s.async_added_to_hass())
    assert s.native_value is None and s._restored_attrs is None
    s = _restoring(_desc("last_door"), hub, data=types.SimpleNamespace(native_value=None))
    asyncio.run(s.async_added_to_hass())
    assert s.native_value is None


def test_a_restored_value_without_a_saved_state_keeps_the_live_attributes():
    hub = _Hub()
    s = _restoring(_desc("last_door"), hub, data=types.SimpleNamespace(native_value="ieri"), last=None)
    asyncio.run(s.async_added_to_hass())
    assert s.native_value == "ieri" and s._restored_attrs is None


def test_a_restored_value_without_attributes_restores_none():
    desc = sensor.VimarSensorDescription(key="z", name="Z", value_fn=lambda hub: None, restore=True)
    s = _restoring(desc, _Hub(), data=types.SimpleNamespace(native_value=5),
                   last=types.SimpleNamespace(attributes={"a": 1}))
    asyncio.run(s.async_added_to_hass())
    assert s.native_value == 5 and s._restored_attrs is None


def test_the_device_list_is_seeded_from_the_saved_attributes():
    hub = _Hub()
    seeded = []
    hub.restore_devices = seeded.append
    s = _restoring(_desc("devices"), hub, last=types.SimpleNamespace(attributes={"dispositivi": ["a"]}))
    asyncio.run(s.async_added_to_hass())
    assert seeded == [["a"]]
    # Nothing saved yet (first start): nothing to seed.
    s = _restoring(_desc("devices"), hub, last=None)
    asyncio.run(s.async_added_to_hass())
    assert seeded == [["a"]]


def test_a_device_list_that_does_not_restore_does_not_stop_the_sensor():
    hub = _Hub()

    def restore_devices(items):
        raise ValueError("bad saved list")

    hub.restore_devices = restore_devices
    s = _restoring(_desc("devices"), hub, last=types.SimpleNamespace(attributes={"dispositivi": 1}))
    asyncio.run(s.async_added_to_hass())
    assert hub.state_cbs == [s._on_state_change]  # still follows the hub


def test_a_sensor_writes_its_state_on_hub_changes_until_removed():
    hub = _Hub()
    s = sensor.VimarStatSensor(hub, "e1", _desc("ring_count"))
    writes = []
    s.async_write_ha_state = lambda: writes.append(True)
    asyncio.run(s.async_added_to_hass())
    hub.state_cbs[0]()
    asyncio.run(s.async_will_remove_from_hass())
    assert writes == [True] and hub.state_cbs == []


# ─── select ──────────────────────────────────────────────────────────────────

def test_the_voicemail_delay_select_appears_once_the_panel_declares_its_values(tmp_path):
    hub = _Hub()
    entry = _entry()
    added = []

    def add(entities, update_before_add=False):
        added.extend(entities)

    asyncio.run(select.async_setup_entry(_hass(hub, tmp_path), entry, add))
    assert _ids(added) == ["e1_away_file"]
    assert (tmp_path / "citofono" / "messaggi").is_dir()
    hub.state_cbs[0]()  # a state change without the values: still nothing
    assert len(added) == 1
    hub.stats["vm_timeout_values"] = [10, 20]
    hub.state_cbs[0]()
    assert _ids(added) == ["e1_away_file", "e1_vm_timeout"] and hub.state_cbs == []
    entry.unloads[0]()  # unloading after it was added: nothing left to remove
    assert hub.state_cbs == []


def test_a_media_folder_that_cannot_be_created_only_warns(caplog):
    hub = _Hub()

    async def executor(f, *a):
        raise PermissionError("read-only")

    hass = types.SimpleNamespace(data={DOMAIN: {"e1": {"hub": hub}}}, async_add_executor_job=executor,
                                 config=types.SimpleNamespace(media_dirs={}))
    added = []
    with caplog.at_level(logging.WARNING):
        asyncio.run(select.async_setup_entry(hass, _entry(), lambda e, u=False: added.extend(e)))
    assert _ids(added) == ["e1_away_file"] and "Cartella messaggi non creata" in caplog.text

    s = added[0]
    s.hass = hass
    with caplog.at_level(logging.WARNING):
        asyncio.run(s.async_update())
    assert "Cartella messaggi non leggibile" in caplog.text


def test_voicemail_delay_select_sets_the_panel_value(ha_error, monkeypatch):
    monkeypatch.setattr(R, "PICG_TARGET", "55010")
    hub = _Hub(vm_timeout_values=[10, 20], vm_timeout=20)
    s = select.VimarVmTimeoutSelect(_entry(), hub)
    writes = []
    s.async_write_ha_state = lambda: writes.append(True)
    assert s.options == ["10", "20"] and s.current_option == "20" and s.available
    asyncio.run(s.async_added_to_hass())
    hub.state_cbs[0]()
    asyncio.run(s.async_select_option("10"))
    assert hub.pressed == [("apt", "vm_timeout", 10)] and writes == [True, True]
    with pytest.raises(_HAError) as err:
        asyncio.run(s.async_select_option("15"))
    assert (err.value.translation_key, err.value.translation_placeholders) == ("vm_timeout_not_allowed", {"option": "15"})
    hub.ok = False
    with pytest.raises(_HAError) as err:
        asyncio.run(s.async_select_option("20"))
    assert (err.value.translation_key, err.value.translation_placeholders) == ("vm_timeout_failed", {"error": "Timeout"})

    async def queued(name, value):
        return False, select.QUEUED

    hub.async_set_apt_param = queued
    with pytest.raises(_HAError) as err:
        asyncio.run(s.async_select_option("20"))
    assert err.value.translation_key == "command_queued"
    asyncio.run(s.async_will_remove_from_hass())
    assert hub.state_cbs == []


def test_voicemail_delay_unknown_value_shows_no_option():
    hub = _Hub(vm_timeout_values=[10], vm_timeout=99)
    assert select.VimarVmTimeoutSelect(_entry(), hub).current_option is None


# ─── switch, text ────────────────────────────────────────────────────────────

def test_switches_address_the_sga(monkeypatch):
    monkeypatch.setattr(R, "SGA_TARGET", "55042")
    entities = _setup(switch, _Hub())
    assert _ids(entities) == ["e1_segreteria", "e1_dnd"]
    assert [e._target for e in entities] == ["55042", "55042"]


def test_a_switch_stops_following_the_hub_and_its_timer_when_removed():
    hub = _Hub()
    (_, dnd) = _setup(switch, hub)
    dnd.async_write_ha_state = lambda: None

    async def run():
        await dnd.async_added_to_hass()
        await dnd.async_turn_on()  # pending confirmation: a timer runs
        assert dnd._expire_handle is not None
        await dnd.async_will_remove_from_hass()

    async def last_state():
        return None

    async def request_status(after_command=False):
        return None

    dnd.async_get_last_state = last_state
    hub.async_request_status = request_status
    asyncio.run(run())
    assert hub.state_cbs == [] and dnd._expire_handle is None


def test_a_confirmation_timeout_after_the_panel_answered_does_not_warn(caplog):
    hub = _Hub(dnd=True)
    (_, dnd) = _setup(switch, hub)
    writes = []
    dnd.async_write_ha_state = lambda: writes.append(dnd._pending)
    dnd._expire()  # nothing pending: nothing to do
    assert writes == []
    dnd._pending = (False, 0.0, 0)
    with caplog.at_level(logging.WARNING):
        dnd._expire()
    assert writes == [None] and "non ha confermato" not in caplog.text


def test_the_text_entity_is_added():
    added = []
    asyncio.run(text.async_setup_entry(None, _entry(), added.extend))
    assert _ids(added) == ["e1_away_text"]


# ─── names and entity_ids (#178) ─────────────────────────────────────────────

# The entity_id a new install gets, in every language: the one the Italian names gave
# before they were translated. Docs, packages/vimar_intercom.yaml and the card use them.
LEGACY_IDS = {
    "e1_sip_registered": "binary_sensor.vimar_intercom_intercom_sip",
    "e1_in_call": "binary_sensor.vimar_intercom_intercom_in_call",
    "e1_ringing": "binary_sensor.vimar_intercom_intercom_squillo",
    "e1_calling": "binary_sensor.vimar_intercom_intercom_chiamata_in_uscita",
    "e1_new_videomessage": "binary_sensor.vimar_intercom_intercom_nuovo_videomessaggio",
    "e1_call": "button.vimar_intercom_chiama",
    "e1_call_ext": "button.vimar_intercom_chiama_video_esterno",
    "e1_call_int": "button.vimar_intercom_chiama_casa_interno",
    "e1_answer": "button.vimar_intercom_rispondi",
    "e1_decline": "button.vimar_intercom_rifiuta",
    "e1_hangup": "button.vimar_intercom_riaggancia",
    "e1_door_street": "button.vimar_intercom_apri_porta",
    "e1_test_ring": "button.vimar_intercom_squillo_di_prova",
    "e1_camera": "camera.vimar_intercom_intercom",
    "e1_doorbell": "event.vimar_intercom_doorbell",
    "e1_fuoriporta": "event.vimar_intercom_fuoriporta",
    "e1_lock": "lock.vimar_intercom_serratura",
    "e1_vm_timeout": "select.vimar_intercom_segreteria_ritardo",
    "e1_away_file": "select.vimar_intercom_segreteria_file_audio",
    "e1_devices": "sensor.vimar_intercom_intercom_dispositivi",
    "e1_status": "sensor.vimar_intercom_intercom_stato",
    "e1_last_caller": "sensor.vimar_intercom_intercom_ultimo_chiamante",
    "e1_last_ring": "sensor.vimar_intercom_intercom_ultimo_squillo",
    "e1_ring_count": "sensor.vimar_intercom_intercom_squilli",
    "e1_call_count": "sensor.vimar_intercom_intercom_chiamate",
    "e1_last_call_duration": "sensor.vimar_intercom_intercom_durata_ultima_chiamata",
    "e1_last_door": "sensor.vimar_intercom_intercom_ultima_apertura",
    "e1_last_command": "sensor.vimar_intercom_intercom_ultimo_comando",
    "e1_last_message_in": "sensor.vimar_intercom_intercom_ultimo_messaggio_ricevuto",
    "e1_vm_level": "sensor.vimar_intercom_intercom_spazio_segreteria",
    "e1_rubrica_ver": "sensor.vimar_intercom_intercom_versione_rubrica",
    "e1_last_missed_call": "sensor.vimar_intercom_intercom_ultima_chiamata_persa",
    "e1_segreteria": "switch.vimar_intercom_segreteria",
    "e1_dnd": "switch.vimar_intercom_non_disturbare",
    "e1_away_text": "text.vimar_intercom_segreteria_testo_del_messaggio",
}
BASE = Path(__file__).resolve().parents[1] / "custom_components" / "vimar_intercom"


def _every_entity(tmp_path):
    hub = _Hub(vm_timeout_values=[10, 20])
    hass = _hass(hub, tmp_path)
    entities = []
    for module in (binary_sensor, button, camera, event, lock, sensor, switch):
        entities += _setup(module, hub, hass=hass)
    selects = _setup(select, hub, hass=hass)  # the delay select comes with the panel's values
    for cb in list(hub.state_cbs):
        cb()
    entities += selects
    asyncio.run(text.async_setup_entry(None, _entry(), entities.extend))
    return {e._attr_unique_id: e for e in entities}


def test_new_installs_get_the_same_entity_ids_in_every_language(tmp_path):
    found = {uid: e.entity_id for uid, e in _every_entity(tmp_path).items()}
    assert found == LEGACY_IDS


def test_every_entity_is_named_by_a_translation(tmp_path):
    tables = {name: json.loads((BASE / name).read_text(encoding="utf-8"))["entity"]
              for name in ("strings.json", "translations/en.json", "translations/it.json")}
    for uid, e in _every_entity(tmp_path).items():
        assert e._attr_has_entity_name is True, uid
        if uid == "e1_camera":  # the device's main entity: named after the device
            assert e._attr_name is None
            continue
        # No fixed name to hide the translation (the stub base answers any hasattr).
        assert "_attr_name" not in vars(e) and not any("_attr_name" in vars(c) for c in type(e).__mro__), uid
        domain, key = e.entity_id.split(".")[0], e._attr_translation_key
        for name, table in tables.items():
            assert table[domain][key]["name"], (name, uid)
    assert tables["strings.json"] == tables["translations/en.json"]  # HA's English source


def test_actuator_buttons_keep_the_name_from_the_options():
    entry = _entry(options={"actuators": [{"name": "Cancello", "msg": "OPEN_1", "target": "AUTO", "icon": "door"}]})
    act = _setup(button, _Hub(), entry)[-1]
    assert act._attr_has_entity_name is True and act._attr_name == "Cancello"


def test_enum_sensor_states_are_translated():
    """An enum sensor shows its raw state ("in_call") unless every option has a translation."""
    tables = {name: json.loads((BASE / name).read_text(encoding="utf-8"))["entity"]["sensor"]
              for name in ("strings.json", "translations/en.json", "translations/it.json")}
    enums = [d for d in sensor.SENSORS if d.options]
    assert enums
    for d in enums:
        for name, table in tables.items():
            assert set(table[d.key]["state"]) == set(d.options), (name, d.key)


def test_every_attribute_has_a_translated_name(tmp_path, monkeypatch):
    """Attribute keys stay as they are (automations read them); Home Assistant shows them by
    the name under the entity's translation_key, in the user's language."""
    from custom_components.vimar_intercom import hub as hub_mod
    entities = _every_entity(tmp_path)
    hub = next(e for e in entities.values() if hasattr(e, "_hub"))._hub
    hub.devices, hub.devices_public, hub.devices_summary = [], [], []
    hub.ring_media = types.MethodType(hub_mod.VimarIntercomHub.ring_media, hub)
    tables = {name: json.loads((BASE / name).read_text(encoding="utf-8"))["entity"]
              for name in ("strings.json", "translations/en.json", "translations/it.json")}
    seen = 0
    for uid, e in entities.items():
        try:
            attrs = e.extra_state_attributes
        except AttributeError:  # the stub base: an entity without attributes of its own
            continue
        if not isinstance(attrs, dict):
            continue
        domain = e.entity_id.split(".")[0]
        for name, table in tables.items():
            names = table[domain][e._attr_translation_key].get("state_attributes", {})
            missing = set(attrs) - set(names)
            assert not missing, (name, uid, sorted(missing))
            assert all(names[a]["name"] for a in attrs), (name, uid)
        seen += len(attrs)
    assert seen > 60  # every entity with attributes was reached (none fell back to None)
    for d in sensor.SENSORS:
        if d.attrs_fn:
            assert isinstance(d.attrs_fn(hub), dict), d.key
