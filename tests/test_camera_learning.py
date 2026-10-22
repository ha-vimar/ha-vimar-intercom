"""The video panel is learned from the plant when the default does not exist.

55100 is the reference plant's video panel. On a 2FV2 the same call answers
404 and auto-call never starts. The panel that last rang is certainly there
and sends video, so it is tried once and remembered, unless the user chose a
panel explicitly.
"""
import asyncio

import pytest

from custom_components.vimar_intercom import hub as hub_mod

sip, R = hub_mod.sip, hub_mod.R


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(R, "SIP_DOMAIN", "p", raising=False)
    monkeypatch.setattr(R, "CAMERA_TARGET", "55100")
    monkeypatch.setattr(R, "INTERCOM", "sip:55100@p")
    monkeypatch.setattr(R, "CAMERA_TARGET_CONFIGURED", False)
    saved = []
    h.set_persist_callback(saved.append)
    return h, saved


def fake_panels(monkeypatch, existing, failure="404 Not Found"):
    calls = []

    async def do_call(target=None, **_kw):
        target = target or R.INTERCOM
        calls.append(target)
        return (True, "Connesso!") if target in existing else (False, failure)

    monkeypatch.setattr(sip, "do_call", do_call)
    return calls


def test_a_missing_default_falls_back_to_the_panel_that_rang(hub, monkeypatch):
    h, saved = hub
    calls = fake_panels(monkeypatch, {"sip:55001@p"})
    h._last_ring_panel = "55001"
    asyncio.run(h._do_auto_call())
    assert calls == ["sip:55100@p", "sip:55001@p"]
    assert R.CAMERA_TARGET == "55001" and R.INTERCOM == "sip:55001@p"
    assert saved == [{"learned_camera_target": "55001"}]


def test_a_chosen_panel_is_never_replaced(hub, monkeypatch):
    h, saved = hub
    monkeypatch.setattr(R, "CAMERA_TARGET_CONFIGURED", True)
    calls = fake_panels(monkeypatch, {"sip:55001@p"})
    h._last_ring_panel = "55001"
    asyncio.run(h._do_auto_call())
    assert calls == ["sip:55100@p"] and saved == []


def test_without_a_ring_there_is_nothing_to_learn_from(hub, monkeypatch):
    h, saved = hub
    calls = fake_panels(monkeypatch, set())
    asyncio.run(h._do_auto_call())
    assert calls == ["sip:55100@p"] and saved == []


def test_a_busy_panel_is_not_a_missing_one(hub, monkeypatch):
    h, saved = hub
    calls = fake_panels(monkeypatch, {"sip:55001@p"}, failure="486 Busy Here")
    h._last_ring_panel = "55001"
    asyncio.run(h._do_auto_call())
    assert calls == ["sip:55100@p"]
    assert R.CAMERA_TARGET == "55100" and saved == []


def test_the_learned_panel_survives_a_restart(monkeypatch):
    for name in ("CAMERA_TARGET", "CAMERA_TARGET_CONFIGURED", "INTERCOM"):
        monkeypatch.setattr(R, name, getattr(R, name))
    R.configure({"sip_user": "1", "sip_domain": "d", "learned_camera_target": "55001"})
    assert R.CAMERA_TARGET == "55001" and not R.CAMERA_TARGET_CONFIGURED


def test_a_chosen_panel_wins_over_a_learned_one(monkeypatch):
    for name in ("CAMERA_TARGET", "CAMERA_TARGET_CONFIGURED", "INTERCOM"):
        monkeypatch.setattr(R, name, getattr(R, name))
    R.configure({"sip_user": "1", "sip_domain": "d", "camera_target": "55200",
                 "learned_camera_target": "55001"})
    assert R.CAMERA_TARGET == "55200" and R.CAMERA_TARGET_CONFIGURED


def test_a_temporarily_unavailable_panel_is_not_a_missing_one(hub, monkeypatch):
    h, saved = hub
    calls = fake_panels(monkeypatch, {"sip:55001@p"},
                        failure="480 Temporarily Unavailable")
    h._last_ring_panel = "55001"
    asyncio.run(h._do_auto_call())
    assert calls == ["sip:55100@p"]
    assert R.CAMERA_TARGET == "55100" and saved == []


def test_a_panel_refusing_the_media_is_not_a_missing_one(hub, monkeypatch):
    """488 says the panel is there and did not like our SDP (SRTP setting)."""
    h, saved = hub
    calls = fake_panels(monkeypatch, {"sip:55001@p"}, failure="488 Not Acceptable Here")
    h._last_ring_panel = "55001"
    asyncio.run(h._do_auto_call())
    assert calls == ["sip:55100@p"]
    assert R.CAMERA_TARGET == "55100" and saved == []


@pytest.mark.parametrize("failure", ["404 Not Found", "604 Does Not Exist Anywhere"])
def test_a_panel_that_does_not_exist_falls_back(hub, monkeypatch, failure):
    h, saved = hub
    calls = fake_panels(monkeypatch, {"sip:55001@p"}, failure=failure)
    h._last_ring_panel = "55001"
    asyncio.run(h._do_auto_call())
    assert calls == ["sip:55100@p", "sip:55001@p"]


VIDEO_OFFER = ("v=0\r\nc=IN IP4 10.0.0.9\r\nm=audio 4000 RTP/AVP 0\r\n"
               "m=video 4002 RTP/AVP 96\r\n")
AUDIO_ONLY_OFFER = "v=0\r\nc=IN IP4 10.0.0.9\r\nm=audio 4000 RTP/AVP 0\r\n"
VIDEO_REFUSED_OFFER = ("v=0\r\nc=IN IP4 10.0.0.9\r\nm=audio 4000 RTP/AVP 0\r\n"
                       "m=video 0 RTP/AVP 96\r\n")


def ring_from(h, monkeypatch, caller, body):
    monkeypatch.setattr(sip, "in_call", False, raising=False)
    monkeypatch.setattr(sip, "calling", False, raising=False)
    monkeypatch.setitem(sip.pending_incoming, "caller_uri", f"sip:{caller}@p")
    monkeypatch.setitem(sip.pending_incoming, "body", body)
    h._update_stats("ring", "")


def test_a_caller_that_offered_video_is_remembered(hub, monkeypatch):
    h, _ = hub
    ring_from(h, monkeypatch, "55001", VIDEO_OFFER)
    assert h._last_ring_panel == "55001"


@pytest.mark.parametrize("body", [AUDIO_ONLY_OFFER, VIDEO_REFUSED_OFFER, None])
def test_an_audio_only_caller_is_not_a_video_panel(hub, monkeypatch, body):
    h, _ = hub
    ring_from(h, monkeypatch, "60002", body)
    assert h._last_ring_panel is None
