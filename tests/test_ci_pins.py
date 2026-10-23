"""The CI runs fixed versions: a new release of a tool or an action cannot turn it red on its own.
Dependabot (.github/dependabot.yml) moves them, once a month, in a PR of its own."""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def test_no_workflow_follows_a_branch_of_an_action():
    moving = [f"{w.name}: {m}" for w in WORKFLOWS
              for m in re.findall(r"uses:\s*(\S+@(?:main|master))", w.read_text(encoding="utf-8"))]
    assert moving == []


def test_the_dev_tools_are_pinned_exactly():
    lines = [ln.strip() for ln in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()]
    assert [ln for ln in lines if ln and not ln.startswith("#") and "==" not in ln] == []


def test_the_ruff_job_takes_its_version_from_requirements_dev():
    validate = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    assert "requirements-dev.txt" in validate.split("ruff check")[0].rsplit("name: ruff", 1)[1]
    assert not re.search(r"pip install ruff==", validate)


def test_a_branch_push_does_not_run_the_ci_twice():
    validate = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    on = validate.split("\non:", 1)[1].split("\npermissions:", 1)[0]
    assert re.search(r"push:\s*\n\s*branches:\s*\[main\]", on)
