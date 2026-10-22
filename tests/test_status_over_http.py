"""#59: the Tab's state over its local HTTP API instead of GET_INIT_STATUS.

Every GET_INIT_STATUS to a real SGA makes the VIEW app show "Apartment configuration
changed" on every phone of the apartment (seen on two plants); `action=status` on the
Tab's local HTTP API answers the same, silently, and is not truncated. The Vimar apps
use it only on the plants that serve it (canUseApiLegacy: 40507, 40607); here it is
tried at start on local UDP, and a plant that does not answer keeps the SIP way.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.vimar_intercom import hub as hub_mod

sip = hub_mod.sip
R = hub_mod.R
rest = hub_mod.rest_client

STATUS = {"dnd": "1", "voicemail": "0", "vm_level": "2/100", "rubrica_ver": "abc"}


@pytest.fixture
def plant(hub, monkeypatch):
    """A hub on local UDP, with counters of what was asked over HTTP and over SIP."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(R, "LOCAL_PROXY", "192.0.2.10")
    monkeypatch.setattr(R, "SIP_DOMAIN", "d")
    asked = {"http": 0, "sip": 0}

    async def send_message(uri, body, extra_headers=None, timeout=15):
        asked["sip"] += 1
        return True, "200", 200

    monkeypatch.setattr(sip, "send_message", send_message)

    def http_ok(host, user, password, timeout=None):
        asked["http"] += 1
        return dict(STATUS)

    monkeypatch.setattr(rest, "get_status", http_ok)
    return hub, asked


def test_on_local_udp_the_state_comes_over_http_and_nothing_is_sent_over_sip(plant):
    hub, asked = plant
    asyncio.run(hub._request_init_status())
    assert asked == {"http": 1, "sip": 0}
    assert hub.stats["dnd"] is True and hub.stats["voicemail"] is False
    assert hub.stats["vm_level"] == "2/100" and hub._init_status_sent is True


def test_a_tab_without_the_http_api_keeps_get_init_status_and_is_not_asked_again(plant, monkeypatch):
    hub, asked = plant

    def refused(*args, **kwargs):
        asked["http"] += 1
        raise ConnectionRefusedError("port 80")

    monkeypatch.setattr(rest, "get_status", refused)
    asyncio.run(hub._request_init_status())
    asyncio.run(hub._request_init_status())
    assert asked == {"http": 1, "sip": 2}


def test_over_the_cloud_http_is_never_tried(plant, monkeypatch):
    hub, asked = plant
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    asyncio.run(hub._request_init_status())
    assert asked == {"http": 0, "sip": 1}


def _after_command(hub, announce: bool):
    async def go():
        await hub.async_request_status(after_command=True)
        if announce:  # the Tab's own DND;ON, as on the 40507 (#192)
            hub.stats["mode_seq"] = hub.stats.get("mode_seq", 0) + 1
        await asyncio.sleep(0.2)
    asyncio.run(go())


def test_after_a_switch_command_over_sip_an_announcement_makes_the_request_unneeded(plant, monkeypatch):
    """The Tab announced the change: asking GET_INIT_STATUS too would only make the phones
    show "configuration changed"."""
    hub, asked = plant
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(hub_mod, "ANNOUNCE_WAIT_S", 0.05)
    _after_command(hub, announce=True)
    assert asked == {"http": 0, "sip": 0}


def test_after_a_switch_command_over_sip_without_announcement_the_state_is_asked(plant, monkeypatch):
    """Plants that never announce (#9) still get their state read back."""
    hub, asked = plant
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(hub_mod, "ANNOUNCE_WAIT_S", 0.05)
    _after_command(hub, announce=False)
    assert asked == {"http": 0, "sip": 1}


@pytest.mark.parametrize("failure", [TimeoutError("read timed out"), {}])
def test_a_timeout_or_an_empty_answer_does_not_give_up_on_http(plant, monkeypatch, failure):
    """A Tab restarting with Home Assistant times out once: HTTP is tried again next time."""
    hub, asked = plant
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        asked["http"] += 1
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return dict(STATUS)

    monkeypatch.setattr(rest, "get_status", flaky)
    asyncio.run(hub._request_init_status())
    asyncio.run(hub._request_init_status())
    assert asked == {"http": 2, "sip": 1} and hub._status_over_http is True


def test_an_http_read_does_not_answer_a_find_sga_probe(plant):
    hub, _ = plant
    seq = hub._init_seq
    asyncio.run(hub._request_init_status())
    assert hub._init_seq == seq


def test_after_a_switch_command_over_http_the_state_is_read_back(plant):
    hub, asked = plant
    asyncio.run(hub.async_request_status(after_command=True))
    assert asked == {"http": 1, "sip": 0}
