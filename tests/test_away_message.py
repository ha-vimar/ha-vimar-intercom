"""Messaggio di assenza: dopo N s di squillo senza risposta, file audio e riaggancio."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile

import pytest

from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import media_handler as mh  # noqa: E402

sip = hub_mod.sip
R = hub_mod.R


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "/x/messaggio.mp3")
    # 0 means "the 20 s default" (0 or 20 == 20), so each test slept 20 real seconds.
    monkeypatch.setattr(R, "AWAY_MESSAGE_DELAY", 0.01)
    monkeypatch.setitem(sip.pending_incoming, "active", True)
    monkeypatch.setitem(sip.pending_incoming, "cid", "ring-1")
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setitem(sip.call_state, "call_id", None)
    return h


def _fakes(hub, monkeypatch, pcm=b"\0" * 640, fine_a_meta=False):
    azioni = []

    async def _load(path):
        azioni.append("load")
        return pcm

    async def _answer():
        azioni.append("answer")
        sip.in_call = True
        sip.call_state["call_id"] = sip.pending_incoming["cid"]
        sip.pending_incoming["active"] = False
        return True, "200"

    async def _send(data, alive):
        azioni.append("send")
        if fine_a_meta:
            sip.in_call = False  # la targa riaggancia durante il messaggio
        azioni.append(f"alive={alive()}")

    async def _hangup():
        azioni.append("hangup")

    monkeypatch.setattr(mh, "load_pcm", _load)
    monkeypatch.setattr(mh, "send_pcm", _send)
    monkeypatch.setattr(sip, "do_answer_incoming", _answer)
    monkeypatch.setattr(hub, "async_hangup", _hangup)
    return azioni


def test_squilla_ancora_risponde_suona_e_riaggancia(hub, monkeypatch):
    azioni = _fakes(hub, monkeypatch)
    asyncio.run(hub._away_message("ring-1"))
    assert azioni == ["load", "answer", "send", "alive=True", "hangup"]


def test_usa_il_ritardo_del_tab_per_il_messaggio_di_ha(hub, monkeypatch):
    _fakes(hub, monkeypatch)
    attese = []

    orig = asyncio.sleep

    async def _sleep(s):
        if s >= 1:  # solo il ritardo, non le pause del riproduttore
            attese.append(s)
        else:
            await orig(s)

    monkeypatch.setattr(hub_mod.asyncio, "sleep", _sleep)
    monkeypatch.setattr(R, "AWAY_MESSAGE_DELAY", 20)
    asyncio.run(hub._away_message("ring-1"))               # il Tab non ha dato valori: le opzioni
    hub.stats["vm_timeout"] = 5
    asyncio.run(hub._away_message("ring-1"))
    assert attese == [20, 5]
    hub.stats.pop("vm_timeout")
    monkeypatch.setattr(R, "AWAY_MESSAGE_DELAY", 0)
    asyncio.run(hub._away_message("ring-1"))               # né Tab né opzioni: default sicuro
    assert attese[-1] == hub_mod.C.DEFAULT_AWAY_DELAY == 20


def test_se_qualcuno_ha_risposto_non_fa_nulla(hub, monkeypatch):
    azioni = _fakes(hub, monkeypatch)
    sip.pending_incoming["active"] = False  # CANCEL: ha risposto il Tab
    asyncio.run(hub._away_message("ring-1"))
    assert azioni == []


def test_timer_di_un_altro_squillo_non_risponde(hub, monkeypatch):
    azioni = _fakes(hub, monkeypatch)
    asyncio.run(hub._away_message("ring-vecchio"))
    assert azioni == []


def test_file_illeggibile_non_risponde(hub, monkeypatch):
    azioni = _fakes(hub, monkeypatch, pcm=None)
    asyncio.run(hub._away_message("ring-1"))
    assert azioni == ["load"]  # lascia squillare: niente risposta muta


def test_chiamata_finita_a_meta_non_riaggancia_altre(hub, monkeypatch):
    azioni = _fakes(hub, monkeypatch, fine_a_meta=True)
    asyncio.run(hub._away_message("ring-1"))
    assert "hangup" not in azioni


def test_rispondi_durante_il_messaggio_prende_la_chiamata(hub, monkeypatch):
    azioni = _fakes(hub, monkeypatch)

    async def _send_lungo(data, alive):
        azioni.append("send")
        await asyncio.sleep(10)

    monkeypatch.setattr(mh, "send_pcm", _send_lungo)

    async def _run():
        hub._away_task = asyncio.create_task(hub._away_message("ring-1"))
        while "send" not in azioni:
            await asyncio.sleep(0)
        ok, _ = await hub.async_answer()
        await asyncio.gather(hub._away_task, return_exceptions=True)
        return ok

    assert asyncio.run(_run())
    assert "hangup" not in azioni  # la chiamata resta a chi ha risposto


def test_un_answered_in_ritardo_dopo_il_messaggio_non_cambia_l_esito(hub, monkeypatch):
    """call_ended azzera _ring_answered: un `C;<id>;ANSWERED` arrivato dopo la fine della
    chiamata del messaggio di assenza cambiava l'esito «away» in «answered_elsewhere»."""
    _fakes(hub, monkeypatch)
    esiti = []
    monkeypatch.setattr(hub, "_log_outcome", esiti.append)
    monkeypatch.setitem(sip.pending_incoming, "caller_uri", "sip:55001@plant.example")
    hub._update_stats("ring", "")
    asyncio.run(hub._away_message("ring-1"))
    hub._update_stats("call_started", "")
    monkeypatch.setattr(sip, "in_call", False)  # call_ended arrives with the call already closed
    hub._update_stats("call_ended", "")
    hub._handle_incoming_message("C;ring-1;ANSWERED")
    assert esiti == ["away"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg non installato")
def test_load_e_send_pcm_a_pacchetti_da_20ms(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "msg.mp3")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=0.2",
                    "-y", path], check=True)
    pacchetti = []
    monkeypatch.setattr(mh, "send_audio", pacchetti.append)

    async def _run():
        pcm = await mh.load_pcm(path)
        await mh.send_pcm(pcm, lambda: True)
        return pcm

    pcm = asyncio.run(_run())
    assert pcm and len(pcm) >= 0.19 * 8000 * 2  # ~0,2 s a 8 kHz, 16 bit
    assert all(len(p) == 320 for p in pacchetti), "the tail is padded to a whole packet"
    joined = b"".join(pacchetti)
    assert joined[:len(pcm)] == pcm and not joined[len(pcm):].strip(b"\x00")


def test_load_pcm_file_mancante():
    assert asyncio.run(mh.load_pcm("/non/esiste.mp3")) is None


def test_load_pcm_passa_il_file_come_url_file(monkeypatch):
    """Un nome che comincia con «-» non deve diventare un'opzione di ffmpeg."""
    argv = []

    class _Proc:
        returncode = 0

        async def communicate(self, data=None):
            return b"\0" * 320, b""

    async def _exec(*args, **kw):
        argv.extend(args)
        return _Proc()

    monkeypatch.setattr(mh.asyncio, "create_subprocess_exec", _exec)
    assert asyncio.run(mh.load_pcm("/config/media/-strano.mp3"))
    assert argv[argv.index("-i") + 1] == "file:/config/media/-strano.mp3"
