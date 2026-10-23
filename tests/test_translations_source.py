"""What the user reads comes from the translations, and strings.json is HA's English source."""
from __future__ import annotations

import asyncio
import importlib
import json
import pathlib
import types

import pytest

BASE = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "vimar_intercom"


def _load(name):
    return json.loads((BASE / name).read_text(encoding="utf-8"))


def test_strings_json_is_the_english_source():
    # Home Assistant's convention: strings.json is English, en.json its copy.
    assert _load("strings.json") == _load("translations/en.json")


@pytest.mark.parametrize("lang", ["en", "it"])
def test_the_first_step_labels_its_two_ways_in_every_language(lang):
    tr = _load(f"translations/{lang}.json")
    assert set(tr["selector"]["mode"]["options"]) == {"qr", "manual"}
    assert tr["config"]["step"]["user"]["data"]["mode"]


def test_the_first_step_takes_its_labels_from_the_translations(monkeypatch):
    cf = importlib.import_module("custom_components.vimar_intercom.config_flow")
    seen = []
    monkeypatch.setattr(cf.selector, "SelectSelectorConfig", lambda **kw: seen.append(kw) or kw, raising=False)
    monkeypatch.setattr(cf.selector, "SelectSelector", lambda config: config, raising=False)
    flow = cf.VimarIntercomConfigFlow()
    flow.async_show_form = lambda **kw: kw
    asyncio.run(flow.async_step_user())
    assert seen and seen[0]["translation_key"] == "mode" and seen[0]["options"] == ["qr", "manual"]


def test_the_import_confirmation_speaks_ha_s_language():
    of = importlib.import_module("custom_components.vimar_intercom.options_flow")
    entry = types.SimpleNamespace(entry_id="e1", options={},
                                  data={"sip_user": "12345", "local_proxy": "192.0.2.1", "gid": "101"})
    flow = of.OptionsFlowHandler(entry)
    flow._imported = {"actuators": [{"name": "Door", "msg": "OPEN", "target": "55001", "icon": "door"}],
                      "sga": None, "door": "55002"}
    flow._imported_gid, flow._picg_from_rest, flow._phonebook_source = "101", None, "rubrica.db"
    flow.hass = types.SimpleNamespace(config=types.SimpleNamespace(language="en"))
    flow.async_show_form = lambda **kw: kw
    info = asyncio.run(flow.async_step_import_confirm())["description_placeholders"]
    assert info["sga_info"].startswith("not found")
    assert info["door_info"] == ("55002 — the panel of the door actuator (GID_PE); "
                                 "confirming sends the door command there.")


def test_every_phrase_has_the_same_placeholders_in_every_language():
    # A placeholder spelt differently in one language would make str.format fail.
    import re
    en, it = _load("translations/en.json"), _load("translations/it.json")

    def walk(node, path=()):
        for key, value in node.items():
            if isinstance(value, dict):
                yield from walk(value, (*path, key))
            else:
                yield (*path, key), set(re.findall(r"\{(\w+)\}", value))

    assert dict(walk(en)) == dict(walk(it))
