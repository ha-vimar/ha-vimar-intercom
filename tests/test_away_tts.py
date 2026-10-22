"""Messaggio di assenza da testo: TTS di HA finto (testo → «mp3» → PCM), cache per
(testo, motore, lingua), fallback se il TTS fallisce, il file vince sul testo."""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import types

import pytest

from custom_components.vimar_intercom import away_tts  # noqa: E402
from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import media_handler as mh  # noqa: E402
from custom_components.vimar_intercom import runtime as R  # noqa: E402

sip = hub_mod.sip
TESTO = "Non siamo in casa, lasciate un messaggio"


@pytest.fixture
def tts(monkeypatch):
    """TTS di HA finto: accetta solo le lingue in `languages`, l'audio è «MP3:<testo>»;
    al posto di ffmpeg un decoder che antepone «PCM:»."""
    fake = types.SimpleNamespace(calls=[], languages={"it"}, fail=False)

    def generate_media_source_id(hass, message, engine=None, language=None):
        if language not in fake.languages:
            raise RuntimeError(f"Language '{language}' not supported")
        fake.calls.append((message, engine, language))
        return f"media-source://tts/{message}"

    async def async_get_media_source_audio(hass, media_id):
        if fake.fail:
            raise OSError("rete assente")
        return "mp3", b"MP3:" + media_id.rsplit("/", 1)[1].encode()

    fake.generate_media_source_id = generate_media_source_id
    fake.async_get_media_source_audio = async_get_media_source_audio
    monkeypatch.setattr(away_tts, "tts", fake)
    monkeypatch.setattr(away_tts, "_hass", types.SimpleNamespace(config=types.SimpleNamespace(language="it")))
    monkeypatch.setattr(away_tts, "_cache", None)
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "")
    monkeypatch.setattr(R, "AWAY_MESSAGE_TEXT", TESTO)
    monkeypatch.setattr(R, "AWAY_MESSAGE_TTS", "")

    async def load_pcm(src):
        return b"PCM:" + src if isinstance(src, bytes) else None
    monkeypatch.setattr(mh, "load_pcm", load_pcm)
    return fake


def test_testo_letto_dal_motore_predefinito_nella_lingua_di_ha(tts):
    assert asyncio.run(away_tts.load_pcm()) == b"PCM:MP3:" + TESTO.encode()
    assert tts.calls == [(TESTO, None, "it")]


def test_motore_scelto_e_lingua_nella_grafia_del_motore(tts, monkeypatch):
    monkeypatch.setattr(R, "AWAY_MESSAGE_TTS", "tts.piper")
    tts.languages = {"it_IT"}  # piper
    assert asyncio.run(away_tts.load_pcm())
    assert tts.calls == [(TESTO, "tts.piper", "it_IT")]


def test_cache_per_testo_motore_e_lingua(tts, monkeypatch):
    assert asyncio.run(away_tts.load_pcm()) == asyncio.run(away_tts.load_pcm())
    assert len(tts.calls) == 1  # seconda volta dalla cache
    monkeypatch.setattr(R, "AWAY_MESSAGE_TEXT", "Torniamo subito")
    assert asyncio.run(away_tts.load_pcm()) == b"PCM:MP3:Torniamo subito"
    monkeypatch.setattr(R, "AWAY_MESSAGE_TTS", "tts.google")
    asyncio.run(away_tts.load_pcm())
    away_tts._hass.config.language = "en"
    tts.languages = {"it", "en"}
    asyncio.run(away_tts.load_pcm())
    assert [c[1:] for c in tts.calls] == [(None, "it"), (None, "it"), ("tts.google", "it"), ("tts.google", "en")]


def test_tts_fallito_avvisa_e_non_resta_in_cache(tts, caplog):
    tts.fail = True
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(away_tts.load_pcm()) is None
    assert "sintesi vocale fallita" in caplog.text and "rete assente" in caplog.text
    tts.fail = False
    assert asyncio.run(away_tts.load_pcm())  # riprova alla chiamata dopo


def test_nessun_motore_tts(tts, caplog):
    tts.languages = set()  # nessun motore: generate_media_source_id fallisce con ogni lingua
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(away_tts.load_pcm()) is None
    assert "sintesi vocale fallita" in caplog.text


def test_senza_testo_o_senza_hass_niente(tts, monkeypatch):
    monkeypatch.setattr(R, "AWAY_MESSAGE_TEXT", "")
    assert asyncio.run(away_tts.load_pcm()) is None
    monkeypatch.setattr(R, "AWAY_MESSAGE_TEXT", TESTO)
    monkeypatch.setattr(away_tts, "_hass", None)
    assert asyncio.run(away_tts.load_pcm()) is None and tts.calls == []


# ─── nell'hub: il file vince, il testo si sente, il TTS fallito lascia squillare ──

@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    # 0 means "the 20 s default" (0 or 20 == 20), so each test slept 20 real seconds.
    monkeypatch.setattr(R, "AWAY_MESSAGE_DELAY", 0.01)
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "")
    monkeypatch.setattr(R, "AWAY_MESSAGE_TEXT", TESTO)
    monkeypatch.setitem(sip.pending_incoming, "active", True)
    monkeypatch.setitem(sip.pending_incoming, "cid", "ring-1")
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setitem(sip.call_state, "call_id", None)
    azioni = []

    async def _file(path):
        azioni.append(f"file:{path}")
        return b"\0" * 640

    async def _tts():
        azioni.append("tts")
        return h.tts_pcm

    async def _answer():
        azioni.append("answer")
        sip.in_call = True
        sip.call_state["call_id"] = "ring-1"
        sip.pending_incoming["active"] = False
        return True, "200"

    async def _send(data, alive):
        azioni.append(f"send:{data!r}")

    async def _hangup():
        azioni.append("hangup")

    h.tts_pcm = b"\1" * 640
    monkeypatch.setattr(mh, "load_pcm", _file)
    monkeypatch.setattr(away_tts, "load_pcm", _tts)
    monkeypatch.setattr(mh, "send_pcm", _send)
    monkeypatch.setattr(sip, "do_answer_incoming", _answer)
    monkeypatch.setattr(h, "async_hangup", _hangup)
    h.azioni = azioni
    return h


def test_hub_senza_file_fa_sentire_il_testo(hub):
    asyncio.run(hub._away_message("ring-1"))
    assert hub.azioni == ["tts", "answer", "send:" + repr(hub.tts_pcm), "hangup"]


def test_hub_il_file_vince_sul_testo(hub, monkeypatch):
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "/x/messaggio.mp3")
    asyncio.run(hub._away_message("ring-1"))
    assert hub.azioni[:2] == ["file:/x/messaggio.mp3", "answer"] and "tts" not in hub.azioni


def test_hub_tts_fallito_lascia_squillare(hub):
    hub.tts_pcm = None
    asyncio.run(hub._away_message("ring-1"))
    assert hub.azioni == ["tts"]  # niente risposta muta


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg non installato")
def test_load_pcm_da_bytes_con_tetto_di_durata():
    mp3 = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=0.5",
                          "-f", "mp3", "pipe:1"], check=True, capture_output=True).stdout
    pcm = asyncio.run(mh.load_pcm(mp3))
    assert pcm and 0.45 * 16000 <= len(pcm) <= 0.6 * 16000  # 0,5 s a 8 kHz, 16 bit
    corto = asyncio.run(mh.load_pcm(mp3, max_seconds=0.2))
    assert corto and len(corto) <= 0.25 * 16000
    assert asyncio.run(mh.load_pcm(b"non e' audio")) is None


def test_setup_prefetches_the_text_once_home_assistant_has_started(tts, monkeypatch):
    started = []
    monkeypatch.setattr(away_tts, "async_at_started", lambda hass, cb: started.append(cb))
    hass = types.SimpleNamespace(config=types.SimpleNamespace(language="it"))
    away_tts.setup(hass)
    assert away_tts._hass is hass and len(started) == 1
    asyncio.run(started[0](None))  # Home Assistant started: the text is synthesised
    assert away_tts._cache[1] == b"PCM:MP3:" + TESTO.encode()


def test_setup_does_not_prefetch_when_a_file_wins_or_there_is_no_text(tts, monkeypatch):
    started = []
    monkeypatch.setattr(away_tts, "async_at_started", lambda hass, cb: started.append(cb))
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "/media/x.mp3")
    away_tts.setup(types.SimpleNamespace())
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "")
    monkeypatch.setattr(R, "AWAY_MESSAGE_TEXT", "")
    away_tts.setup(types.SimpleNamespace())
    assert started == []


def test_audio_that_does_not_decode_is_not_cached(tts, monkeypatch):
    async def load_pcm(src):
        return None  # ffmpeg could not decode the TTS audio
    monkeypatch.setattr(mh, "load_pcm", load_pcm)
    assert asyncio.run(away_tts.load_pcm()) is None
    assert away_tts._cache is None
