"""Affidabilità dello stack SIP (1.0.7).

Tre difetti trovati dal debug del 21/09/2026, ciascuno con un effetto visibile:

* in UDP le richieste partivano una volta sola: un datagramma perso dava
  «REGISTER: nessuna risposta finale utile (0 risposte)» e due minuti di
  «Non registrato» — esattamente ciò che compariva nel log dell'impianto di
  sviluppo ogni 1-3 ore;
* `Content-Length` contava caratteri, non byte;
* l'attesa della risposta all'INFO di keyframe poteva girare senza cedere
  l'event loop, bloccando Home Assistant per 3 secondi (da Python 3.12).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from custom_components.vimar_intercom import runtime
from custom_components.vimar_intercom import sip_client as sip


def _ok(cid: str) -> str:
    return f"SIP/2.0 200 OK\r\nCall-ID: {cid}\r\nCSeq: 1 REGISTER\r\nContent-Length: 0\r\n\r\n"


@pytest.fixture
def udp(monkeypatch):
    monkeypatch.setattr(runtime, "USE_LOCAL_UDP", True, raising=False)
    # Intervalli accorciati: la logica è la stessa, il test dura millisecondi.
    monkeypatch.setattr(sip, "_T1", 0.02)
    monkeypatch.setattr(sip, "_T2", 0.08)


# ─── Content-Length ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("corpo, attesi", [
    ("GET_INIT_STATUS", 15),
    ("NICK;Cucina è", 14),          # «è» è due byte in UTF-8
    ("NICK;Città 2°", 15),
    ("", 0),
])
def test_content_length_e_in_byte(corpo, attesi):
    assert sip._clen(corpo) == attesi == len(corpo.encode("utf-8"))


def test_nessun_content_length_calcolato_con_len_su_stringa():
    """Guardia: nessun punto del modulo deve tornare a contare caratteri."""
    import inspect
    src = inspect.getsource(sip)
    for riga in src.splitlines():
        if "Content-Length: {len(" in riga:
            pytest.fail(f"Content-Length calcolato in caratteri: {riga.strip()}")


# ─── ritrasmissione UDP ──────────────────────────────────────────────────────

def test_un_datagramma_perso_viene_ritrasmesso(monkeypatch, udp):
    """Il primo invio si perde; la ritrasmissione arriva e riceve risposta."""
    inviati: list[str] = []

    async def _send(msg):
        inviati.append(msg)
        if len(inviati) == 2:        # risponde solo alla ritrasmissione
            await sip.pending_responses["c1"].put(_ok("c1"))

    monkeypatch.setattr(sip, "send", _send)
    risposte = asyncio.run(sip._send_request("REGISTER x\r\n\r\n", "c1", timeout=2))
    assert len(risposte) == 1 and risposte[0].startswith("SIP/2.0 200")
    assert len(inviati) == 2
    assert inviati[0] == inviati[1], "la ritrasmissione deve essere identica (stesso branch)"


def test_intervalli_crescenti_e_poi_costanti(monkeypatch, udp):
    tempi: list[float] = []

    async def _send(_msg):
        tempi.append(time.monotonic())

    monkeypatch.setattr(sip, "send", _send)
    assert asyncio.run(sip._send_request("MESSAGE x\r\n\r\n", "c2", timeout=0.6)) == []
    gaps = [b - a for a, b in zip(tempi, tempi[1:], strict=False)]
    assert len(gaps) >= 4
    # T1=0.02 raddoppia fino a T2=0.08, poi resta lì (con un margine per lo scheduler).
    assert gaps[0] < gaps[1] < gaps[2] + 0.01
    assert all(g <= 0.08 + 0.05 for g in gaps)


def test_dopo_una_risposta_provvisoria_non_si_ritrasmette(monkeypatch, udp):
    inviati: list[str] = []

    async def _send(msg):
        inviati.append(msg)
        if len(inviati) == 1:
            await sip.pending_responses["c3"].put("SIP/2.0 100 Trying\r\nCall-ID: c3\r\n\r\n")

    monkeypatch.setattr(sip, "send", _send)
    asyncio.run(sip._send_request("MESSAGE x\r\n\r\n", "c3", timeout=0.3))
    assert len(inviati) == 1


def test_su_tls_non_si_ritrasmette(monkeypatch):
    monkeypatch.setattr(runtime, "USE_LOCAL_UDP", False, raising=False)
    monkeypatch.setattr(sip, "_T1", 0.02)
    inviati: list[str] = []

    async def _send(msg):
        inviati.append(msg)

    monkeypatch.setattr(sip, "send", _send)
    asyncio.run(sip._send_request("REGISTER x\r\n\r\n", "c4", timeout=0.2))
    assert len(inviati) == 1


def test_la_coda_esiste_prima_dell_invio(monkeypatch, udp):
    """Una risposta immediata non deve essere scartata come «stale»."""
    async def _send(_msg):
        assert "c5" in sip.pending_responses, "coda creata dopo l'invio"
        await sip.pending_responses["c5"].put(_ok("c5"))

    monkeypatch.setattr(sip, "send", _send)
    assert len(asyncio.run(sip._send_request("REGISTER x\r\n\r\n", "c5", timeout=1))) == 1
    assert "c5" not in sip.pending_responses, "la coda va ripulita alla fine"


# ─── l'event loop non si blocca durante il keyframe ──────────────────────────

def test_il_keyframe_non_blocca_l_event_loop(monkeypatch):
    """Una risposta estranea nella coda del dialog non deve far girare a vuoto il ciclo."""
    cid = "dlg-1"
    monkeypatch.setattr(runtime, "USE_LOCAL_UDP", True, raising=False)
    monkeypatch.setattr(sip, "in_call", True, raising=False)
    monkeypatch.setitem(sip.call_state, "call_id", cid)
    monkeypatch.setitem(sip.call_state, "from_tag", "lt")
    monkeypatch.setitem(sip.call_state, "to_tag", "rt")
    monkeypatch.setitem(sip.call_state, "remote_contact", "sip:55100@192.0.2.1")
    monkeypatch.setitem(sip.call_state, "original_target", "sip:55100@x.test")

    async def _send(_msg):
        return None

    monkeypatch.setattr(sip, "send", _send)

    async def scenario():
        estranea = f"SIP/2.0 200 OK\r\nCall-ID: {cid}\r\nCSeq: 7 BYE\r\n\r\n"
        sip.pending_responses[cid] = asyncio.Queue()
        await sip.pending_responses[cid].put(estranea)

        blocco_max = 0.0
        stop = False

        async def sentinella():
            nonlocal blocco_max
            last = time.monotonic()
            while not stop:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                blocco_max = max(blocco_max, now - last)
                last = now

        task = asyncio.create_task(sentinella())
        await sip.send_keyframe_request()
        stop = True
        await task
        # La risposta estranea è stata restituita alla coda, non persa.
        assert sip.pending_responses[cid].qsize() == 1
        return blocco_max

    blocco = asyncio.run(scenario())
    assert blocco < 0.5, f"event loop bloccato per {blocco:.2f}s"
