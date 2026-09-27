"""Recupero della registrazione SIP (v1.0.6).

Fino alla 1.0.5 il keepalive era racchiuso in `if sip.registered:`. Persa la
registrazione, il loop girava a vuoto per sempre: in UDP locale — il default —
non esisteva nessun altro percorso di recupero, e il citofono restava
scollegato fino al riavvio di Home Assistant. Peggio, `do_register()` non
azzerava `registered` quando falliva, quindi spesso quel flag restava `True` e
Home Assistant mostrava il citofono raggiungibile mentre non lo era.
"""
from __future__ import annotations

import asyncio

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")

sip = hub_mod.sip


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    return h


@pytest.fixture
def chiamate(monkeypatch):
    """Registra cosa il tick ha provato a fare, senza toccare la rete."""
    fatte = {"register": 0, "reconnect": 0, "init_status": 0, "ping": 0}

    async def _register():
        fatte["register"] += 1
        return fatte.get("register_ok", True)

    async def _reconnect():
        fatte["reconnect"] += 1
        return fatte.get("reconnect_ok", True)

    monkeypatch.setattr(sip, "do_register", _register)
    async def _ping():
        fatte["ping"] += 1
        return True

    monkeypatch.setattr(sip, "reconnect", _reconnect)
    monkeypatch.setattr(sip, "send_keepalive", _ping)
    return fatte


def _tick(hub, fatte, monkeypatch):
    async def _init():
        fatte["init_status"] += 1
        hub._init_status_sent = True

    monkeypatch.setattr(hub, "_request_init_status", _init)
    asyncio.run(hub._keepalive_tick())


# ─── registrato: keepalive normale ───────────────────────────────────────────

def test_se_registrati_basta_il_ping(hub, chiamate, monkeypatch):
    """Il rinnovo lo fa sip_client alla scadenza concessa dal registrar."""
    monkeypatch.setattr(sip, "registered", True, raising=False)
    hub._init_status_sent = True

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["ping"] == 1
    assert chiamate["register"] == 0
    assert chiamate["reconnect"] == 0
    assert chiamate["init_status"] == 0, "lo stato iniziale c'era già: non si richiede"


# ─── non registrato: il caso che prima non esisteva ──────────────────────────

def test_se_non_registrati_si_tenta_il_recupero(hub, chiamate, monkeypatch):
    monkeypatch.setattr(sip, "registered", False, raising=False)
    hub._init_status_sent = True

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["reconnect"] == 1, "il loop girava a vuoto invece di riconnettersi"


def test_dopo_il_recupero_si_richiede_lo_stato(hub, chiamate, monkeypatch):
    """Mentre eravamo scollegati segreteria, DND e versione rubrica possono
    essere cambiati sul Tab: ripartire con i valori di prima è sbagliato."""
    monkeypatch.setattr(sip, "registered", False, raising=False)
    hub._init_status_sent = True

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["init_status"] == 1


def test_recupero_fallito_conta_come_fallimento(hub, chiamate, monkeypatch):
    monkeypatch.setattr(sip, "registered", False, raising=False)
    chiamate["reconnect_ok"] = False
    prima = hub.stats["register_failures"]

    _tick(hub, chiamate, monkeypatch)

    assert hub.stats["register_failures"] == prima + 1
    assert chiamate["init_status"] == 0


def test_un_errore_non_ferma_il_loop(hub, monkeypatch):
    """Il tick è dentro un try: un'eccezione qui ucciderebbe il keepalive e
    riporterebbe al bug originale, solo per un'altra strada."""
    async def _esplode():
        raise RuntimeError("rete sparita")

    monkeypatch.setattr(sip, "registered", True, raising=False)
    monkeypatch.setattr(sip, "send_keepalive", _esplode)

    asyncio.run(hub._keepalive_tick())  # non deve sollevare
