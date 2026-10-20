"""The names of the plant's well-known SIP addresses follow Home Assistant's language (#178):
Italian for Italian (any region), English for every other language."""
from __future__ import annotations

import pytest

from custom_components.vimar_intercom import plant_messages as pm
from custom_components.vimar_intercom import runtime as R


@pytest.mark.parametrize(("language", "name"), [
    ("it", "Targa Esterna"), ("it-IT", "Targa Esterna"),
    ("en", "Entrance panel"), ("en-GB", "Entrance panel"), ("de", "Entrance panel"), ("", "Entrance panel"),
])
def test_names_follow_the_home_assistant_language(monkeypatch, language, name):
    monkeypatch.setattr(R, "LANGUAGE", language)
    assert pm.sip_id_name("55001") == name
    assert pm.sip_id_name("55003") == "55003"  # an address without a name stays the address
    assert pm.sip_id_name(None) is None


def test_both_languages_name_the_same_addresses():
    assert set(pm.SIP_ID_NAMES) == {"it", "en"}
    assert set(pm.SIP_ID_NAMES["it"]) == set(pm.SIP_ID_NAMES["en"])
