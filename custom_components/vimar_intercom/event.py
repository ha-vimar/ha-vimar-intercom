"""Event platform for Vimar Intercom — doorbell ring detection."""

from __future__ import annotations

import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_FUORIPORTA
from .device import device_info, legacy_entity_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([
        VimarDoorbellEvent(hub, entry.entry_id),
        VimarFuoriportaEvent(hub, entry.entry_id),
    ])


class VimarDoorbellEvent(EventEntity):
    """Doorbell ring event — fires when an incoming SIP INVITE is received.

    In HomeKit this maps to a Doorbell accessory, enabling push
    notifications with video snapshot on Apple devices.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "doorbell"
    _attr_icon = "mdi:bell-ring"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_doorbell"
        self.entity_id = legacy_entity_id("event", "doorbell")
        self._attr_device_info = device_info(entry_id)

    async def async_added_to_hass(self) -> None:
        """Register ring callback when entity is added."""
        self._hub.register_ring_callback(self._handle_ring)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister ring callback when entity is removed."""
        self._hub.unregister_ring_callback(self._handle_ring)

    @callback
    def _handle_ring(self) -> None:
        """Handle incoming SIP INVITE (doorbell ring)."""
        self._trigger_event("ring")
        self.async_write_ha_state()
        _LOGGER.info("Doorbell ring detected")


class VimarFuoriportaEvent(EventEntity):
    """Evento fuoriporta — scatta su SIP MESSAGE 'FP;{...}' dal Tab.

    [da confermare sul campo — PROTOCOL.md §4]
    """

    _attr_has_entity_name = True
    _attr_translation_key = "fuoriporta"
    _attr_icon = "mdi:doorbell"
    _attr_event_types = ["fuoriporta"]

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_fuoriporta"
        self.entity_id = legacy_entity_id("event", "fuoriporta")
        self._attr_device_info = device_info(entry_id)

    async def async_added_to_hass(self) -> None:
        self._hub.register_event_callback(self._handle_event)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_event_callback(self._handle_event)

    @callback
    def _handle_event(self, event_type: str, data: dict) -> None:
        if event_type != EVENT_FUORIPORTA:
            return
        self._trigger_event("fuoriporta", dict(data or {}))
        self.async_write_ha_state()
        _LOGGER.info("Fuoriporta event: %s", data)
