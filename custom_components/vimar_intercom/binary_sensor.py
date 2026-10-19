"""Binary sensor platform for Vimar Intercom."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .device import device_info, legacy_entity_id
from .plant_messages import sip_id_name


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([
        VimarSIPRegistrationSensor(hub, entry.entry_id),
        VimarInCallSensor(hub, entry.entry_id),
        VimarRingingSensor(hub, entry.entry_id),
        VimarCallingSensor(hub, entry.entry_id),
        VimarNewVideomessageSensor(hub, entry.entry_id),
    ])


class VimarSIPRegistrationSensor(BinarySensorEntity):
    """Shows whether SIP registration is active."""

    _attr_has_entity_name = True
    _attr_translation_key = "sip_registered"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:lan-connect"

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_sip_registered"
        self.entity_id = legacy_entity_id("binary_sensor", "intercom_sip")
        self._attr_device_info = device_info(entry_id)

    @property
    def is_on(self) -> bool:
        return self._hub.registered

    async def async_added_to_hass(self) -> None:
        self._hub.register_state_callback(self._on_state_change)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_state_callback(self._on_state_change)

    @callback
    def _on_state_change(self) -> None:
        self.async_write_ha_state()


class VimarInCallSensor(BinarySensorEntity):
    """Shows whether there is an active SIP call."""

    _attr_has_entity_name = True
    _attr_translation_key = "in_call"
    _attr_icon = "mdi:phone-in-talk"

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_in_call"
        self.entity_id = legacy_entity_id("binary_sensor", "intercom_in_call")
        self._attr_device_info = device_info(entry_id)

    @property
    def is_on(self) -> bool:
        return self._hub.in_call

    async def async_added_to_hass(self) -> None:
        self._hub.register_state_callback(self._on_state_change)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_state_callback(self._on_state_change)

    @callback
    def _on_state_change(self) -> None:
        self.async_write_ha_state()


class _VimarHubBinarySensor(BinarySensorEntity):
    """Base: binary sensor agganciato ai callback di stato dell'hub."""

    _attr_has_entity_name = True

    def __init__(self, hub, entry_id: str, key: str, object_id: str, icon: str) -> None:
        self._hub = hub
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_{key}"
        self.entity_id = legacy_entity_id("binary_sensor", object_id)
        self._attr_device_info = device_info(entry_id)

    async def async_added_to_hass(self) -> None:
        self._hub.register_state_callback(self._on_state_change)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_state_callback(self._on_state_change)

    @callback
    def _on_state_change(self) -> None:
        self.async_write_ha_state()


class VimarRingingSensor(_VimarHubBinarySensor):
    """ON mentre il citofono sta squillando (INVITE in arrivo non ancora gestito)."""

    def __init__(self, hub, entry_id: str) -> None:
        super().__init__(hub, entry_id, "ringing", "intercom_squillo", "mdi:bell-ring")

    @property
    def is_on(self) -> bool:
        return self._hub.is_ringing

    @property
    def extra_state_attributes(self) -> dict:
        st = self._hub.stats
        return {
            "chiamante": sip_id_name(st.get("last_caller_id")),
            "chiamante_id": st.get("last_caller_id"),
        }


class VimarCallingSensor(_VimarHubBinarySensor):
    """ON mentre HA sta chiamando (INVITE in uscita non ancora connesso)."""

    def __init__(self, hub, entry_id: str) -> None:
        super().__init__(hub, entry_id, "calling", "intercom_chiamata_in_uscita", "mdi:phone-outgoing")

    @property
    def is_on(self) -> bool:
        return self._hub.calling


class VimarNewVideomessageSensor(_VimarHubBinarySensor):
    """ON quando il Tab annuncia un nuovo videomessaggio (VM;VIDEO_MESSAGE_CHANGE;NEW)."""

    def __init__(self, hub, entry_id: str) -> None:
        super().__init__(hub, entry_id, "new_videomessage",
                         "intercom_nuovo_videomessaggio", "mdi:message-video")

    @property
    def is_on(self) -> bool:
        return bool(self._hub.stats.get("new_videomessage"))

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "ultimo_cambio": self._hub.stats.get("last_videomessage"),
            "spazio": self._hub.stats.get("vm_level"),
        }
