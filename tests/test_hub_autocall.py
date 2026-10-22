"""Regressioni sul percorso di autoaccensione e sullo stato iniziale (v1.0.3).

Tre bug che convivevano nello stesso file e che si vedevano solo sul campo:

* l'URI dell'auto-call veniva costruito su `sip.C.SIP_DOMAIN`, che non esiste:
  il dominio vive in `runtime`, non in `const`;
* `_auto_called` non veniva azzerato quando la chiamata finiva, e da lì in poi
  ogni squillo vero veniva scambiato per l'eco della nostra chiamata;
* `_init_status_sent` veniva messo a True anche quando l'invio falliva, il che
  rendeva irraggiungibile il retry previsto nel keepalive.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.vimar_intercom import hub as hub_mod

sip = hub_mod.sip
C = hub_mod.C
R = hub_mod.R


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    return h


# ─── A1: il dominio SIP si legge da runtime, mai da const ────────────────────

def test_const_non_espone_un_dominio_sip():
    """Se un giorno `SIP_DOMAIN` ricomparisse in const, il bug potrebbe tornare
    silenziosamente: l'auto-call userebbe di nuovo un valore statico invece di
    quello attivo (che cambia fra modalità locale e cloud)."""
    assert not hasattr(C, "SIP_DOMAIN")


def test_auto_call_chiama_la_targa_video(hub, monkeypatch):
    """L'autoaccensione usa il default di do_call, R.INTERCOM = sip:<camera_target>@dominio
    (vedi test_runtime), non il PICG."""
    chiamate = []

    async def _fake_do_call(target=None, silence_limit=None, **_kw):
        chiamate.append(target)
        return True, "200"

    monkeypatch.setattr(sip, "do_call", _fake_do_call)
    asyncio.run(hub._do_auto_call())
    assert chiamate == [None]


# ─── A2: fine chiamata → gli squilli successivi tornano veri ─────────────────

def test_call_ended_azzera_auto_called(hub):
    """Il watchdog `_delayed_hangup` non copre questo caso: la sua guardia
    richiede `sip.in_call`, che dopo call_ended è già False."""
    hub._auto_called = True

    asyncio.run(hub._handle_broadcast("call_ended", ""))

    assert hub._auto_called is False


def test_squillo_dopo_una_chiamata_chiusa_non_viene_soppresso(hub, monkeypatch):
    """Il sintomo osservato: chiusa una chiamata dal citofono, ogni squillo
    successivo veniva rifiutato con 603 Decline finché non si riavviava HA."""
    declines = []

    async def _fake_decline(*_args):
        declines.append(True)

    monkeypatch.setattr(sip, "do_decline_incoming", _fake_decline)
    monkeypatch.setattr(sip, "in_call", False, raising=False)
    monkeypatch.setattr(sip, "calling", False, raising=False)

    squilli = []
    hub.register_ring_callback(lambda: squilli.append(True))

    hub._auto_called = True
    asyncio.run(hub._handle_broadcast("call_ended", ""))
    asyncio.run(hub._handle_broadcast("ring", ""))

    assert squilli, "lo squillo è stato soppresso"
    assert not declines


def test_squillo_insieme_al_bye_della_targa_non_e_un_eco(hub, monkeypatch):
    """La targa chiude la vista e fa squillare nello stesso istante: il ring arriva
    mentre stop_media chiude ancora ffmpeg, prima del call_ended. Fino a qui era
    preso per l'eco dell'auto-call e rifiutato con 603 (chiuso anche sul Tab)."""
    declines = []

    async def _fake_decline(*_args):
        declines.append(True)

    monkeypatch.setattr(sip, "do_decline_incoming", _fake_decline)
    monkeypatch.setattr(sip, "calling", False, raising=False)
    monkeypatch.setattr(sip, "in_call", True, raising=False)
    squilli = []
    hub.register_ring_callback(lambda: squilli.append(True))
    hub._auto_called = True
    hub._on_sip_state_change()                         # in chiamata
    monkeypatch.setattr(sip, "in_call", False, raising=False)
    hub._on_sip_state_change()                         # BYE della targa: in_call scende
    asyncio.run(hub._handle_broadcast("ring", ""))     # ...e il call_ended non è ancora arrivato
    assert squilli and not declines


# ─── A3: il retry dello stato iniziale deve restare raggiungibile ────────────

def test_init_status_non_si_marca_inviato_se_fallisce(hub, monkeypatch):
    async def _fail(*args, **kwargs):
        return False, "timeout", None

    # GET_INIT_STATUS goes straight to the SIP layer (it is not the user's command).
    monkeypatch.setattr(R, "SIP_DOMAIN", "d")
    monkeypatch.setattr(sip, "send_message", _fail)

    asyncio.run(hub._request_init_status())

    assert hub._init_status_sent is False, "il retry del keepalive non scatterebbe mai"


def test_init_status_si_marca_inviato_se_riesce(hub, monkeypatch):
    async def _ok(*args, **kwargs):
        return True, "200", 200

    # GET_INIT_STATUS goes straight to the SIP layer (it is not the user's command).
    monkeypatch.setattr(R, "SIP_DOMAIN", "d")
    monkeypatch.setattr(sip, "send_message", _ok)

    asyncio.run(hub._request_init_status())

    assert hub._init_status_sent is True


@pytest.mark.parametrize("local, wait", [(True, 15), (False, 20)])
def test_init_status_waits_for_the_cloud_relay(hub, monkeypatch, local, wait):
    """Over the cloud the relay answered after ~15.2 s (#14, #186): 15 s ran out just
    before it, and GET_INIT_STATUS was sent again at every renewal. Same wait as the door."""
    timeouts = []

    async def _send(uri, body, extra_headers=None, timeout=15):
        timeouts.append(timeout)
        return True, "200", 200

    monkeypatch.setattr(R, "SIP_DOMAIN", "d")
    monkeypatch.setattr(R, "USE_LOCAL_UDP", local)
    monkeypatch.setattr(sip, "send_message", _send)
    asyncio.run(hub._request_init_status())
    assert timeouts == [wait]


def test_init_status_queued_by_the_relay_is_not_sent_again(hub, monkeypatch):
    """A 202 from the relay is a success for send_message (any 2xx): the panel's reply
    (or its silence) decides, and the keepalive does not send it again."""
    async def _queued(*args, **kwargs):
        return True, "OK (202)", 202

    monkeypatch.setattr(R, "SIP_DOMAIN", "d")
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(sip, "send_message", _queued)
    asyncio.run(hub._request_init_status())
    assert hub._init_status_sent is True


# ─── Squillo in corso: aprire lo stream non risponde e non chiama ────────────

def test_stream_durante_lo_squillo_non_risponde_ne_chiama(hub, monkeypatch):
    """Una card o un tablet con lo stream aperto non deve rubare lo squillo al
    Tab (rispondere) né prendersi un 603 (chiamare la targa che squilla)."""
    azioni = []

    async def _fake_answer():
        azioni.append("answer")
        return True, "200"

    async def _fake_do_call(target=None, silence_limit=None, **_kw):
        azioni.append("call")
        return True, "200"

    monkeypatch.setattr(hub, "async_answer", _fake_answer)
    monkeypatch.setattr(sip, "do_call", _fake_do_call)
    monkeypatch.setattr(sip, "registered", True)
    monkeypatch.setitem(sip.pending_incoming, "active", True)

    async def _run():
        await hub.stream_opened()
        await asyncio.sleep(0)

    asyncio.run(_run())

    assert azioni == []
    assert not hub._auto_called


def test_due_stream_insieme_una_sola_chiamata(hub, monkeypatch):
    """go2rtc e lo stream worker aprono /av nello stesso istante."""
    chiamate = []

    async def _fake_do_call(target=None, silence_limit=None, **_kw):
        chiamate.append(target)
        return True, "200"

    monkeypatch.setattr(sip, "do_call", _fake_do_call)
    monkeypatch.setattr(sip, "registered", True)
    monkeypatch.setitem(sip.pending_incoming, "active", False)
    monkeypatch.setattr(R, "SIP_DOMAIN", "impianto.example", raising=False)

    async def _run():
        await hub.stream_opened()
        await hub.stream_opened()
        await asyncio.sleep(0)

    asyncio.run(_run())

    assert len(chiamate) == 1
