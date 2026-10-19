"""SIP User-Agent / Server header → commercial model and firmware."""
from __future__ import annotations

import pytest

from custom_components.vimar_intercom import model_detect as md


@pytest.mark.parametrize("ua, model, fw", [
    ("Vimar Tab 7S Up Plus FwVer:2.4.1", "Elvox Tab 7S Plus", "2.4.1"),
    ("Elvox TAB_7S", "Elvox Tab 7S", None),
    ("tab-5s plus/1.2", "Elvox Tab 5S Plus", "1.2"),
    ("Tab 5S", "Elvox Tab 5S", None),
    ("TAB4S version 3.0.2", "Elvox Tab 4S", "3.0.2"),
    ("VIMAR K40517.1 fw:1.0.0", "Elvox Tab 7S Up", "1.0.0"),
    ("device 40515", "Elvox Tab 5S Up", None),
    ("device 40945", "Elvox Tab 5S Plus", None),
    ("device 40607", "Elvox Tab 7S IP", None),
    ("Pixel entrance panel", "Elvox Pixel", None),
    ("panel 41019", "Elvox Pixel", None),
    ("Vimar tab/v1.9", "Elvox Tab IP", "1.9"),
])
def test_known_user_agents_map_to_a_model_and_firmware(ua, model, fw):
    got_model, got_fw, priority = md.match(ua)
    assert (got_model, got_fw) == (model, fw)
    assert md.MODEL_PATTERNS[priority][1] == model


def test_a_more_specific_pattern_has_a_lower_priority():
    _, _, specific = md.match("Tab 7S Plus")
    _, _, generic = md.match("some tab")
    assert 0 <= specific < generic


@pytest.mark.parametrize("ua", [
    "", "Flexisip 2.1", "Linphone/5.0", "TOGA/2.4.0", "Home Assistant", "belle-sip/4.5",
    "Unknown Phone 1.0",
])
def test_proxies_apps_and_unknown_devices_give_no_model(ua):
    assert md.match(ua) == (None, None, -1)


@pytest.mark.parametrize("ua", [
    # Video Door 2.4.7: "Video.Door_<brand><model>_Android<sdk>/<linphone>|AppVer:…|ProtVer:…|"
    "Video.Door_GooglePixel7_Android34/5.2.0|AppVer:2.4.7|ProtVer:1.0|",
    # VIEW 2.18: same shape with the TOGA_ prefix
    "TOGA_GooglePixel7_Android34/5.2.0|AppVer:2.4.0|ProtVer:2.18.0|",
])
def test_a_phone_running_a_vimar_app_is_not_a_device_model(ua):
    """A Pixel phone running an app must not become an "Elvox Pixel" panel."""
    assert md.is_ignored(ua)
    assert md.match(ua) == (None, None, -1)


def test_is_ignored_is_case_insensitive():
    assert md.is_ignored("FLEXISIP")
    assert not md.is_ignored("Elvox Tab 7S")


def test_a_crafted_user_agent_cannot_stall_the_match():
    """The model patterns used to backtrack quadratically on a long run of
    spaces; the User-Agent comes from any SIP peer, and this runs on the loop."""
    import time

    start = time.perf_counter()
    assert md.match("tab 7s" + " " * 60_000 + "x")[0] == "Elvox Tab 7S"
    assert time.perf_counter() - start < 0.5
    assert md.match("Tab 5S Up Plus")[0] == "Elvox Tab 5S Plus"
