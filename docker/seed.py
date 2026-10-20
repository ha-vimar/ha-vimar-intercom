"""Initialize only the private development config before Home Assistant starts."""
import json
import os
import shutil
from pathlib import Path

from homeassistant.components.onboarding import STORAGE_KEY, STORAGE_VERSION
from homeassistant.components.onboarding.const import STEP_USER, STEPS


def seed(state: Path, templates: Path) -> None:
    os.umask(0o077)
    state.mkdir(mode=0o700, exist_ok=True)
    state.chmod(0o700)
    config = state / "config"
    config.mkdir(mode=0o700, exist_ok=True)
    for name in ("configuration.yaml", "ui-lovelace.yaml"):
        if not (config / name).exists():
            shutil.copyfile(templates / name, config / name)
    credentials = state / "credentials.json"
    if not credentials.exists():
        credentials.write_text(json.dumps({
            "username": "developer", "password": "developer",
        }) + "\n")
    credentials.chmod(0o600)
    storage = config / ".storage"
    storage.mkdir(mode=0o700, exist_ok=True)
    onboarding = storage / STORAGE_KEY
    if not onboarding.exists():
        # Skip location/analytics/discovery onboarding; the HTTP user endpoint
        # still creates the actual owner and hashes the password normally.
        onboarding.write_text(json.dumps({
            "version": STORAGE_VERSION, "minor_version": 1, "key": STORAGE_KEY,
            "data": {"done": [step for step in STEPS if step != STEP_USER]},
        }) + "\n")
    print("Development configuration ready; no intercom entry or credentials imported.")


if __name__ == "__main__":
    seed(Path("/state"), Path("/dev-tools"))
