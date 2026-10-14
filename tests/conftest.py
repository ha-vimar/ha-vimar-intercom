"""Stub minimi di Home Assistant per eseguire i test senza HA installato.
Aggiunge la radice del repo a sys.path così `custom_components.vimar_intercom` è importabile.

Marker (esclusi di default da pyproject, come `live`): `media` vuole ffmpeg/ffprobe e
aiohttp, `browser` vuole Playwright e aiohttp. Se mancano, i test si saltano da soli.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

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
    def __getattr__(self, item):
        return _Any()
    def __call__(self, *a, **k):
        return _Any()
    def __iter__(self):
        return iter(())


class _ConfigFlow:
    """`class VimarIntercomConfigFlow(ConfigFlow, domain=DOMAIN)`: the base must
    accept the class argument, which the generic jolly does not."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    # As in Home Assistant: no entries unless a test gives some.
    def _async_current_entries(self, include_ignore=None):
        return []

    def async_abort(self, *, reason, description_placeholders=None):
        return {"type": "abort", "reason": reason}


@dataclass(frozen=True, kw_only=True)
class _SensorEntityDescription:
    key: str
    name: str | None = None
    icon: str | None = None
    device_class: str | None = None
    options: list | None = None
    state_class: str | None = None
    native_unit_of_measurement: str | None = None


class _HomeAssistantError(Exception):
    """HA's signature: the integration's errors carry a translation key (#128)."""

    def __init__(self, *args, translation_domain=None, translation_key=None, translation_placeholders=None):
        super().__init__(*args)
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders


def _stub_ha() -> None:
    if "homeassistant" in sys.modules and not getattr(sys.modules["homeassistant"], "_is_stub", False):
        return  # HA vero installato
    ha = _mod("homeassistant", _is_stub=True)
    for sub in [
        "core", "config_entries", "const", "exceptions", "helpers", "helpers.entity", "helpers.entity_platform",
        "helpers.restore_state", "helpers.device_registry", "helpers.entity_registry", "helpers.storage", "helpers.event", "helpers.aiohttp_client",
        "helpers.config_validation", "helpers.selector", "helpers.start", "requirements", "components", "components.http", "components.camera",
        "components.sensor", "components.binary_sensor", "components.switch", "components.button", "components.event",
        "components.lock", "components.select", "components.text", "components.number", "components.ffmpeg", "components.tts", "util", "util.dt",
        "components.file_upload", "data_entry_flow",
    ]:
        m = _mod(f"homeassistant.{sub}")
        m.__getattr__ = lambda name, _m=m: _Any  # type: ignore[attr-defined]
        parent, _, child = f"homeassistant.{sub}".rpartition(".")
        setattr(sys.modules[parent], child, m)
    sys.modules["homeassistant.const"].EntityCategory = types.SimpleNamespace(CONFIG="config", DIAGNOSTIC="diagnostic")
    sys.modules["homeassistant.helpers.entity_registry"].async_get = lambda hass: _Any()
    ha.core.HomeAssistant = _Any
    ha.core.ServiceCall = _Any
    ha.core.callback = lambda f: f
    ha.core.SupportsResponse = types.SimpleNamespace(NONE="none", OPTIONAL="optional", ONLY="only")
    # Real exception classes: code under test raises them and tests expect them.
    for exc in ("Unauthorized", "ConfigEntryNotReady"):
        setattr(ha.exceptions, exc, type(exc, (Exception,), {}))
    ha.exceptions.HomeAssistantError = _HomeAssistantError
    ha.config_entries.ConfigEntry = _Any
    ha.config_entries.ConfigFlow = _ConfigFlow
    ha.config_entries.OptionsFlow = _Any
    ha.const.Platform = _Any()
    # selector: il config flow ne usa classi e attributi (SelectSelectorMode.DROPDOWN)
    # al momento di costruire lo schema, non solo all'import.
    sel = _mod("homeassistant.helpers.selector",
               SelectSelector=_Any, SelectSelectorConfig=_Any, SelectSelectorMode=_Any(),
               FileSelector=_Any, FileSelectorConfig=_Any)
    ha.helpers.selector = sel
    ha.components.file_upload.process_uploaded_file = None
    ha.data_entry_flow.FlowResult = dict
    # switch.py derives from both: two distinct classes, not the same jolly twice.
    ha.components.switch.SwitchEntity = type("SwitchEntity", (), {})
    ha.helpers.restore_state.RestoreEntity = type("RestoreEntity", (), {})
    # sensor.py builds dataclasses and enums from these at import time.
    s = ha.components.sensor
    s.SensorEntityDescription = _SensorEntityDescription
    s.RestoreSensor = type("RestoreSensor", (), {})
    s.SensorDeviceClass = types.SimpleNamespace(ENUM="enum", TIMESTAMP="timestamp", DURATION="duration")
    s.SensorStateClass = types.SimpleNamespace(TOTAL_INCREASING="total_increasing",
                                               MEASUREMENT="measurement")
    ha.const.UnitOfTime = types.SimpleNamespace(SECONDS="s")
    # Enum members the platforms read at class definition time.
    ha.components.binary_sensor.BinarySensorDeviceClass = types.SimpleNamespace(CONNECTIVITY="connectivity")
    ha.components.event.EventDeviceClass = types.SimpleNamespace(DOORBELL="doorbell")
    ha.components.camera.CameraEntityFeature = types.SimpleNamespace(STREAM=2)
    # DeviceInfo is a TypedDict in Home Assistant: a plain dict behaves the same.
    ha.helpers.device_registry.DeviceInfo = dict
    _mod("voluptuous", Schema=_Any, Required=_Any, Optional=_Any, All=_Any, Coerce=_Any, In=_Any, Range=_Any,
         Match=_Any, Invalid=type("Invalid", (Exception,), {}))


_stub_ha()

try:
    import aiohttp  # noqa: F401
    REAL_AIOHTTP = True
except ImportError:  # __init__.py lo importa: basta uno stub (le view usano harness.web.WEB)
    _mod("aiohttp", web=_Any(), ClientSession=_Any)
    REAL_AIOHTTP = False

_MISSING = {
    "media": None if REAL_AIOHTTP and shutil.which("ffmpeg") and shutil.which("ffprobe")
    else "servono ffmpeg, ffprobe e aiohttp",
    "browser": None if REAL_AIOHTTP and importlib.util.find_spec("playwright")
    else "servono playwright e aiohttp",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        for marker, reason in _MISSING.items():
            if reason and marker in item.keywords:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(autouse=True)
def _keep_runtime(monkeypatch):
    """runtime.configure() rewrites module globals (SIP_USER, SIP_DOMAIN, ...) and
    resets plant_state's: restore every uppercase attribute of both after each
    test so none leaks into the next. It also registers the values log_redact masks."""
    from custom_components.vimar_intercom import log_redact
    from custom_components.vimar_intercom import plant_state as S
    from custom_components.vimar_intercom import runtime as R
    for mod in (R, S):
        for name in dir(mod):
            if name.isupper():
                monkeypatch.setattr(mod, name, getattr(mod, name))
    monkeypatch.setattr(log_redact, "_plant", log_redact._plant)


@pytest.fixture(autouse=True)
def _fresh_sip_state(monkeypatch):
    """sip_client keeps the call, the dialogs and the device list in module
    globals: each test gets a fresh device list and the module is put back to
    its starting state afterwards, so nothing a test leaves behind (a call
    "in progress", an SRTP key, a seen request) leaks into the next one."""
    try:
        from custom_components.vimar_intercom import sip_client as sip
        from custom_components.vimar_intercom.inventory import DeviceInventory
    except Exception:  # noqa: BLE001 - a module that does not import skips its own tests
        yield
        return
    monkeypatch.setattr(sip, "DEVICES", DeviceInventory())
    monkeypatch.setattr(sip.SM, "_nonce_count", {})
    for name in ("reader", "writer", "_udp_sock", "_state_change_callback", "MY_IP"):
        monkeypatch.setattr(sip, name, getattr(sip, name))
    yield
    # Nothing to close or notify: those references belong to the test.
    for name in ("reader", "writer", "_udp_sock", "_state_change_callback", "MY_IP"):
        setattr(sip, name, None)
    sip.reset_state()


@pytest.fixture
def assert_private_mode():
    """Check that a file is readable and writable by its owner only (0600).

    Takes a path, or a mode read earlier for a file that is gone by now. On
    Windows it checks nothing: there are no POSIX permissions there, os.stat()
    reports 0o666 whatever os.open asked for."""
    def check(path_or_mode) -> None:
        if sys.platform == "win32":
            return
        mode = path_or_mode if isinstance(path_or_mode, int) else os.stat(path_or_mode).st_mode
        assert oct(mode & 0o777) == "0o600"
    return check


@pytest.fixture
def hub(monkeypatch):
    """Hub vero con SIP finto: registrato, a riposo; do_call riuscita e registrata in hub.chiamate."""
    from custom_components.vimar_intercom import hub as hub_mod
    sip = hub_mod.sip
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    monkeypatch.setattr(sip, "registered", True)
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    monkeypatch.setitem(sip.pending_incoming, "active", False)
    h.chiamate = []

    async def _fake_do_call(target=None, silence_limit=None, **_kw):
        h.chiamate.append(target)
        return True, "200"

    monkeypatch.setattr(sip, "do_call", _fake_do_call)
    return h
