"""Sensor platform for Vimar Intercom — stato esteso e statistiche."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import plant_state as S
from .const import DOMAIN
from .device import device_info, legacy_entity_id
from .plant_messages import sip_id_name

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class VimarSensorDescription(SensorEntityDescription):
    """Descrizione sensore Vimar con funzioni value/attributes basate sull'hub."""

    value_fn: Callable[[Any], Any]
    attrs_fn: Callable[[Any], dict] | None = None
    # "Ultimo ..." che l'hub riempie solo quando succede: senza, un riavvio di HA lo
    # riporta a "Sconosciuto" fino al prossimo evento (l'apertura di stamattina sparisce).
    restore: bool = False
    # The entity_id a new install gets (device.legacy_entity_id), the name being translated.
    object_id: str = ""
    # Called with the last saved attributes, to seed the hub before its first
    # update (the device list, which otherwise starts empty after a restart).
    restore_fn: Callable[[Any, dict], None] | None = None
    # Attributes kept out of the recorder (large or changing often).
    unrecorded_attrs: frozenset[str] = frozenset()


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
        # Solo dagli impianti con la risposta lunga di GET_INIT_STATUS (issue #4).
        "gid_appartamento": st.get("apt_gid"),
        "nomi_appartamento": st.get("apt_names"),
        "cifratura_impianto": st.get("media_enc"),
        "srtp_attivo": S.MEDIA_ENC,
    }


SENSORS: tuple[VimarSensorDescription, ...] = (
    VimarSensorDescription(
        key="devices",
        object_id="intercom_dispositivi",
        icon="mdi:devices",
        # Every mobile device shares one SIP user: without this list there is
        # no way to tell which phones are paired, and generating a new QR
        # rotates the shared credential and unpairs them.
        value_fn=lambda hub: len(hub.devices),
        # Identifiers masked, no addresses: the full values stay in the hub,
        # where devices are merged.
        attrs_fn=lambda hub: {
            "dispositivi": hub.devices_public,
            "riepilogo": hub.devices_summary,
        },
        restore_fn=lambda hub, attrs: hub.restore_devices(attrs.get("dispositivi")),
        unrecorded_attrs=frozenset({"dispositivi", "riepilogo"}),
    ),
    VimarSensorDescription(
        key="status",
        object_id="intercom_stato",
        icon="mdi:doorbell-video",
        device_class=SensorDeviceClass.ENUM,
        options=["offline", "idle", "ringing", "in_call", "calling"],
        value_fn=lambda hub: hub.status,
        attrs_fn=_status_attrs,
    ),
    VimarSensorDescription(
        key="last_caller",
        restore=True,
        object_id="intercom_ultimo_chiamante",
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
        restore=True,
        object_id="intercom_ultimo_squillo",
        icon="mdi:bell-clock",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda hub: hub.stats.get("last_ring_time"),
        # foto/foto_url, clip/clip_url: percorso e URL (autenticato) di foto e clip
        # dell'ultimo squillo, per le notifiche (hub.ring_media)
        attrs_fn=lambda hub: {
            "chiamante": sip_id_name(hub.stats.get("last_caller_id")),
            **hub.ring_media(),
        },
    ),
    VimarSensorDescription(
        key="ring_count",
        object_id="intercom_squilli",
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
        object_id="intercom_chiamate",
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
        restore=True,
        object_id="intercom_durata_ultima_chiamata",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda hub: hub.stats.get("last_call_duration"),
    ),
    VimarSensorDescription(
        key="last_door",
        restore=True,
        object_id="intercom_ultima_apertura",
        icon="mdi:door-open",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda hub: hub.stats.get("last_door_time"),
        attrs_fn=lambda hub: {
            "target": sip_id_name(hub.stats.get("last_door_target")),
            "target_id": hub.stats.get("last_door_target"),
            "esito": hub.stats.get("last_door_result"),
            "codice": hub.stats.get("last_door_code"),
            "comando": hub.stats.get("last_door_command"),
            "origine_comando": hub.stats.get("last_door_command_source"),
            "aperture": hub.stats.get("door_count"),
        },
    ),
    VimarSensorDescription(
        key="last_command",
        object_id="intercom_ultimo_comando",
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
        object_id="intercom_ultimo_messaggio_ricevuto",
        icon="mdi:message-text",
        value_fn=lambda hub: hub.stats.get("last_message_in"),
        attrs_fn=lambda hub: {
            "ora": hub.stats.get("last_message_in_time"),
        },
    ),
    VimarSensorDescription(
        key="vm_level",
        object_id="intercom_spazio_segreteria",
        icon="mdi:voicemail",
        value_fn=lambda hub: hub.stats.get("vm_level"),
        attrs_fn=lambda hub: {
            "nota": "Occupazione videomessaggi (usato/totale) dal GET_INIT_STATUS",
        },
    ),
    VimarSensorDescription(
        key="rubrica_ver",
        object_id="intercom_versione_rubrica",
        icon="mdi:book-account",
        value_fn=lambda hub: hub.stats.get("rubrica_ver"),
        attrs_fn=lambda hub: {
            "vm_ver": hub.stats.get("vm_ver"),
            "nota": "Cambia quando la rubrica del Tab viene aggiornata → reimporta gli attuatori",
        },
    ),
    VimarSensorDescription(
        key="last_missed_call",
        restore=True,
        object_id="intercom_ultima_chiamata_persa",
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
    async_add_entities(sensor_class(d)(hub, entry.entry_id, d) for d in SENSORS)


_CLASSES: dict[frozenset, type] = {}


def sensor_class(description: VimarSensorDescription) -> type:
    """The entity class for a description.

    Home Assistant reads _unrecorded_attributes from the class (it combines
    them when the class is created), and VimarStatSensor is shared by every
    sensor: a description with unrecorded attributes gets its own subclass.
    """
    attrs = description.unrecorded_attrs
    if not attrs:
        return VimarStatSensor
    if attrs not in _CLASSES:
        _CLASSES[attrs] = type("VimarStatSensor", (VimarStatSensor,), {
            "_unrecorded_attributes": frozenset(
                getattr(VimarStatSensor, "_unrecorded_attributes", frozenset()) | attrs),
        })
    return _CLASSES[attrs]


class VimarStatSensor(RestoreSensor):
    """Sensore generico alimentato dalle statistiche dell'hub (push, no polling).

    Con `restore` riprende valore e attributi salvati da HA finché l'hub non ha un
    valore suo (dopo un riavvio: fino al prossimo squillo, apertura, chiamata persa).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub, entry_id: str, description: VimarSensorDescription) -> None:
        self._hub = hub
        self.entity_description = description
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self.entity_id = legacy_entity_id("sensor", description.object_id)
        self._attr_device_info = device_info(entry_id)
        self._restored = None
        self._restored_attrs: dict | None = None

    def _live_value(self):
        try:
            return self.entity_description.value_fn(self._hub)
        except Exception:  # noqa: BLE001
            return None

    def _live_attrs(self) -> dict | None:
        fn = self.entity_description.attrs_fn
        if not fn:
            return None
        try:
            return fn(self._hub)
        except Exception:  # noqa: BLE001
            return None

    @property
    def native_value(self):
        value = self._live_value()
        if value is None and self.entity_description.restore:
            return self._restored
        return value

    @property
    def extra_state_attributes(self) -> dict | None:
        attrs = self._live_attrs()
        if (self.entity_description.restore and self._live_value() is None
                and self._restored_attrs is not None):
            return self._restored_attrs
        return attrs

    async def async_added_to_hass(self) -> None:
        if self.entity_description.restore:
            await self._async_restore()
        if self.entity_description.restore_fn:
            last = await self.async_get_last_state()
            if last is not None:
                try:
                    self.entity_description.restore_fn(self._hub, dict(last.attributes))
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Restoring %s failed", self.entity_id, exc_info=True)
        self._hub.register_state_callback(self._on_state_change)

    async def _async_restore(self) -> None:
        data = await self.async_get_last_sensor_data()
        if data is None or data.native_value is None:
            return
        self._restored = data.native_value
        last = await self.async_get_last_state()
        keys = set(self._live_attrs() or {})
        if last is not None and keys:
            # Solo gli attributi nostri: nome, icona e device_class li rimette HA.
            self._restored_attrs = {k: v for k, v in last.attributes.items() if k in keys}

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_state_callback(self._on_state_change)

    @callback
    def _on_state_change(self) -> None:
        self.async_write_ha_state()
