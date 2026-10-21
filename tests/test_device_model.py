"""The model on the device page (#211).

The SIP User-Agent names the model on few plants: behind the cloud relay only Flexisip,
Linphone and `Vimar IP-PBX` answer. The fallback was the name of the development Tab
(40507), so a 40515 showed "Elvox Tab 7S 2F+ WiFi". Now the pairing QR's product code
(the Tab the user paired) comes first, then what SIP detected, then a generic name.
"""
from __future__ import annotations

import pytest

from custom_components.vimar_intercom import const, runtime
from custom_components.vimar_intercom import plant_state as S

device = pytest.importorskip("custom_components.vimar_intercom.device")

BASE = {"sip_user": "u", "sip_password": "p", "sip_domain": "d"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(S, "DETECTED_MODEL", "")
    monkeypatch.setattr(S, "DETECTED_FW", "")
    yield
    runtime.configure(BASE)


def _model(**data):
    runtime.configure({**BASE, **data})
    return device.device_info("e1")["model"]


def test_the_fallback_names_no_real_model():
    assert "Tab" not in const.MODEL and "40507" not in const.MODEL and "2F+" not in const.MODEL


def test_the_qr_product_code_names_the_paired_tab():
    assert _model(product_code="40515", plant_type="2FV2") == "Elvox Tab 5S Up (Due Fili Plus EVO)"


def test_the_qr_product_code_wins_over_a_sip_guess():
    """SIP may name another device of the plant (an entrance panel)."""
    assert _model(product_code="40517", detected_model="Elvox Pixel") == "Elvox Tab 7S Up (Due Fili Plus EVO)"


def test_an_unknown_product_code_is_shown_as_it_is():
    assert _model(product_code="40999") == "Vimar Elvox 40999"


def test_a_product_code_that_is_not_a_code_is_ignored():
    assert _model(product_code="<b>x</b>") == const.MODEL


def test_without_a_code_the_sip_model_then_the_generic_one():
    assert _model() == const.MODEL
    assert _model(detected_model="Elvox Tab 7S") == "Elvox Tab 7S"
