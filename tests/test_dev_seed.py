"""Development startup must never overwrite an existing HA configuration."""
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions, symlinks and bash")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def seed(monkeypatch):
    onboarding = ModuleType("homeassistant.components.onboarding")
    onboarding.STORAGE_KEY = "onboarding"
    onboarding.STORAGE_VERSION = 4
    const = ModuleType("homeassistant.components.onboarding.const")
    const.STEP_USER = "user"
    const.STEPS = ["user", "core_config", "integration", "analytics"]
    monkeypatch.setitem(sys.modules, onboarding.__name__, onboarding)
    monkeypatch.setitem(sys.modules, const.__name__, const)
    spec = importlib.util.spec_from_file_location("dev_seed", ROOT / "docker" / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    previous_umask = os.umask(0o077)
    try:
        yield module.seed
    finally:
        os.umask(previous_umask)


def test_fresh_start_leaves_owner_creation_to_the_http_api(seed, tmp_path):
    state = tmp_path / "state"
    seed(state, ROOT / "docker")
    assert json.loads((state / "credentials.json").read_text()) == {
        "username": "developer", "password": "developer",
    }
    storage = state / "config" / ".storage"
    onboarding = json.loads((storage / "onboarding").read_text())
    assert set(onboarding["data"]["done"]) == {"core_config", "integration", "analytics"}
    assert not (storage / "auth").exists()
    assert not (storage / "core.config_entries").exists()
    assert (state.stat().st_mode & 0o777) == 0o700
    assert ((state / "credentials.json").stat().st_mode & 0o777) == 0o600


def test_updates_preserve_credentials_onboarding_and_user_configuration(seed, tmp_path):
    state = tmp_path / "state"
    seed(state, ROOT / "docker")
    saved = {
        "credentials.json": '{"username": "existing", "password": "synthetic-test-password"}\n',
        "config/.storage/onboarding": '{"data": {"done": ["user"]}}\n',
        "config/.storage/auth": '{"users": ["existing-owner"]}\n',
        "config/.storage/core.config_entries": '{"entries": ["existing-entry"]}\n',
        "config/configuration.yaml": "homeassistant:\n  name: Custom development\n",
        "config/ui-lovelace.yaml": "title: My dashboard\n",
    }
    for name, contents in saved.items():
        (state / name).write_text(contents)
    seed(state, ROOT / "docker")
    assert {name: (state / name).read_text() for name in saved} == saved


def test_missing_template_is_restored_without_resetting_other_files(seed, tmp_path):
    state = tmp_path / "state"
    seed(state, ROOT / "docker")
    (state / "config" / "ui-lovelace.yaml").unlink()
    seed(state, ROOT / "docker")
    assert (state / "config" / "ui-lovelace.yaml").read_bytes() == (ROOT / "docker" / "ui-lovelace.yaml").read_bytes()
