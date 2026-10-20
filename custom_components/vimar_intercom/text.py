"""Text platform: testo del messaggio di assenza (opzione away_message_text)."""
from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.const import EntityCategory

from . import runtime as R
from .away_config import require_admin, set_away
from .const import AWAY_TEXT_MAX
from .device import device_info, legacy_entity_id


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([VimarAwayText(entry)])


class VimarAwayText(TextEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "away_text"
    _attr_icon = "mdi:text-to-speech"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False
    _attr_native_max = AWAY_TEXT_MAX  # come nel form delle opzioni

    def __init__(self, entry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_away_text"
        self.entity_id = legacy_entity_id("text", "segreteria_testo_del_messaggio")
        self._attr_device_info = device_info(entry.entry_id)

    @property
    def native_value(self) -> str:
        return R.AWAY_MESSAGE_TEXT[:AWAY_TEXT_MAX]

    async def async_set_value(self, value: str) -> None:
        await require_admin(self.hass, self._context)
        set_away(self.hass, self._entry, "away_message_text", value.strip())
        self.async_write_ha_state()
