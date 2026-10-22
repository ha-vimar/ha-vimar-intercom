"""Porta di ascolto e stato della registrazione (v1.0.6).

Due bug che si vedevano solo sul campo, e nel modo peggiore: l'integrazione
diceva di essere a posto mentre non lo era.
"""
from __future__ import annotations

import pytest

from custom_components.vimar_intercom import runtime  # noqa: E402
from custom_components.vimar_intercom import sip_client as sip


class _FakeSock:
    """Una socket che dichiara la porta su cui e' finita davvero."""

    def __init__(self, porta):
        self._porta = porta

    def getsockname(self):
        return ("0.0.0.0", self._porta)


@pytest.fixture(autouse=True)
def _pulizia():
    originale = sip._udp_sock
    yield
    sip._udp_sock = originale


def test_la_porta_annunciata_e_quella_su_cui_ascoltiamo(monkeypatch):
    """`connect()` ripiega su una porta effimera se la 5060 e' occupata.

    Fino alla 1.0.5 `_my_port()` restituiva comunque quella configurata, e da li'
    uscivano Via e il Contact della REGISTER: la registrazione riusciva (le
    risposte tornano al source port) ma l'INVITE in arrivo veniva instradato su
    una porta muta. Campanello morto, nessun log.
    """
    monkeypatch.setattr(runtime, "USE_LOCAL_UDP", True, raising=False)
    monkeypatch.setattr(runtime, "LOCAL_UDP_PORT", 5060, raising=False)
    sip._udp_sock = _FakeSock(51234)

    assert sip._my_port() == 51234, "annuncia una porta diversa da quella reale"


def test_senza_socket_si_usa_la_porta_configurata(monkeypatch):
    monkeypatch.setattr(runtime, "USE_LOCAL_UDP", True, raising=False)
    monkeypatch.setattr(runtime, "LOCAL_UDP_PORT", 5060, raising=False)
    sip._udp_sock = None

    assert sip._my_port() == 5060


def test_in_cloud_la_porta_resta_quella_del_transport(monkeypatch):
    monkeypatch.setattr(runtime, "USE_LOCAL_UDP", False, raising=False)
    sip._udp_sock = _FakeSock(51234)

    assert sip._my_port() == 5070


def test_via_e_contact_usano_la_porta_vera(monkeypatch):
    """Il punto per cui la funzione esiste: questi due header sono quelli che il
    citofono usa per richiamarci."""
    monkeypatch.setattr(runtime, "USE_LOCAL_UDP", True, raising=False)
    monkeypatch.setattr(runtime, "LOCAL_UDP_PORT", 5060, raising=False)
    monkeypatch.setattr(runtime, "SIP_USER", "101", raising=False)
    monkeypatch.setattr(sip, "MY_IP", "192.0.2.10", raising=False)
    sip._udp_sock = _FakeSock(51234)

    assert ":51234" in sip._via_line("z9hG4bKtest")
    assert ":51234" in sip._contact_hdr()
    assert ":51234" in sip._simple_contact()


# ─── una REGISTER fallita deve azzerare `registered` ─────────────────────────

def _risposte(monkeypatch, raws):
    async def _send(_msg):
        return None

    async def _wait(cid, timeout=15):
        return list(raws)

    async def _request(_msg, cid, timeout=15):
        return list(raws)

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(sip, "_wait_final", _wait)
    # Dalla 1.0.7 REGISTER e MESSAGE passano da _send_request (ritrasmissione UDP).
    monkeypatch.setattr(sip, "_send_request", _request)


def _prepara(monkeypatch):
    monkeypatch.setattr(runtime, "USE_LOCAL_UDP", True, raising=False)
    monkeypatch.setattr(runtime, "SIP_USER", "101", raising=False)
    monkeypatch.setattr(runtime, "SIP_DOMAIN", "x.test", raising=False)
    monkeypatch.setattr(sip, "MY_IP", "192.0.2.10", raising=False)
    sip._udp_sock = _FakeSock(5060)


@pytest.mark.parametrize("risposta, descrizione", [
    ("SIP/2.0 403 Forbidden\r\n\r\n", "rifiutata"),
    ("SIP/2.0 503 Service Unavailable\r\n\r\n", "servizio giu'"),
    ("SIP/2.0 401 Unauthorized\r\n\r\n", "401 senza challenge"),
])
def test_una_register_fallita_non_lascia_registered_a_true(monkeypatch, risposta, descrizione):
    """Fino alla 1.0.5 questi percorsi facevano `return False` senza toccare il
    flag: il keepalive falliva e Home Assistant continuava a mostrare il
    citofono online, a volte per un'ora."""
    import asyncio

    _prepara(monkeypatch)
    _risposte(monkeypatch, [risposta])
    sip.registered = True

    ok = asyncio.run(sip.do_register())

    assert ok is False
    assert sip.registered is False, f"registrazione {descrizione}: flag rimasto a True"


def test_nessuna_risposta_azzera_il_flag(monkeypatch):
    import asyncio

    _prepara(monkeypatch)
    _risposte(monkeypatch, [])
    sip.registered = True

    assert asyncio.run(sip.do_register()) is False
    assert sip.registered is False


def test_una_register_riuscita_mette_registered(monkeypatch):
    import asyncio

    _prepara(monkeypatch)
    _risposte(monkeypatch, ["SIP/2.0 200 OK\r\n\r\n"])
    sip.registered = False

    assert asyncio.run(sip.do_register()) is True
    assert sip.registered is True
