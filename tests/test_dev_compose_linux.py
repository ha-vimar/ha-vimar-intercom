"""Exercise Linux Docker Engine ownership/reset in a disposable CI checkout.

A dedicated, path-filtered GitHub Actions workflow selects the docker marker.
Local unit tests must not start Docker or operate a developer's retained
Home Assistant configuration.
"""
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.docker
@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true",
    reason="Linux Docker Engine lifecycle check runs only on disposable GitHub Actions runners",
)
def test_linux_engine_onboarding_persistence_and_root_owned_reset(tmp_path):
    checkout = tmp_path / "checkout"
    shutil.copytree(ROOT / "docker", checkout / "docker")
    shutil.copytree(ROOT / "custom_components/vimar_intercom", checkout / "custom_components/vimar_intercom")
    project = "vimar-dev-ci-" + uuid.uuid4().hex[:10]
    env = {**os.environ, "COMPOSE_PROJECT_NAME": project, "HA_DEV_PORT": "18123"}
    compose = ["docker", "compose", "-f", "docker/docker-compose.yaml"]

    def run(*args, timeout=300):
        result = subprocess.run(args, cwd=checkout, env=env, capture_output=True, text=True, timeout=timeout)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    info = json.loads(run("docker", "info", "--format", "{{json .}}"))
    assert info["OSType"] == "linux" and "Docker Desktop" not in info["OperatingSystem"]
    engine = info["ServerVersion"]
    try:
        run("./docker/dev.sh")
        # Do not read the private files as the host user: this is the root-owned
        # Linux case. Hash them through an offline seed container instead.
        snapshot = """
import hashlib, json
from pathlib import Path
p = Path('/state')
names = ['credentials.json', 'config/.storage/auth_provider.homeassistant',
         'config/configuration.yaml', 'config/ui-lovelace.yaml']
print(json.dumps({n: hashlib.sha256((p/n).read_bytes()).hexdigest() for n in names}))
"""
        before = run(*compose, "run", "--rm", "--no-deps", "--entrypoint", "python", "seed", "-c", snapshot)
        run("./docker/dev.sh")
        after = run(*compose, "run", "--rm", "--no-deps", "--entrypoint", "python", "seed", "-c", snapshot)
        assert json.loads(before) == json.loads(after)
        # Assert this really exercised the reviewer's root ownership case.
        owner = run(*compose, "run", "--rm", "--no-deps", "--entrypoint", "python", "seed", "-c",
                    "from pathlib import Path; print(Path('/state/config').stat().st_uid)")
        assert owner.strip() == "0"
        run("./docker/dev.sh", "reset")
        assert not (checkout / ".dev-ha").exists()
        assert not run("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project).strip()
        # Startup after reset must create a usable new owner/configuration too.
        run("./docker/dev.sh")
        run("./docker/dev.sh", "reset")
        assert not (checkout / ".dev-ha").exists()
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a") as output:
                output.write(f"\nLinux Docker Engine {engine}: fresh onboarding, restart persistence, "
                             "root-owned state reset and fresh onboarding after reset passed.\n")
    finally:
        # Teardown touches only the unique project in this disposable checkout.
        subprocess.run(compose + ["logs", "--no-color", "--tail", "50"], cwd=checkout, env=env, check=False)
        subprocess.run(["./docker/dev.sh", "reset"], cwd=checkout, env=env, check=False, timeout=120)
