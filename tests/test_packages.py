"""The example package exists in Italian and in English (#178): the same automations, only
the words differ, and its entity_ids are the ones a new install gets."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORDS = {"alias", "description", "title", "message"}  # what the two languages may say differently


def _load(name):
    return yaml.safe_load((ROOT / "packages" / name).read_text(encoding="utf-8"))


def _without_words(node):
    if isinstance(node, dict):
        return {k: _without_words(v) for k, v in node.items() if k not in WORDS}
    if isinstance(node, list):
        return [_without_words(v) for v in node]
    if isinstance(node, str):
        return node.replace("YOUR_PHONE", "IL_TUO_TELEFONO")
    return node


def test_the_english_package_is_the_italian_one_translated():
    it, en = _load("vimar_intercom.yaml"), _load("vimar_intercom_en.yaml")
    assert _without_words(en) == _without_words(it)


def test_the_package_uses_the_entity_ids_of_a_new_install():
    from test_platform_setup import LEGACY_IDS

    text = (ROOT / "packages" / "vimar_intercom.yaml").read_text(encoding="utf-8")
    used = set(re.findall(r"\b(?:binary_sensor|button|camera|event|lock|sensor|switch)\.vimar_intercom_\w+", text))
    assert used and used <= set(LEGACY_IDS.values()), sorted(used - set(LEGACY_IDS.values()))
