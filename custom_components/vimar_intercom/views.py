"""HTTP views (/av, /audio_ws, /debug, /rings), moved out of __init__.py as is."""

import asyncio
import functools
import ipaddress
import json
import logging
import os
import tempfile
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized

from . import av_passive, av_stream, away_config, ring_log, runtime
from . import log_buffer as _log_buffer
from . import media_handler as media
from . import sip_client as sip
from .const import DOMAIN
from .hub import VimarIntercomHub

_LOGGER = logging.getLogger(__name__)

# The request key HA's auth middleware sets on every request, True for a bearer token or
# a signed path (homeassistant.helpers.http.KEY_AUTHENTICATED). Spelled out here because
# the tests run without Home Assistant installed.
KEY_AUTHENTICATED = "ha_authenticated"


def _user_allowed(request: web.Request) -> bool:
    """Opzione `allowed_users`: chi può vedere squilli, foto, clip e media live. Admin
    sempre; lista vuota = ogni utente autenticato; senza utente (/av in LAN dallo stream
    worker o da go2rtc, senza token) come oggi."""
    user = request.get("hass_user")
    return (user is None or user.is_admin or not runtime.ALLOWED_USERS
            or getattr(user, "id", None) in runtime.ALLOWED_USERS)


def _entry_data(hass: HomeAssistant) -> dict:
    """Dati dell'entry attiva. Le view HTTP restano registrate anche dopo aver
    tolto e riaggiunto l'integrazione (entry_id nuovo), e la prima registrata
    vince: legate al vecchio entry_id rispondevano 503 fino al riavvio di HA."""
    # Only an entry's own dict (it has a "hub"): any other key under DOMAIN
    # (a flag, a cache) must never be taken for the active entry.
    return next((v for v in hass.data.get(DOMAIN, {}).values()
                 if isinstance(v, dict) and "hub" in v), {})


# Header che indicano un hop di proxy davanti a noi.
_FORWARDED_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


def _is_local_request(request) -> bool:
    """True se la richiesta arriva da rete locale/loopback.

    Blocca l'accesso agli stream video da Internet (es. remote UI / port
    forwarding). I consumatori legittimi (camera HA, HomeKit) girano sull'host
    HA stesso, quindi vedono IP loopback o privato.

    Dietro un reverse proxy (add-on NGINX, Cloudflare tunnel, Remote UI)
    `request.remote` e' l'indirizzo del proxy, non del chiamante: fino alla
    1.0.5 questo rendeva "locale" tutto Internet. Un hop dichiarato e' quindi
    motivo sufficiente per rifiutare, perche' i consumatori legittimi di questi
    endpoint parlano con Home Assistant in diretta e non ne dichiarano mai.
    """
    for h in _FORWARDED_HEADERS:
        if h in request.headers:
            _LOGGER.warning(
                "Richiesta a %s rifiutata: arriva da un proxy (%s), quindi "
                "l'indirizzo del chiamante non e' verificabile",
                getattr(request, "path", "?"), h)
            return False

    peer = getattr(request, "remote", None)
    if not peer:
        return False
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local



ADMIN_WS_ACTIONS = {"command", "probe", "scan", "register", "reconnect"}


class VimarAudioWSView(HomeAssistantView):
    """WebSocket endpoint for bidirectional audio + intercom control.

    Binary messages:
      Server → Client: 0x01 + PCM16LE (intercom audio, 8kHz mono)
      Server → Client: 0x03 + 00 00 00 01 + NAL H.264 (video, Annex B; la card lo
                       decodifica con WebCodecs, l'app iOS con VideoToolbox)
      Client → Server: 0x02 + PCM16LE (mic audio, 8kHz mono)

    Text messages (JSON):
      Client → Server: {"action": "call"|"hangup"|"door"|"register"|"status"}
      Server → Client: {"type": "state"|"call_started"|"call_ended"|"ring"|"door"|"error", ...}
    """

    url = "/api/vimar_intercom/audio_ws"
    name = "api:vimar_intercom:audio_ws"
    # HARDENING: richiede autenticazione HA. Le azioni di controllo (door, call,
    # ecc.) non sono più raggiungibili senza un token valido.
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        self._hass = hass

    @property
    def _hub(self) -> "VimarIntercomHub | None":
        """Risolve l'hub dalla entry attiva (sicuro con reload)."""
        return _entry_data(self._hass).get("hub")

    @property
    def _ws_clients(self) -> "set[web.WebSocketResponse]":
        """Risolve il set di WS client attivi dalla entry attiva."""
        return _entry_data(self._hass).get("audio_ws_clients", set())

    async def _broadcast(self, msg: dict) -> None:
        """Manda un messaggio JSON a tutti i WS client attivi."""
        text = json.dumps(msg)
        clients = self._ws_clients
        dead = set()
        for ws in list(clients):
            try:
                await ws.send_str(text)
            except Exception:
                dead.add(ws)
        clients.difference_update(dead)

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        # Pings close a phone gone without a FIN (suspended, 5G dropped) in ~45 s, not
        # minutes later by TCP; its sender queue and task in __init__ go with it.
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        hub = self._hub
        if hub is None:
            await ws.close(code=1011, message=b"Integration not loaded")
            return ws
        user = request.get("hass_user")
        is_admin = user is not None and user.is_admin
        if not _user_allowed(request):
            await ws.close(code=1008, message=b"Not allowed")
            return ws
        # ?only=video / ?only=audio: the card opens one socket per player and each
        # dropped the other's media, so over the cloud the video went up twice.
        # Without it (iOS app, other clients): everything, as before.
        want = {"audio": b"\x01", "video": b"\x03"}.get(request.query.get("only"))
        wants = _entry_data(self._hass).get("ws_wants", {})
        if want:
            wants[ws] = want
        ts = _entry_data(self._hass).get("ws_ts", set())
        if request.query.get("ts") == "1":
            ts.add(ws)
        clients = self._ws_clients
        clients.add(ws)
        _LOGGER.info("Audio WS client connected (%d total)", len(clients))
        # Video già in corso (squillo, chiamata): il GOP corrente subito, senza
        # aspettare il prossimo IDR. Anche per chi non è admin: è la vista della card.
        # ?only=audio: niente replay, sarebbero NAL scartati uno per uno per niente.
        if media.video_proto and want != b"\x01":
            # Through this client's own queue, in order with the live NALs.
            media.video_proto.replay_gop_ws(functools.partial(media.ws_send_bytes, only=ws))

        # Send initial state
        await ws.send_str(json.dumps(hub.state_message()))

        loud_ms = 0.0  # voce di fila sopra soglia mentre squilla
        # Risposta a voce secondo l'opzione voice_answer: "declared" solo chi manda
        # ?voice_answer=1 (un microfono lasciato aperto non risponde allo squillo dopo),
        # "off" mai, "any" chiunque.
        mode = runtime.VOICE_ANSWER
        voice_answer = mode == "any" or (mode == "declared" and request.query.get("voice_answer") == "1")
        was_in_call = False  # questa connessione era in chiamata: niente voce finché non torna idle
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._handle_text(ws, msg.data, is_admin)
                elif msg.type == web.WSMsgType.BINARY:
                    # Client sending mic audio: 0x02 prefix + PCM16LE
                    if len(msg.data) <= 1 or msg.data[0] != 0x02:
                        continue
                    pcm = msg.data[1:]
                    if hub.in_call:
                        was_in_call = True
                        hub.claim_call()
                        media.send_audio(pcm)
                    elif not hub.is_ringing:
                        loud_ms = 0.0
                        was_in_call = False
                    elif voice_answer and not was_in_call:
                        # Parlare mentre squilla risponde (stessa strada di "Rispondi");
                        # sotto soglia, o a riposo, il PCM si butta.
                        loud_ms = loud_ms + len(pcm) / 16 if media.rms(pcm) >= media.VOICE_RMS else 0.0
                        if loud_ms >= media.VOICE_ANSWER_MS:
                            loud_ms = 0.0
                            await hub.async_answer()
                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        except Exception as e:
            _LOGGER.error("Audio WS error: %s", e)
        finally:
            clients.discard(ws)
            wants.pop(ws, None)
            ts.discard(ws)
            _LOGGER.info("Audio WS client disconnected (%d remaining)", len(clients))

        return ws

    async def _handle_text(self, ws: web.WebSocketResponse, text: str, is_admin: bool):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return

        action = data.get("action")
        _LOGGER.debug("WS action received: %s (data=%s)", action, data)
        if action in ADMIN_WS_ACTIONS and not is_admin:
            # SIP MESSAGE arbitrari, scansioni e registrazione: roba da amministratore.
            await ws.send_str(json.dumps({"type": "error", "msg": "Admin only"}))
            return
        hub = self._hub
        if hub is None:
            await ws.send_str(json.dumps({"type": "error", "msg": "Integration not loaded"}))
            return

        if action == "status":
            await ws.send_str(json.dumps(hub.state_message()))

        elif action == "call":
            target = data.get("target")  # optional: "55002" etc.
            try:
                ok, m = await hub.async_call(target=target)
                if ok:
                    await self._broadcast({"type": "call_started", "msg": m,
                                           "target": target,
                                           "registered": hub.registered, "in_call": True})
                elif sip.in_call:
                    # Already connected — tell the app immediately
                    _LOGGER.info("Call request: already in call, notifying client")
                    await self._broadcast({"type": "call_started", "msg": "Already in call",
                                           "target": target,
                                           "registered": hub.registered, "in_call": True})
                elif sip.calling:
                    # Call in progress (connecting) — SIP broadcast will notify when connected
                    _LOGGER.info("Call request: already calling, will notify on connect")
                else:
                    await ws.send_str(json.dumps({"type": "error", "msg": m}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "hangup":
            try:
                await hub.async_hangup()
                await self._broadcast({"type": "call_ended", "msg": "Call ended",
                                       "registered": hub.registered, "in_call": False})
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "switch":
            # Atomic panel switch: BYE current + INVITE new (like official app)
            target = data.get("target")
            if not target:
                await ws.send_str(json.dumps({"type": "error", "msg": "No target"}))
            else:
                try:
                    with sip.silenced():  # il BYE del cambio targa non è una fine chiamata
                        await hub.async_hangup()
                    await asyncio.sleep(0.05)  # Minimal — just enough for BYE to send
                    ok, m = await hub.async_call(target=target)
                    if ok:
                        await self._broadcast({"type": "call_started", "msg": m,
                                               "target": target,
                                               "registered": hub.registered, "in_call": True})
                    else:
                        await self._broadcast({"type": "call_ended", "msg": f"Switch failed: {m}",
                                               "registered": hub.registered, "in_call": False})
                except Exception as e:
                    await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "door":
            target = data.get("target")  # "55001" (esterno) or "55002" (interno)
            _LOGGER.info("Door action: target=%s", target)
            try:
                ok, result, code = await hub.async_door(target=target)
                t = "door" if ok else "error"
                await self._broadcast({"type": t, "msg": result, "code": code})
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "command":
            # Comando SIP MESSAGE arbitrario: {"action":"command","body":"...","target":"55001"}
            try:
                ok, m = await hub.async_send_command(
                    body=data.get("body", ""),
                    target=data.get("target"),  # default: SGA, in hub.async_send_command
                    header_name=data.get("header_name", "Panda"),
                    header_value=data.get("header_value", "command"),
                )
                await ws.send_str(json.dumps({"type": "command_result", "ok": ok, "msg": m,
                                              "body": data.get("body")}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "register":
            try:
                ok = await sip.do_register()
                if ok:
                    await self._broadcast({"type": "registered", "msg": "SIP registered",
                                           "registered": True, "in_call": hub.in_call})
                else:
                    await ws.send_str(json.dumps({"type": "error", "msg": "Registration failed"}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "probe":
            target = data.get("target", "")
            try:
                ok, m = await hub.async_probe(target)
                await ws.send_str(json.dumps({"type": "probe_result",
                                               "target": target, "ok": ok, "msg": m}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "scan":
            start = data.get("start", 55001)
            end = data.get("end", 55020)
            try:
                results = await hub.async_scan(start, end)
                await ws.send_str(json.dumps({"type": "scan_result", "results": results}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "answer":
            try:
                ok, m = await hub.async_answer()
                if ok:
                    # Broadcast ring_ended FIRST so other devices stop ringing
                    await self._broadcast({"type": "ring_ended", "msg": "Answered on another device",
                                           "registered": hub.registered, "in_call": True})
                    await self._broadcast({"type": "call_started", "msg": m,
                                           "registered": hub.registered, "in_call": True})
                else:
                    await ws.send_str(json.dumps({"type": "error", "msg": m}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "decline":
            try:
                # il ring_ended lo manda già do_decline_incoming
                ok, _ = await hub.async_decline()
                if not ok:
                    await ws.send_str(json.dumps({"type": "error", "msg": "No ringing call"}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "reconnect":
            _LOGGER.info("Force reconnect requested via WS")
            try:
                ok = await sip.reconnect()
                await ws.send_str(json.dumps(hub.state_message(msg="Reconnected" if ok else "Reconnect failed")))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))


class VimarDebugView(HomeAssistantView):
    """Debug endpoint — returns recent vimar_intercom logs as plain text."""

    url = "/api/vimar_intercom/debug"
    name = "api:vimar_intercom:debug"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        # `requires_auth` da solo lascia leggere il log a qualunque utente
        # di Home Assistant, ospiti compresi. Qui dentro passa la traccia
        # SIP dell'impianto: e' materiale da amministratore.
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            raise Unauthorized()
        try:
            n = int(request.query.get("lines", "100"))
        except ValueError:
            n = 100
        text = "\n".join(_log_buffer.tail(n))
        return web.Response(text=text, content_type="text/plain")


def _save_body(body: bytes | bytearray, folder: str | None, name: str) -> str:
    """Il corpo della richiesta in un file temporaneo, poi save_upload come dalle opzioni."""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "upload")
        with open(src, "wb") as f:
            f.write(body)
        return away_config.save_upload(src, folder, name)


def _loaded_entry(hass):
    """The config entry that is set up, or None (only one is allowed, ADR-22)."""
    return next(iter(hass.config_entries.async_loaded_entries(DOMAIN)), None)


class VimarAwayUploadView(HomeAssistantView):
    """File del messaggio di assenza caricato dalla card: POST col file come corpo e
    ?name=<nome del file>. Solo admin, come le entità del messaggio. Stesse regole
    del caricamento dalle opzioni (save_upload); il file salvato diventa quello scelto."""

    url = "/api/vimar_intercom/away_upload"
    name = "api:vimar_intercom:away_upload"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            raise Unauthorized()
        entry = _loaded_entry(self._hass)
        if entry is None:
            return web.json_response({"error": "not_loaded"}, status=503)
        # Il limite vale mentre si legge: un corpo più grande non finisce mai in memoria.
        too_big = web.json_response({"error": "upload_too_big"}, status=413)
        if (request.content_length or 0) > away_config.UPLOAD_MAX:
            return too_big
        body = bytearray()
        async for chunk in request.content.iter_chunked(64 * 1024):
            body += chunk
            if len(body) > away_config.UPLOAD_MAX:
                return too_big
        try:
            path = await self._hass.async_add_executor_job(
                _save_body, body, away_config.messages_dir(self._hass),
                request.query.get("name", ""))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except OSError:
            _LOGGER.exception("Away message upload not saved")
            return web.json_response({"error": "upload_failed"}, status=500)
        away_config.set_away(self._hass, entry, "away_message_file", path)
        return web.json_response({"file": os.path.basename(path)})


class VimarRingsView(HomeAssistantView):
    """Ultimi squilli (registro accanto alle foto), dal più recente. Per la card.
    Senza cartella foto nelle opzioni: lista vuota."""

    url = "/api/vimar_intercom/rings"
    name = "api:vimar_intercom:rings"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        if not _user_allowed(request):
            raise Unauthorized()
        if not runtime.SNAPSHOT_DIR:
            return web.json_response([])
        try:  # ?limit=: 10 se manca o non è un numero, fra 1 e 50
            limit = max(1, min(50, int(request.query.get("limit") or 10)))
        except ValueError:
            limit = 10
        rings = await self._hass.async_add_executor_job(
            ring_log.recent_rings, runtime.SNAPSHOT_DIR, limit)
        return web.json_response(rings)


class VimarRingPhotoView(HomeAssistantView):
    """Foto (jpg) o clip (mp4) di uno squillo. Solo squillo_AAAAMMGG_HHMMSS[_mmm].{jpg,mp4}
    dentro la cartella foto: nessun altro file è raggiungibile. La card li carica con un
    percorso firmato; FileResponse serve il clip anche a pezzi (Range) per il <video>."""

    url = "/api/vimar_intercom/rings/{name}"
    name = "api:vimar_intercom:ring_photo"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        self._hass = hass

    async def get(self, request: web.Request, name: str) -> web.StreamResponse:
        if not _user_allowed(request):
            raise Unauthorized()
        path = await self._hass.async_add_executor_job(
            ring_log.ring_photo_path, runtime.SNAPSHOT_DIR, name)
        if path is None:
            return web.Response(status=404)
        return web.FileResponse(path)


async def _pump_response(request: web.Request, queue: asyncio.Queue, unsubscribe) -> web.StreamResponse:
    """MPEG-TS in streaming dalla coda finché non arriva None (client via, o l'ffmpeg è
    uscito): stessa pompa per /av (chiamata vera, av_stream) e /av?idle=image (av_passive)."""
    response = web.StreamResponse()
    response.content_type = "video/mp2t"
    try:
        await response.prepare(request)
        while (chunk := await queue.get()) is not None:
            await response.write(chunk)
    except ConnectionResetError:
        pass
    finally:
        await unsubscribe(queue)
    return response


class VimarAVStreamView(HomeAssistantView):
    """Serve MPEG-TS stream (H264 video + PCMU audio) at /api/vimar_intercom/av."""

    url = "/api/vimar_intercom/av"
    name = "api:vimar_intercom:av"
    requires_auth = False

    def __init__(self, hass: HomeAssistant):
        self._hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        if not _is_local_request(request):
            return web.Response(status=403, text="Forbidden (local network only)")
        if not _user_allowed(request):
            return web.Response(status=403, text="Forbidden (user)")
        # The key (#63). A wrong key is refused in every mode. Refusals are 403, never
        # 401: HA's "invalid authentication" warning prints the requested URL, key
        # included, and it comes from the 401 path only.
        key = request.query.get(runtime.AV_KEY_PARAM)
        if key is not None and not runtime.av_key_valid(key):
            _LOGGER.warning("AV stream refused: wrong key")
            return web.Response(status=403, text="Forbidden (key)")
        hub = _entry_data(self._hass).get("hub")
        if hub is None:
            return web.Response(status=503, text="Integration not loaded")
        # ?autocall=0 (o mode=passive): Scrypted, go2rtc, Frigate (docs/EXTERNAL.md).
        # Mai una chiamata da soli e nessuno spettatore per l'hub (non tiene aperto un
        # auto-call): video solo se c'è già (squillo o chiamata), altrimenti 503 subito,
        # così i loro tentativi in ciclo non toccano la targa condominiale.
        passive = request.query.get("autocall") == "0" or request.query.get("mode") == "passive"
        # Plain /av places a call: an authenticated HA user or the key, on top of
        # the local-network check. Passive /av never calls; the key stays optional
        # there for now (docs/EXTERNAL.md), so go2rtc/Frigate setups keep working.
        if not passive and key is None and not request.get(KEY_AUTHENTICATED):
            _LOGGER.warning("AV stream refused: no key and no authenticated user")
            return web.Response(status=403, text="Forbidden (key required)")
        if passive and request.query.get("idle") == "image":
            return await self._idle_image(request, hub)
        if passive and not hub.video_active:
            return web.Response(status=503, text="No call (passive)")
        _LOGGER.info("AV stream requested%s", " (passive)" if passive else "")
        try:  # tutto dentro: se il client se ne va prima, lo spettatore va comunque tolto
            # Inside the try too: stream_opened counts the viewer at once and can
            # then wait for a hang-up; a client leaving meanwhile is uncounted.
            wait = passive or await hub.stream_opened()
            if not wait:
                return web.Response(status=503, text="No call")
            waited = 0
            asked_at = time.monotonic()
            # 25 s: col cloud Vimar la chiamata a volte parte dopo ~15 s (riconnessione TLS).
            # Ma una chiamata finita, annullata o rifiutata (486) non darà video: 503
            # subito, non 25 s di rotella sull'iPhone (contando come spettatore).
            while not hub.video_active and waited < 25 and hub.call_pending:
                await asyncio.sleep(0.1)  # ogni decimo conta: la targa chiude dopo ~10 s
                waited += 0.1
            if not hub.video_active:
                # waited counts 0.1 s steps; the loop also ends early when the
                # call is given up, so the real time says what happened (#44).
                _LOGGER.warning("AV stream: call not established (%.1f s)",
                                time.monotonic() - asked_at)
                return web.Response(status=503, text="Call not established")

            queue = await av_stream.av_subscribe()
            if queue is None:
                return web.Response(status=503, text="ffmpeg failed to start")
            # Primo fotogramma subito, non al prossimo IDR. Non attesa: l'INFO SIP
            # (con eventuale 407) può durare secondi e ritarderebbe gli header.
            self._hass.async_create_background_task(
                sip.send_keyframe_request(), "vimar_intercom keyframe")
            return await _pump_response(request, queue, av_stream.av_unsubscribe)
        finally:
            if not passive:
                await hub.stream_closed()

    async def _idle_image(self, request: web.Request, hub) -> web.StreamResponse:
        """`&idle=image`: stream continuo, standby a riposo e video della targa durante
        squillo o chiamata (av_passive). Mai una chiamata, mai uno spettatore per l'hub."""
        _LOGGER.info("AV stream requested (passive, idle image)")
        queue = await av_passive.subscribe(
            lambda: hub.video_active,
            lambda: self._hass.async_create_background_task(
                sip.send_keyframe_request(), "vimar_intercom keyframe"))
        if queue is None:
            return web.Response(status=503, text="ffmpeg failed to start")
        return await _pump_response(request, queue, av_passive.unsubscribe)
