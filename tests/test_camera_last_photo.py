"""Between calls the camera image is the last ring photo, not an error.

HA turns a None image into a 500/502: a broken tile on dashboards without the
custom card. A thumbnail must still never place a call.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import types

import pytest

from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import ring_log

R = hub_mod.R
sip = hub_mod.sip


@pytest.fixture
def camera(monkeypatch):
    cam_mod = sys.modules["homeassistant.components.camera"]
    monkeypatch.setattr(cam_mod, "Camera", type("Camera", (), {"__init__": lambda self: None}),
                        raising=False)
    monkeypatch.setattr(cam_mod, "CameraEntityFeature", types.SimpleNamespace(STREAM=2),
                        raising=False)
    monkeypatch.delitem(sys.modules, "custom_components.vimar_intercom.camera", raising=False)
    return importlib.import_module("custom_components.vimar_intercom.camera")


@pytest.fixture
def cam(camera, monkeypatch):
    monkeypatch.setattr(sip, "in_call", False, raising=False)
    monkeypatch.setattr(sip, "calling", False, raising=False)
    calls = []

    async def do_call(target=None, **_kw):
        calls.append(target)
        return True, "200"

    monkeypatch.setattr(sip, "do_call", do_call)
    hass = types.SimpleNamespace(
        async_add_executor_job=lambda f, *a: asyncio.get_running_loop().run_in_executor(None, f, *a))
    c = camera.VimarIntercomCamera(hub_mod.VimarIntercomHub(), "e1", hass)
    c.calls = calls
    return c


def test_between_calls_the_last_ring_photo_is_returned(cam, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
    ring_log.write_photo(str(tmp_path), "squillo_20260929_101010.jpg", b"\xff\xd8photo")
    cam._hub.stats["last_photo_path"] = str(tmp_path / "squillo_20260929_101010.jpg")
    assert asyncio.run(cam.async_camera_image()) == b"\xff\xd8photo"
    assert cam.calls == []


def test_after_a_restart_the_copy_of_the_last_photo_is_used(cam, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
    (tmp_path / ring_log.LAST_PHOTO).write_bytes(b"\xff\xd8last")
    assert cam._hub.stats["last_photo_path"] is None
    assert asyncio.run(cam.async_camera_image()) == b"\xff\xd8last"


def test_without_a_photo_the_image_is_none_and_nothing_is_called(cam, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "SNAPSHOT_DIR", "")
    assert asyncio.run(cam.async_camera_image()) is None
    monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
    cam._hub.stats["last_photo_path"] = str(tmp_path / "gone.jpg")
    assert asyncio.run(cam.async_camera_image()) is None
    assert cam.calls == []
