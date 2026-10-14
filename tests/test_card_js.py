"""The card's video player logic that a browser test can't pin down (pacing needs a fake
clock; frame_num on several encoder profiles), checked with node on the real nal-player.js."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).parent / "js"


@pytest.mark.parametrize("check", ["check_pacing.mjs", "check_frame_num.mjs"])
def test_nal_player_js(check):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    if check == "check_frame_num.mjs" and not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    r = subprocess.run(["node", str(JS / check)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
