"""DeviceInfo condiviso fra tutte le piattaforme.

Il modello non è più una costante: viene rilevato a runtime dagli header SIP
del citofono (vedi ``model_detect``) e memorizzato in ``plant_state``. Finché il
rilevamento non è avvenuto si usa il valore generico di ``const.MODEL``.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from . import plant_state as S
from .const import DOMAIN, MANUFACTURER, MODEL


def legacy_entity_id(domain: str, object_id: str) -> str:
    """The entity_id a new install gets: the one the Italian names gave before they were
    translated (#178). With translated names Home Assistant would build it from the name
    in the install's language, and docs, the example package and the card's defaults
    could not name it. An existing install keeps the entity_id in its registry."""
    return f"{domain}.vimar_intercom_{object_id}"


def device_info(entry_id: str) -> DeviceInfo:
    """DeviceInfo del citofono, con il modello rilevato se disponibile."""
    info = DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Vimar Intercom",
        manufacturer=MANUFACTURER,
        model=S.DETECTED_MODEL or MODEL,
    )
    if S.DETECTED_FW:
        info["sw_version"] = S.DETECTED_FW
    return info
