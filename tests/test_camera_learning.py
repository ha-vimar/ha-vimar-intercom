"""The video panel is learned from the plant when the default does not exist.

55100 is the reference plant's video panel. On a 2FV2 the same call answers
404 and auto-call never starts. The panel that last rang is certainly there
and sends video, so it is tried once and remembered, unless the user chose a
panel explicitly.
"""
import asyncio

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")
sip, R = hub_mod.sip, hub_mod.R


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    monkeypatch.setattr(R, "SIP_DOMAIN", "p", raising=False)
    monkeypatch.setattr(R, "CAMERA_TARGET", "55100")
    monkeypatch.setattr(R, "CAMERA_TARGET_CONFIGURED", False)
    saved = []
    h.set_persist_callback(saved.append)
    return h, saved


def fake_panels(monkeypatch, existing):
    calls = []

    async def do_call(target=None):
        calls.append(target)
        return (True, "Connesso!") if target in existing else (False, "404 Not Found")

    monkeypatch.setattr(sip, "do_call", do_call)
    return calls


def test_a_missing_default_falls_back_to_the_panel_that_rang(hub, monkeypatch):
    h, saved = hub
    calls = fake_panels(monkeypatch, {"sip:55001@p"})
    h._last_ring_panel = "55001"
    asyncio.run(h._do_auto_call(None))
    assert calls == ["sip:55100@p", "sip:55001@p"]
    assert R.CAMERA_TARGET == "55001"
    assert saved == [{"learned_camera_target": "55001"}]


def test_a_chosen_panel_is_never_replaced(hub, monkeypatch):
    h, saved = hub
    monkeypatch.setattr(R, "CAMERA_TARGET_CONFIGURED", True)
    calls = fake_panels(monkeypatch, {"sip:55001@p"})
    h._last_ring_panel = "55001"
    asyncio.run(h._do_auto_call(None))
    assert calls == ["sip:55100@p"] and saved == []


def test_without_a_ring_there_is_nothing_to_learn_from(hub, monkeypatch):
    h, saved = hub
    calls = fake_panels(monkeypatch, set())
    asyncio.run(h._do_auto_call(None))
    assert calls == ["sip:55100@p"] and saved == []


def test_a_busy_panel_is_not_a_missing_one(hub, monkeypatch):
    h, saved = hub

    async def busy(target=None):
        return False, "486 Busy Here"

    monkeypatch.setattr(sip, "do_call", busy)
    h._last_ring_panel = "55001"
    asyncio.run(h._do_auto_call(None))
    assert R.CAMERA_TARGET == "55100" and saved == []


def test_the_learned_panel_survives_a_restart():
    runtime = pytest.importorskip("custom_components.vimar_intercom.runtime")
    runtime.configure({"sip_user": "1", "sip_domain": "d", "learned_camera_target": "55001"})
    assert runtime.CAMERA_TARGET == "55001" and not runtime.CAMERA_TARGET_CONFIGURED
    runtime.configure({"sip_user": "1", "sip_domain": "d", "learned_camera_target": "55001",
                       "camera_target": "55200"})
    assert runtime.CAMERA_TARGET == "55200" and runtime.CAMERA_TARGET_CONFIGURED
