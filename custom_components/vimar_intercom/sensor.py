"""Sensor platform for Vimar Intercom — stato esteso e statistiche."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .device import device_info
from .hub import sip_id_name


@dataclass(frozen=True, kw_only=True)
class VimarSensorDescription(SensorEntityDescription):
    """Descrizione sensore Vimar con funzioni value/attributes basate sull'hub."""

    value_fn: Callable[[Any], Any]
    attrs_fn: Callable[[Any], dict] | None = None


def _status_attrs(hub) -> dict:
    st = hub.stats
    return {
        "registrato": hub.registered,
        "in_chiamata": hub.in_call,
        "squillo": hub.is_ringing,
        "chiamata_in_uscita": hub.calling,
        "sip_user": hub.sip_user,
        "sip_domain": hub.sip_domain,
        "proxy": hub.proxy,
        "transport": hub.transport,
        "ip_locale": hub.local_ip,
        "ultima_registrazione": st.get("last_register_time"),
        "registrazioni_fallite": st.get("register_failures"),
        "ultimo_errore": st.get("last_error"),
        "ultimo_errore_ora": st.get("last_error_time"),
        "avviato_il": st.get("started_at"),
    }


SENSORS: tuple[VimarSensorDescription, ...] = (
    VimarSensorDescription(
        key="devices",
        name="Intercom Dispositivi",
        icon="mdi:devices",
        # Tutti i dispositivi mobili condividono un unico utente SIP: senza
        # questo elenco non c'è modo di sapere quali telefoni sono abbinati,
        # e generare un nuovo QR ruota la credenziale condivisa sganciandoli.
        value_fn=lambda hub: len(hub.devices),
        attrs_fn=lambda hub: {
            "dispositivi": hub.devices,
            "riepilogo": hub.devices_summary,
        },
    ),
    VimarSensorDescription(
        key="status",
        name="Intercom Stato",
        icon="mdi:doorbell-video",
        device_class=SensorDeviceClass.ENUM,
        options=["offline", "idle", "ringing", "in_call", "calling"],
        value_fn=lambda hub: hub.status,
        attrs_fn=_status_attrs,
    ),
    VimarSensorDescription(
        key="last_caller",
        name="Intercom Ultimo Chiamante",
        icon="mdi:account-voice",
        value_fn=lambda hub: sip_id_name(hub.stats.get("last_caller_id")),
        attrs_fn=lambda hub: {
            "id": hub.stats.get("last_caller_id"),
            "uri": hub.stats.get("last_caller_uri"),
            "ora": hub.stats.get("last_ring_time"),
        },
    ),
    VimarSensorDescription(
        key="last_ring",
        name="Intercom Ultimo Squillo",
        icon="mdi:bell-clock",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda hub: hub.stats.get("last_ring_time"),
        attrs_fn=lambda hub: {
            "chiamante": sip_id_name(hub.stats.get("last_caller_id")),
        },
    ),
    VimarSensorDescription(
        key="ring_count",
        name="Intercom Squilli",
        icon="mdi:bell-plus",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda hub: hub.stats.get("ring_count"),
        attrs_fn=lambda hub: {
            "non_risposti_da_ha": hub.stats.get("missed_count"),
            "nota": "Conteggio dall'ultimo avvio dell'integrazione",
        },
    ),
    VimarSensorDescription(
        key="call_count",
        name="Intercom Chiamate",
        icon="mdi:phone-log",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda hub: hub.stats.get("call_count"),
        attrs_fn=lambda hub: {
            "ultima_direzione": hub.stats.get("last_call_direction"),
            "ultimo_inizio": hub.stats.get("last_call_start"),
            "ultima_fine": hub.stats.get("last_call_end"),
            "nota": "Conteggio dall'ultimo avvio dell'integrazione",
        },
    ),
    VimarSensorDescription(
        key="last_call_duration",
        name="Intercom Durata Ultima Chiamata",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda hub: hub.stats.get("last_call_duration"),
    ),
    VimarSensorDescription(
        key="last_door",
        name="Intercom Ultima Apertura",
        icon="mdi:door-open",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda hub: hub.stats.get("last_door_time"),
        attrs_fn=lambda hub: {
            "target": sip_id_name(hub.stats.get("last_door_target")),
            "target_id": hub.stats.get("last_door_target"),
            "esito": hub.stats.get("last_door_result"),
            "aperture": hub.stats.get("door_count"),
        },
    ),
    VimarSensorDescription(
        key="last_command",
        name="Intercom Ultimo Comando",
        icon="mdi:console-line",
        value_fn=lambda hub: hub.stats.get("last_command_result"),
        attrs_fn=lambda hub: {
            "body": hub.stats.get("last_command_body"),
            "target": hub.stats.get("last_command_target"),
            "ora": hub.stats.get("last_command_time"),
        },
    ),
    VimarSensorDescription(
        key="last_message_in",
        name="Intercom Ultimo Messaggio Ricevuto",
        icon="mdi:message-text",
        value_fn=lambda hub: hub.stats.get("last_message_in"),
        attrs_fn=lambda hub: {
            "ora": hub.stats.get("last_message_in_time"),
        },
    ),
    VimarSensorDescription(
        key="vm_level",
        name="Intercom Spazio Segreteria",
        icon="mdi:voicemail",
        value_fn=lambda hub: hub.stats.get("vm_level"),
        attrs_fn=lambda hub: {
            "nota": "Occupazione videomessaggi (usato/totale) dal GET_INIT_STATUS",
        },
    ),
    VimarSensorDescription(
        key="rubrica_ver",
        name="Intercom Versione Rubrica",
        icon="mdi:book-account",
        value_fn=lambda hub: hub.stats.get("rubrica_ver"),
        attrs_fn=lambda hub: {
            "vm_ver": hub.stats.get("vm_ver"),
            "nota": "Cambia quando la rubrica del Tab viene aggiornata → reimporta gli attuatori",
        },
    ),
    VimarSensorDescription(
        key="last_missed_call",
        name="Intercom Ultima Chiamata Persa",
        icon="mdi:phone-missed",
        value_fn=lambda hub: (
            (hub.stats.get("last_missed_call") or {}).get("name")
            or (hub.stats.get("last_missed_call") or {}).get("sip_id")
        ),
        attrs_fn=lambda hub: {
            "sip_id": (hub.stats.get("last_missed_call") or {}).get("sip_id"),
            "ts": (hub.stats.get("last_missed_call") or {}).get("ts"),
            "totale": hub.stats.get("missed_call_count"),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities(VimarStatSensor(hub, entry.entry_id, d) for d in SENSORS)


class VimarStatSensor(SensorEntity):
    """Sensore generico alimentato dalle statistiche dell'hub (push, no polling)."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, hub, entry_id: str, description: VimarSensorDescription) -> None:
        self._hub = hub
        self.entity_description = description
        self._attr_name = description.name
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = device_info(entry_id)

    @property
    def native_value(self):
        try:
            return self.entity_description.value_fn(self._hub)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self) -> dict | None:
        fn = self.entity_description.attrs_fn
        if not fn:
            return None
        try:
            return fn(self._hub)
        except Exception:  # noqa: BLE001
            return None

    async def async_added_to_hass(self) -> None:
        self._hub.register_state_callback(self._on_state_change)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_state_callback(self._on_state_change)

    @callback
    def _on_state_change(self) -> None:
        self.async_write_ha_state()
