"""Le view HTTP vere (/av, /audio_ws, /rings) di __init__.py, in due modi:

* `load_views()` + `WEB`: un aiohttp minimo finto, gira ovunque (CI);
* `start()`: un aiohttp vero con `handler_cancellation=True` come l'HTTP di Home
  Assistant, più la pagina della card (card.py) e un hass finto collegato all'hub.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from urllib.parse import quote

from custom_components.vimar_intercom import const as C
from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import runtime

# ─── aiohttp minimo finto ──────────────────────────────────────────────────────

class _WS:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.closed = False

    async def prepare(self, request):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        m = await self.inbox.get()
        if m is None:
            raise StopAsyncIteration
        return m

    async def send_str(self, s):
        self.sent.append(s)

    async def send_bytes(self, b):
        self.sent.append(b)

    async def close(self, **k):
        self.closed = True
        self.close_args = k
        self.inbox.put_nowait(None)


class _Stream:
    def __init__(self):
        self.chunks: list[bytes] = []
        self.status = 200

    async def prepare(self, request):
        return self

    async def write(self, chunk):
        self.chunks.append(chunk)


class _Response:
    def __init__(self, status=200, text="", content_type=None):
        self.status, self.text, self.content_type = status, text, content_type


class _JSONResponse(_Response):
    def __init__(self, data, status=200):
        super().__init__(status=status, text=json.dumps(data), content_type="application/json")
        self.data = data


class _FileResponse(_Response):
    def __init__(self, path):
        super().__init__()
        self.path = path


WEB = types.SimpleNamespace(
    WebSocketResponse=_WS, StreamResponse=_Stream, Response=_Response,
    json_response=_JSONResponse, FileResponse=_FileResponse,
    WSMsgType=types.SimpleNamespace(TEXT="text", BINARY="binary", ERROR="error", CLOSE="close"))


class Request:
    def __init__(self, admin=True, query=None):
        self.headers = {}
        self.remote = "127.0.0.1"
        self.path = "/api/vimar_intercom/x"
        self.query = query or {}
        self._user = types.SimpleNamespace(is_admin=admin)

    def get(self, k, d=None):
        # Like HA's auth middleware (homeassistant/components/http/auth.py): a user
        # means an authenticated request, under the key HA really uses.
        if k == "ha_authenticated":
            return self._user is not None
        return self._user if k == "hass_user" else d


def load_views(monkeypatch, web=WEB):
    """Carica __init__.py (le view) sugli stub di HA del conftest, con `web` a scelta."""
    for name in ("homeassistant.components.frontend", "homeassistant.helpers.service"):
        if name not in sys.modules:
            m = types.ModuleType(name)
            m.__getattr__ = lambda n: (lambda *a, **k: None)  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, name, m)
    vol = sys.modules["voluptuous"]
    for n in ("Match", "Invalid"):
        if not hasattr(vol, n):
            monkeypatch.setattr(vol, n, lambda *a, **k: None, raising=False)
    path = os.path.join(os.path.dirname(C.__file__), "__init__.py")
    spec = importlib.util.spec_from_file_location(
        "custom_components.vimar_intercom._views", path, submodule_search_locations=None)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "web", web)
    # The views live in views.py (#33): they read `web` from there.
    monkeypatch.setattr(importlib.import_module("custom_components.vimar_intercom.views"), "web", web)
    return mod


def fake_ws(monkeypatch, views, ws=None):
    """The socket the view's next /audio_ws request gets (a new fake one by default)."""
    ws = views.web.WebSocketResponse() if ws is None else ws
    monkeypatch.setattr(views.web, "WebSocketResponse", lambda **_: ws)
    return ws


def make_hass(rig) -> types.SimpleNamespace:
    """hass.data come lo prepara async_setup_entry, con i WS audio collegati al media."""
    clients: set = set()
    rig.audio_ws_clients = clients

    async def ws_send_bytes(data, only=None):
        for ws in list(clients) if only is None else [only]:
            try:
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001
                clients.discard(ws)

    media.ws_send_bytes = ws_send_bytes
    rig.hub._has_ws_clients = lambda: len(clients) > 0
    return types.SimpleNamespace(
        data={C.DOMAIN: {"e1": {"hub": rig.hub, "audio_ws_clients": clients}}},
        async_create_background_task=lambda coro, name: asyncio.create_task(coro),
        async_add_executor_job=lambda f, *a: asyncio.get_running_loop().run_in_executor(None, f, *a))


def open_av(views, hass, request=None, passive=False) -> asyncio.Task:
    """Un client di /av (go2rtc, stream worker): il task finisce con la risposta.
    `passive`: /av?autocall=0, come Scrypted o Frigate (docs/EXTERNAL.md)."""
    request = request or Request(query={"autocall": "0"} if passive else None)
    return asyncio.create_task(views.VimarAVStreamView(hass).get(request))


# ─── aiohttp vero ──────────────────────────────────────────────────────────────

def _translations(lang: str) -> dict:
    return json.loads((Path(C.__file__).parent / "translations" / f"{lang}.json").read_text(encoding="utf-8"))


async def start(rig):
    """Server vero su 127.0.0.1: le view, la pagina della card (/) e il suo hass finto
    (/state, /svc). Imposta rig.base, rig.views, rig.hass."""
    from aiohttp import web

    from .card import CARD_JS, PAGE

    rig.views = views = load_views(rig.mp, web)
    rig.hass = hass = make_hass(rig)
    hub = rig.hub

    @web.middleware
    async def auth(request, handler):
        request["hass_user"] = types.SimpleNamespace(is_admin=True)
        return await handler(request)

    async def svc(request):
        name = f"{request.match_info['d']}.{request.match_info['s']}"
        rig.services.append(name)
        if name == "vimar_intercom.hangup":
            await hub.async_hangup()
            ok, msg = True, "Chiamata terminata"
        else:
            ok, msg, *code = await {"vimar_intercom.call": hub.async_call, "vimar_intercom.answer": hub.async_answer,
                                    "lock.unlock": hub.async_door, "button.press": hub.async_decline,
                                    "vimar_intercom.decline": hub.async_decline}[name]()
            if name == "lock.unlock" and not ok:  # as HA's call_service error: English message plus the key
                key, ph = f"door_{msg}", {"code": str(code[0])}
                return web.json_response({"ok": False, "error": {
                    "message": _translations("en")["exceptions"][key]["message"].format(**ph),
                    "translation_domain": C.DOMAIN, "translation_key": key, "translation_placeholders": ph}})
        return web.json_response({"ok": ok, "result": msg})

    async def translations(r):  # what hass.loadBackendTranslation fetches
        return web.json_response(_translations(r.match_info["lang"]))

    av, aws = views.VimarAVStreamView(hass), views.VimarAudioWSView(hass)
    rings, photo = views.VimarRingsView(hass), views.VimarRingPhotoView(hass)
    upload = views.VimarAwayUploadView(hass)  # hass.config / config_entries: li mette il test
    app = web.Application(middlewares=[auth])
    async def av_get(r):  # stato imposto alla card: niente stream (farebbe un auto-call)
        return web.Response(status=503) if rig.state_override else await av.get(r)

    app.router.add_get(av.url, av_get)
    app.router.add_get(aws.url, aws.get)
    app.router.add_get(rings.url, rings.get)
    app.router.add_post(upload.url, upload.post)
    async def page(r):  # the fake picture entity opens /av as HA's camera does: with the key
        return web.Response(text=PAGE.replace("__AV_URL__", av_url()), content_type="text/html")

    async def state(r):  # come i sensori: stato, e foto/clip dell'ultimo squillo (attributi)
        return web.json_response({"status": rig.state_override or hub.status, "last_ring": hub.ring_media(),
                                  "last_ring_time": (t := hub.stats.get("last_ring_time")) and t.isoformat()})

    async def ring_photo(r):
        return await photo.get(r, r.match_info["name"])

    app.router.add_get(photo.url, ring_photo)
    app.router.add_get("/", page)
    app.router.add_static("/vimar_intercom", CARD_JS.parent)  # the card's modules, as HA serves www/
    app.router.add_get("/state", state)
    app.router.add_post("/svc/{d}/{s}", svc)
    app.router.add_get("/translations/{lang}", translations)
    rig.http = web.AppRunner(app, handler_cancellation=True, shutdown_timeout=0.5)  # come HA
    await rig.http.setup()
    site = web.TCPSite(rig.http, "127.0.0.1", 0)
    await site.start()
    rig.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def av_url(path="/api/vimar_intercom/av") -> str:
    """`path` with the installation's key (#63) when it places a call, as HA's camera
    sends it to its stream worker. Passive paths (autocall=0) stay as given: the key
    is optional there, and the tests for that case must keep working without it."""
    if "autocall=0" in path or "mode=passive" in path or f"{runtime.AV_KEY_PARAM}=" in path:
        return path
    return f"{path}{'&' if '?' in path else '?'}{runtime.AV_KEY_PARAM}={quote(runtime.AV_KEY)}"


class AvClient:
    """Legge /av come go2rtc: finito uno stream si riconnette dopo `retry` s
    (`reconnect=False`: una volta sola, come l'iPhone che apre la camera).
    A calling path gets the key, like the integration's camera (av_url)."""

    def __init__(self, base, retry=0.3, reconnect=True, path="/api/vimar_intercom/av"):
        self.base, self.retry, self.reconnect, self.path = base, retry, reconnect, path
        self.segments: list[bytearray] = []
        self.statuses: list[int] = []
        self.task: asyncio.Task | None = None

    def start(self) -> AvClient:
        self.task = asyncio.create_task(self._run())
        return self

    @property
    def bytes(self) -> int:
        return sum(len(s) for s in self.segments)

    async def _run(self):
        import aiohttp
        async with aiohttp.ClientSession() as s:
            while True:
                buf = bytearray()
                try:
                    async with s.get(self.base + av_url(self.path)) as r:
                        self.statuses.append(r.status)
                        if r.status == 200:
                            self.segments.append(buf)
                            async for chunk in r.content.iter_any():
                                buf += chunk
                except aiohttp.ClientError:
                    pass
                if not self.reconnect:
                    return
                await asyncio.sleep(self.retry)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def ws_json(d: dict) -> types.SimpleNamespace:
    """Messaggio di testo per il WebSocket finto (/audio_ws)."""
    return types.SimpleNamespace(type="text", data=json.dumps(d))
