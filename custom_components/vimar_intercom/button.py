"""Button platform for Vimar Intercom."""

from __future__ import annotations

import logging
import re

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import runtime as R
from .away_config import require_admin
from .const import DOMAIN
from .device import device_info, legacy_entity_id
from .hub import QUEUED

_LOGGER = logging.getLogger(__name__)

# Mappatura icona logica → icona mdi per gli attuatori dinamici (parse_rubrica.py)
_ACTUATOR_ICONS = {
    "door":   "mdi:door",
    "light":  "mdi:lightbulb",
    "switch": "mdi:toggle-switch-variant",
}
# Target di default (targa/porta) usato dall'apri-porta quando target == "AUTO".
_AUTO_TARGET_SENTINEL = "AUTO"


def _slug(*parts: str) -> str:
    """Slug deterministico da name+msg per un unique_id stabile."""
    raw = "_".join(p for p in parts if p)
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_") or "act"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]

    entities: list[ButtonEntity] = [
        VimarCallButton(hub, entry.entry_id),
        # Targa video = opzione camera_target (issue #3). Prima era l'SGA, che
        # riceve i comandi ma non accetta chiamate su tutti gli impianti.
        VimarCallTargetButton(hub, entry.entry_id, R.CAMERA_TARGET, "chiama_video_esterno", "call_ext"),
        # Pannello interno = opzione internal_panel_target (default in const).
        VimarCallTargetButton(hub, entry.entry_id, R.INTERNAL_PANEL_TARGET,
                              "chiama_casa_interno", "call_int"),
        VimarAnswerButton(hub, entry.entry_id),
        VimarDeclineButton(hub, entry.entry_id),
        VimarHangupButton(hub, entry.entry_id),
        # target=None → stesso default dell'apri-porta di hub.async_door.
        VimarDoorButton(hub, entry.entry_id, None, "apri_porta", "door_street", "mdi:door-open"),
        VimarTestRingButton(hub, entry.entry_id),
    ]

    # Attuatori dinamici dalla rubrica: options["actuators"] ha la precedenza
    # su entry.data. Lista di dict {name, msg, target, icon}. Default vuoto.
    actuators = (entry.options.get("actuators")
                 or entry.data.get("actuators") or [])
    for act in actuators:
        try:
            entities.append(VimarActuatorButton(hub, entry.entry_id, act))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Attuatore ignorato (config non valida): %s", act)

    async_add_entities(entities)


class VimarCallButton(ButtonEntity):
    """Button to call the intercom (initiate SIP INVITE)."""

    _attr_has_entity_name = True
    _attr_translation_key = "call"
    _attr_icon = "mdi:phone-outgoing"

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_call"
        self.entity_id = legacy_entity_id("button", "chiama")
        self._attr_device_info = device_info(entry_id)

    async def async_press(self) -> None:
        ok, msg = await self._hub.async_call()
        if not ok:
            raise HomeAssistantError(f"Chiamata non riuscita: {msg}")


class VimarCallTargetButton(ButtonEntity):
    """Button to call a specific SIP target (targa interna/esterna)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:phone-outgoing"

    def __init__(self, hub, entry_id: str, target: str, object_id: str, key: str) -> None:
        self._hub = hub
        self._target = target
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry_id}_{key}"
        self.entity_id = legacy_entity_id("button", object_id)
        self._attr_device_info = device_info(entry_id)

    async def async_press(self) -> None:
        ok, msg = await self._hub.async_call(target=self._target)
        if not ok:
            raise HomeAssistantError(f"Chiamata a {self._target} non riuscita: {msg}")


class VimarAnswerButton(ButtonEntity):
    """Button to answer an incoming intercom call.

    Disponibile solo mentre squilla: a riposo il tasto è grigio invece di
    "funzionare" e poi scrivere nel log «Nessuna chiamata in arrivo». Per
    guardare la targa si apre la camera, non si risponde.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "answer"
    _attr_icon = "mdi:phone-incoming"
    _attr_should_poll = False

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_answer"
        self.entity_id = legacy_entity_id("button", "rispondi")
        self._attr_device_info = device_info(entry_id)
        self._was_available: bool | None = None

    @property
    def available(self) -> bool:
        return bool(self._hub.registered and self._hub.is_ringing)

    async def async_added_to_hass(self) -> None:
        self._hub.register_state_callback(self._on_state_change)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_state_callback(self._on_state_change)

    @callback
    def _on_state_change(self) -> None:
        # Le statistiche cambiano spesso: si scrive lo stato solo quando lo squillo
        # inizia o finisce, non a ogni keepalive.
        now = self.available
        if now != self._was_available:
            self._was_available = now
            self.async_write_ha_state()

    async def async_press(self) -> None:
        ok, msg = await self._hub.async_answer()
        if not ok:
            raise HomeAssistantError(f"Risposta non riuscita: {msg}")


class VimarDeclineButton(VimarAnswerButton):
    """Rifiuta lo squillo (603 Decline): smette di suonare tutta la casa, come l'app.

    Stessa disponibilità di «Rispondi»: solo mentre suona.
    """

    _attr_translation_key = "decline"
    _attr_icon = "mdi:phone-hangup"

    def __init__(self, hub, entry_id: str) -> None:
        super().__init__(hub, entry_id)
        self._attr_unique_id = f"{entry_id}_decline"
        self.entity_id = legacy_entity_id("button", "rifiuta")

    async def async_press(self) -> None:
        ok, msg = await self._hub.async_decline()
        if not ok:
            raise HomeAssistantError(f"Rifiuto non riuscito: {msg}")


class VimarHangupButton(ButtonEntity):
    """Button to hang up the current call (SIP BYE)."""

    _attr_has_entity_name = True
    _attr_translation_key = "hangup"
    _attr_icon = "mdi:phone-hangup"

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_hangup"
        self.entity_id = legacy_entity_id("button", "riaggancia")
        self._attr_device_info = device_info(entry_id)

    async def async_press(self) -> None:
        await self._hub.async_hangup()


class VimarDoorButton(ButtonEntity):
    """Button to open a door (SIP MESSAGE)."""

    _attr_has_entity_name = True

    def __init__(self, hub, entry_id: str, target: str | None, object_id: str, key: str, icon: str) -> None:
        self._hub = hub
        self._target = target
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_{key}"
        self.entity_id = legacy_entity_id("button", object_id)
        self._attr_device_info = device_info(entry_id)

    async def async_press(self) -> None:
        ok, result, code = await self._hub.async_door(target=self._target)
        if not ok:
            # Come la serratura: un errore visibile, non un «premuto» con la porta
            # chiusa e la riga nel log (issue #23).
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key=f"door_{result}", translation_placeholders={"code": str(code)}
            )


class VimarTestRingButton(ButtonEntity):
    """Test ring: the `simulate_ring` service with its default duration, so ring
    automations and notifications can be tried without writing YAML."""

    _attr_has_entity_name = True
    _attr_translation_key = "test_ring"
    _attr_icon = "mdi:bell-ring-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_test_ring"
        self.entity_id = legacy_entity_id("button", "squillo_di_prova")
        self._attr_device_info = device_info(entry_id)

    async def async_press(self) -> None:
        # Like the simulate_ring admin service: a fake ring fires webhooks, announcements
        # and automations, so administrators and automations only.
        await require_admin(self.hass, self._context)
        if not self._hub.simulate_ring(20):
            raise HomeAssistantError("Squillo di prova non avviato: una chiamata è già in corso")


class VimarActuatorButton(ButtonEntity):
    """Attuatore dinamico via SIP MESSAGE (Panda: command).

    Configurato dall'utente nell'options flow (lista prodotta da
    tools/parse_rubrica.py): dict {name, msg, target, icon}.
    Riusa lo stesso meccanismo dell'apri-porta: hub.async_send_command con
    header ``Panda: command`` — non reimplementa nulla dello stack SIP.
    """

    _attr_has_entity_name = True

    def __init__(self, hub, entry_id: str, act: dict) -> None:
        self._hub = hub
        name = str(act["name"])
        self._command = str(act["msg"])
        target = str(act.get("target", _AUTO_TARGET_SENTINEL))
        # target "AUTO" → stesso destinatario di default dell'apri-porta
        # (la targa usata da hub.async_door/servizio open_door, cioè
        # R.DOOR_TARGET = R.DOOR_ESTERNO senza lo schema sip:).
        # Così l'utente non deve conoscere l'id SIP della targa master, e il
        # valore segue eventuali override in options (manuali o da rubrica.db).
        self._target = R.DOOR_TARGET if target.upper() == _AUTO_TARGET_SENTINEL else target
        self._attr_name = name
        self._attr_icon = _ACTUATOR_ICONS.get(act.get("icon", ""), "mdi:gesture-tap-button")
        slug = _slug(name, self._command)
        self._attr_unique_id = f"{entry_id}_act_{slug}"
        self._attr_device_info = device_info(entry_id)

    async def async_press(self) -> None:
        ok, msg = await self._hub.async_send_command(
            body=self._command, target=self._target,
            header_name="Panda", header_value="command")
        if msg == QUEUED:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="command_queued")
        if not ok:
            raise HomeAssistantError(f"{self._attr_name} non riuscito: {msg}")
