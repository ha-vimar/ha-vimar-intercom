"""Vimar Intercom integration for Home Assistant."""

import asyncio
import json
import logging
import time
from pathlib import Path

from aiohttp import web
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.requirements import async_process_requirements

from . import av_passive, away_config, away_tts, runtime, webhook
from . import log_buffer as _log_buffer
from . import media_handler as media
from .const import CONF_HOMEKIT_ACCESSORY, DEFAULT_HOMEKIT_ACCESSORY, DOMAIN, HOMEKIT_REQUIREMENTS
from .hub import VimarIntercomHub
from .services import (
    SERVICE_ANSWER,
    SERVICE_CALL,
    SERVICE_DECLINE,
    SERVICE_FETCH_LOCAL,
    SERVICE_FIND_SGA,
    SERVICE_HANGUP,
    SERVICE_OPEN_DOOR,
    SERVICE_SEND_COMMAND,
    SERVICE_SIMULATE_RING,
    _register_services,
)
from .services import _sip_id as _sip_id  # re-exported: the tests reach it from the package
from .views import (
    VimarAudioWSView,
    VimarAVStreamView,
    VimarAwayUploadView,
    VimarDebugView,
    VimarRingPhotoView,
    VimarRingsView,
)
from .views import _entry_data as _entry_data  # re-exported: the tests reach it from the package
from .views import _is_local_request as _is_local_request  # re-exported, as above

_LOGGER = logging.getLogger(__name__)

# Per-client WebSocket queue: ~2.5 s of voice and video (50 audio + ~50 NAL a second).
WS_CLIENT_QUEUE = 256

# Buffer interno dei log e inoltro al log di HA: vedi log_buffer.py.
_log_buffer.install()

PLATFORMS = ["camera", "lock", "button", "event", "binary_sensor", "sensor", "switch", "select",
             "text"]


CARD_URL = "/vimar_intercom/vimar-intercom-card.js"
CARD_DIR = CARD_URL.rpartition("/")[0]


async def _register_card(hass: HomeAssistant) -> None:
    """Card del citofono (www/vimar-intercom-card.js): servita e caricata dal
    frontend da sola, una volta sola anche dopo un reload dell'entry."""
    if hass.data.get(f"{DOMAIN}_card"):
        return
    www = Path(__file__).parent / "www"
    # The card is ES modules importing each other ("./nal-player.js"), so www/ is served as
    # a folder. A ?v= on the entry would not reach those imports, so the version is a path
    # segment instead: /vimar_intercom/<v>/... changes whenever any card file changes, and
    # browsers and the app fetch every module again. The plain folder keeps CARD_URL valid.
    v = int(await hass.async_add_executor_job(
        lambda: max(f.stat().st_mtime for f in www.glob("*.js"))))
    await hass.http.async_register_static_paths([
        StaticPathConfig(CARD_DIR, str(www), False),
        StaticPathConfig(f"{CARD_DIR}/{v}", str(www), False),
    ])
    add_extra_js_url(hass, f"{CARD_DIR}/{v}/vimar-intercom-card.js")
    hass.data[f"{DOMAIN}_card"] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vimar Intercom from a config entry."""
    # Identità dispositivo: una per installazione, generata al primo avvio e
    # salvata nell'entry. Fino alla 1.0.1 era una costante in const.py uguale per
    # tutti: sul cloud Vimar la registrazione (e le push) sono associate
    # all'identità, quindi due impianti con lo stesso valore si scalzano a
    # vicenda. Gli entry già esistenti vengono migrati qui, in silenzio.
    if (identity := _missing_identity(entry.data)) is not None:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, **identity})
        _LOGGER.info("Identità dispositivo generata per questa installazione")
    # The /av key (#63), once per installation like the identity. Never logged.
    if (key := _missing_av_key(entry.data)) is not None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **key})
        _LOGGER.info("Generated the /av key for this installation")

    # Popola il modulo runtime con i dati del config entry.
    # Le options (impostazioni rete modificate da OptionsFlow) sovrascrivono
    # i valori di default presenti in entry.data.
    runtime.configure({**entry.data, **entry.options})
    _LOGGER.info(
        "Runtime configurato: user=%s domain=%s local_proxy=%s udp=%s",
        runtime.SIP_USER, runtime.SIP_DOMAIN,
        runtime.LOCAL_PROXY, entry.data.get("use_local_udp", True),
    )

    away_tts.setup(hass)  # sintetizza il messaggio di assenza da testo, se configurato
    webhook.setup(hass)   # GET a inizio/fine squillo, se configurato

    hub = VimarIntercomHub()

    @callback
    def _persist_learned(updates: dict) -> None:
        """Values learned from the plant go in the entry data, not the
        options, so saving them does not reload the integration."""
        if (data := _learned_data(entry.data, updates)) is not None:
            hass.config_entries.async_update_entry(entry, data=data)

    hub.set_persist_callback(_persist_learned)

    # Insieme di WS audio attivi: vive in hass.data per evitare globals
    # a livello di modulo (sicuro con reload e multi-entry).
    audio_ws_clients: set[web.WebSocketResponse] = set()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"hub": hub, "audio_ws_clients": audio_ws_clients,
                                         "applied": dict(entry.options)}
    # Il messaggio di assenza si è unito a Segreteria: via l'entità separata rimasta orfana.
    reg = er.async_get(hass)
    if orphan := reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_away_message"):
        reg.async_remove(orphan)

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
        # Ultimi SPS/PPS della targa (pochi byte): .storage/vimar_intercom.<entry>.sps_pps
        await hub.async_start(Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.sps_pps"))
    except OSError as e:
        # Cloud irraggiungibile (dopo un blackout HA riparte prima del router):
        # HA riprova da solo. Un'eccezione qualsiasi lasciava l'entry in errore
        # fino al reload a mano, e le porte RTP occupate dal tentativo fallito.
        await hub.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise ConfigEntryNotReady(f"Proxy SIP non raggiungibile: {e}") from e

    # Closures locali: catturano audio_ws_clients (nessun global di modulo).
    # One small queue and one sender task per client: the audio and video loops only
    # enqueue, so a slow phone loses its own frames and nobody else waits for it.
    senders: dict[web.WebSocketResponse, tuple[asyncio.Queue, asyncio.Task]] = {}
    resync: set[web.WebSocketResponse] = set()  # backlog dropped: no video until a keyframe
    # ?only=audio|video (views.py): the one media type a socket wants, b"\x01" or b"\x03".
    wants: dict[web.WebSocketResponse, bytes] = {}
    # ?ts=1 (views.py): a {"type": "video_ts"} text before each IDR, so a client behind
    # a buffering proxy (send_bytes returns at once, the backlog grows there) can tell
    # it is late. Opt-in: an older client may not expect a new text message type.
    ts_clients: set[web.WebSocketResponse] = set()
    hass.data[DOMAIN][entry.entry_id]["ws_senders"] = senders
    hass.data[DOMAIN][entry.entry_id]["ws_wants"] = wants
    hass.data[DOMAIN][entry.entry_id]["ws_ts"] = ts_clients

    async def _drain(ws, queue: asyncio.Queue):
        try:
            while True:
                msg = await queue.get()
                await (ws.send_str(msg) if isinstance(msg, str) else ws.send_bytes(msg))
        except Exception:  # noqa: BLE001 - a failing send drops the client, as before
            audio_ws_clients.discard(ws)

    async def _ws_send_bytes(data: bytes, only=None):
        # Clients gone since the last packet: stop their sender.
        for ws in [ws for ws in senders if ws not in audio_ws_clients]:
            senders.pop(ws)[1].cancel()
            resync.discard(ws)
        for ws in audio_ws_clients:
            if (only is not None and ws is not only) or wants.get(ws, data[:1]) != data[:1]:
                continue
            if ws not in senders:
                queue = asyncio.Queue(maxsize=WS_CLIENT_QUEUE)
                senders[ws] = (queue, asyncio.create_task(_drain(ws, queue)))
            queue = senders[ws][0]
            if queue.full():
                # Too slow: drop its whole backlog. Half a GOP would only smear until
                # the next IDR, so its video restarts at the next SPS (sent before
                # every IDR) and the voice catches up instead of lagging.
                while not queue.empty():
                    queue.get_nowait()
                resync.add(ws)
            if ws in resync and data[:1] == b"\x03":
                if data[5] & 0x1F != 7:
                    continue
                resync.discard(ws)
            # Live IDRs only: a replayed one is old, and would set the card's baseline early.
            # Monotonic: the card compares intervals, a clock step must not look like lag.
            if (only is None and ws in ts_clients and data[:1] == b"\x03" and data[5] & 0x1F == 5
                    and queue.qsize() < WS_CLIENT_QUEUE - 1):
                queue.put_nowait(json.dumps({"type": "video_ts", "t": int(time.monotonic() * 1000)}))
            queue.put_nowait(data)

    async def _broadcast(data: dict):
        text = json.dumps(data)
        dead = set()
        for ws in list(audio_ws_clients):
            try:
                await ws.send_str(text)
            except Exception:
                dead.add(ws)
        audio_ws_clients.difference_update(dead)

    # Wire up audio broadcast to WebSocket clients
    media.ws_send_bytes = _ws_send_bytes
    hub.set_ws_broadcast(_broadcast)
    hub._has_ws_clients = lambda: len(audio_ws_clients) > 0

    _register_services(hass)

    hass.http.register_view(VimarAVStreamView(hass))
    hass.http.register_view(VimarAudioWSView(hass))
    await _register_card(hass)
    hass.http.register_view(VimarDebugView())
    hass.http.register_view(VimarRingsView(hass))
    hass.http.register_view(VimarRingPhotoView(hass))
    hass.http.register_view(VimarAwayUploadView(hass))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The HomeKit video doorbell, published by the integration itself (Home
    # Assistant's HomeKit bridge cannot carry the talk direction). If it fails,
    # the rest of the integration keeps working.
    if entry.options.get(CONF_HOMEKIT_ACCESSORY, DEFAULT_HOMEKIT_ACCESSORY):
        try:
            # Installed only when HomeKit is on: in the manifest they were
            # installed for everyone, and a failed install (or a clash with
            # the core HomeKit integration's pin) stopped the whole
            # integration from loading, not just HomeKit.
            await async_process_requirements(hass, f"{DOMAIN}.homekit", HOMEKIT_REQUIREMENTS)
            from .homekit_accessory import async_setup_homekit  # noqa: PLC0415

            hass.data[DOMAIN][entry.entry_id]["homekit_stop"] = (
                await async_setup_homekit(hass, entry, hub))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("HomeKit video doorbell not started")

    # Ricarica l'entry quando cambiano le options (es. lista attuatori):
    # così i bottoni dinamici vengono ricreati con la nuova configurazione.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when the options are saved, except when:

    - nothing in the options changed: the listener fires on every change of
      the entry, including the integration saving the detected model or a
      learned panel into the entry data, and that used to reload everything,
      in the middle of a call too;
    - only the away message keys changed: those apply in memory.
    """
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data is not None and not _options_changed(data.get("applied"), entry.options):
        return
    if data is not None and away_config.apply_options(data, entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _missing_identity(data) -> dict | None:
    """The device identity to add to an entry that lacks one, or None.

    Entries from the m4r1k fork already have an identity under one name
    ("device_id", used for both headers): changing it would break the
    pairing, so it is kept.
    """
    if data.get("device_imei") and data.get("device_uuid"):
        return None
    legacy = str(data.get("device_id") or "").strip()
    if legacy:
        return {"device_imei": legacy, "device_uuid": legacy}
    return runtime.new_device_identity()


def _missing_av_key(data) -> dict | None:
    """The /av key to add to an entry that lacks one, or None."""
    if str(data.get("av_key") or "").strip():
        return None
    return {"av_key": runtime.new_av_key()}


def _learned_data(data, updates: dict) -> dict | None:
    """The entry data with the learned values, or None when nothing changes."""
    if all(data.get(k) == v for k, v in updates.items()):
        return None
    return {**data, **updates}


def _options_changed(started_with, options) -> bool:
    """True when the options differ from those the entry started with."""
    return started_with != dict(options)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        # HomeKit first: it frees its port, which the reloaded entry wants back.
        if stop := data.get("homekit_stop"):
            try:
                await stop()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Stopping the HomeKit video doorbell")
        # Chiudi tutti i WS audio attivi prima di fermare l'hub:
        # evita che le views (ancora registrate in HA) usino il vecchio hub
        # e che i client rimangano connessi a un hub non più valido.
        for _queue, task in data.get("ws_senders", {}).values():
            task.cancel()
        for ws in list(data.get("audio_ws_clients", set())):
            await ws.close()
        await data["hub"].async_stop()
        if not hass.data[DOMAIN]:
            await av_passive.stop()
            for svc in (SERVICE_SEND_COMMAND, SERVICE_CALL, SERVICE_ANSWER,
                        SERVICE_DECLINE, SERVICE_HANGUP, SERVICE_OPEN_DOOR, SERVICE_FETCH_LOCAL,
                        SERVICE_SIMULATE_RING, SERVICE_FIND_SGA):
                hass.services.async_remove(DOMAIN, svc)
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The entry is deleted: its HomeKit pairing and setup code go with it."""
    try:
        from .homekit_files import remove_homekit_files  # noqa: PLC0415

        await hass.async_add_executor_job(remove_homekit_files, hass, entry.entry_id)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Removing the HomeKit pairing files")
