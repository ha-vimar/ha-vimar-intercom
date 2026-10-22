"""Foto di chi suona: il primo fotogramma subito nella cartella delle opzioni, poi quello
migliore dopo SNAPSHOT_DELAY sullo stesso file (0 = solo il primo)."""
from __future__ import annotations

import asyncio
import os
import tempfile

from custom_components.vimar_intercom import frame_grabber  # noqa: E402
from custom_components.vimar_intercom import hub as hub_mod

R = hub_mod.R
NAME = "squillo_20260927_101500.jpg"
PRIMA, DOPO = b"\xff\xd8PRIMA\xff\xd9", b"\xff\xd8DOPO\xff\xd9"


def _run(monkeypatch, first, better=DOPO, delay=0):
    folder = os.path.join(tempfile.mkdtemp(), "citofono")
    monkeypatch.setattr(R, "SNAPSHOT_DIR", folder)
    monkeypatch.setattr(R, "SNAPSHOT_DELAY", delay)
    asked = []

    async def _wait(timeout=6, after=0):
        asked.append(after)
        return better if after else first

    monkeypatch.setattr(frame_grabber, "wait_frame", _wait)
    hub = hub_mod.VimarIntercomHub()
    asyncio.run(hub._save_ring_photo(NAME))
    return folder, hub, asked


def test_salva_subito_la_prima_foto_e_ultimo_squillo(monkeypatch):
    folder, hub, asked = _run(monkeypatch, PRIMA)
    assert sorted(os.listdir(folder)) == [NAME, "ultimo_squillo.jpg"]
    assert open(os.path.join(folder, NAME), "rb").read() == PRIMA
    assert asked == [0], "con delay 0 non si aspetta una foto migliore"
    media = hub.ring_media()
    assert hub.stats["last_photo"] == NAME and media["foto"] == os.path.join(folder, NAME)
    assert media["foto_url"] == f"/api/vimar_intercom/rings/{NAME}?v={hub.stats['last_photo_v']}"
    assert media["clip"] is None and media["clip_url"] is None


def test_dopo_il_delay_la_foto_migliore_sostituisce_la_prima(monkeypatch):
    folder, hub, asked = _run(monkeypatch, PRIMA, delay=0.2)
    assert asked == [0, 1], "la seconda foto è un fotogramma dopo il primo (scuro)"
    for n in (NAME, "ultimo_squillo.jpg"):
        assert open(os.path.join(folder, n), "rb").read() == DOPO
    assert hub.stats["last_photo_v"] and hub.ring_media()["foto_url"].endswith(f"?v={hub.stats['last_photo_v']}")


def test_stessa_foto_dopo_il_delay_non_riscrive(monkeypatch):
    """Squillo finito prima del secondo IDR: wait_frame ridà la prima, niente riscrittura."""
    folder, hub, _ = _run(monkeypatch, PRIMA, better=PRIMA, delay=0.1)
    assert open(os.path.join(folder, NAME), "rb").read() == PRIMA


def test_senza_anteprima_non_salva(monkeypatch):
    folder, hub, _ = _run(monkeypatch, None)
    assert not os.path.exists(folder) and hub.stats["last_photo"] is None
