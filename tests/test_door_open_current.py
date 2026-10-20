"""OPEN_CURRENT: the door of the panel that is calling, on plants behind a gateway (#186).

On a 2FV2 with a gateway (40515, cloud) the VIEW app opens the entrance only during a
call from it: a MESSAGE to the gateway's "current entrance" 60002, Panda: command, body
OPEN_CURRENT (seen on the wire, the door opened). The phonebook's door actuator does not
open there. Off by default: on a single-panel 2F 60002 does not exist.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.vimar_intercom import const, runtime

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")
sip = hub_mod.sip

BASE = {"sip_user": "u", "sip_password": "p", "sip_domain": "d", "sga_target": "61000",
        "door_target": "55001",
        "actuators": [{"name": "Porta", "msg": "OPEN_2F", "target": "55001", "icon": "door"}]}
PHONEBOOK_DOOR = ("sip:55001@d", "OPEN_2F", {"Panda": "command"})
CURRENT_DOOR = ("sip:60002@d", "OPEN_CURRENT", {"Panda": "command"})


@pytest.fixture(autouse=True)
def _restore_runtime():
    yield
    runtime.configure({"sip_user": "u", "sip_password": "p", "sip_domain": "d"})


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    sent = []

    async def fake_msg(uri, body, extra_headers=None, timeout=15):
        sent.append((uri, body, extra_headers))
        return True, "200", 200

    monkeypatch.setattr(sip, "send_message", fake_msg)
    monkeypatch.setattr(sip, "ringing", lambda cid=None: False)
    monkeypatch.setattr(sip, "in_call", False)
    h.sent = sent
    return h


def _ring(monkeypatch):
    monkeypatch.setattr(sip, "ringing", lambda cid=None: True)


def test_it_is_off_by_default():
    runtime.configure(BASE)
    assert runtime.DOOR_OPEN_CURRENT is False


def test_off_a_ring_still_opens_with_the_phonebook_door(hub, monkeypatch):
    runtime.configure(BASE)
    _ring(monkeypatch)
    asyncio.run(hub.async_door())
    assert hub.sent == [PHONEBOOK_DOOR]


def test_on_during_a_ring_the_current_entrance_is_opened(hub, monkeypatch):
    runtime.configure({**BASE, "door_open_current": True})
    _ring(monkeypatch)
    asyncio.run(hub.async_door())
    assert hub.sent == [CURRENT_DOOR]
    assert hub.stats["last_door_target"] == const.OPEN_CURRENT_TARGET
    assert hub.stats["last_door_command_source"] == "current_call"


def test_on_during_an_answered_ring_the_current_entrance_is_opened(hub, monkeypatch):
    runtime.configure({**BASE, "door_open_current": True})
    monkeypatch.setattr(sip, "in_call", True)
    hub._ring_answered = True
    asyncio.run(hub.async_door())
    assert hub.sent == [CURRENT_DOOR]


def test_our_own_outgoing_call_is_not_a_calling_entrance(hub, monkeypatch):
    """A view or a call to the indoor monitor: the gateway's current entrance may be
    another door, so the usual one."""
    runtime.configure({**BASE, "door_open_current": True})
    monkeypatch.setattr(sip, "in_call", True)
    asyncio.run(hub.async_door())
    assert hub.sent == [PHONEBOOK_DOOR]


def test_a_404_says_to_turn_the_option_off(hub, monkeypatch, caplog):
    """On a plant without a gateway 60002 does not exist."""
    runtime.configure({**BASE, "door_open_current": True})
    _ring(monkeypatch)

    async def not_found(uri, body, extra_headers=None, timeout=15):
        hub.sent.append((uri, body, extra_headers))
        return False, "404", 404

    async def register():
        return True

    monkeypatch.setattr(sip, "send_message", not_found)
    monkeypatch.setattr(sip, "do_register", register)
    with caplog.at_level("INFO"):
        ok, _, code = asyncio.run(hub.async_door())
    assert not ok and code == 404 and hub.sent == [CURRENT_DOOR, CURRENT_DOOR]  # the usual retry
    assert any("turn off the option" in r.getMessage() for r in caplog.records)


def test_on_without_a_call_nothing_changes(hub):
    """With no call there is no current entrance: the usual door, as before."""
    runtime.configure({**BASE, "door_open_current": True})
    asyncio.run(hub.async_door())
    assert hub.sent == [PHONEBOOK_DOOR]


def test_the_test_ring_is_not_a_call(hub):
    """simulate_ring has no panel behind it: OPEN_CURRENT would reach no one."""
    runtime.configure({**BASE, "door_open_current": True})

    async def go():
        assert hub.simulate_ring(5) is True
        assert hub.is_ringing
        await hub.async_door()
        hub._stop_simulated_ring()
    asyncio.run(go())
    assert hub.sent == [PHONEBOOK_DOOR]


@pytest.mark.parametrize("kw, expected", [
    ({"target": "55003"}, ("sip:55003@d", "OPEN_2F", {"Panda": "command"})),
    ({"command": "OPEN_X"}, ("sip:55001@d", "OPEN_X", {"Panda": "command"})),
])
def test_a_target_or_command_given_by_hand_wins(hub, monkeypatch, kw, expected):
    runtime.configure({**BASE, "door_open_current": True})
    _ring(monkeypatch)
    asyncio.run(hub.async_door(**kw))
    assert hub.sent == [expected]
