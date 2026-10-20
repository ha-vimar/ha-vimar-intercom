"""Reset deletes development state without following links outside its mount."""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions, symlinks and bash")

SPEC = importlib.util.spec_from_file_location(
    "dev_reset", Path(__file__).resolve().parents[1] / "docker/reset.py")
reset_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reset_module)


def test_reset_refuses_an_unmounted_directory(tmp_path):
    saved = tmp_path / "keep.txt"
    saved.write_text("keep")
    with pytest.raises(ValueError, match="state mount"):
        reset_module.reset(tmp_path)
    assert saved.read_text() == "keep"


def test_reset_clears_nested_state_and_does_not_follow_symlinks(tmp_path, monkeypatch):
    state = tmp_path / "state"
    storage = state / "config/.storage"
    storage.mkdir(parents=True)
    (storage / "auth").write_text("synthetic")
    (state / "credentials.json").write_text("synthetic")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (state / "linked-directory").symlink_to(outside, target_is_directory=True)
    (state / "linked-file").symlink_to(outside / "keep.txt")
    (state / "broken-link").symlink_to(tmp_path / "missing")
    monkeypatch.setattr(Path, "is_mount", lambda path: path == state)
    reset_module.reset(state)
    assert list(state.iterdir()) == []
    assert (outside / "keep.txt").read_text() == "keep"
    reset_module.reset(state)  # Clearing an already-empty mount also succeeds.


@pytest.mark.parametrize("argument", ["up", "reset", "invalid"])
def test_launcher_refuses_unsafe_state_or_bad_arguments_before_docker(tmp_path, argument):
    tools = tmp_path / "docker"
    tools.mkdir()
    launcher = tools / "dev.sh"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "docker/dev.sh", launcher)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (tmp_path / ".dev-ha").symlink_to(outside, target_is_directory=True)
    binary = tmp_path / "bin"
    binary.mkdir()
    marker = tmp_path / "docker-was-called"
    fake_docker = binary / "docker"
    fake_docker.write_text('#!/bin/sh\ntouch "$DOCKER_MARKER"\n')
    fake_docker.chmod(0o755)
    result = subprocess.run(
        ["bash", str(launcher), argument], capture_output=True, text=True,
        env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"], "DOCKER_MARKER": str(marker)},
    )
    assert result.returncode == (2 if argument == "invalid" else 1)
    assert not marker.exists()
    assert (outside / "keep.txt").read_text() == "keep"
