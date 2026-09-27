"""Lock platform for Vimar Intercom."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import const as C
from .const import DOMAIN
from .device import device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([
        # door_target=None → hub.async_door usa runtime.DOOR_ESTERNO: la targa
        # dell'attuatore porta nella rubrica (door_target), o l'SGA se la
        # rubrica non è stata importata. Un indirizzo sbagliato risponde 200
        # senza effetto e la serratura mostra "sbloccata" con la porta chiusa.
        VimarIntercomLock(hub, entry.entry_id, key="lock", name="Serratura",
                          door_target=None, door_command=C.DOOR_COMMAND),
    ])


class VimarIntercomLock(LockEntity):
    """Door lock — unlock sends SIP MESSAGE to open the intercom relay.

    In HomeKit this maps to a DoorLock accessory. The physical lock
    auto-relocks after a few seconds, so we transition back to locked
    after 5 seconds.
    """

    _attr_has_entity_name = False
    _attr_icon = "mdi:door-closed-lock"

    def __init__(self, hub, entry_id: str, *, key: str, name: str, door_target: str | None, door_command: str = "OPEN_2F") -> None:
        self._hub = hub
        self._door_target = door_target
        self._door_command = door_command
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}_{key}"
        self._is_locked = True
        self._relock_task: asyncio.Task | None = None
        self._attr_device_info = device_info(entry_id)

    @property
    def is_locked(self) -> bool:
        return self._is_locked

    @property
    def icon(self) -> str:
        return "mdi:door-closed-lock" if self._is_locked else "mdi:door-open"

    async def async_lock(self, **kwargs) -> None:
        """No-op: door auto-relocks."""
        self._is_locked = True
        self.async_write_ha_state()

    async def async_unlock(self, **kwargs) -> None:
        """Open the door via SIP MESSAGE."""
        ok, msg = await self._hub.async_door(target=self._door_target, command=self._door_command)
        if ok:
            self._is_locked = False
            self.async_write_ha_state()
            if self._relock_task:
                self._relock_task.cancel()
            self._relock_task = asyncio.create_task(self._auto_relock())
        else:
            _LOGGER.error("Door open failed: %s", msg)

    async def async_will_remove_from_hass(self) -> None:
        """Cancella il task di richiusura automatica se l'entità viene rimossa."""
        if self._relock_task:
            self._relock_task.cancel()
            self._relock_task = None

    async def _auto_relock(self):
        await asyncio.sleep(5)
        self._is_locked = True
        self.async_write_ha_state()
