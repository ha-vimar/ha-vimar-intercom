"""Setting up and unloading the entry (__init__.py) and the services it registers,
on stub HA objects: a fake hub, fake registries, a fake hass that records calls."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types

import pytest
import requests
from harness.web import load_views

from custom_components.vimar_intercom import away_tts, runtime, webhook
from custom_components.vimar_intercom import media_handler as media


class FakeHub:
    """Stands in for VimarIntercomHub: records what setup wires into it."""

    instances: list = []
    start_error: Exception | None = None

    def __init__(self):
        self.started_with = None
        self.stopped = False
        self.persist = self.model_cb = self.event_cb = self.broadcast = None
        self.calls: list = []
        self.rings = 0
        self.picg_result = {"ok": True, "picg": "55000", "probes": [1, 2]}
        FakeHub.instances.append(self)

    def set_persist_callback(self, cb):
        self.persist = cb

    def register_model_callback(self, cb):
        self.model_cb = cb

    def register_event_callback(self, cb):
        self.event_cb = cb

    def set_ws_broadcast(self, cb):
        self.broadcast = cb

    async def async_start(self, store):
        self.started_with = store
        if self.start_error:
            raise self.start_error

    async def async_stop(self):
        self.stopped = True

    async def async_send_command(self, **kw):
        self.calls.append(("send_command", kw))
        return True, "200 OK"

    async def async_call(self, target=None):
        self.calls.append(("call", target))
        return True, "calling"

    async def async_answer(self):
        self.calls.append(("answer",))
        return True, "answered"

    async def async_decline(self):
        self.calls.append(("decline",))
        return False, "no ring"

    async def async_hangup(self):
        self.calls.append(("hangup",))

    async def async_door(self, target=None, command=None):
        self.calls.append(("door", target, command))
        return True, "opened", 200

    def simulate_ring(self, duration):
        self.rings += 1
        self.calls.append(("simulate_ring", duration))
        return True

    async def async_find_picg(self, targets, **kw):
        self.calls.append(("find_picg", targets, kw))
        return dict(self.picg_result)


class FakeServices:
    def __init__(self):
        self.handlers: dict = {}
        self.admin: set = set()
        self.removed: list = []

    def has_service(self, domain, name):
        return (domain, name) in self.handlers

    def async_register(self, domain, name, handler, schema=None, supports_response=None):
        self.handlers[(domain, name)] = handler

    def async_remove(self, domain, name):
        self.removed.append(name)
        self.handlers.pop((domain, name), None)


class FakeEntry(types.SimpleNamespace):
    def __init__(self, data=None, options=None, entry_id="e1"):
        super().__init__(entry_id=entry_id, data=dict(data or {}), options=dict(options or {}),
                         listeners=[], unloads=[])

    def add_update_listener(self, listener):
        self.listeners.append(listener)
        return "remove-listener"

    def async_on_unload(self, fn):
        self.unloads.append(fn)


class FakeDevice(types.SimpleNamespace):
    pass


def _hass(tmp_path, entries=()):
    updates: list = []
    forwarded: list = []
    views: list = []
    static: list = []
    fired: list = []

    def async_update_entry(entry, data=None, options=None):
        updates.append({"data": data, "options": options})
        if data is not None:
            entry.data = data
        if options is not None:
            entry.options = options
        return True

    async def async_forward_entry_setups(entry, platforms):
        forwarded.append(list(platforms))

    async def async_unload_platforms(entry, platforms):
        return hass.unload_ok

    async def async_register_static_paths(paths):
        static.extend(paths)

    async def executor(f, *a):
        return f(*a)

    hass = types.SimpleNamespace(
        data={}, unload_ok=True, updates=updates, forwarded=forwarded, views=views, static=static,
        fired=fired, services=FakeServices(),
        config_entries=types.SimpleNamespace(
            async_update_entry=async_update_entry,
            async_forward_entry_setups=async_forward_entry_setups,
            async_unload_platforms=async_unload_platforms,
            async_entries=lambda domain: list(entries)),
        http=types.SimpleNamespace(async_register_static_paths=async_register_static_paths,
                                   register_view=views.append),
        bus=types.SimpleNamespace(async_fire=lambda t, d: fired.append((t, d))),
        config=types.SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p))),
        async_add_executor_job=executor)
    return hass


@pytest.fixture
def init(monkeypatch):
    mod = load_views(monkeypatch)
    FakeHub.instances = []
    monkeypatch.setattr(mod, "VimarIntercomHub", FakeHub)
    monkeypatch.setattr(mod, "Store", lambda hass, version, key: ("store", key))
    monkeypatch.setattr(mod, "StaticPathConfig", lambda url, path, cache: (url, path, cache))
    js_urls: list = []
    monkeypatch.setattr(mod, "add_extra_js_url", lambda hass, url: js_urls.append(url))
    mod.js_urls = js_urls

    def admin_service(hass, domain, name, handler, schema=None, supports_response=None):
        hass.services.async_register(domain, name, handler)
        hass.services.admin.add(name)
    monkeypatch.setattr(sys.modules["custom_components.vimar_intercom.services"],
                        "async_register_admin_service", admin_service)

    removed: list = []
    registry = types.SimpleNamespace(
        async_get_entity_id=lambda domain, platform, uid: (
            f"switch.{uid}" if uid.endswith("_away_message") and mod.orphan else None),
        async_remove=removed.append)
    mod.orphan, mod.removed_entities = True, removed
    monkeypatch.setattr(mod, "er", types.SimpleNamespace(async_get=lambda hass: registry))

    device_updates: list = []
    devices = types.SimpleNamespace(device=None)
    dev_registry = types.SimpleNamespace(
        async_get_device=lambda identifiers: devices.device,
        async_update_device=lambda device_id, **kw: device_updates.append((device_id, kw)))
    monkeypatch.setattr(mod, "dr", types.SimpleNamespace(async_get=lambda hass: dev_registry))
    mod.devices, mod.device_updates = devices, device_updates

    # setup() of these modules keeps hass in a module global: put it back afterwards.
    monkeypatch.setattr(away_tts, "_hass", away_tts._hass)
    monkeypatch.setattr(webhook, "_hass", webhook._hass)
    monkeypatch.setattr(media, "ws_send_bytes", media.ws_send_bytes)
    return mod


def _setup(init, hass, entry):
    assert asyncio.run(init.async_setup_entry(hass, entry)) is True
    return FakeHub.instances[-1]


def _entry(**data):
    base = {"sip_user": "1001", "sip_password": "pw", "sip_domain": "example.invalid",
            "device_imei": "imei-test", "device_uuid": "uuid-test", "av_key": "av-key-test"}
    base.update(data)
    return FakeEntry(data=base)


# ─── async_setup_entry ─────────────────────────────────────────────────────

def test_setup_wires_the_hub_views_services_card_and_platforms(init, tmp_path):
    hass = _hass(tmp_path)
    entry = FakeEntry(data={"sip_user": "1001", "sip_domain": "example.invalid"},
                      options={"camera_target": "55100"})
    hub = _setup(init, hass, entry)

    # An entry without identity gets one, saved in its data.
    assert entry.data["device_imei"] and entry.data["device_uuid"]
    assert runtime.SIP_USER == "1001"
    stored = hass.data[init.DOMAIN]["e1"]
    assert stored["hub"] is hub and stored["applied"] == {"camera_target": "55100"}
    assert stored["audio_ws_clients"] == set()
    assert hub.started_with == ("store", f"{init.DOMAIN}.e1.sps_pps")
    assert init.removed_entities == ["switch.e1_away_message"]
    assert hass.forwarded == [init.PLATFORMS]
    assert entry.listeners == [init._async_update_listener] and entry.unloads == ["remove-listener"]
    assert {type(v).__name__ for v in hass.views} == {
        "VimarAVStreamView", "VimarAudioWSView", "VimarDebugView", "VimarRingsView", "VimarRingPhotoView",
        "VimarAwayUploadView"}
    # The card: www/ served as a folder twice, plain (CARD_URL stays valid) and under a version
    # segment, which the frontend loads so every module of the card is fetched fresh.
    www = os.path.join(os.path.dirname(init.__file__), "www")
    v = int(max(os.path.getmtime(os.path.join(www, f)) for f in os.listdir(www) if f.endswith(".js")))
    assert [(p[0], p[1]) for p in hass.static] == [(init.CARD_DIR, www), (f"{init.CARD_DIR}/{v}", www)]
    assert os.path.isfile(os.path.join(www, init.CARD_URL.rpartition("/")[2]))
    assert init.js_urls == [f"{init.CARD_DIR}/{v}/vimar-intercom-card.js"]
    names = {n for _, n in hass.services.handlers}
    assert names == {init.SERVICE_SEND_COMMAND, init.SERVICE_CALL, init.SERVICE_ANSWER,
                     init.SERVICE_DECLINE, init.SERVICE_HANGUP, init.SERVICE_OPEN_DOOR,
                     init.SERVICE_FETCH_LOCAL, init.SERVICE_SIMULATE_RING, init.SERVICE_FIND_SGA}
    # Anything that sends arbitrary messages or carries the SIP password is admin only.
    assert hass.services.admin == {init.SERVICE_SEND_COMMAND, init.SERVICE_FETCH_LOCAL,
                                   init.SERVICE_SIMULATE_RING, init.SERVICE_FIND_SGA}


def test_a_second_setup_registers_the_card_and_services_once(init, tmp_path):
    hass = _hass(tmp_path)
    _setup(init, hass, _entry())
    handlers = dict(hass.services.handlers)
    init.orphan = False
    _setup(init, hass, _entry())
    assert len(hass.static) == 2 and len(init.js_urls) == 1
    assert hass.services.handlers == handlers
    assert init.removed_entities == ["switch.e1_away_message"]


def test_an_entry_with_identity_is_not_rewritten_at_setup(init, tmp_path):
    hass = _hass(tmp_path)
    _setup(init, hass, _entry())
    assert hass.updates == []


def test_an_entry_without_an_av_key_gets_one_once_and_it_is_not_logged(init, tmp_path, caplog):
    """#63: the key is stored in the entry data (no reload) and used by runtime."""
    hass = _hass(tmp_path)
    entry = _entry()
    del entry.data["av_key"]
    with caplog.at_level("DEBUG"):
        _setup(init, hass, entry)
    key = entry.data["av_key"]
    assert len(key) == 32 and init.runtime.AV_KEY == key
    assert len(hass.updates) == 1 and hass.updates[0]["options"] is None
    assert key not in caplog.text
    _setup(init, hass, entry)
    assert entry.data["av_key"] == key and len(hass.updates) == 1


def test_setup_retries_when_the_proxy_is_unreachable(init, tmp_path, monkeypatch):
    hass = _hass(tmp_path)
    monkeypatch.setattr(FakeHub, "start_error", OSError("unreachable"))
    with pytest.raises(sys.modules["homeassistant.exceptions"].ConfigEntryNotReady):
        asyncio.run(init.async_setup_entry(hass, _entry()))
    hub = FakeHub.instances[-1]
    assert hub.stopped and hass.data[init.DOMAIN] == {}
    assert hass.forwarded == []


def test_learned_values_are_saved_in_the_entry_data_once(init, tmp_path):
    hass = _hass(tmp_path)
    entry = _entry()
    hub = _setup(init, hass, entry)
    hub.persist({"learned_camera_target": "55001"})
    assert entry.data["learned_camera_target"] == "55001"
    assert entry.options == {}          # data, not options: no reload
    hub.persist({"learned_camera_target": "55001"})
    assert len(hass.updates) == 1


def test_a_detected_model_updates_the_device_and_the_entry(init, tmp_path):
    hass = _hass(tmp_path)
    entry = _entry()
    hub = _setup(init, hass, entry)
    init.devices.device = FakeDevice(id="d1", model="Old", sw_version="1.0")
    hub.model_cb("Elvox Tab 7S", "2.0", "UA/2.0", 3)
    assert init.device_updates == [("d1", {"model": "Elvox Tab 7S", "sw_version": "2.0"})]
    assert entry.data["detected_model"] == "Elvox Tab 7S" and entry.data["detected_priority"] == 3
    # Same values again: neither the device nor the entry is written.
    init.devices.device = FakeDevice(id="d1", model="Elvox Tab 7S", sw_version="2.0")
    hub.model_cb("Elvox Tab 7S", "2.0", "UA/2.0", 3)
    assert len(init.device_updates) == 1 and len(hass.updates) == 1


def test_a_detected_model_without_firmware_keeps_the_device_version(init, tmp_path):
    hass = _hass(tmp_path)
    hub = _setup(init, hass, _entry())
    init.devices.device = FakeDevice(id="d1", model="Old", sw_version="1.0")
    hub.model_cb("Elvox Tab 7S", "", "UA", 3)
    assert init.device_updates == [("d1", {"model": "Elvox Tab 7S"})]


def test_a_detected_model_before_the_device_exists_is_kept_in_the_entry(init, tmp_path):
    hass = _hass(tmp_path)
    entry = _entry()
    hub = _setup(init, hass, entry)
    hub.model_cb("Elvox Tab 5S", None, "UA", 1)
    assert init.device_updates == [] and entry.data["detected_model"] == "Elvox Tab 5S"


def test_hub_events_go_on_the_bus_and_a_failing_bus_is_contained(init, tmp_path):
    hass = _hass(tmp_path)
    hub = _setup(init, hass, _entry())
    hub.event_cb("vimar_intercom_fuoriporta", {"x": 1})
    hub.event_cb("vimar_intercom_ring", None)
    assert hass.fired == [("vimar_intercom_fuoriporta", {"x": 1}), ("vimar_intercom_ring", {})]

    def boom(t, d):
        raise RuntimeError("bus down")
    hass.bus.async_fire = boom
    hub.event_cb("vimar_intercom_ring", {})  # logged, not raised


class _Client:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    async def send_bytes(self, b):
        if self.fail:
            raise ConnectionResetError
        self.sent.append(b)

    async def send_str(self, s):
        if self.fail:
            raise ConnectionResetError
        self.sent.append(s)


def _send_all(*packets, only=None):
    """ws_send_bytes for each packet, then let the per-client senders drain."""
    async def run():
        for p in packets:
            await media.ws_send_bytes(p, only=only)
        await asyncio.sleep(0.01)
    asyncio.run(run())


def test_audio_and_json_go_to_every_client_and_dead_ones_are_dropped(init, tmp_path):
    hass = _hass(tmp_path)
    hub = _setup(init, hass, _entry())
    clients = hass.data[init.DOMAIN]["e1"]["audio_ws_clients"]
    assert hub._has_ws_clients() is False
    good, dead = _Client(), _Client(fail=True)
    clients.update({good, dead})
    assert hub._has_ws_clients() is True

    async def run():
        await media.ws_send_bytes(b"\x01pcm")
        await asyncio.sleep(0.01)
        assert good.sent == [b"\x01pcm"] and clients == {good}
        await media.ws_send_bytes(b"\x01pcm2")  # the dead client's sender goes with it
        assert list(hass.data[init.DOMAIN]["e1"]["ws_senders"]) == [good]
        await asyncio.sleep(0.01)
    asyncio.run(run())
    clients.add(dead)
    asyncio.run(hub.broadcast({"type": "ring"}))
    assert good.sent[-1] == '{"type": "ring"}' and clients == {good}


def test_one_slow_client_does_not_hold_the_others(init, tmp_path):
    """#53, then #54's review: a client that never finishes sending must not delay
    the next packet of anyone. ws_send_bytes only enqueues, so it returns at once and
    the fast client gets every packet, in order."""
    hass = _hass(tmp_path)
    _setup(init, hass, _entry())
    clients = hass.data[init.DOMAIN]["e1"]["audio_ws_clients"]
    fast = _Client()

    class _Stuck:
        async def send_bytes(self, b):
            await asyncio.Event().wait()

    clients.update({_Stuck(), fast})
    packets = [b"\x01" + bytes([i]) for i in range(20)]

    async def run():
        for p in packets:
            await asyncio.wait_for(media.ws_send_bytes(p), 0.05)
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert fast.sent == packets


def test_only_one_client_gets_the_replay(init, tmp_path):
    hass = _hass(tmp_path)
    _setup(init, hass, _entry())
    clients = hass.data[init.DOMAIN]["e1"]["audio_ws_clients"]
    new, old, gone = _Client(), _Client(), _Client()
    clients.update({new, old})
    _send_all(b"\x03gop", only=new)
    _send_all(b"\x03gop", only=gone)  # left before its replay: nothing to send
    assert new.sent == [b"\x03gop"] and old.sent == [] and gone.sent == []


def test_a_socket_that_asked_for_one_media_gets_only_that(init, tmp_path):
    """The card opens one socket per player (?only=video, ?only=audio): each used to get
    the other's media too, so the video went up twice. No ?only: everything."""
    hass = _hass(tmp_path)
    _setup(init, hass, _entry())
    data = hass.data[init.DOMAIN]["e1"]
    video, audio, both = _Client(), _Client(), _Client()
    data["audio_ws_clients"].update({video, audio, both})
    data["ws_wants"].update({video: b"\x03", audio: b"\x01"})
    nal = b"\x03\x00\x00\x00\x01\x67s"
    _send_all(b"\x01pcm", nal)
    assert video.sent == [nal] and audio.sent == [b"\x01pcm"] and both.sent == [b"\x01pcm", nal]


def test_a_client_that_asked_for_it_gets_a_timestamp_before_each_idr(init, tmp_path, monkeypatch):
    """Behind a buffering proxy send_bytes returns at once and the backlog grows in the
    tunnel: a {"type": "video_ts"} text before each IDR lets the card tell it is late.
    Only for ?ts=1, only on live IDRs (a replayed one is old), on a monotonic clock."""
    hass = _hass(tmp_path)
    _setup(init, hass, _entry())
    data = hass.data[init.DOMAIN]["e1"]
    card, app = _Client(), _Client()
    data["audio_ws_clients"].update({card, app})
    data["ws_ts"].add(card)
    nal = b"\x03\x00\x00\x00\x01"
    sps, idr, pf = nal + b"\x67s", nal + b"\x65i", nal + b"\x41p"

    async def run():
        for p in (sps, idr):                        # the GOP replayed to a new client
            await media.ws_send_bytes(p, only=card)
        for p in (b"\x01pcm", sps, idr, pf):        # live
            await media.ws_send_bytes(p)
        await asyncio.sleep(0.01)

    before = init.time.monotonic() * 1000
    asyncio.run(run())
    after = init.time.monotonic() * 1000
    assert card.sent[:2] == [sps, idr], "the replayed IDR gets no timestamp"
    assert card.sent[2:] == [b"\x01pcm", sps, card.sent[4], idr, pf]
    ts = json.loads(card.sent[4])
    assert ts["type"] == "video_ts" and before - 1 <= ts["t"] <= after
    assert app.sent == [b"\x01pcm", sps, idr, pf]


def test_a_full_client_queue_drops_its_backlog_and_waits_for_a_keyframe(init, tmp_path, monkeypatch):
    """A half GOP only smears: a client that fell behind loses its backlog and gets
    video again from the next SPS (sent before each IDR); its audio goes on."""
    monkeypatch.setattr(init, "WS_CLIENT_QUEUE", 3)
    hass = _hass(tmp_path)
    _setup(init, hass, _entry())
    clients = hass.data[init.DOMAIN]["e1"]["audio_ws_clients"]
    slow = _Client()
    clients.add(slow)
    nal = b"\x03\x00\x00\x00\x01"
    p1, p2, p3, sps, idr = nal + b"\x41a", nal + b"\x41b", nal + b"\x41c", nal + b"\x67s", nal + b"\x65i"

    async def run():
        for p in (p1, p2, p3, b"\x01pcm", nal + b"\x41d", sps, idr):
            await media.ws_send_bytes(p)  # nothing drains in between: the queue fills
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert slow.sent == [b"\x01pcm", sps, idr]


# ─── async_unload_entry and the update listener ────────────────────────────

class _WSClient:
    closed = False

    async def close(self):
        self.closed = True


def test_unloading_the_last_entry_closes_clients_and_removes_services(init, tmp_path, monkeypatch):
    hass = _hass(tmp_path)
    hub = _setup(init, hass, _entry())
    ws = _WSClient()
    hass.data[init.DOMAIN]["e1"]["audio_ws_clients"].add(ws)
    stopped = []

    async def passive_stop():
        stopped.append(True)
    monkeypatch.setattr(init.av_passive, "stop", passive_stop)
    sender = types.SimpleNamespace(cancel=lambda: stopped.append("sender"))
    hass.data[init.DOMAIN]["e1"]["ws_senders"][ws] = (None, sender)
    assert asyncio.run(init.async_unload_entry(hass, FakeEntry())) is True
    assert ws.closed and hub.stopped and stopped == ["sender", True]
    assert hass.data[init.DOMAIN] == {} and hass.services.handlers == {}
    assert len(hass.services.removed) == 9


def test_unloading_one_of_two_entries_keeps_the_services(init, tmp_path, monkeypatch):
    hass = _hass(tmp_path)
    _setup(init, hass, _entry())
    hass.data[init.DOMAIN]["e2"] = {"hub": FakeHub()}
    monkeypatch.setattr(init.av_passive, "stop", None)  # must not be called
    assert asyncio.run(init.async_unload_entry(hass, FakeEntry())) is True
    assert list(hass.data[init.DOMAIN]) == ["e2"] and hass.services.removed == []


def test_a_failed_platform_unload_keeps_everything(init, tmp_path):
    hass = _hass(tmp_path)
    hub = _setup(init, hass, _entry())
    hass.unload_ok = False
    assert asyncio.run(init.async_unload_entry(hass, FakeEntry())) is False
    assert not hub.stopped and "e1" in hass.data[init.DOMAIN]


def test_an_away_message_change_applies_without_reload(init):
    notified = []
    hub = types.SimpleNamespace(notify=lambda: notified.append(True))
    reloads = []

    async def async_reload(entry_id):
        reloads.append(entry_id)
    hass = types.SimpleNamespace(
        data={init.DOMAIN: {"e1": {"applied": {}, "hub": hub}}},
        config_entries=types.SimpleNamespace(async_reload=async_reload))
    entry = FakeEntry(options={"away_message_text": "Back soon"})
    asyncio.run(init._async_update_listener(hass, entry))
    assert reloads == [] and notified == [True]
    assert runtime.AWAY_MESSAGE_TEXT == "Back soon"


def test_an_options_change_for_an_unloaded_entry_reloads(init):
    reloads = []

    async def async_reload(entry_id):
        reloads.append(entry_id)
    hass = types.SimpleNamespace(data={}, config_entries=types.SimpleNamespace(async_reload=async_reload))
    asyncio.run(init._async_update_listener(hass, FakeEntry(options={"x": 1})))
    assert reloads == ["e1"]


# ─── services ──────────────────────────────────────────────────────────────

def _call(**data):
    return types.SimpleNamespace(data=data)


@pytest.fixture
def svc(init, tmp_path):
    hass = _hass(tmp_path, entries=[FakeEntry(options={"camera_target": "55100"})])
    hub = _setup(init, hass, _entry())

    def run(name, **data):
        return asyncio.run(hass.services.handlers[(init.DOMAIN, name)](_call(**data)))
    return types.SimpleNamespace(run=run, hub=hub, hass=hass, init=init, tmp_path=tmp_path)


def test_call_services_reach_the_hub_and_report_the_outcome(svc):
    assert svc.run("send_command", body="PING", target="55001", header_name="", header_value="x") == {
        "ok": True, "result": "200 OK"}
    assert svc.hub.calls[-1] == ("send_command", {
        "body": "PING", "target": "55001", "header_name": None, "header_value": "x"})
    assert svc.run("call", target="55002") == {"ok": True, "result": "calling"}
    assert svc.run("answer") == {"ok": True, "result": "answered"}
    assert svc.run("decline") == {"ok": False, "result": "no ring"}
    assert svc.run("hangup") == {"ok": True, "result": "Chiamata terminata"}
    assert svc.run("open_door", target="55003", command="OPEN") == {"ok": True, "result": "opened", "code": 200}
    assert svc.hub.calls[1:] == [("call", "55002"), ("answer",), ("decline",), ("hangup",),
                                 ("door", "55003", "OPEN")]
    assert svc.run("simulate_ring", duration=5.0) == {"ok": True, "result": "Squillo di prova"}
    assert svc.hub.rings == 1 and svc.hub.calls[-1] == ("simulate_ring", 5.0)


def test_the_sip_id_validator_accepts_numbers_and_rejects_the_rest(init):
    assert init._sip_id(" 55001 ") == "55001"
    with pytest.raises(sys.modules["voluptuous"].Invalid):
        init._sip_id("55001;x=1")


_FIND = dict(start="55000", end="55002", targets=None, probe="get_nicks", reply_wait=3.0,
             delay=1.0, sip_timeout=8.0, apply=False, apply_sga=False)


def test_find_sga_reports_what_it_found_without_changing_anything(svc):
    result = svc.run("find_sga", **_FIND)
    assert result["ok"] and result["picg"] == "55000" and result["scanned"] == 2
    assert result["applied"] == {} and svc.hass.updates == []
    _, targets, kw = svc.hub.calls[-1]
    assert targets == ["55000", "55001", "55002"] and kw["probe"] == "GET_NICKS"


def test_find_sga_applies_the_found_picg_when_asked(svc):
    entry = svc.hass.config_entries.async_entries(svc.init.DOMAIN)[0]
    result = svc.run("find_sga", **{**_FIND, "apply": True, "apply_sga": True})
    assert result["applied"] == {"picg_target": "55000", "sga_target": "55000"}
    assert entry.options == {"camera_target": "55100", "picg_target": "55000", "sga_target": "55000"}


def test_find_sga_applies_only_the_picg_when_asked(svc):
    result = svc.run("find_sga", **{**_FIND, "apply": True})
    assert result["applied"] == {"picg_target": "55000"}


def test_find_sga_applies_only_the_sga_when_asked(svc):
    result = svc.run("find_sga", **{**_FIND, "apply_sga": True})
    assert result["applied"] == {"sga_target": "55000"}


def test_find_sga_refuses_a_malformed_picg_from_the_panel(svc):
    svc.hub.picg_result = {"ok": True, "picg": "55000;evil", "probes": []}
    result = svc.run("find_sga", **{**_FIND, "apply": True})
    assert "non valido" in result["error"] and result["applied"] == {}
    assert svc.hass.updates == []


def test_find_sga_without_an_entry_applies_nothing(svc):
    svc.hass.config_entries.async_entries = lambda domain: []
    result = svc.run("find_sga", **{**_FIND, "apply": True})
    assert result["applied"] == {} and result["picg"] == "55000"


def test_find_sga_rejects_a_bad_range(svc):
    result = svc.run("find_sga", **{**_FIND, "start": "abc"})
    assert result["ok"] is False and result["error"]
    assert not any(c[0] == "find_picg" for c in svc.hub.calls)


def test_find_sga_without_a_loaded_hub_says_so(svc):
    svc.hass.data[svc.init.DOMAIN].clear()
    assert svc.run("find_sga", **_FIND) == {"ok": False, "error": "Integrazione non caricata"}


class _Resp:
    status_code = 200
    headers = {"Content-Type": "text/plain"}
    content = b"status=ok"


def test_fetch_local_refuses_a_public_host(svc, monkeypatch):
    monkeypatch.setattr(requests, "get", None)  # must not be called
    result = svc.run("fetch_local", path="rest/x", host="intercom.example.invalid", scheme="http")
    assert result == {"ok": False, "error": "host non consentito: intercom.example.invalid"}


def test_fetch_local_refuses_a_save_as_without_a_file_name(svc, monkeypatch):
    monkeypatch.setattr(requests, "get", None)
    result = svc.run("fetch_local", path="x", host="192.0.2.10", save_as="config/../")
    assert result["ok"] is False and "nome file non valido" in result["error"]


def test_fetch_local_fetches_with_digest_and_saves_the_file(svc, monkeypatch):
    seen = {}

    def get(url, auth=None, timeout=None, headers=None, allow_redirects=None):
        seen.update(url=url, user=auth.username, redirects=allow_redirects)
        return _Resp()
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(runtime, "LOCAL_PROXY", "192.0.2.10")
    result = svc.run("fetch_local", path="/rest/get_info.php?action=status",
                     scheme="https", save_as="status.txt")
    assert seen == {"url": "https://192.0.2.10/rest/get_info.php?action=status",
                    "user": runtime.SIP_USER, "redirects": False}
    saved = svc.tmp_path / svc.init.DOMAIN / "status.txt"
    assert result == {"ok": True, "url": seen["url"], "status": 200, "content_type": "text/plain",
                      "size": 9, "saved": str(saved), "preview": "status=ok"}
    assert saved.read_bytes() == b"status=ok"


def test_fetch_local_without_save_as_saves_nothing(svc, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kw: _Resp())
    result = svc.run("fetch_local", path="x", host="127.0.0.1", scheme="http")
    assert result["ok"] and result["saved"] is None
    assert not (svc.tmp_path / svc.init.DOMAIN).exists()


def test_fetch_local_reports_a_connection_error(svc, monkeypatch):
    def get(url, **kw):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "get", get)
    result = svc.run("fetch_local", path="x", host="192.0.2.2", scheme="http")
    assert result == {"ok": False, "url": "http://192.0.2.2/x", "error": "refused"}

