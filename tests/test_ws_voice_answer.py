"""Voce su /audio_ws mentre squilla = «Rispondi» (Echo Show / HomeKit via Scrypted)."""
import asyncio
import types

from harness.web import Request, fake_ws, load_views, make_hass

from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import runtime
from custom_components.vimar_intercom import sip_client as sip

SILENCE = b"\x02" + b"\x00\x00" * 341                # 42,6 ms a 8 kHz, RMS 0
VOICE = b"\x02" + (10000).to_bytes(2, "little") * 341  # RMS 10000


def _run(monkeypatch, hub, frames, ringing, declared=True, mode="declared"):
    monkeypatch.setattr(runtime, "VOICE_ANSWER", mode)
    views = load_views(monkeypatch)
    monkeypatch.setattr(sip, "ringing", lambda: ringing)
    answered, sent = [], []

    async def answer():
        answered.append(1)
        monkeypatch.setattr(sip, "in_call", True)  # da qui la voce va alla targa
        return True, "ok"

    monkeypatch.setattr(hub, "async_answer", answer)
    monkeypatch.setattr(media, "send_audio", sent.append)
    hass = make_hass(types.SimpleNamespace(hub=hub))
    ws = fake_ws(monkeypatch, views)
    for f in frames:
        ws.inbox.put_nowait(types.SimpleNamespace(type="binary", data=f))
    ws.inbox.put_nowait(None)
    asyncio.run(views.VimarAudioWSView(hass).get(Request(query={"voice_answer": "1"} if declared else {})))
    return len(answered), len(sent)


def test_il_silenzio_non_risponde(monkeypatch, hub):
    assert _run(monkeypatch, hub, [SILENCE] * 20, ringing=True) == (0, 0)


def test_la_voce_risponde_una_volta_e_poi_passa_alla_targa(monkeypatch, hub):
    # 4 frame di voce = 170 ms: non basta; il 5º supera i 200 ms e risponde; i 3 dopo vanno alla targa.
    assert _run(monkeypatch, hub, [VOICE] * 4, ringing=True) == (0, 0)
    assert _run(monkeypatch, hub, [VOICE] * 8, ringing=True) == (1, 3)


def test_la_voce_a_riposo_si_butta(monkeypatch, hub):
    assert _run(monkeypatch, hub, [VOICE] * 8, ringing=False) == (0, 0)


def test_senza_dichiarazione_la_voce_non_risponde(monkeypatch, hub):
    assert _run(monkeypatch, hub, [VOICE] * 8, ringing=True, declared=False) == (0, 0)


def test_off_la_voce_non_risponde_nemmeno_se_dichiarata(monkeypatch, hub):
    assert _run(monkeypatch, hub, [VOICE] * 8, ringing=True, mode="off") == (0, 0)


def test_any_risponde_anche_senza_dichiarazione(monkeypatch, hub):
    assert _run(monkeypatch, hub, [VOICE] * 8, ringing=True, declared=False, mode="any") == (1, 3)
