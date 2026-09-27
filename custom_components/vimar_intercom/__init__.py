"""Vimar Intercom integration for Home Assistant."""

import asyncio
import ipaddress
import json
import logging

from aiohttp import web

import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_register_admin_service

from .const import CONF_HOMEKIT_ACCESSORY, DEFAULT_HOMEKIT_ACCESSORY, DOMAIN
from . import log_buffer as _log_buffer
from . import validate
from .hub import VimarIntercomHub
from . import media_handler as media
from . import sip_client as sip
from . import runtime

_LOGGER = logging.getLogger(__name__)

# Buffer interno dei log e inoltro al log di HA: vedi log_buffer.py.
_debug_log = _log_buffer.debug_log
_log_buffer.install()

PLATFORMS = ["camera", "lock", "button", "event", "binary_sensor", "sensor", "switch"]

# ─── Servizi ──────────────────────────────────────────────────────────────────
SERVICE_SEND_COMMAND = "send_command"
SERVICE_CALL = "call"
SERVICE_ANSWER = "answer"
SERVICE_HANGUP = "hangup"
SERVICE_OPEN_DOOR = "open_door"
SERVICE_FETCH_LOCAL = "fetch_local"

def _default_sga_target() -> str:
    """Default dinamico per i servizi: valore SGA correntemente configurato
    (options manuali o importer rubrica.db), non più fisso a 55001."""
    return runtime.SGA_TARGET or "55001"


SEND_COMMAND_SCHEMA = vol.Schema({
    vol.Required("body"): cv.string,
    vol.Optional("target", default=_default_sga_target): cv.string,
    vol.Optional("header_name", default="Panda"): cv.string,
    vol.Optional("header_value", default="command"): cv.string,
})
CALL_SCHEMA = vol.Schema({vol.Optional("target"): cv.string})
FETCH_LOCAL_SCHEMA = vol.Schema({
    vol.Required("path"): cv.string,                    # es. rest/get_info.php?action=status
    vol.Optional("save_as"): cv.string,                 # nome file in /config (opzionale)
    vol.Optional("host"): cv.string,                    # default: local_proxy
    vol.Optional("scheme", default="http"): cv.string,
})
def _default_door_target() -> str:
    """La targa che apre la porta (door_target, o l'SGA se non configurata)."""
    return runtime.DOOR_TARGET


OPEN_DOOR_SCHEMA = vol.Schema({
    vol.Optional("target", default=_default_door_target): cv.string,
    vol.Optional("command", default="OPEN_2F"): cv.string,
})

def _get_hub_from_hass(hass: HomeAssistant) -> VimarIntercomHub:
    """Risolve l'hub dalla entry attiva in hass.data[DOMAIN].

    Guarda solo i valori che sono davvero entry (hanno un hub): una chiave di
    servizio finita lì dentro non deve bastare a rompere tutti i servizi.
    """
    for data in hass.data.get(DOMAIN, {}).values():
        if isinstance(data, dict) and data.get("hub") is not None:
            return data["hub"]
    raise HomeAssistantError("Vimar Intercom non è pronto")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vimar Intercom from a config entry."""
    # Identità dispositivo: una per installazione, generata al primo avvio e
    # salvata nell'entry. Fino alla 1.0.1 era una costante in const.py uguale per
    # tutti: sul cloud Vimar la registrazione (e le push) sono associate
    # all'identità, quindi due impianti con lo stesso valore si scalzano a
    # vicenda. Gli entry già esistenti vengono migrati qui, in silenzio.
    if not entry.data.get("device_imei") or not entry.data.get("device_uuid"):
        identity = runtime.new_device_identity()
        # Le entry del fork m4r1k hanno già un'identità, sotto un solo nome
        # («device_id», usato per entrambi gli header): cambiarla spezzerebbe
        # l'abbinamento, quindi la si conserva.
        legacy = str(entry.data.get("device_id") or "").strip()
        if legacy:
            identity = {"device_imei": legacy, "device_uuid": legacy}
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, **identity})
        _LOGGER.info("Identità dispositivo generata per questa installazione")

    # Popola il modulo runtime con i dati del config entry.
    # Le options (impostazioni rete modificate da OptionsFlow) sovrascrivono
    # i valori di default presenti in entry.data.
    runtime.configure({**entry.data, **entry.options})
    _LOGGER.info(
        "Runtime configurato: user=%s domain=%s local_proxy=%s udp=%s",
        runtime.SIP_USER, runtime.SIP_DOMAIN,
        runtime.LOCAL_PROXY, entry.data.get("use_local_udp", True),
    )

    hub = VimarIntercomHub()

    # Insieme di WS audio attivi: vive in hass.data per evitare globals
    # a livello di modulo (sicuro con reload e multi-entry).
    audio_ws_clients: set[web.WebSocketResponse] = set()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "hub": hub, "audio_ws_clients": audio_ws_clients,
        # Le options con cui è partita: il listener ricarica solo se cambiano.
        "options": dict(entry.options),
    }

    @callback
    def _on_model_detected(model: str, fw: str, ua: str, priority: int) -> None:
        """Propaga al device registry il modello rilevato via SIP."""
        registry = dr.async_get(hass)
        device = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
        if device:
            updates: dict[str, str] = {}
            if device.model != model:
                updates["model"] = model
            if fw and device.sw_version != fw:
                updates["sw_version"] = fw
            if updates:
                registry.async_update_device(device.id, **updates)
                _LOGGER.info("Device registry aggiornato: %s", updates)

        # Persisti nel config entry: al prossimo avvio il modello è noto subito
        stored = {
            "detected_model":    model,
            "detected_fw":       fw,
            "detected_ua":       ua,
            "detected_priority": priority,
        }
        if any(entry.data.get(k) != v for k, v in stored.items()):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, **stored})

    hub.register_model_callback(_on_model_detected)

    @callback
    def _persist_learned(updates: dict) -> None:
        """Valori imparati dall'impianto: nei dati dell'entry, non nelle
        options, così salvarli non ricarica l'integrazione."""
        if any(entry.data.get(k) != v for k, v in updates.items()):
            hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})

    hub.set_persist_callback(_persist_learned)

    @callback
    def _on_hub_event(event_type: str, data: dict) -> None:
        """Propaga gli eventi in ingresso del citofono sul bus di HA.

        event_type è già uno dei vimar_intercom_* di const.EVENT_*.
        Payload documentato in README §Eventi.
        """
        try:
            hass.bus.async_fire(event_type, data or {})
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Fire bus event %s failed", event_type)

    hub.register_event_callback(_on_hub_event)

    try:
        await hub.async_start()
    except (OSError, ConnectionError, asyncio.TimeoutError) as err:
        # Rete o relay non raggiungibili all'avvio: Home Assistant riprova da
        # solo più tardi. Prima l'integrazione restava in errore fino a un
        # ricaricamento a mano, con i socket RTP già aperti.
        await hub.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise ConfigEntryNotReady(f"Citofono non raggiungibile: {err}") from err

    # Closures locali: catturano audio_ws_clients (nessun global di modulo).
    async def _ws_send_bytes(data: bytes):
        dead = set()
        for ws in audio_ws_clients:
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.add(ws)
        audio_ws_clients.difference_update(dead)

    async def _broadcast(data: dict):
        text = json.dumps(data)
        dead = set()
        for ws in audio_ws_clients:
            try:
                await ws.send_str(text)
            except Exception:
                dead.add(ws)
        audio_ws_clients.difference_update(dead)

    # Wire up audio broadcast to WebSocket clients
    media.ws_send_bytes = _ws_send_bytes
    media.has_ws_clients = lambda: len(audio_ws_clients) > 0
    media.request_keyframe = hub.request_keyframe
    hub.set_ws_broadcast(_broadcast)

    _register_services(hass)

    hass.http.register_view(VimarAVStreamView(hass, entry.entry_id))
    hass.http.register_view(VimarAudioWSView(hass, entry.entry_id))
    hass.http.register_view(VimarDebugView())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # SPS/PPS della targa salvati: la prima apertura dopo un riavvio non deve
    # aspettare che la targa li rimandi (vedi media_handler.load_parameter_sets).
    await hass.async_add_executor_job(
        media.load_parameter_sets,
        hass.config.path(".storage", f"{DOMAIN}.video_params.json"))

    # Il videocitofono in HomeKit lo pubblichiamo noi: il ponte di Home
    # Assistant non riceve la voce di chi risponde (vedi homekit_accessory).
    # Se qualcosa va storto qui, il resto dell'integrazione deve funzionare
    # lo stesso: il citofono in Home Assistant non dipende da HomeKit.
    if entry.options.get(CONF_HOMEKIT_ACCESSORY, DEFAULT_HOMEKIT_ACCESSORY):
        try:
            from .homekit_accessory import async_setup_homekit  # noqa: PLC0415

            hass.data[DOMAIN][entry.entry_id]["homekit_stop"] = (
                await async_setup_homekit(hass, entry, hub))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Videocitofono HomeKit non avviato")

    # Ricarica l'entry quando cambiano le options (es. lista attuatori):
    # così i bottoni dinamici vengono ricreati con la nuova configurazione.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Ricarica l'integrazione al salvataggio delle options.

    Il listener scatta a ogni modifica dell'entry, anche quando è
    l'integrazione stessa a salvare nei dati il modello rilevato: prima
    quello bastava a ricaricare tutto, anche a chiamata in corso.
    """
    current = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    if current.get("options") == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        # Prima HomeKit, che libera la sua porta: a un ricaricamento il nuovo
        # server la vuole subito.
        if stop := data.get("homekit_stop"):
            try:
                await stop()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Arresto del videocitofono HomeKit")
        # Chiudi tutti i WS audio attivi prima di fermare l'hub:
        # evita che le views (ancora registrate in HA) usino il vecchio hub
        # e che i client rimangano connessi a un hub non più valido.
        for ws in list(data.get("audio_ws_clients", set())):
            await ws.close()
        await data["hub"].async_stop()
        if not any(isinstance(v, dict) and "hub" in v for v in hass.data[DOMAIN].values()):
            for svc in (SERVICE_SEND_COMMAND, SERVICE_CALL, SERVICE_ANSWER,
                        SERVICE_HANGUP, SERVICE_OPEN_DOOR, SERVICE_FETCH_LOCAL):
                hass.services.async_remove(DOMAIN, svc)
    return ok


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


def _register_services(hass: HomeAssistant) -> None:
    """Registra i servizi vimar_intercom.* (una sola volta)."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_COMMAND):
        return

    async def _svc_send_command(call: ServiceCall):
        hub = _get_hub_from_hass(hass)
        ok, msg = await hub.async_send_command(
            body=call.data["body"],
            target=call.data.get("target") or runtime.SGA_TARGET,
            header_name=call.data.get("header_name") or None,
            header_value=call.data.get("header_value") or None,
        )
        _LOGGER.info("Service send_command → ok=%s msg=%s", ok, msg)
        return {"ok": ok, "result": msg}

    async def _svc_call(call: ServiceCall):
        hub = _get_hub_from_hass(hass)
        ok, msg = await hub.async_call(target=call.data.get("target"))
        return {"ok": ok, "result": msg}

    async def _svc_answer(call: ServiceCall):
        hub = _get_hub_from_hass(hass)
        ok, msg = await hub.async_answer()
        return {"ok": ok, "result": msg}

    async def _svc_hangup(call: ServiceCall):
        hub = _get_hub_from_hass(hass)
        await hub.async_hangup()
        return {"ok": True, "result": "Chiamata terminata"}

    async def _svc_fetch_local(call: ServiceCall):
        """GET HTTP (Digest sipID/password) verso l'interfaccia locale del citofono.

        Replica ciò che fa l'app in "home mode": /rest/get_info.php?action=status|nickname,
        /rest/get_file.php?name=rubrica|mailbox. Restituisce stato+anteprima e può salvare il file.
        """
        import os

        import requests as _rq
        from requests.auth import HTTPDigestAuth

        # Il citofono e' in LAN, e questa richiesta porta la password SIP in
        # Digest. Un host arbitrario significava farsi mandare quelle credenziali
        # da un server scelto dal chiamante: si accettano solo indirizzi IP
        # privati o di loopback, scritti per esteso.
        host = call.data.get("host") or runtime.LOCAL_PROXY
        if not validate.is_private_host(host):
            _LOGGER.error("fetch_local: host %r rifiutato (serve un IP privato)", host)
            return {"ok": False, "error": f"host non consentito: {host}"}
        scheme = validate.http_scheme(call.data.get("scheme"), "http")
        url = f"{scheme}://{host}/{call.data['path'].lstrip('/')}"

        # `save_as` finiva in hass.config.path() cosi' com'era: un "../"
        # scriveva ovunque sotto l'utente di Home Assistant. Ora e' solo un nome
        # di file, e il file nasce in una sottocartella dedicata.
        raw_name = call.data.get("save_as")
        save_as = validate.safe_filename(raw_name) if raw_name else None
        if raw_name and not save_as:
            _LOGGER.error("fetch_local: save_as %r rifiutato (nome file non valido)", raw_name)
            return {"ok": False, "error": f"nome file non valido: {raw_name}"}
        out_dir = hass.config.path(DOMAIN)

        def _do():
            r = _rq.get(url, auth=HTTPDigestAuth(runtime.SIP_USER, runtime.SIP_PASSWORD),
                        timeout=15, headers={"User-Agent": "TOGA/2.4.0"},
                        allow_redirects=False)
            data = r.content
            saved = None
            if save_as:
                os.makedirs(out_dir, exist_ok=True)
                dst = os.path.join(out_dir, save_as)
                with open(dst, "wb") as f:
                    f.write(data)
                saved = dst
            return r.status_code, dict(r.headers), data, saved

        try:
            code, hdrs, data, saved = await hass.async_add_executor_job(_do)
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("fetch_local %s failed: %s", url, e)
            return {"ok": False, "url": url, "error": str(e)}
        preview = data[:600].decode("utf-8", errors="replace")
        _LOGGER.info("fetch_local %s → %s (%d bytes) saved=%s", url, code, len(data), saved)
        return {"ok": 200 <= code < 300, "url": url, "status": code,
                "content_type": hdrs.get("Content-Type"), "size": len(data),
                "saved": saved, "preview": preview}

    async def _svc_open_door(call: ServiceCall):
        hub = _get_hub_from_hass(hass)
        ok, msg = await hub.async_door(
            target=call.data.get("target"), command=call.data.get("command"))
        return {"ok": ok, "result": msg}

    # Comandi arbitrari all'impianto e richieste con la password SIP: solo
    # per gli amministratori. Le automazioni (senza utente) passano comunque.
    async_register_admin_service(
        hass, DOMAIN, SERVICE_SEND_COMMAND, _svc_send_command,
        schema=SEND_COMMAND_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(
        DOMAIN, SERVICE_CALL, _svc_call,
        schema=CALL_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(
        DOMAIN, SERVICE_ANSWER, _svc_answer,
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(
        DOMAIN, SERVICE_HANGUP, _svc_hangup,
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(
        DOMAIN, SERVICE_OPEN_DOOR, _svc_open_door,
        schema=OPEN_DOOR_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    async_register_admin_service(
        hass, DOMAIN, SERVICE_FETCH_LOCAL, _svc_fetch_local,
        schema=FETCH_LOCAL_SCHEMA, supports_response=SupportsResponse.OPTIONAL)


class VimarAudioWSView(HomeAssistantView):
    """WebSocket endpoint for bidirectional audio + intercom control.

    Binary messages:
      Server → Client: 0x01 + PCM16LE (intercom audio, 8kHz mono)
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

    def __init__(self, hass: HomeAssistant, entry_id: str):
        self._hass = hass
        self._entry_id = entry_id

    @property
    def _hub(self) -> "VimarIntercomHub | None":
        """Risolve l'hub dalla entry attiva (sicuro con reload)."""
        return self._hass.data.get(DOMAIN, {}).get(self._entry_id, {}).get("hub")

    @property
    def _ws_clients(self) -> "set[web.WebSocketResponse]":
        """Risolve il set di WS client attivi dalla entry attiva."""
        return self._hass.data.get(DOMAIN, {}).get(self._entry_id, {}).get("audio_ws_clients", set())

    async def _broadcast(self, msg: dict) -> None:
        """Manda un messaggio JSON a tutti i WS client attivi."""
        text = json.dumps(msg)
        clients = self._ws_clients
        dead = set()
        for ws in clients:
            try:
                await ws.send_str(text)
            except Exception:
                dead.add(ws)
        clients.difference_update(dead)

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hub = self._hub
        if hub is None:
            await ws.close(code=1011, message=b"Integration not loaded")
            return ws
        clients = self._ws_clients
        clients.add(ws)
        _LOGGER.info("Audio WS client connected (%d total)", len(clients))

        # Send initial state
        await ws.send_str(json.dumps({
            "type": "state",
            "registered": hub.registered,
            "in_call": hub.in_call,
        }))

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._handle_text(ws, msg.data, request.get("hass_user"))
                elif msg.type == web.WSMsgType.BINARY:
                    # Client sending mic audio: 0x02 prefix + PCM16LE
                    if len(msg.data) > 1 and msg.data[0] == 0x02 and hub.in_call:
                        media.send_audio(msg.data[1:])
                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        except Exception as e:
            _LOGGER.error("Audio WS error: %s", e)
        finally:
            clients.discard(ws)
            _LOGGER.info("Audio WS client disconnected (%d remaining)", len(clients))

        return ws

    # Azioni da manutenzione: comandi SIP arbitrari, scansioni, registrazioni.
    # Chiamare, rispondere, riagganciare e aprire restano a tutti gli utenti:
    # è quello che fa la scheda del citofono per chiunque in casa.
    _ADMIN_ACTIONS = frozenset({"command", "register", "probe", "scan", "reconnect"})

    async def _handle_text(self, ws: web.WebSocketResponse, text: str, user=None):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return

        action = data.get("action")
        _LOGGER.info("WS action received: %s (data=%s)", action, data)
        hub = self._hub
        if hub is None:
            await ws.send_str(json.dumps({"type": "error", "msg": "Integration not loaded"}))
            return
        if action in self._ADMIN_ACTIONS and not (user and user.is_admin):
            await ws.send_str(json.dumps(
                {"type": "error", "msg": f"'{action}' richiede un amministratore"}))
            return

        if action == "status":
            await ws.send_str(json.dumps({
                "type": "state",
                "registered": hub.registered,
                "in_call": hub.in_call,
            }))

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
                    # Suppress broadcast during switch — do hangup silently
                    sip._suppress_broadcast = True
                    await hub.async_hangup()
                    sip._suppress_broadcast = False
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
                    sip._suppress_broadcast = False
                    await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "door":
            target = data.get("target")  # "55001" (esterno) or "55002" (interno)
            _LOGGER.info("Door action: target=%s", target)
            try:
                ok, m = await hub.async_door(target=target)
                t = "door" if ok else "error"
                await self._broadcast({"type": t, "msg": m})
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "command":
            # Comando SIP MESSAGE arbitrario: {"action":"command","body":"...","target":"55001"}
            try:
                ok, m = await hub.async_send_command(
                    body=data.get("body", ""),
                    target=data.get("target") or runtime.SGA_TARGET,
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
                await hub.async_decline()
                await self._broadcast({"type": "ring_ended", "msg": "Declined",
                                       "registered": hub.registered, "in_call": False})
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "reconnect":
            _LOGGER.info("Force reconnect requested via WS")
            try:
                ok = await sip.reconnect()
                await ws.send_str(json.dumps({
                    "type": "state",
                    "registered": hub.registered,
                    "in_call": hub.in_call,
                    "msg": "Reconnected" if ok else "Reconnect failed",
                }))
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
        text = "\n".join(list(_debug_log)[-n:])
        return web.Response(text=text, content_type="text/plain")


class VimarAVStreamView(HomeAssistantView):
    """Serve MPEG-TS stream (H264 video + PCMU audio) at /api/vimar_intercom/av."""

    url = "/api/vimar_intercom/av"
    name = "api:vimar_intercom:av"
    # Autenticato: l'ffmpeg di Home Assistant arriva con un URL firmato (vedi
    # camera.prepare_homekit_source). Prima bastava un indirizzo di rete
    # locale, e da un port-forward con SNAT, un tunnel sullo stesso host o un
    # qualunque dispositivo in LAN si faceva partire una chiamata e si
    # guardava la strada.
    requires_auth = True

    def __init__(self, hass: HomeAssistant, entry_id: str):
        self._hass = hass
        self._entry_id = entry_id

    async def get(self, request: web.Request) -> web.StreamResponse:
        if not _is_local_request(request):
            return web.Response(status=403, text="Forbidden (local network only)")
        hub = self._hass.data.get(DOMAIN, {}).get(self._entry_id, {}).get("hub")
        if hub is None:
            return web.Response(status=503, text="Integration not loaded")
        _LOGGER.info("AV stream requested — triggering auto-call")
        await hub.stream_opened()
        # Da qui in poi ogni uscita deve passare da stream_closed(), altrimenti
        # il contatore degli spettatori non torna a zero e la chiamata resta
        # aperta fino al limite di sicurezza.
        try:
            # Attesa a grana fine: con passi da mezzo secondo si buttavano via
            # fino a 450 ms su una chiamata che era già pronta.
            waited = 0.0
            while not hub.in_call and waited < 15:
                await asyncio.sleep(0.02)
                waited += 0.02

            if not hub.in_call:
                _LOGGER.warning("AV stream: call not established after 15s")
                return web.Response(status=503, text="Call not established")

            # Ci si iscrive PRIMA di avviare ffmpeg: così non si perdono i
            # primi pacchetti (PAT/PMT e, se capita, il primo IDR) e non esiste
            # la finestra in cui un altro client che esce spegne tutto.
            queue = media.subscribe_av()
            try:
                return await self._serve(request, hub, queue)
            finally:
                await media.release_av(queue)
        finally:
            await hub.stream_closed()

    async def _serve(self, request, hub, queue) -> web.StreamResponse:
        # Prima di avviare ffmpeg: l'INFO parte subito e SPS/PPS arrivano
        # mentre ffmpeg si sta ancora alzando, così il video è già dichiarabile
        # al primo avvio e non serve riavviarlo.
        keyframe = asyncio.create_task(hub.request_keyframe())
        await media.start_av_ffmpeg()
        if not media.av_ffmpeg_proc:
            return web.Response(status=503, text="ffmpeg failed to start")

        del keyframe  # già in volo: la si lascia finire per conto suo

        response = web.StreamResponse()
        response.content_type = "video/mp2t"
        await response.prepare(request)

        # Lo stdout di ffmpeg viene letto una volta sola e distribuito: due
        # client che leggessero la pipe si spartirebbero i byte e nessuno dei
        # due riceverebbe un flusso decodificabile.
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    # Nessun byte per 10 s: se la chiamata è finita si chiude,
                    # altrimenti si aspetta ancora invece di appendere il
                    # client a una coda che nessuno alimenterà più.
                    if not hub.in_call:
                        break
                    continue
                if not chunk:
                    break
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response
