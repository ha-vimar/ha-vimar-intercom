"""Smoke test: i moduli puri devono importarsi con gli stub HA."""
import importlib
from pathlib import Path

import pytest

PURE = ["const", "qr_decoder", "model_detect", "srtp", "rubrica_import", "runtime", "log_redact", "validate", "rest_client"]

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "vimar_intercom"

# Import che un modulo "puro" non deve avere: sono ciò che lo renderebbe
# testabile solo dentro Home Assistant.
FORBIDDEN_IMPORTS = ("homeassistant", "aiohttp", "voluptuous")


@pytest.mark.parametrize("name", PURE)
def test_import_pure_modules(name):
    mod = importlib.import_module(f"custom_components.vimar_intercom.{name}")
    assert mod is not None


@pytest.mark.parametrize("name", PURE)
def test_pure_modules_do_not_import_home_assistant(name):
    """Controlla il sorgente, non l'import.

    Gli stub in conftest.py fanno riuscire `import homeassistant` anche dove non
    dovrebbe esserci: senza guardare il testo del modulo, un import di HA che si
    infilasse in un modulo puro passerebbe inosservato fino al primo test fuori
    da questa suite.
    """
    src = (COMPONENT / f"{name}.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            continue
        for forbidden in FORBIDDEN_IMPORTS:
            assert not stripped.startswith((f"import {forbidden}", f"from {forbidden}")), (
                f"{name}.py importa {forbidden}: i moduli puri devono restare "
                f"testabili senza Home Assistant ({stripped})"
            )


def test_const_has_no_guessed_actuators():
    from custom_components.vimar_intercom import const
    # ADR-4: nessun attuatore ipotizzato nel codice attivo
    assert getattr(const, "ACTUATORS", []) == []
    assert const.DOOR_COMMAND == "OPEN_2F"
    assert const.SEGRETERIA_HEADER_VALUE == "blue"


def test_no_credentials_in_const():
    import inspect

    from custom_components.vimar_intercom import const
    src = inspect.getsource(const)
    for bad in ("PWD=", "password=", "60992:"):
        assert bad not in src
