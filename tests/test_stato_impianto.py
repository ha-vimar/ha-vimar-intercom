"""Issue #4 (risposta lunga di GET_INIT_STATUS) e #9 (switch con stato supposto).

La risposta lunga è quella di un Tab 5S Up 40515 / 2FV2 pubblicata da @CPietro in #4;
la corta quella del 40507 / 2F dell'impianto di sviluppo.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types

import pytest

from custom_components.vimar_intercom import plant_state as S
from custom_components.vimar_intercom import runtime as R

LUNGA = "GET_INIT_STATUS_REPLY;" + json.dumps([
    {"PARAM": "dnd", "VALUE": "0"},
    {"PARAM": "voicemail", "VALUE": "0"},
    {"PARAM": "rubrica_ver", "VALUE": "abc"},
    {"PARAM": "vm_ver", "VALUE": "def"},
    {"PARAM": "vm_level", "VALUE": "0/100"},
    {"PARAM": "vm_timeout", "VALUE": 5},
    {"PARAM": "vm_timeout_values", "VALUE": [1, 5, 10, 15, 20]},
    {"PARAM": "apt_names", "VALUE": ["7", "", ""]},
    {"PARAM": "token", "VALUE": "segreto"},
    {"PARAM": "GID", "VALUE": 7},
    {"PARAM": "media_enc", "VALUE": "srtp"},
], separators=(",", ":"))
CORTA = ('GET_INIT_STATUS_REPLY;[{"PARAM":"rubrica_ver","VALUE":"abc"},{"PARAM":"vm_ver","VALUE":"def"},'
         '{"PARAM":"vm_level","VALUE":"0/100"},{"PARAM":"dnd","VALUE":"0"},{"PARAM":"voicemail","VALUE":"1"}]')


@pytest.fixture(autouse=True)
def _runtime_pulito(monkeypatch):
    for k in ("MEDIA_ENC_OPTION", "PICG_TARGET"):
        monkeypatch.setattr(R, k, getattr(R, k))
    for k in ("MEDIA_ENC", "MEDIA_ENC_PLANT"):
        monkeypatch.setattr(S, k, getattr(S, k))
    R.MEDIA_ENC_OPTION, S.MEDIA_ENC_PLANT, S.MEDIA_ENC = "auto", None, False
    R.PICG_TARGET = "55001"


# --- media_enc ----------------------------------------------------------------------

@pytest.mark.parametrize("salvato, modo", [
    (True, "on"), (False, "auto"), (None, "auto"), ("auto", "auto"), ("ON", "on"), ("off", "off"), ("boh", "auto"),
])
def test_opzione_media_enc_dalle_versioni_precedenti(salvato, modo):
    assert R.media_enc_mode(salvato) == modo


def test_configure_ricalcola_e_dimentica_l_impianto(monkeypatch):
    # configure() riscrive tutto il modulo: si rimette com'era alla fine del test.
    for mod in (R, S):
        for k, v in list(vars(mod).items()):
            if k.isupper():
                monkeypatch.setattr(mod, k, v)
    R.configure({"media_enc": True})
    assert (R.MEDIA_ENC_OPTION, S.MEDIA_ENC) == ("on", True)
    R.configure({"media_enc": False})
    assert (R.MEDIA_ENC_OPTION, S.MEDIA_ENC, S.MEDIA_ENC_PLANT) == ("auto", False, None)


def test_risposta_lunga_accende_srtp_in_automatico(hub):
    hub._handle_incoming_message(LUNGA)
    assert S.MEDIA_ENC is True
    assert hub.stats["media_enc"] == "srtp"


@pytest.mark.parametrize("opzione, atteso", [("off", False), ("on", True)])
def test_l_opzione_forzata_vince_sull_impianto(hub, opzione, atteso):
    R.MEDIA_ENC_OPTION = opzione
    hub._handle_incoming_message(LUNGA)
    assert S.MEDIA_ENC is atteso


def test_risposta_corta_non_tocca_la_cifratura_ne_i_parametri(hub):
    hub._handle_incoming_message(CORTA)
    assert S.MEDIA_ENC is False and S.MEDIA_ENC_PLANT is None
    assert hub.stats["voicemail"] is True and hub.stats["dnd"] is False
    for k in ("vm_timeout", "vm_timeout_values", "apt_names", "apt_gid", "media_enc"):
        assert hub.stats.get(k) is None, k


# --- parametri dell'appartamento ---------------------------------------------------------

def test_risposta_lunga_parametri_dell_appartamento(hub):
    hub._handle_incoming_message(LUNGA)
    st = hub.stats
    assert st["vm_timeout"] == 5
    assert st["vm_timeout_values"] == [1, 5, 10, 15, 20]
    assert st["apt_names"] == ["7", "", ""]
    assert st["apt_gid"] == 7


def test_apt_params_changed_aggiorna_il_ritardo(hub):
    hub._handle_incoming_message(LUNGA)
    hub._handle_incoming_message('APT_PARAMS_CHANGED;{"PARAM":"vm_timeout","VALUE":15}')
    assert hub.stats["vm_timeout"] == 15
    hub._handle_incoming_message("APT_PARAMS_CHANGED;{rotto")  # niente eccezioni
    hub._handle_incoming_message('APT_PARAMS_CHANGED;{"PARAM":"vm_timeout","VALUE":"x"}')
    assert hub.stats["vm_timeout"] == 15


def _manda(hub, monkeypatch, risposta):
    """async_send_command finto: registra l'invio e fa arrivare `risposta(msgid)`."""
    inviati = []

    async def send(body, target=None, header_name=None, header_value=None):
        inviati.append((body, target, header_name, header_value))
        msgid = json.loads(body.split(";", 1)[1])["MSGID"]
        if risposta:
            asyncio.get_running_loop().call_soon(hub._handle_incoming_message, risposta(msgid))
        return True, "202"

    monkeypatch.setattr(hub, "async_send_command", send)
    return inviati


def test_set_apt_params_riuscito(hub, monkeypatch):
    hub._handle_incoming_message(LUNGA)
    inviati = _manda(hub, monkeypatch, lambda m: f'SET_APT_PARAMS_REPLY;{{"MSGID":"{m}","ERRCODE":"ERR_NONE"}}')
    ok, msg = asyncio.run(hub.async_set_apt_param("vm_timeout", 10))
    assert (ok, msg) == (True, "ERR_NONE")
    body, target, hname, hval = inviati[0]
    j = json.loads(body.split(";", 1)[1])
    assert body.startswith("SET_APT_PARAMS;") and (j["PARAM"], j["VALUE"]) == ("vm_timeout", 10)
    assert 8 <= len(j["MSGID"]) <= 10
    assert (target, hname, hval) == ("55001", "Panda", "set")
    assert hub.stats["vm_timeout"] == 10
    assert hub._apt_param_waiters == {}


@pytest.mark.parametrize("risposta, motivo", [
    (lambda m: f'SET_APT_PARAMS_REPLY;{{"MSGID":"{m}","ERRCODE":"ERR_INVALID_VALUE"}}', "ERR_INVALID_VALUE"),
    (lambda m: f'SET_APT_PARAMS_REPLY;{{"MSGID":"{m}"}}', "nessun ERRCODE"),
    (lambda m: 'SET_APT_PARAMS_REPLY;{"MSGID":"altro","ERRCODE":"ERR_NONE"}', "Nessuna risposta"),
    (None, "Nessuna risposta"),
])
def test_set_apt_params_fallito(hub, monkeypatch, risposta, motivo):
    hub._handle_incoming_message(LUNGA)
    _manda(hub, monkeypatch, risposta)
    ok, msg = asyncio.run(hub.async_set_apt_param("vm_timeout", 10, timeout=0.2))
    assert not ok and motivo in msg
    assert hub.stats["vm_timeout"] == 5


# --- entità: stub minimi di HA ------------------------------------------------------------

class _HAError(Exception):
    def __init__(self, *args, **translation):  # translation_key, placeholders: as in HA
        super().__init__(*args)
        self.translation_key = translation.get("translation_key")


def _carica(nome):
    if getattr(sys.modules.get("homeassistant"), "_is_stub", False):
        class _Base:
            async def async_get_last_state(self):
                return None

            def async_write_ha_state(self):
                pass

        sys.modules["homeassistant.components.switch"].SwitchEntity = type("SwitchEntity", (_Base,), {})
        sys.modules["homeassistant.helpers.restore_state"].RestoreEntity = type("RestoreEntity", (), {})
        sys.modules["homeassistant.components.select"].SelectEntity = type("SelectEntity", (_Base,), {})
    mod = importlib.import_module(f"custom_components.vimar_intercom.{nome}")
    mod.HomeAssistantError = _HAError
    return mod


class _Hub:
    registered = True

    def __init__(self, ok=True):
        self.stats = {}
        self.ok = ok
        self.cbs = []
        self.inviati = []
        self.status_chiesti = 0
        self.param = None

    async def async_send_command(self, body, target=None, header_name=None, header_value=None):
        self.inviati.append(body)
        return self.ok, "200" if self.ok else "Timeout"

    async def async_request_status(self):
        self.status_chiesti += 1

    async def async_set_apt_param(self, p, v):
        self.param = (p, v)
        self.stats["vm_timeout"] = v
        return True, "ERR_NONE"

    def register_state_callback(self, cb):
        self.cbs.append(cb)

    def unregister_state_callback(self, cb):
        if cb in self.cbs:
            self.cbs.remove(cb)

    def touch(self):
        for cb in list(self.cbs):
            cb()


def _dnd(hub):
    sw = _carica("switch")
    s = sw.VimarModeSwitch(hub, "e1", key="dnd", object_id="dnd", icon="x", target="55001",
                           cmd_on="DND;ON", cmd_off="DND;OFF", state_attr="dnd",
                           hname="Panda", hvalue="blue")

    async def nessuno_stato():
        return None

    # switch.py può essere già stato importato da un altro test con basi diverse.
    s.async_write_ha_state = lambda: None
    s.async_get_last_state = nessuno_stato
    return sw, s


def _annuncio(hub, valore):
    """DND;ON|OFF dal Tab (o GET_INIT_STATUS_REPLY): valore e numero di sequenza."""
    hub.stats["dnd"] = valore
    hub.stats["mode_seq"] = hub.stats.get("mode_seq", 0) + 1
    hub.touch()


# --- #9: switch ---------------------------------------------------------------------------

def test_senza_annuncio_lo_switch_torna_sconosciuto(monkeypatch):
    hub = _Hub()
    sw, s = _dnd(hub)
    monkeypatch.setattr(sw, "CONFIRM_S", 0.05)

    async def prova():
        await s.async_added_to_hass()
        await s.async_turn_on()
        assert s.is_on is True and s.assumed_state          # in attesa di conferma
        assert hub.status_chiesti == 1                     # e intanto lo chiede al citofono
        await asyncio.sleep(0.1)
        return s.is_on

    assert asyncio.run(prova()) is None                    # nessuno ha confermato


def test_l_annuncio_del_tab_conferma_e_resta(monkeypatch):
    hub = _Hub()
    sw, s = _dnd(hub)
    monkeypatch.setattr(sw, "CONFIRM_S", 0.05)

    async def prova():
        await s.async_added_to_hass()
        await s.async_turn_on()
        _annuncio(hub, True)                               # DND;ON dal Tab
        await asyncio.sleep(0.1)
        return s.is_on, s.assumed_state, s.extra_state_attributes["stato_reale"]

    assert asyncio.run(prova()) == (True, False, True)


def test_il_tab_smentisce_e_vince_lo_stato_vero(monkeypatch):
    hub = _Hub()
    hub.stats["dnd"] = False
    sw, s = _dnd(hub)
    monkeypatch.setattr(sw, "CONFIRM_S", 0.05)

    async def prova():
        await s.async_added_to_hass()
        await s.async_turn_on()                            # 200 ma il Tab non cambia niente
        assert s.is_on is True
        _annuncio(hub, False)                              # la risposta a GET_INIT_STATUS lo smentisce
        return s.is_on, s.assumed_state

    assert asyncio.run(prova()) == (False, False)


def test_a_command_the_plant_ignores_warns_about_sga_target(monkeypatch, caplog):
    """200 OK, then the Tab says the state stayed OFF: usually the wrong recipient."""
    hub = _Hub()
    hub.stats["dnd"] = False
    sw, s = _dnd(hub)
    monkeypatch.setattr(sw, "CONFIRM_S", 0.05)

    async def prova():
        await s.async_added_to_hass()
        await s.async_turn_on()                            # ON sent, 200 OK
        _annuncio(hub, False)                              # mode_seq bumped, still OFF

    with caplog.at_level("WARNING"):
        asyncio.run(prova())
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("sga_target" in w and "55001" in w for w in warnings), warnings


def test_a_confirmed_command_does_not_warn(monkeypatch, caplog):
    hub = _Hub()
    hub.stats["dnd"] = False
    sw, s = _dnd(hub)
    monkeypatch.setattr(sw, "CONFIRM_S", 0.05)

    async def prova():
        await s.async_added_to_hass()
        await s.async_turn_on()
        _annuncio(hub, True)                               # the Tab confirms

    with caplog.at_level("WARNING"):
        asyncio.run(prova())
    assert "sga_target" not in caplog.text


def test_comando_fallito_solleva_e_non_cambia_lo_stato():
    hub = _Hub(ok=False)
    _, s = _dnd(hub)

    async def prova():
        await s.async_added_to_hass()
        with pytest.raises(_HAError):
            await s.async_turn_on()
        return s.is_on, hub.status_chiesti

    assert asyncio.run(prova()) == (None, 0)


@pytest.mark.parametrize("attributi, atteso", [
    ({"stato_reale": True}, True),     # confermato dal Tab prima del riavvio
    ({"stato_reale": None}, None),     # solo supposto: non si riprende
    ({}, None),
])
def test_al_riavvio_si_riprende_solo_uno_stato_confermato(attributi, atteso):
    _, s = _dnd(_Hub())

    async def last():
        return types.SimpleNamespace(state="on", attributes=attributi)

    s.async_get_last_state = last
    asyncio.run(s.async_added_to_hass())
    s.async_get_last_state = None
    assert s.is_on is atteso


# --- #4: select del ritardo segreteria -----------------------------------------------------

def test_il_select_nasce_solo_quando_arrivano_i_valori(monkeypatch):
    sel = _carica("select")
    hub = _Hub()
    aggiunte = []
    away = []
    # monkeypatch: assigned directly, the fake stayed in select.py for every later test.
    monkeypatch.setattr(sel, "set_away", lambda hass, entry, key, value: away.append((key, value)))
    async def executor(f, *a): return None

    hass = types.SimpleNamespace(data={"vimar_intercom": {"e1": {"hub": hub}}},
                                 config=types.SimpleNamespace(media_dirs={}), async_add_executor_job=executor)
    entry = types.SimpleNamespace(entry_id="e1", async_on_unload=lambda f: None)
    asyncio.run(sel.async_setup_entry(hass, entry, lambda ents, *_: aggiunte.extend(
        e for e in ents if not isinstance(e, sel.VimarAwayFileSelect))))
    assert aggiunte == []                                  # risposta corta: niente entità
    hub.touch()
    assert aggiunte == []
    hub.stats.update(vm_timeout=5, vm_timeout_values=[1, 5, 10, 15, 20])
    hub.touch()
    hub.touch()
    assert len(aggiunte) == 1                              # una volta sola
    e = aggiunte[0]
    assert e.options == ["1", "5", "10", "15", "20"] and e.current_option == "5"
    asyncio.run(e.async_select_option("10"))
    assert hub.param == ("vm_timeout", 10) and e.current_option == "10"
    assert away == []                                      # il select non riscrive più away_message_delay
    with pytest.raises(_HAError):
        asyncio.run(e.async_select_option("7"))


def test_annuncio_arrivato_durante_l_invio_conta_come_conferma():
    """Sul 40507 il DND;OFF del Tab arriva prima del 200 del nostro MESSAGE."""
    hub = _Hub()
    hub.stats["dnd"] = True
    _, s = _dnd(hub)
    invia = hub.async_send_command

    async def send_con_annuncio(body, **kw):
        _annuncio(hub, False)
        return await invia(body, **kw)

    hub.async_send_command = send_con_annuncio

    async def prova():
        await s.async_added_to_hass()
        await s.async_turn_off()
        return s.is_on, s.assumed_state

    assert asyncio.run(prova()) == (False, False)


# --- Non disturbare: disponibilita' ----------------------------------------------------------

def test_dnd_disponibile_da_registrato_anche_senza_stato_del_tab():
    hub = _Hub()
    sw, s = _dnd(hub)
    assert s.available is True and s.is_on is None         # stato ignoto, ma comandabile
    hub.registered = False
    assert s.available is False                            # non registrato


# --- Segreteria: switch e ritardo -------------------------------------------------------------

def test_segreteria_e_ritardo_disponibili_solo_con_risposta_del_tab(monkeypatch):
    hub = _Hub()
    sw = _carica("switch")
    s = sw.VimarVoicemailSwitch(hub, "e1", key="segreteria", object_id="segreteria", icon="x", target="55001",
                           cmd_on="VOICEMAIL;ON", cmd_off="VOICEMAIL;OFF", state_attr="voicemail",
                           hname="Panda", hvalue="blue")
    sel = _carica("select")
    e = sel.VimarVmTimeoutSelect(types.SimpleNamespace(entry_id="e1"), hub)
    assert s.available is True and e.available is False   # switch comandabile, select no
    hub.stats.update(voicemail=False, vm_timeout=5, vm_timeout_values=[5, 10])
    assert s.available is True and e.available is True
    monkeypatch.setattr(sel.R, "PICG_TARGET", "")
    assert e.available is False                            # nessun indirizzo: niente comandi


# --- Segreteria unica: messaggio di HA se configurato, altrimenti quella del Tab --------------

def _seg(hub, monkeypatch, testo="ciao", file=""):
    sw = _carica("switch")
    monkeypatch.setattr(sw.R, "AWAY_MESSAGE_DELAY", 0)  # il ritardo non conta piu' per "configurato"
    monkeypatch.setattr(sw.R, "AWAY_MESSAGE_TEXT", testo)
    monkeypatch.setattr(sw.R, "AWAY_MESSAGE_FILE", file)
    hub.away_enabled = False
    hub.set_away_enabled = lambda on: setattr(hub, "away_enabled", on)
    hub.on_voicemail_on = lambda: hub.set_away_enabled(False)
    s = sw.VimarVoicemailSwitch(hub, "e1", key="segreteria", object_id="segreteria", icon="x", target="55001",
                           cmd_on="VOICEMAIL;ON", cmd_off="VOICEMAIL;OFF", state_attr="voicemail",
                           hname="Panda", hvalue="blue")
    s.async_write_ha_state = lambda: None
    return s


def test_segreteria_on_con_testo_usa_ha_e_spegne_il_tab(monkeypatch):
    hub = _Hub()
    s = _seg(hub, monkeypatch)
    asyncio.run(s.async_turn_on())
    assert hub.inviati == ["VOICEMAIL;OFF"] and hub.away_enabled is True
    assert s.is_on is True and s.extra_state_attributes["modo"] == "Home Assistant"


def test_segreteria_on_senza_messaggio_accende_quella_del_tab(monkeypatch):
    hub = _Hub()
    s = _seg(hub, monkeypatch, testo="")
    asyncio.run(s.async_turn_on())
    assert hub.inviati == ["VOICEMAIL;ON"] and hub.away_enabled is False
    assert s.extra_state_attributes["modo"] == "Tab"


def test_segreteria_on_con_solo_file_usa_ha(monkeypatch):
    hub = _Hub()
    s = _seg(hub, monkeypatch, testo="", file="/x/a.mp3")
    asyncio.run(s.async_turn_on())
    assert hub.inviati == ["VOICEMAIL;OFF"] and hub.away_enabled is True


def test_segreteria_off_spegne_entrambe(monkeypatch):
    hub = _Hub()
    s = _seg(hub, monkeypatch)
    hub.away_enabled = True
    asyncio.run(s.async_turn_off())
    assert hub.inviati == ["VOICEMAIL;OFF"] and hub.away_enabled is False


def test_segreteria_on_con_invio_fallito_non_attiva_away(monkeypatch):
    hub = _Hub(ok=False)
    s = _seg(hub, monkeypatch)
    with pytest.raises(_HAError):
        asyncio.run(s.async_turn_on())
    assert hub.away_enabled is False


def test_annuncio_del_tab_mentre_si_usa_ha_torna_al_tab(monkeypatch):
    hub = _Hub()
    s = _seg(hub, monkeypatch)
    hub.away_enabled = True
    hub.stats["voicemail"] = True
    hub.on_voicemail_on()                                  # come fa l'hub all'annuncio VOICEMAIL;ON
    assert s.is_on is True and s.extra_state_attributes["modo"] == "Tab"


def test_hub_annuncio_voicemail_on_spegne_away():
    from custom_components.vimar_intercom import hub as hub_mod
    h = object.__new__(hub_mod.VimarIntercomHub)
    h.stats = {}
    h._away_task = None
    h._state_callbacks = []
    h._away_enabled = True
    h._handle_incoming_message("VOICEMAIL;OFF")
    assert h.away_enabled is True
    h._handle_incoming_message("VOICEMAIL;ON")
    assert h.away_enabled is False and h.stats["voicemail"] is True


def test_hub_init_status_voicemail_on_spegne_away(hub):
    hub.set_away_enabled(True)
    hub._handle_incoming_message(CORTA)                    # voicemail=on
    assert hub.away_enabled is False


def test_restore_segreteria_riprende_il_modo_senza_comandi(monkeypatch):
    hub = _Hub()
    s = _seg(hub, monkeypatch)

    def ripristina(modo):
        async def ultimo():
            return types.SimpleNamespace(state="on", attributes={"modo": modo})
        s.async_get_last_state = ultimo
        s._hub.register_state_callback = lambda cb: None
        asyncio.run(s.async_added_to_hass())

    ripristina("Home Assistant")
    assert hub.away_enabled is True
    hub.stats["voicemail"] = True                          # il Tab ha la segreteria accesa
    ripristina("Home Assistant")
    assert hub.away_enabled is False and hub.inviati == []


def test_aggiornamento_senza_stato_con_modo_parte_con_away_disattivato(monkeypatch):
    """Chi aveva testo e ritardo 0 (o lo switch away spento) non si ritrova HA che risponde a ogni squillo."""
    hub = _Hub()
    s = _seg(hub, monkeypatch)
    hub.away_enabled = True

    async def ultimo(): return types.SimpleNamespace(attributes={"stato_reale": False})  # niente "modo"

    s.async_get_last_state = ultimo
    asyncio.run(s.async_added_to_hass())
    assert hub.away_enabled is False
    hub.away_enabled = True

    async def con_modo(): return types.SimpleNamespace(attributes={"modo": "Home Assistant"})

    s.async_get_last_state = con_modo
    asyncio.run(s.async_added_to_hass())
    assert hub.away_enabled is True  # dal secondo avvio si riprende com'era
