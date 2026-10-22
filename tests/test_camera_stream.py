"""Issue #8: la camera era sempre nera.

Tre cause, tre guardie:
1. le porte dell'ffmpeg AV si sovrapponevano (ffmpeg apre RTP **e** RTCP = RTP+1
   per ogni riga m=): 19201/19202 → l'RTCP del video cadeva sull'audio, bind
   fallito, /av mai partito;
2. il lettore dello stderr usciva appena il processo era già morto, cioè proprio
   quando lo stderr spiegava il perché;
3. la camera dichiarava MJPEG senza `CameraEntityFeature.STREAM`, quindi HA non
   apriva mai `stream_source()`.
"""
from __future__ import annotations

import asyncio
import collections
import importlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from custom_components.vimar_intercom import av_stream
from custom_components.vimar_intercom import const as C

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "vimar_intercom"


def _porte_ffmpeg() -> set[int]:
    """Porte che ffmpeg apre davvero: RTP e RTCP (+1) di ogni flusso."""
    porte: list[int] = []
    for p in (C.FFMPEG_AV_VIDEO_PORT, C.FFMPEG_AV_AUDIO_PORT):
        porte += [p, p + 1]
    assert len(porte) == len(set(porte)), f"porte RTP/RTCP sovrapposte: {porte}"
    return set(porte)


def test_porte_av_pari_e_senza_sovrapposizioni():
    assert C.FFMPEG_AV_VIDEO_PORT % 2 == 0
    assert C.FFMPEG_AV_AUDIO_PORT % 2 == 0
    _porte_ffmpeg()


def test_porte_av_non_toccano_quelle_della_chiamata():
    chiamata = {C.RTP_AUDIO_PORT, C.RTP_AUDIO_PORT + 1, C.RTP_VIDEO_PORT, C.RTP_VIDEO_PORT + 1}
    assert not (_porte_ffmpeg() & chiamata)


def test_l_sdp_di_ffmpeg_usa_le_costanti(tmp_path, monkeypatch):
    sdp = Path(av_stream._write_av_sdp()).read_text()
    porte = {int(p) for p in re.findall(r"^m=\w+ (\d+) ", sdp, flags=re.M)}
    assert porte == {C.FFMPEG_AV_VIDEO_PORT, C.FFMPEG_AV_AUDIO_PORT}


def test_l_sdp_di_ffmpeg_porta_sps_e_pps(tmp_path, monkeypatch):
    """SPS/PPS già visti vanno nell'SDP: ffmpeg decodifica dal primo IDR, non da quello
    che li porta in banda (~8 s sulla 40515, oltre la chiamata)."""
    from custom_components.vimar_intercom import media_handler as media
    monkeypatch.setattr(media, "video_proto", type("V", (), {"sps_pps": lambda self: (bytes([0x67, 0x42]), bytes([0x68, 0xCE]))})())
    assert "sprop-parameter-sets=Z0I=,aM4=" in Path(av_stream._write_av_sdp()).read_text()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg non installato")
def test_ffmpeg_riesce_a_fare_il_bind(tmp_path, monkeypatch):
    """Prova vera: ffmpeg apre l'SDP e resta in ascolto (con le porte vecchie
    usciva subito con «bind failed»)."""
    sdp = av_stream._write_av_sdp()
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-protocol_whitelist", "file,udp,rtp",
         "-i", sdp, "-c", "copy", "-f", "mpegts", "-y", str(tmp_path / "out.ts")],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return  # ancora vivo = porte aperte, in attesa di RTP: giusto
        err = proc.stderr.read().decode(errors="replace")
        pytest.fail(f"ffmpeg è uscito subito (rc={proc.returncode}): {err}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_lo_stderr_si_legge_anche_da_processo_morto():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stderr.write('bind failed\\n')"],
        stderr=subprocess.PIPE)
    proc.wait()
    tail: collections.deque[str] = collections.deque(maxlen=5)
    asyncio.run(av_stream._read_av_ffmpeg_stderr(proc, tail))
    assert list(tail) == ["bind failed"]


def _codice(nome: str) -> str:
    righe = (COMPONENT / nome).read_text(encoding="utf-8").splitlines()
    return "\n".join(r for r in righe if not r.lstrip().startswith("#"))


def test_la_camera_dichiara_lo_stream():
    src = _codice("camera.py")
    assert "_attr_supported_features = CameraEntityFeature.STREAM" in src
    assert "def frontend_stream_type" not in src, "MJPEG forzato: HA non userebbe stream_source()"
    assert "def stream_source" in src


def test_anteprime_senza_chiamare_la_targa():
    """Da ferma, una miniatura non deve far chiamare la targa: le foto non vengono
    dallo stream (use_stream_for_stills resta al default False) ma dal frame
    grabber, e fuori da chiamata o squillo non c'è immagine."""
    src = _codice("camera.py")
    assert "def use_stream_for_stills" not in src
    corpo = src.split("def async_camera_image", 1)[1]
    assert "if not self._hub.video_active:" in corpo
    assert "return None" in corpo


def test_camera_disponibile_e_stream_fermato_a_fine_chiamata():
    """Dal campo: i 503 voluti di /av a riposo facevano segnare la camera
    "unavailable", e lo stream di HA restava a riprovare con attese di 10-30 s:
    al "Vedi esterno" dopo, card bianca. A fine chiamata lo stream si ferma."""
    import asyncio
    import sys
    from types import SimpleNamespace

    ha_camera = sys.modules.get("homeassistant.components.camera")
    if getattr(sys.modules.get("homeassistant"), "_is_stub", False) and ha_camera is not None:
        ha_camera.Camera = object  # lo stub di conftest non ha la classe vera
        ha_camera.CameraEntityFeature = SimpleNamespace(STREAM=2)
        sys.modules.pop("custom_components.vimar_intercom.camera", None)
    camera_mod = importlib.import_module("custom_components.vimar_intercom.camera")
    hub_mod = importlib.import_module("custom_components.vimar_intercom.hub")

    fermati = []

    async def _stop():
        fermati.append(True)

    async def run():
        hub = hub_mod.VimarIntercomHub()
        cam = camera_mod.VimarIntercomCamera.__new__(camera_mod.VimarIntercomCamera)
        cam._hub = hub
        cam.hass = SimpleNamespace(async_create_task=asyncio.ensure_future)
        cam.stream = SimpleNamespace(stop=_stop, outputs=dict)
        hub.register_video_end_callback(cam._stop_stream)
        hub._was_busy = True
        hub._on_sip_state_change()  # in_call/calling scendono: fine chiamata
        await asyncio.sleep(0)
        assert cam.available == hub.registered

    asyncio.run(run())
    assert fermati == [True]


@pytest.mark.parametrize("http, expected", [
    (dict(server_port=8124, ssl_certificate=None), "http://127.0.0.1:8124/api/vimar_intercom/av"),
    (dict(server_port=443, ssl_certificate="/ssl/fullchain.pem"),
     "https://127.0.0.1:443/api/vimar_intercom/av"),
    (None, "http://127.0.0.1:8123/api/vimar_intercom/av"),
])
def test_stream_source_is_loopback_on_the_real_http_port(http, expected):
    """internal_url may point at a reverse proxy: its X-Forwarded-For makes
    /av refuse the request (403, _is_local_request)."""
    import asyncio
    import sys
    from types import SimpleNamespace

    ha_camera = sys.modules.get("homeassistant.components.camera")
    if getattr(sys.modules.get("homeassistant"), "_is_stub", False) and ha_camera is not None:
        ha_camera.Camera = object
        ha_camera.CameraEntityFeature = SimpleNamespace(STREAM=2)
        sys.modules.pop("custom_components.vimar_intercom.camera", None)
    camera_mod = importlib.import_module("custom_components.vimar_intercom.camera")

    cam = camera_mod.VimarIntercomCamera.__new__(camera_mod.VimarIntercomCamera)
    cam._hass = SimpleNamespace(
        config=SimpleNamespace(internal_url="https://proxy.example.test"),
        **({"http": SimpleNamespace(**http)} if http else {}))
    url = asyncio.run(cam.stream_source())
    base, _, query = url.partition("?")
    assert base == expected
    # The /av key (#63) rides along, so plain /av accepts the stream worker.
    assert query == f"{camera_mod.R.AV_KEY_PARAM}={camera_mod.R.AV_KEY}" and camera_mod.R.AV_KEY
