"""La conferma dell'import rubrica: il PICG che promette è quello che salva.

Regressione dalla PR #16. Con la rubrica caricata **da file** il riepilogo diceva
«resta quello già configurato», ma il salvataggio sovrascriveva `picg_target`
con l'SGA letto dalla rubrica: un `60001` messo a mano per un impianto 2FV2
diventava `55001`. Il file non dice nulla sul PICG, quindi non deve toccarlo.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest


def _stub(name: str, **attrs) -> None:
    module = sys.modules.get(name) or types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


@pytest.fixture(scope="module")
def of():
    # Moduli di HA che config_flow importa e che conftest.py non fornisce.
    _stub("homeassistant.components.file_upload", process_uploaded_file=None)
    _stub("homeassistant.data_entry_flow", FlowResult=dict)
    _stub("homeassistant.helpers.selector")

    # `class VimarIntercomConfigFlow(ConfigFlow, domain=DOMAIN)`: la base deve
    # accettare l'argomento di classe, cosa che lo stub generico non fa.
    class _ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    ce = sys.modules["homeassistant.config_entries"]
    if getattr(sys.modules["homeassistant"], "_is_stub", False):
        ce.ConfigFlow = _ConfigFlow
    return pytest.importorskip("custom_components.vimar_intercom.options_flow")


class _Entry:
    def __init__(self, options: dict):
        self.data = {"sip_user": "12345", "sip_password": "x", "local_proxy": "192.0.2.1", "gid": "101"}
        self.options = options


def _flow(of, options: dict, picg_from_rest: str | None = None):
    flow = of.OptionsFlowHandler(_Entry(options))
    flow._imported = {
        "actuators": [{"name": "Serratura", "msg": "OPEN", "target": "55001", "icon": "door"}],
        "sga": "55001",
        "system": {},
    }
    flow._imported_gid = "101"
    flow._picg_from_rest = picg_from_rest
    # Le due chiamate di HA che il passo usa: restituiscono ciò che ricevono.
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    return flow


def _confirm(of, options, picg_from_rest=None):
    form = asyncio.run(_flow(of, options, picg_from_rest).async_step_import_confirm(None))
    saved = asyncio.run(_flow(of, options, picg_from_rest).async_step_import_confirm({}))
    return form["description_placeholders"]["picg_info"], saved["data"]["picg_target"]


def test_da_file_il_picg_configurato_a_mano_resta(of):
    info, saved = _confirm(of, {"sga_target": "55001", "picg_target": "60001"})
    assert saved == "60001"
    assert "resta" in info and "60001" in info


def test_da_file_senza_picg_configurato_prende_l_sga(of):
    """Comportamento storico dell'importer, che resta valido quando non c'è altro."""
    info, saved = _confirm(of, {"sga_target": "55001"})
    assert saved == "55001"
    assert "uguale all'SGA" in info


def test_dal_citofono_vince_il_picg_dichiarato(of):
    info, saved = _confirm(of, {"sga_target": "55001", "picg_target": "60001"}, picg_from_rest="55009")
    assert saved == "55009"
    assert "dichiarato dal citofono" in info


def test_il_riepilogo_e_il_salvataggio_non_divergono_mai(of):
    """Qualunque combinazione: il valore nel riepilogo è quello salvato."""
    for options in ({}, {"picg_target": "60001"}, {"sga_target": "55001"}):
        for rest in (None, "55001", "55009"):
            info, saved = _confirm(of, options, rest)
            assert saved in info, (options, rest, info, saved)


# ─── camera_target dall'import rubrica — issue #3 ────────────────────────────

def _confirm_camera(of, options, camera):
    def mk():
        f = _flow(of, options)
        f._imported = {**f._imported, "camera": camera}
        return f
    form = asyncio.run(mk().async_step_import_confirm(None))
    saved = asyncio.run(mk().async_step_import_confirm({}))
    return form["description_placeholders"]["camera_info"], saved["data"]


def test_import_imposta_la_targa_video_della_rubrica(of):
    info, data = _confirm_camera(of, {}, "55001")
    assert data["camera_target"] == "55001"
    assert "55001" in info


def test_import_senza_pe_lascia_la_targa_configurata(of):
    info, data = _confirm_camera(of, {"camera_target": "55100"}, None)
    assert data["camera_target"] == "55100"
    assert "resta" in info


def test_import_non_tocca_il_pannello_interno(of):
    """La rubrica non dice quale sia: il valore a mano deve sopravvivere."""
    _, data = _confirm_camera(of, {"internal_panel_target": "60001"}, "55001")
    assert data["internal_panel_target"] == "60001"


# ─── Each options page saves only its own fields ─────────────────────────────

FOREIGN = {"homekit_accessory": True, "homekit_video_smooth": False,
           "homekit_answer": "open", "door_target": "55001",
           "camera_target": "55001", "something_new": 1}


def _save(of, step, user_input, options):
    flow = _flow(of, dict(options))
    return asyncio.run(getattr(flow, step)(user_input))


def test_the_homekit_page_keeps_the_network_settings(of):
    options = {**FOREIGN, "local_proxy": "192.0.2.1", "sga_target": "61000"}
    out = _save(of, "async_step_homekit",
                {"homekit_accessory": False, "homekit_video_smooth": True,
                 "homekit_answer": "talk"}, options)
    assert out["type"] == "create_entry"
    assert out["data"]["sga_target"] == "61000"
    assert out["data"]["door_target"] == "55001"
    assert out["data"]["homekit_accessory"] is False
    assert out["data"]["homekit_answer"] == "talk"
    assert out["data"]["homekit_ring_button"] is False, "off unless ticked"


SETTINGS = {"local_proxy": "192.0.2.1", "use_local_udp": False, "local_udp_port": 5060,
            "media_enc": False, "actuators": "", "sga_target": "61000",
            "picg_target": "61000", "camera_target": "55001", "door_target": "55001"}


def test_the_settings_page_saves_the_video_bandwidth(of):
    """It changes every video from the panel, so it lives on the general page."""
    out = _save(of, "async_step_settings", {**SETTINGS, "video_bandwidth": "2048"}, FOREIGN)
    assert out["type"] == "create_entry", out
    assert out["data"]["video_bandwidth"] == "2048"
    out = _save(of, "async_step_settings", SETTINGS, FOREIGN)
    assert out["data"]["video_bandwidth"] == "256", "the default when not chosen"
    out = _save(of, "async_step_settings", SETTINGS, {**FOREIGN, "video_bandwidth": "2048"})
    assert out["data"]["video_bandwidth"] == "2048", "a saved choice is kept"


def test_the_homekit_page_keeps_the_video_bandwidth(of):
    out = _save(of, "async_step_homekit", {"homekit_accessory": True},
                {**FOREIGN, "video_bandwidth": "2048"})
    assert out["data"]["video_bandwidth"] == "2048"


def test_the_settings_page_keeps_homekit_and_unknown_keys(of):
    """Saving the network page used to drop the HomeKit options: HomeKit
    switched itself off after an unrelated change."""
    out = _save(of, "async_step_settings", {
        "local_proxy": "192.0.2.1", "use_local_udp": False, "local_udp_port": 5060,
        "media_enc": False, "actuators": "", "sga_target": "61000",
        "picg_target": "61000", "camera_target": "55001", "door_target": "55001",
    }, FOREIGN)
    assert out["type"] == "create_entry", out
    assert out["data"]["homekit_accessory"] is True
    assert out["data"]["homekit_video_smooth"] is False
    assert out["data"]["homekit_answer"] == "open"
    assert out["data"]["something_new"] == 1


# ─── HomeKit pairing: the code and QR live on the options page ───────────────

class _Hass:
    def __init__(self, data, language="en"):
        self.data = data
        self.config = type("C", (), {"language": language})()


def _homekit_form(of, monkeypatch, data, language="en"):
    signed = []

    def sign(_hass, path, expiration):
        signed.append((path, expiration))
        return f"{path}&authSig=SIGNED"

    monkeypatch.setitem(sys.modules, "homeassistant.components.http.auth",
                        types.SimpleNamespace(async_sign_path=sign))
    flow = _flow(of, {"homekit_accessory": True})
    flow._entry.entry_id = "e1"
    flow.hass = _Hass(data, language)
    form = asyncio.run(flow.async_step_homekit(None))
    return form["description_placeholders"]["pairing"], signed


def test_the_homekit_page_shows_the_code_and_a_signed_qr(of, monkeypatch):
    """The persistent notification is seen by every user; the options page
    only by administrators. The QR view needs an authenticated admin, so the
    image goes through a signed path."""
    data = {of.HOMEKIT_DATA: {"pairing": {"e1": {"pin": "123-45-678", "token": "tok",
                                                  "svg": b"<svg/>"}}}}
    text, signed = _homekit_form(of, monkeypatch, data)
    assert "123-45-678" in text
    assert f"![QR]({of.HOMEKIT_QR_URL}?t=tok&authSig=SIGNED)" in text
    assert signed and signed[0][0] == f"{of.HOMEKIT_QR_URL}?t=tok"


def test_the_homekit_page_shows_nothing_once_paired(of, monkeypatch):
    text, signed = _homekit_form(of, monkeypatch, {of.HOMEKIT_DATA: {"pairing": {}}})
    assert text == "" and signed == []
    text, _ = _homekit_form(of, monkeypatch, {})
    assert text == ""


def test_every_language_has_the_pairing_placeholder():
    import json
    from pathlib import Path
    base = Path(__file__).parent.parent / "custom_components" / "vimar_intercom"
    for path in [base / "strings.json", *sorted((base / "translations").glob("*.json"))]:
        step = json.loads(path.read_text(encoding="utf-8"))["options"]["step"]["homekit"]
        assert "{pairing}" in step["description"], path.name
