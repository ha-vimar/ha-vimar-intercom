"""Validazione delle targhe nello step "settings" dell'options flow: solo id
SIP numerici (validate.sip_target), vuoto ammesso."""
from __future__ import annotations

import asyncio
import importlib
import types

import pytest


@pytest.fixture(scope="module")
def of():
    # conftest stubs the HA modules it imports, ConfigFlow included.
    return importlib.import_module("custom_components.vimar_intercom.options_flow")


class _Entry:
    def __init__(self, data: dict, options: dict | None = None):
        self.data = data
        self.options = options or {}


def _hass(**kw) -> types.SimpleNamespace:
    """hass finto: l'elenco utenti (per `allowed_users`) e ciò che il test aggiunge."""
    async def _users():
        return []
    return types.SimpleNamespace(auth=types.SimpleNamespace(async_get_users=_users), **kw)


def _flow(of, data: dict, options: dict | None = None):
    flow = of.OptionsFlowHandler(_Entry(data, options))
    flow.hass = _hass()
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    return flow


def _base_entry_data() -> dict:
    return {
        "sip_user": "12345", "sip_password": "x", "sip_domain": "example.test",
        "local_proxy": "192.0.2.1", "use_local_udp": False, "gid": "101",
    }


@pytest.mark.parametrize("key", ["camera_target", "internal_panel_target", "door_target"])
def test_non_digit_target_rejected(of, key):
    flow = _flow(of, _base_entry_data())
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1",
        "use_local_udp": False,
        key: "55001@altro",
    }))
    assert result["type"] == "form"
    assert result["errors"][key] == "invalid_target"


@pytest.mark.parametrize("target, key", [("55001@altro", "invalid_actuator_target"), ("AUTO", None), ("55002", None)])
def test_target_attuatore_come_le_targhe(of, target, key):
    """Il target di un attuatore finisce nella request line del MESSAGE: stessa regola
    numerica di hub.sip_uri, più AUTO (la targa dell'apri-porta)."""
    flow = _flow(of, _base_entry_data())
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1", "use_local_udp": False,
        "actuators": f'[{{"name": "Luce", "msg": "L", "target": "{target}", "icon": "light"}}]',
    }))
    if key:
        assert result["type"] == "form" and result["errors"]["actuators"] == key
        assert "'55001@altro'" in result["description_placeholders"]["actuators_error"]
    else:
        assert result["type"] == "create_entry" and result["data"]["actuators"][0]["target"] == target


def test_cartella_foto_sotto_www_rifiutata(of, tmp_path):
    """/config/www è servita su /local senza login: la foto della strada no."""
    flow = _flow(of, _base_entry_data())

    async def _job(f, *a):
        return f(*a)

    flow.hass = _hass(
        async_add_executor_job=_job,
        config=types.SimpleNamespace(is_allowed_path=lambda p: True, path=lambda *p: str(tmp_path.joinpath(*p))))
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1", "use_local_udp": False,
        "snapshot_dir": str(tmp_path / "www" / "citofono")}))
    assert result["type"] == "form" and result["errors"]["snapshot_dir"] == "path_public"


def test_messaggio_di_assenza_fuori_dalle_cartelle_lette_da_ha(of, tmp_path):
    """Il percorso va dritto a `ffmpeg -i`: stessa regola di snapshot_dir (is_allowed_path)."""
    flow = _flow(of, _base_entry_data())
    f = tmp_path / "messaggio.mp3"
    f.write_bytes(b"x")

    async def _job(fn, *a):
        return fn(*a)

    flow.hass = _hass(
        async_add_executor_job=_job,
        config=types.SimpleNamespace(is_allowed_path=lambda p: False, path=lambda *p: str(tmp_path.joinpath("cfg", *p))))
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1", "use_local_udp": False, "away_message_file": str(f)}))
    assert result["type"] == "form" and result["errors"]["away_message_file"] == "file_not_allowed"


def test_testo_del_messaggio_oltre_255_rifiutato(of):
    flow = _flow(of, _base_entry_data())
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1", "use_local_udp": False, "away_message_text": "x" * 256}))
    assert result["errors"]["away_message_text"] == "text_too_long"


def test_view_keepalive_predefinito_segue_il_cambio_di_modalita(of, monkeypatch):
    flow = _flow(of, {**_base_entry_data(), "use_local_udp": False})
    async def _ok(**kw): return True, ""
    monkeypatch.setattr(of, "_test_sip_registration", _ok)
    # cloud -> locale con il 120 della vecchia modalità: diventa il 0 della nuova
    r = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1", "use_local_udp": True, "view_keepalive": 120}))
    assert r["type"] == "create_entry" and r["data"]["view_keepalive"] == 0
    # un valore scelto dall'utente resta
    r = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1", "use_local_udp": True, "view_keepalive": 30}))
    assert r["data"]["view_keepalive"] == 30
def _form_defaults(of, monkeypatch, flow) -> dict:
    """The defaults the settings form offers, by key (voluptuous is a stub here)."""
    defaults: dict = {}

    def _optional(key, default=None, **_kw):
        defaults[key] = default
        return ("optional", key)

    monkeypatch.setattr(of.vol, "Optional", _optional)
    result = asyncio.run(flow.async_step_settings(None))
    assert result["type"] == "form"
    return defaults


def test_camera_target_is_not_prefilled_with_55100(of, monkeypatch):
    """Pre-filled 55100 got saved on the first save: CAMERA_TARGET_CONFIGURED
    became true and the panel learned from the last ring was never used."""
    defaults = _form_defaults(of, monkeypatch, _flow(of, _base_entry_data()))
    assert defaults["camera_target"] == ""


def test_camera_target_prefills_a_configured_value(of, monkeypatch):
    flow = _flow(of, _base_entry_data(), {"camera_target": "55102"})
    assert _form_defaults(of, monkeypatch, flow)["camera_target"] == "55102"


def test_an_empty_camera_target_is_saved_empty(of):
    flow = _flow(of, _base_entry_data(), {"camera_target": "55102"})
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1", "use_local_udp": False, "camera_target": ""}))
    assert result["type"] == "create_entry"
    assert result["data"]["camera_target"] == ""


@pytest.mark.parametrize("extra, expected", [
    ({"local_domain": "local.test"}, "local.test"),
    ({}, "example.test"),
])
def test_options_sip_test_uses_the_local_domain(of, monkeypatch, extra, expected):
    """In local UDP mode runtime.configure registers on local_domain: the
    options flow must test that domain, as the setup flow does."""
    seen = {}

    async def _test(**kw):
        seen.update(kw)
        return True, "ok"

    monkeypatch.setattr(of, "_test_sip_registration", _test)
    flow = _flow(of, {**_base_entry_data(), **extra})
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.9", "use_local_udp": True}))
    assert result["type"] == "create_entry"
    assert seen["sip_domain"] == expected


@pytest.mark.parametrize("test_ok, registered", [(True, 1), (False, 0)])
def test_the_options_sip_test_registers_the_live_binding_again(of, monkeypatch, test_ok, registered):
    """The test's unregister (Expires: 0, same +sip.instance) can remove the live
    binding: the running hub registers again at once, even if the form is not
    saved (review of #31)."""
    async def _test(**kw):
        return test_ok, "ok" if test_ok else "503"

    monkeypatch.setattr(of, "_test_sip_registration", _test)
    calls, tasks = [], []

    class Hub:
        async def async_register_now(self):
            calls.append(True)
            return True

    flow = _flow(of, _base_entry_data())
    flow._entry.entry_id = "e1"
    flow.hass.data = {of.DOMAIN: {"e1": {"hub": Hub()}}}
    flow.hass.async_create_background_task = lambda coro, name: tasks.append(coro)

    async def main():
        await flow.async_step_settings({"local_proxy": "192.0.2.9", "use_local_udp": True})
        for coro in tasks:
            await coro

    asyncio.run(main())
    assert len(calls) == registered
