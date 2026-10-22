"""Rules for the test suite itself: a broken module must fail its tests, not
skip them; the HA stubs live in conftest only; the harness hands out ports
that cannot collide."""
from __future__ import annotations

import re
from pathlib import Path

from harness import rig

TESTS = Path(__file__).parent


def _test_files():
    return sorted(p for p in TESTS.glob("test_*.py") if p.name != Path(__file__).name)


def test_the_project_modules_are_imported_not_skipped():
    # pytest.importorskip turns an ImportError into a skip: a module of ours that
    # no longer imports would quietly drop its tests from the run. Third-party
    # packages may still be optional.
    found = [f"{p.name}:{n}" for p in _test_files()
             for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
             if 'importorskip("custom_components' in line]
    assert found == []


def test_only_conftest_replaces_the_ha_config_flow_base():
    # A module fixture that swaps ConfigFlow for a poorer stub never puts it
    # back: config_flow imported after it lacks _async_current_entries, and the
    # config flow tests failed or passed depending on the file order.
    rule = re.compile(r"(\bce|config_entries\"\])\.ConfigFlow\s*=")
    found = [p.name for p in _test_files() if rule.search(p.read_text(encoding="utf-8"))]
    assert found == []


def test_the_free_udp_ports_are_distinct():
    # Picked one at a time (bind, read, close) two "free" ports can be the same
    # one: the sockets are held until all of them are picked.
    ports = rig.free_udp_ports(8)
    assert len(set(ports)) == 8
