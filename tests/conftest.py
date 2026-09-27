"""Stub minimi di Home Assistant per eseguire i test senza HA installato.
Aggiunge la radice del repo a sys.path così `custom_components.vimar_intercom` è importabile."""
from __future__ import annotations

import importlib.machinery
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "vimar_intercom"
sys.path.insert(0, str(ROOT))


def _stub_package(name: str, path: Path) -> types.ModuleType:
    """Registra un package il cui corpo non viene mai eseguito.

    `custom_components/vimar_intercom/__init__.py` importa aiohttp, Home
    Assistant, l'hub e le view HTTP: importarlo per arrivare a un modulo puro
    tirerebbe dentro tutto il componente. Con un package fittizio il cui
    `__path__` punta alla cartella, `from custom_components.vimar_intercom
    import runtime` risolve `runtime.py` normalmente — import relativi
    compresi — senza che `__init__.py` giri mai.
    """
    module = types.ModuleType(name)
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(path)]
    module.__path__ = [str(path)]
    module.__spec__ = spec
    sys.modules[name] = module
    return module


_parent = _stub_package("custom_components", COMPONENT.parent)
_parent.vimar_intercom = _stub_package("custom_components.vimar_intercom", COMPONENT)


def _mod(name: str, **attrs) -> types.ModuleType:
    m = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Any:
    """Classe jolly: accetta qualsiasi sottoclasse/attributo/chiamata."""
    def __init__(self, *a, **k): ...
    # HA dichiara le sottoclassi con argomenti, es. ConfigFlow(domain=DOMAIN).
    def __init_subclass__(cls, **k): ...
    def __getattr__(self, item):
        return _Any()
    def __call__(self, *a, **k):
        return _Any()
    def __iter__(self):
        return iter(())


def _stub_symbol(module, name):
    cache = module.__dict__.setdefault("_stub_symbols", {})
    if name not in cache:
        cache[name] = type(name, (_Any,), {})
    return cache[name]


def _stub_ha() -> None:
    if "homeassistant" in sys.modules and not getattr(sys.modules["homeassistant"], "_is_stub", False):
        return  # HA vero installato
    ha = _mod("homeassistant", _is_stub=True)
    for sub in [
        "core", "config_entries", "const", "exceptions", "helpers", "helpers.entity", "helpers.entity_platform",
        "helpers.restore_state", "helpers.device_registry", "helpers.storage", "helpers.event", "helpers.aiohttp_client",
        "helpers.config_validation", "helpers.service", "helpers.network", "components", "components.http", "components.http.auth",
        "components.camera", "components.sensor",
        "components.binary_sensor", "components.switch", "components.button", "components.event", "components.lock",
        "components.ffmpeg", "components.file_upload", "helpers.selector",
        "data_entry_flow", "util", "util.dt",
    ]:
        m = _mod(f"homeassistant.{sub}")
        # Ogni nome deve dare una classe DIVERSA: restituendo sempre la stessa,
        # una classe che eredita da due simboli stub (es. SwitchEntity e
        # RestoreEntity) fallirebbe con "duplicate base class".
        m.__getattr__ = lambda name, _m=m: _stub_symbol(_m, name)  # type: ignore[attr-defined]
        parent, _, child = f"homeassistant.{sub}".rpartition(".")
        setattr(sys.modules[parent], child, m)
    ha.core.HomeAssistant = _Any
    ha.core.ServiceCall = _Any
    ha.core.callback = lambda f: f
    ha.config_entries.ConfigEntry = _Any
    ha.config_entries.ConfigFlow = _Any
    ha.config_entries.OptionsFlow = _Any
    ha.data_entry_flow.FlowResult = _Any
    ha.const.Platform = _Any()
    _mod("voluptuous", Schema=_Any, Required=_Any, Optional=_Any, All=_Any, Coerce=_Any, In=_Any, Range=_Any)
    _mod("aiohttp", web=_Any(), ClientSession=_Any)


_stub_ha()

# pycryptodome è un requirement reale: se manca, i test che lo usano vengono saltati
try:
    import Crypto  # noqa: F401
except Exception:  # pragma: no cover
    pass
