"""Select platform for Vimar Intercom — ritardo della segreteria (issue #4).

Solo sugli impianti che mandano la risposta lunga di GET_INIT_STATUS (visto su un
Tab 5S Up 40515 / 2FV2): `vm_timeout` e la lista dei valori ammessi
`vm_timeout_values`. L'entità nasce quando la lista arriva, non prima: sugli
impianti con la risposta corta (40507 / 2F) non compare affatto, invece di
restare "non disponibile" per sempre.

Scrittura: SET_APT_PARAMS al PICG con `Panda: set`; il valore cambia solo con
`ERR_NONE` nella risposta (PROTOCOL §3).
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import runtime as R
from .away_config import NONE_OPTION, ensure_dir, list_files, messages_dir, require_admin, set_away
from .const import DOMAIN
from .device import device_info, legacy_entity_id
from .hub import QUEUED

_LOGGER = logging.getLogger(__name__)

# Letto da HA a livello di modulo (come attributo di classe verrebbe ignorato): la
# cartella dei messaggi si rilegge ogni minuto.
SCAN_INTERVAL = timedelta(seconds=60)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    try:
        await hass.async_add_executor_job(ensure_dir, messages_dir(hass))
    except OSError as e:
        _LOGGER.warning("Cartella messaggi non creata: %s", e)
    async_add_entities([VimarAwayFileSelect(entry)], True)
    added = False

    @callback
    def _maybe_add() -> None:
        nonlocal added
        if added or not hub.stats.get("vm_timeout_values"):
            return
        added = True
        hub.unregister_state_callback(_maybe_add)
        async_add_entities([VimarVmTimeoutSelect(entry, hub)])

    hub.register_state_callback(_maybe_add)
    entry.async_on_unload(lambda: hub.unregister_state_callback(_maybe_add))
    _maybe_add()


class VimarVmTimeoutSelect(SelectEntity):
    """Dopo quanto risponde la segreteria: uno dei valori che il Tab dichiara."""

    _attr_has_entity_name = True
    _attr_translation_key = "vm_timeout"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:timer-cog-outline"
    _attr_should_poll = False

    def __init__(self, entry, hub) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry.entry_id}_vm_timeout"
        self.entity_id = legacy_entity_id("select", "segreteria_ritardo")
        self._attr_device_info = device_info(entry.entry_id)

    @property
    def options(self) -> list[str]:
        return [str(v) for v in self._hub.stats.get("vm_timeout_values") or []]

    @property
    def current_option(self) -> str | None:
        cur = self._hub.stats.get("vm_timeout")
        return str(cur) if cur is not None and str(cur) in self.options else None

    @property
    def available(self) -> bool:
        # Senza registrazione o senza indirizzo del PICG il comando non può partire.
        return bool(self._hub.registered and R.PICG_TARGET and self.options)

    async def async_added_to_hass(self) -> None:
        self._hub.register_state_callback(self._on_state_change)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_state_callback(self._on_state_change)

    @callback
    def _on_state_change(self) -> None:
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise HomeAssistantError(f"Valore non ammesso dal citofono: {option}")
        ok, msg = await self._hub.async_set_apt_param("vm_timeout", int(option))
        if msg == QUEUED:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="command_queued")
        if not ok:
            raise HomeAssistantError(f"Ritardo segreteria non cambiato: {msg}")
        self.async_write_ha_state()


class VimarAwayFileSelect(SelectEntity):
    """File audio del messaggio di assenza: quelli in <media>/citofono/messaggi
    (si caricano da Media > Local media). L'elenco si rilegge ogni minuto."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:file-music-outline"
    _attr_should_poll = True
    _attr_translation_key = "away_file"  # name and the NONE_OPTION state

    def __init__(self, entry) -> None:
        self._entry = entry
        self._files: list[str] = []
        self._attr_unique_id = f"{entry.entry_id}_away_file"
        self.entity_id = legacy_entity_id("select", "segreteria_file_audio")
        self._attr_device_info = device_info(entry.entry_id)

    async def async_update(self) -> None:
        try:
            self._files = await self.hass.async_add_executor_job(
                list_files, messages_dir(self.hass))
        except OSError as e:
            _LOGGER.warning("Cartella messaggi non leggibile: %s", e)

    def _outside(self) -> str | None:
        """Il file scelto dalle opzioni se sta fuori dalla cartella messaggi (il path)."""
        path = R.AWAY_MESSAGE_FILE
        inside = os.path.join(messages_dir(self.hass) or "", os.path.basename(path))
        return path if path and os.path.normpath(path) != os.path.normpath(inside) else None

    @property
    def options(self) -> list[str]:
        return [NONE_OPTION, *self._files, *filter(None, [self._outside()])]

    @property
    def current_option(self) -> str:
        return self._outside() or os.path.basename(R.AWAY_MESSAGE_FILE) or NONE_OPTION

    async def async_select_option(self, option: str) -> None:
        await require_admin(self.hass, self._context)
        if option == self._outside():
            return  # è già il file in uso, da fuori cartella
        path = "" if option == NONE_OPTION else os.path.join(messages_dir(self.hass), option)
        set_away(self.hass, self._entry, "away_message_file", path)
        self.async_write_ha_state()
