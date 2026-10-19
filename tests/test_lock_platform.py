"""The door lock: unlocking opens the door through the hub, shows "unlocked"
and locks itself again after a few seconds; a failed opening is an error."""
from __future__ import annotations

import asyncio
import json
import re
import types
from pathlib import Path

import pytest
from homeassistant.exceptions import HomeAssistantError as _HAError

from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import lock as lock_mod
from custom_components.vimar_intercom.const import DOMAIN


class _Hub:
    def __init__(self, ok=True):
        self.ok = ok
        self.doors = []

    async def async_door(self, target=None, command=None):
        self.doors.append((target, command))
        return (True, "opened", 200) if self.ok else (False, "timeout", None)


@pytest.fixture
def relock(monkeypatch):
    """No real 5 s wait: the relock sleep returns at once and records its delay."""
    monkeypatch.setattr(lock_mod, "HomeAssistantError", _HAError)
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(lock_mod, "asyncio", types.SimpleNamespace(
        sleep=sleep, create_task=asyncio.create_task))
    return delays


def _lock(hub):
    added = []
    hass = types.SimpleNamespace(data={DOMAIN: {"e1": {"hub": hub}}})
    asyncio.run(lock_mod.async_setup_entry(hass, types.SimpleNamespace(entry_id="e1"), added.extend))
    (entity,) = added
    entity.states = []
    entity.async_write_ha_state = lambda: entity.states.append(entity.is_locked)
    return entity


def test_the_lock_uses_the_default_door_and_starts_locked(relock):
    lock = _lock(_Hub())
    assert lock._attr_unique_id == "e1_lock"
    assert lock.is_locked is True and lock.icon == "mdi:door-closed-lock"


def test_unlock_opens_the_default_door_then_relocks_after_five_seconds(relock):
    hub = _Hub()
    lock = _lock(hub)

    async def run():
        await lock.async_unlock()
        assert lock.is_locked is False and lock.icon == "mdi:door-open"
        await lock._relock_task

    asyncio.run(run())
    # target and command None: hub.async_door picks the door panel and its body
    # from the phonebook (#58), not a fixed OPEN_2F.
    assert hub.doors == [(None, None)]
    assert relock == [5]
    assert lock.states == [False, True] and lock.is_locked is True


def test_a_failed_opening_raises_and_stays_locked(relock):
    lock = _lock(_Hub(ok=False))
    with pytest.raises(_HAError) as err:
        asyncio.run(lock.async_unlock())
    assert lock.is_locked is True and lock.states == [] and lock._relock_task is None
    # Translated by HA in its own language, not Italian text from the hub (#128).
    assert (err.value.translation_domain, err.value.translation_key) == (DOMAIN, "door_timeout")


def test_every_door_failure_has_a_message_in_every_language():
    """Each key async_door can fail with, with the same placeholders everywhere (as hassfest wants);
    every other exception has the same placeholders in every language."""
    base = Path(lock_mod.__file__).parent
    keys = {f"door_{k}" for k in (hub_mod.DOOR_BUSY, hub_mod.DOOR_QUEUED, hub_mod.DOOR_UNCONFIRMED,
                                  hub_mod.DOOR_NOT_REGISTERED, hub_mod.DOOR_TIMEOUT, hub_mod.DOOR_ERROR,
                                  hub_mod.DOOR_SEND_FAILED)} | {"command_queued"}
    for name in ("strings.json", "translations/en.json", "translations/it.json"):
        exc = json.loads((base / name).read_text(encoding="utf-8"))["exceptions"]
        assert keys <= set(exc), name
        for key in keys:
            placeholders = set(re.findall(r"{(\w+)}", exc[key]["message"]))
            assert placeholders == ({"code"} if key == "door_error" else set()), (name, key)
    tables = [json.loads((base / n).read_text(encoding="utf-8"))["exceptions"]
              for n in ("strings.json", "translations/en.json", "translations/it.json")]
    assert set(tables[0]) == set(tables[1]) == set(tables[2])
    for key in tables[0]:
        found = [set(re.findall(r"{(\w+)}", t[key]["message"])) for t in tables]
        assert found[0] == found[1] == found[2], key


def test_a_second_unlock_restarts_the_relock_timer(relock):
    lock = _lock(_Hub())

    async def run():
        await lock.async_unlock()
        first = lock._relock_task
        await lock.async_unlock()
        await asyncio.sleep(0)
        assert first.cancelled()
        await lock._relock_task

    asyncio.run(run())
    assert lock.is_locked is True


def test_removing_the_entity_cancels_the_pending_relock(relock):
    lock = _lock(_Hub())

    async def run():
        await lock.async_unlock()
        task = lock._relock_task
        await lock.async_will_remove_from_hass()
        await asyncio.sleep(0)
        return task

    task = asyncio.run(run())
    assert task.cancelled() and lock._relock_task is None
    asyncio.run(lock.async_will_remove_from_hass())  # nothing pending: no error


def test_lock_is_a_no_op_that_shows_locked(relock):
    lock = _lock(_Hub())
    lock._is_locked = False
    asyncio.run(lock.async_lock())
    assert lock.is_locked is True and lock.states == [True]
