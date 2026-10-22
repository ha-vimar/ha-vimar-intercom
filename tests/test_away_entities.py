"""Messaggio di assenza dalla pagina del dispositivo: le entità scrivono le opzioni."""
from __future__ import annotations

import asyncio
import types

import pytest

from custom_components.vimar_intercom import away_config as ac
from custom_components.vimar_intercom import runtime as R  # noqa: E402
from custom_components.vimar_intercom.const import DOMAIN  # noqa: E402
from custom_components.vimar_intercom.select import VimarAwayFileSelect  # noqa: E402
from custom_components.vimar_intercom.text import VimarAwayText  # noqa: E402


@pytest.fixture(autouse=True)
def _no_calling_user(monkeypatch):
    """Real entities have `_context = None` until a service call sets it; the HA stub does not."""
    for cls in (VimarAwayText, VimarAwayFileSelect):
        monkeypatch.setattr(cls, "_context", None, raising=False)


class _Hub:
    touched = 0

    def notify(self):
        self.touched += 1


async def ac_ensure(hass):
    await hass.async_add_executor_job(ac.ensure_dir, ac.messages_dir(hass))


def _env(tmp_path, monkeypatch):
    for k in ("AWAY_MESSAGE_TEXT", "AWAY_MESSAGE_FILE", "AWAY_MESSAGE_DELAY"):
        monkeypatch.setattr(R, k, "" if k != "AWAY_MESSAGE_DELAY" else 0)
    entry = types.SimpleNamespace(entry_id="e1", options={"x": 1})
    calls = []

    def upd(e, options):
        calls.append(options)
        e.options = options
        return True

    async def executor(f, *a):
        return f(*a)

    hass = types.SimpleNamespace(
        data={DOMAIN: {"e1": {"hub": _Hub()}}},
        config=types.SimpleNamespace(media_dirs={"local": str(tmp_path)}),
        config_entries=types.SimpleNamespace(async_update_entry=upd),
        async_add_executor_job=executor)
    return hass, entry, calls


def test_text_and_delay_write_options_without_reload(tmp_path, monkeypatch):
    hass, entry, calls = _env(tmp_path, monkeypatch)
    t = VimarAwayText(entry)
    t.hass = hass
    t.async_write_ha_state = lambda: None
    asyncio.run(t.async_set_value("  Torno subito "))
    assert entry.options == {"x": 1, "away_message_text": "Torno subito"}
    assert R.AWAY_MESSAGE_TEXT == "Torno subito" and R.away_message_configured()
    assert t.native_value == "Torno subito"
    monkeypatch.setattr(R, "AWAY_MESSAGE_TEXT", "x" * 300)
    assert len(t.native_value) == 255                  # mai oltre il limite dell'entità
    monkeypatch.setattr(R, "AWAY_MESSAGE_TEXT", "")
    assert not R.away_message_configured()             # il ritardo (0) non c'entra più
    assert hass.data[DOMAIN]["e1"]["hub"].touched == 1


def test_file_select_lists_folder_and_writes(tmp_path, monkeypatch):
    hass, entry, _ = _env(tmp_path, monkeypatch)
    d = tmp_path / "citofono" / "messaggi"
    d.mkdir(parents=True)
    (d / "b.mp3").write_bytes(b"x")
    (d / "a.WAV").write_bytes(b"x")
    (d / "note.txt").write_text("x")
    asyncio.run(ac_ensure(hass))
    s = VimarAwayFileSelect(entry)
    s.hass = hass
    s.async_write_ha_state = lambda: None
    asyncio.run(s.async_update())
    assert s.options == [ac.NONE_OPTION, "a.WAV", "b.mp3"]
    asyncio.run(s.async_select_option("b.mp3"))
    assert entry.options["away_message_file"] == str(d / "b.mp3") and s.current_option == "b.mp3"
    asyncio.run(s.async_select_option(ac.NONE_OPTION))
    assert entry.options["away_message_file"] == "" and s.current_option == ac.NONE_OPTION


def test_missing_folder_is_created(tmp_path, monkeypatch):
    hass, entry, _ = _env(tmp_path, monkeypatch)
    s = VimarAwayFileSelect(entry)
    s.hass = hass
    asyncio.run(ac_ensure(hass))
    asyncio.run(s.async_update())
    assert (tmp_path / "citofono" / "messaggi").is_dir() and s.options == [ac.NONE_OPTION]


def test_file_fuori_cartella_si_vede_e_non_diventa_nessuno(tmp_path, monkeypatch):
    hass, entry, _ = _env(tmp_path, monkeypatch)
    fuori = str(tmp_path / "altrove" / "c.mp3")
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", fuori)
    s = VimarAwayFileSelect(entry)
    s.hass = hass
    s.async_write_ha_state = lambda: None
    assert s.current_option == fuori and fuori in s.options
    asyncio.run(s.async_select_option(fuori))  # rieffettuare la scelta non lo azzera
    assert R.AWAY_MESSAGE_FILE == fuori


def test_opzioni_non_ricaricano_per_le_sole_chiavi_away(monkeypatch):
    hub = _Hub()
    data = {"hub": hub, "applied": {"a": 1}}
    monkeypatch.setattr(R, "AWAY_MESSAGE_TEXT", "")
    assert ac.apply_options(data, {"a": 1, "away_message_text": "ciao"}) is True  # niente reload
    assert R.AWAY_MESSAGE_TEXT == "ciao" and hub.touched == 1
    assert ac.apply_options(data, {"a": 2, "away_message_text": "ciao"}) is False  # altro: si ricarica



def test_without_a_media_folder_nothing_is_created_or_listed(tmp_path):
    hass = types.SimpleNamespace(config=types.SimpleNamespace(media_dirs={}))
    assert ac.messages_dir(hass) is None
    ac.ensure_dir(None)  # no folder configured: nothing to create, no error
    assert ac.list_files(None) == []
    assert list(tmp_path.iterdir()) == []


def _admin_env(tmp_path, monkeypatch, is_admin):
    hass, entry, calls = _env(tmp_path, monkeypatch)
    users = {"u1": types.SimpleNamespace(is_admin=is_admin)}

    async def get_user(uid):
        return users.get(uid)

    hass.auth = types.SimpleNamespace(async_get_user=get_user)
    return hass, entry, calls


def _entities(hass, entry, user_id):
    ctx = types.SimpleNamespace(user_id=user_id)
    t, s = VimarAwayText(entry), VimarAwayFileSelect(entry)
    for e in (t, s):
        e.hass, e._context = hass, ctx
        e.async_write_ha_state = lambda: None
    return t, s


@pytest.mark.parametrize("user_id, is_admin, allowed", [
    ("u1", True, True),       # administrator
    ("u1", False, False),     # regular user
    ("ghost", True, False),   # id that no longer resolves to a user
    (None, False, True),      # no user: automation or system call
])
def test_away_entities_are_admin_only(tmp_path, monkeypatch, user_id, is_admin, allowed):
    from homeassistant.exceptions import Unauthorized
    hass, entry, calls = _admin_env(tmp_path, monkeypatch, is_admin)
    t, s = _entities(hass, entry, user_id)
    for coro in (t.async_set_value("Back soon"), s.async_select_option("a.wav")):
        if allowed:
            asyncio.run(coro)
        else:
            with pytest.raises(Unauthorized):
                asyncio.run(coro)
    assert len(calls) == (2 if allowed else 0)  # a refused call writes nothing
