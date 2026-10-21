"""DeviceInfo condiviso fra tutte le piattaforme.

Il modello non è più una costante: viene rilevato a runtime dagli header SIP
del citofono (vedi ``model_detect``) e memorizzato in ``plant_state``. Finché il
rilevamento non è avvenuto si usa il valore generico di ``const.MODEL``.
"""

from __future__ import annotations

import re

from homeassistant.helpers.device_registry import DeviceInfo

from . import plant_state as S
from . import profiles
from . import runtime as R
from .const import DOMAIN, MANUFACTURER, MODEL


def legacy_entity_id(domain: str, object_id: str) -> str:
    """The entity_id a new install gets: the one the Italian names gave before they were
    translated (#178). With translated names Home Assistant would build it from the name
    in the install's language, and docs, the example package and the card's defaults
    could not name it. An existing install keeps the entity_id in its registry."""
    return f"{domain}.vimar_intercom_{object_id}"


def _model() -> str:
    """The paired Tab from the QR's product code first: SIP may name another device of
    the plant (an entrance panel), or nothing behind the cloud relay (#211). Then the
    SIP model, then a generic name."""
    code = R.PRODUCT_CODE
    if re.fullmatch(r"\d{4,6}", code):
        return profiles.model_name(code) or f"Vimar Elvox {code}"
    return S.DETECTED_MODEL or MODEL


def device_info(entry_id: str) -> DeviceInfo:
    """DeviceInfo del citofono, con il modello rilevato se disponibile."""
    info = DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Vimar Intercom",
        manufacturer=MANUFACTURER,
        model=_model(),
    )
    if S.DETECTED_FW:
        info["sw_version"] = S.DETECTED_FW
    return info
