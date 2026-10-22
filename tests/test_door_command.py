"""The door command body comes from the phonebook, paired with its panel (#58).

The MSG column of the door actuator (icon "door") is what the official app sends,
towards that actuator's GID_PE. OPEN_2F is only the fallback when the phonebook
has no door actuator for the panel. A relay module may use the same body towards
another panel, so the body is never taken without its target.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from custom_components.vimar_intercom import const, runtime
from custom_components.vimar_intercom import hub as hub_mod

BASE = {"sip_user": "u", "sip_password": "p", "sip_domain": "d", "sga_target": "55001"}
DOOR_OPEN = {"name": "Portone", "msg": "OPEN", "target": "55001", "icon": "door"}
# 40515 (Apeiv, #58): door and a relay module share the body, not the panel.
DOOR_2FV2 = {"name": "Porta", "msg": "OPEN_2F", "target": "55001", "icon": "door"}
RELAY_2FV2 = {"name": "Relè", "msg": "OPEN_2F", "target": "55003", "icon": "switch"}


@pytest.fixture(autouse=True)
def _restore_runtime():
    yield
    runtime.configure(BASE)


def test_the_body_comes_from_the_door_actuator_of_that_panel():
    runtime.configure({**BASE, "actuators": [DOOR_OPEN]})
    assert runtime.door_command_for("55001") == ("OPEN", "phonebook")


def test_no_door_actuator_falls_back_to_open_2f():
    runtime.configure(BASE)
    assert runtime.door_command_for("55001") == (const.DOOR_COMMAND, "default")


def test_another_panel_does_not_borrow_the_door_body():
    """The pair, not the body: the door's MSG is not sent to a panel that has no door actuator."""
    runtime.configure({**BASE, "actuators": [DOOR_OPEN]})
    assert runtime.door_command_for("55002") == (const.DOOR_COMMAND, "default")


def test_a_relay_with_the_same_body_is_not_a_door():
    runtime.configure({**BASE, "actuators": [RELAY_2FV2, DOOR_2FV2]})
    assert runtime.door_command_for("55003") == (const.DOOR_COMMAND, "default")
    assert runtime.door_command_for("55001") == ("OPEN_2F", "phonebook")


def test_auto_target_means_the_door_panel():
    runtime.configure({**BASE, "door_target": "55001",
                       "actuators": [{"name": "Porta", "msg": "OPEN", "target": "AUTO", "icon": "door"}]})
    assert runtime.door_command_for("55001") == ("OPEN", "phonebook")


@pytest.mark.parametrize("msg", ["", "open", "OPEN;X", "OPEN 2F", "OPEN\r\nX: y", "A" * 33])
def test_a_body_that_is_not_a_plain_token_is_not_sent(msg):
    runtime.configure({**BASE, "actuators": [{**DOOR_OPEN, "msg": msg}]})
    assert runtime.door_command_for("55001") == (const.DOOR_COMMAND, "default")


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    sent = []

    async def fake_msg(uri, body, extra_headers=None, timeout=15):
        sent.append((uri, body, extra_headers))
        return True, "200", 200

    monkeypatch.setattr(hub_mod.sip, "send_message", fake_msg)
    h.sent = sent
    return h


def test_the_lock_and_the_button_send_the_phonebook_body(hub):
    """hub.async_door() with nothing given: panel and body both from the phonebook."""
    runtime.configure({**BASE, "door_target": "55001", "actuators": [DOOR_OPEN]})
    asyncio.run(hub.async_door())
    assert hub.sent == [("sip:55001@d", "OPEN", {"Panda": "command"})]
    assert hub.stats["last_door_command"] == "OPEN"
    assert hub.stats["last_door_command_source"] == "phonebook"


def test_without_a_door_actuator_the_default_is_sent_and_said_so(hub):
    runtime.configure(BASE)
    asyncio.run(hub.async_door())
    assert hub.sent[-1][1] == const.DOOR_COMMAND
    assert hub.stats["last_door_command_source"] == "default"


def test_an_explicit_command_wins(hub):
    runtime.configure({**BASE, "actuators": [DOOR_OPEN]})
    asyncio.run(hub.async_door(command="OPEN_2F"))
    assert hub.sent[-1][1] == "OPEN_2F"
    assert hub.stats["last_door_command_source"] == "explicit"


def test_a_target_given_by_hand_uses_its_own_door_actuator(hub):
    runtime.configure({**BASE, "actuators": [DOOR_OPEN, {"name": "Interno", "msg": "OPEN_INT",
                                                         "target": "55002", "icon": "door"}]})
    asyncio.run(hub.async_door(target="55002"))
    assert hub.sent[-1][:2] == ("sip:55002@d", "OPEN_INT")


def test_the_open_door_service_has_no_default_command():
    """Without `command` the service must not force OPEN_2F over the phonebook."""
    src = (Path(runtime.__file__).parent / "services.py").read_text(encoding="utf-8")
    block = src.split("OPEN_DOOR_SCHEMA = vol.Schema({", 1)[1].split("})", 1)[0]
    assert 'vol.Optional("command"):' in block
    assert "default=" not in block.split('vol.Optional("command")', 1)[1].split("\n", 1)[0]


def test_a_row_for_the_panel_wins_over_an_auto_row():
    """AUTO counts only after the rows naming the panel itself (review on #75)."""
    runtime.configure({**BASE, "door_target": "55002", "actuators": [
        {"name": "Porta", "msg": "OPEN", "target": "AUTO", "icon": "door"},
        {"name": "Interno", "msg": "OPEN_X", "target": "55002", "icon": "door"}]})
    assert runtime.door_command_for("55002") == ("OPEN_X", "phonebook")


def test_an_auto_row_still_serves_the_door_panel_when_nothing_else_does():
    runtime.configure({**BASE, "door_target": "55001", "actuators": [
        {"name": "Porta", "msg": "OPEN", "target": "AUTO", "icon": "door"},
        {"name": "Interno", "msg": "OPEN_X", "target": "55002", "icon": "door"}]})
    assert runtime.door_command_for("55001") == ("OPEN", "phonebook")
    assert runtime.door_command_for("55003") == (const.DOOR_COMMAND, "default")


def test_the_default_is_logged_once_per_panel(hub, caplog):
    runtime.configure(BASE)
    with caplog.at_level("INFO"):
        for _ in range(3):
            asyncio.run(hub.async_door())
        asyncio.run(hub.async_door(target="55002"))
    lines = [r.getMessage() for r in caplog.records if "no door actuator" in r.getMessage()]
    assert len(lines) == 2 and len(hub.sent) == 4
