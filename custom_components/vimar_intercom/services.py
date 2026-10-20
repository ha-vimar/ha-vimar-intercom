"""Services vimar_intercom.*, moved out of __init__.py as is."""

import logging
import os

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.service import async_register_admin_service

from . import runtime, validate
from .const import DOMAIN
from .views import _entry_data

_LOGGER = logging.getLogger(__name__)

# ─── Servizi ──────────────────────────────────────────────────────────────────
SERVICE_SEND_COMMAND = "send_command"
SERVICE_CALL = "call"
SERVICE_ANSWER = "answer"
SERVICE_DECLINE = "decline"
SERVICE_HANGUP = "hangup"
SERVICE_OPEN_DOOR = "open_door"
SERVICE_FETCH_LOCAL = "fetch_local"
SERVICE_SIMULATE_RING = "simulate_ring"
SERVICE_FIND_SGA = "find_sga"


def _sip_id(value) -> str:
    """Id SIP numerico, o errore leggibile nel servizio (non un 500)."""
    if (target := validate.sip_target(value)) is None:
        raise vol.Invalid(f"invalid SIP id: {value!r} (it must be a number, e.g. 55001)")
    return target


SEND_COMMAND_SCHEMA = vol.Schema({
    vol.Required("body"): cv.string,
    vol.Optional("target"): cv.string,  # vuoto: l'SGA, in hub.async_send_command
    vol.Optional("header_name", default="Panda"): cv.string,
    vol.Optional("header_value", default="command"): cv.string,
})
CALL_SCHEMA = vol.Schema({vol.Optional("target"): _sip_id})
FETCH_LOCAL_SCHEMA = vol.Schema({
    vol.Required("path"): cv.string,                    # es. rest/get_info.php?action=status
    vol.Optional("save_as"): cv.string,                 # nome file in /config (opzionale)
    vol.Optional("host"): cv.string,                    # default: local_proxy
    vol.Optional("scheme", default="http"): cv.string,
})
# Nessun default per target: senza, hub.async_door usa runtime.DOOR_TARGET (la
# targa che apre la porta, dalla rubrica). Con l'SGA come default, su un 2FV2
# il comando andava al 61000, che risponde 200 e non apre.
FIND_SGA_SCHEMA = vol.Schema({
    vol.Optional("start", default="55000"): cv.string,
    vol.Optional("end", default="55010"): cv.string,
    vol.Optional("targets"): cv.string,
    vol.Optional("delay", default=1.0): vol.All(vol.Coerce(float), vol.Range(min=0.2, max=10)),
    vol.Optional("reply_wait", default=3.0): vol.All(vol.Coerce(float), vol.Range(min=1, max=15)),
    vol.Optional("probe", default="get_nicks"): vol.In(["get_nicks", "get_init_status"]),
    vol.Optional("sip_timeout", default=8.0): vol.All(vol.Coerce(float), vol.Range(min=2, max=30)),
    vol.Optional("apply", default=False): cv.boolean,
    vol.Optional("apply_sga", default=False): cv.boolean,
})
# Test ring length: the default lets a card or dashboard be looked at, the
# maximum is a real ring's (sip_client.RING_MAX_S).
SIMULATE_RING_SCHEMA = vol.Schema({
    vol.Optional("duration", default=20): vol.All(vol.Coerce(float), vol.Range(min=1, max=90)),
})
OPEN_DOOR_SCHEMA = vol.Schema({
    vol.Optional("target"): _sip_id,  # vuoto: runtime.DOOR_TARGET (targa dell'attuatore porta, altrimenti SGA)
    # Solo comandi di apertura (OPEN, OPEN_2F, ...): il servizio è aperto a ogni
    # utente, i MESSAGE liberi no.
    # Senza command: il corpo dell'attuatore porta di quella targa nella
    # rubrica, altrimenti OPEN_2F (#58).
    vol.Optional("command"): vol.All(cv.string, vol.Match(r"^OPEN(_[A-Z0-9_]{1,16})?\Z")),
})



def _register_services(hass: HomeAssistant) -> None:
    """Registra i servizi vimar_intercom.* (una sola volta)."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_COMMAND):
        return

    async def _svc_send_command(call: ServiceCall):
        # MESSAGE arbitrari verso qualunque target: roba da amministratore
        # (async_register_admin_service: le automazioni passano, un utente non admin no).
        hub = _entry_data(hass)["hub"]
        ok, msg = await hub.async_send_command(
            body=call.data["body"],
            target=call.data.get("target"),
            header_name=call.data.get("header_name") or None,
            header_value=call.data.get("header_value") or None,
        )
        _LOGGER.info("Service send_command → ok=%s msg=%s", ok, msg)
        return {"ok": ok, "result": msg}

    async def _svc_call(call: ServiceCall):
        hub = _entry_data(hass)["hub"]
        ok, msg = await hub.async_call(target=call.data.get("target"))
        return {"ok": ok, "result": msg}

    async def _svc_answer(call: ServiceCall):
        hub = _entry_data(hass)["hub"]
        ok, msg = await hub.async_answer()
        return {"ok": ok, "result": msg}

    async def _svc_decline(call: ServiceCall):
        hub = _entry_data(hass)["hub"]
        ok, msg = await hub.async_decline()
        return {"ok": ok, "result": msg}

    async def _svc_hangup(call: ServiceCall):
        hub = _entry_data(hass)["hub"]
        await hub.async_hangup()
        return {"ok": True, "result": "Chiamata terminata"}

    async def _svc_simulate_ring(call: ServiceCall):
        ok = _entry_data(hass)["hub"].simulate_ring(call.data["duration"])
        return {"ok": ok, "result": "Squillo di prova" if ok else "Squillo o chiamata in corso"}

    async def _svc_find_sga(call: ServiceCall):
        """Cerca il PICG interrogando gli indirizzi indicati (issue #14).

        Non scrive nulla nella configurazione, salvo `apply: true` e un PICG
        trovato; `apply_sga: true` aggiorna anche sga_target (sugli impianti visti
        finora i due coincidono, ma sono due valori distinti). Solo admin: manda
        MESSAGE a una serie di indirizzi e può cambiare la configurazione.
        """
        try:
            targets = validate.scan_targets(
                call.data.get("start"), call.data.get("end"), call.data.get("targets"))
        except ValueError as err:
            return {"ok": False, "error": str(err)}
        hub = _entry_data(hass).get("hub")
        if hub is None:
            return {"ok": False, "error": "Integration not loaded"}
        result = await hub.async_find_picg(
            targets,
            probe=call.data["probe"].upper(),
            reply_wait=call.data["reply_wait"],
            delay=call.data["delay"],
            sip_timeout=call.data["sip_timeout"],
        )
        result["scanned"] = len(result.get("probes") or [])
        result["applied"] = {}
        picg = result.get("picg")
        if result.get("ok") and picg and (call.data["apply"] or call.data["apply_sga"]):
            if not validate.sip_target(picg):
                result["error"] = f"PICG dichiarato non valido: {picg!r}"
                return result
            entry = next(iter(hass.config_entries.async_entries(DOMAIN)), None)
            if entry is not None:
                changes = {}
                if call.data["apply"]:
                    changes["picg_target"] = picg
                if call.data["apply_sga"]:
                    changes["sga_target"] = picg
                result["applied"] = changes
                _LOGGER.warning("find_sga: configurazione aggiornata %s", changes)
                # L'update listener ricarica l'entry: runtime riparte coi valori nuovi.
                hass.config_entries.async_update_entry(
                    entry, options={**entry.options, **changes})
        return result

    async def _svc_fetch_local(call: ServiceCall):
        """GET HTTP (Digest sipID/password) verso l'interfaccia locale del citofono.

        Replica ciò che fa l'app in "home mode": /rest/get_info.php?action=status|nickname,
        /rest/get_file.php?name=rubrica|mailbox. Restituisce stato+anteprima e può salvare il file.
        """
        # Porta la password SIP in Digest verso un host della LAN: solo admin
        # (async_register_admin_service).
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
        hub = _entry_data(hass)["hub"]
        ok, result, code = await hub.async_door(
            target=call.data.get("target"), command=call.data.get("command"))
        return {"ok": ok, "result": result, "code": code}

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
        DOMAIN, SERVICE_DECLINE, _svc_decline,
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
    async_register_admin_service(
        hass, DOMAIN, SERVICE_SIMULATE_RING, _svc_simulate_ring,
        schema=SIMULATE_RING_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    async_register_admin_service(
        hass, DOMAIN, SERVICE_FIND_SGA, _svc_find_sga,
        schema=FIND_SGA_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
