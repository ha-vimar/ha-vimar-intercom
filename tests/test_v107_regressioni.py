"""Regressioni corrette nella 1.0.7 (debug del 21/09/2026).

Ogni test fissa il comportamento corretto, quasi sempre ricavato dal sorgente
dell'app VIEW (i receiver dei messaggi di sistema). Valori anonimi: gli hash
sono inventati, gli interni sono quelli di default.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")
from custom_components.vimar_intercom import const as C  # noqa: E402
from custom_components.vimar_intercom import log_buffer, log_redact  # noqa: E402
from custom_components.vimar_intercom import sip_client as sip  # noqa: E402

MD5_RUB = "0123456789abcdef0123456789abcdef"
MD5_VM = "fedcba9876543210fedcba9876543210"
# Forma reale del Tab 40507: VALUE prima di PARAM, spazi dopo i due punti.
INIT_REPLY = (
    f'GET_INIT_STATUS_REPLY;[{{"VALUE": "{MD5_RUB}", "PARAM": "rubrica_ver"}},'
    f'{{"VALUE": "{MD5_VM}", "PARAM": "vm_ver"}},'
    '{"VALUE": "0/100", "PARAM": "vm_level"},{"VALUE": "0", "PARAM": "dnd"},'
    '{"VALUE": "0", "PARAM": "voicemail"}]'
)


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    h.events = []
    monkeypatch.setattr(h, "_fire_event", lambda t, d: h.events.append((t, d)))
    return h


# ─── NEW_PHONEBOOK;<ver>;<gid> ───────────────────────────────────────────────

def test_new_phonebook_versione_prima_del_gid(hub):
    hub._update_stats("message", f"NEW_PHONEBOOK;{MD5_RUB};60001")
    assert hub.stats["rubrica_ver"] == MD5_RUB
    tipo, dati = hub.events[-1]
    assert tipo == C.EVENT_PHONEBOOK_CHANGED
    assert dati == {"gid": "60001", "rubrica_ver": MD5_RUB}


def test_new_phonebook_poi_init_reply_nessun_evento_doppio(hub):
    """Con l'ordine invertito il GET_INIT_STATUS_REPLY successivo «cambiava»
    di nuovo la versione e partiva un secondo phonebook_changed."""
    hub._update_stats("message", f"NEW_PHONEBOOK;{MD5_RUB};60001")
    n = len(hub.events)
    hub._update_stats("message", INIT_REPLY)
    assert len(hub.events) == n


# ─── CALL_INFO: lo zero è un valore ──────────────────────────────────────────

def test_call_info_conserva_gli_zeri(hub):
    hub._update_stats("message", 'CALL_INFO;{"SIP_ID":"55001","REASON":0,"MEDIA_TYPE":0,"VIDEO_SRC":0}')
    d = hub.stats["last_call_info"]
    assert d == {"sip_id": "55001", "reason": 0, "media_type": 0, "video_src": 0}


def test_call_info_chiavi_minuscole(hub):
    hub._update_stats("message", 'CALL_INFO;{"sip_id":"55001","media_type":2}')
    d = hub.stats["last_call_info"]
    assert d["media_type"] == 2 and d["reason"] is None


# ─── send_command: niente «Panda: None» ──────────────────────────────────────

def _cattura(monkeypatch):
    got = {}

    async def _dsm(uri, body, extra_headers=None):
        got["h"] = extra_headers
        return True, "OK (200)"

    monkeypatch.setattr(sip, "do_system_message", _dsm)
    return got


@pytest.mark.parametrize("valore", ["", None, "   "])
def test_header_value_vuoto_non_manda_none(monkeypatch, hub, valore):
    got = _cattura(monkeypatch)
    asyncio.run(hub.async_send_command(body="X", target="55001", header_name="Panda", header_value=valore))
    assert got["h"] is None


def test_header_completo_passa(monkeypatch, hub):
    got = _cattura(monkeypatch)
    asyncio.run(hub.async_send_command(body="X", target="55001", header_name="Panda", header_value="blue"))
    assert got["h"] == {"Panda": "blue"}


def test_header_con_a_capo_rifiutato(monkeypatch, hub):
    got = _cattura(monkeypatch)
    ok, _ = asyncio.run(hub.async_send_command(
        body="X", target="55001", header_name="Panda", header_value="blue\r\nX-Evil: 1"))
    assert ok is False and "h" not in got


# ─── switch: stato supposto e solo su comando riuscito ───────────────────────

def _import_switch():
    """Con gli stub HA SwitchEntity e RestoreEntity sono la stessa classe jolly:
    per importare switch.py servono due basi distinte."""
    import sys
    sw_stub = sys.modules.get("homeassistant.components.switch")
    rs_stub = sys.modules.get("homeassistant.helpers.restore_state")
    if getattr(sys.modules.get("homeassistant"), "_is_stub", False):
        class SwitchEntity:  # noqa: D401 - stub
            pass

        class RestoreEntity:
            pass

        sw_stub.SwitchEntity = SwitchEntity
        rs_stub.RestoreEntity = RestoreEntity
    return pytest.importorskip("custom_components.vimar_intercom.switch")


switch_mod = _import_switch()


class _Hub:
    def __init__(self, ok, reale=None):
        self.stats = {"voicemail": reale}
        self._ok = ok

    async def async_send_command(self, **_kw):
        return self._ok, "OK (200)" if self._ok else "Non registrato"


def _switch(hub):
    sw = switch_mod.VimarModeSwitch(
        hub, "e1", key="segreteria", name="Segreteria", icon="mdi:voicemail",
        target="55001", cmd_on="VOICEMAIL;ON", cmd_off="VOICEMAIL;OFF",
        state_attr="voicemail", hname="Panda", hvalue="blue")
    sw.async_write_ha_state = lambda: None
    return sw


def test_comando_fallito_non_cambia_lo_stato():
    sw = _switch(_Hub(ok=False))
    asyncio.run(sw.async_turn_on())
    assert sw.is_on is False


def test_comando_riuscito_sposta_lo_stato_supposto():
    sw = _switch(_Hub(ok=True))
    asyncio.run(sw.async_turn_on())
    assert sw.is_on is True and sw.assumed_state is True


def test_con_stato_reale_non_e_supposto():
    sw = _switch(_Hub(ok=True, reale=False))
    assert sw.assumed_state is False
    asyncio.run(sw.async_turn_on())
    # Finché la verifica è in corso si vede lo stato chiesto, così nessuno
    # preme di nuovo; a verifica chiusa prevale l'annuncio del Tab.
    assert sw.is_on is True
    sw._set_verified(False)
    assert sw.is_on is False, "prevale l'annuncio del Tab, non il comando"


# ─── oscuramento: le forme reali del token ───────────────────────────────────

TOK = "s3cr3tT0kenValue"


@pytest.mark.parametrize("riga", [
    f'GET_INIT_STATUS_REPLY;[{{"PARAM":"token","VALUE":"{TOK}"}}]',
    f'GET_INIT_STATUS_REPLY;[{{"VALUE": "{TOK}", "PARAM": "token"}}]',
    f'status={{"dnd": "0", "token": "{TOK}"}}',
    f"init_status={ {'token': TOK}!r}",
    f"token={TOK}",
])
def test_token_oscurato_in_ogni_forma(riga):
    out = log_redact.redact(riga)
    assert TOK not in out and log_redact.MASK in out


def test_authorization_dentro_un_repr():
    msg = ('REGISTER sip:x SIP/2.0\r\nAuthorization: Digest username="12345", realm="r", '
           'nonce="abc", uri="sip:x", response="0123456789abcdef0123456789abcdef"\r\nCSeq: 2 REGISTER\r\n')
    out = log_redact.redact(f"raw={msg!r}")
    assert "0123456789abcdef" not in out
    assert "CSeq: 2 REGISTER" in out, "oscura solo l'header, non il resto del messaggio"


def test_hash_di_versione_non_oscurati():
    """rubrica_ver/vm_ver non sono segreti: servono per il debug."""
    assert MD5_RUB in log_redact.redact(INIT_REPLY)


# ─── logging: inoltro a HA dal livello scelto ────────────────────────────────

class _Cattura(logging.Handler):
    def __init__(self):
        super().__init__()
        self.righe: list[str] = []

    def emit(self, record):
        self.righe.append(record.getMessage())


@pytest.fixture
def logger_isolato():
    nome = "test_v107.vimar"
    log = log_buffer.install(nome)
    root = logging.getLogger()
    cap = _Cattura()
    root.addHandler(cap)
    log_buffer.debug_log.clear()
    yield log, cap
    root.removeHandler(cap)
    for h in list(log.handlers):
        log.removeHandler(h)


def test_di_base_solo_warning_arriva_a_ha(logger_isolato):
    log, cap = logger_isolato
    figlio = logging.getLogger(log.name + ".sip_client")
    figlio.debug("d1")
    figlio.info("i1")
    figlio.warning("w1")
    assert cap.righe == ["w1"]
    assert len(log_buffer.debug_log) == 3, "il buffer riceve comunque tutto"


def test_set_level_debug_porta_il_debug_nel_log_di_ha(logger_isolato):
    log, cap = logger_isolato
    log.setLevel(logging.DEBUG)          # ciò che fa logger.set_level
    logging.getLogger(log.name + ".hub").debug("d2")
    assert cap.righe == ["d2"]


def test_riga_inoltrata_e_oscurata(logger_isolato):
    log, cap = logger_isolato
    logging.getLogger(log.name + ".hub").warning("init_status=%r", {"token": TOK})
    assert cap.righe and TOK not in cap.righe[0]
    assert TOK not in log_buffer.debug_log[-1]


def test_install_e_idempotente(logger_isolato):
    log, _ = logger_isolato
    log_buffer.install(log.name)
    nostri = [h for h in log.handlers
              if isinstance(h, (log_buffer.DebugBufferHandler, log_buffer.ForwardToRootHandler))]
    assert len(nostri) == 2
