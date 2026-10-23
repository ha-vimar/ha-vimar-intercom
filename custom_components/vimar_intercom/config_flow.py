"""Config flow per Vimar Intercom — onboarding via QR o credenziali manuali."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import secrets
import socket
import ssl
import time
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from . import discovery, profiles, qr_decoder, runtime
from .const import (
    CA_PATH,
    DEFAULT_CLOUD_PROXY,
    DOMAIN,
    LOCAL_SIP_PORT,
    MY_NAME,
    USER_AGENT,
)
from .const import (
    SIP_PORT as CLOUD_SIP_PORT,
)
from .sip_client import _resolve_sip_targets
from .sip_message import _challenge_params

if TYPE_CHECKING:
    from .options_flow import OptionsFlowHandler

_LOGGER = logging.getLogger(__name__)

# ─── Chiavi config entry ─────────────────────────────────────────────────────
KEY_SIP_USER      = "sip_user"
KEY_SIP_PASSWORD  = "sip_password"
KEY_SIP_DOMAIN    = "sip_domain"
KEY_SIP_HA1       = "sip_ha1"
KEY_CLOUD_PROXY   = "cloud_proxy"
KEY_LOCAL_PROXY   = "local_proxy"
KEY_GID           = "gid"
KEY_PLANT_TYPE    = "plant_type"
KEY_MAC           = "mac"
KEY_LOCAL_DOMAIN  = "local_domain"
KEY_CLOUD_DOMAIN  = "cloud_domain"
KEY_USE_LOCAL_UDP  = "use_local_udp"
KEY_LOCAL_UDP_PORT = "local_udp_port"
KEY_ACTUATORS      = "actuators"
KEY_MEDIA_ENC      = "media_enc"
KEY_VOICE_ANSWER   = "voice_answer"
KEY_SGA_TARGET     = "sga_target"
# Why the entry uses another transport than the one asked for (setup fallback).
KEY_SETUP_NOTE     = "setup_note"
KEY_PICG_TARGET    = "picg_target"
KEY_CAMERA_TARGET  = "camera_target"
KEY_INTERNAL_PANEL_TARGET = "internal_panel_target"
KEY_DOOR_TARGET    = "door_target"
KEY_DOOR_OPEN_CURRENT = "door_open_current"
KEY_AWAY_FILE      = "away_message_file"
KEY_AWAY_TEXT      = "away_message_text"
KEY_AWAY_TTS       = "away_message_tts"
KEY_AWAY_DELAY     = "away_message_delay"
KEY_SNAP_DIR       = "snapshot_dir"
KEY_SNAP_DELAY     = "snapshot_delay"
KEY_VIEW_KA        = "view_keepalive"
KEY_ALLOWED_USERS  = "allowed_users"
KEY_RING_WEBHOOK_URL     = "ring_webhook_url"
KEY_RING_END_WEBHOOK_URL = "ring_end_webhook_url"

DEFAULT_LOCAL_SIP_PORT = LOCAL_SIP_PORT
DEFAULT_LOCAL_UDP_PORT = 5060

# ─── Validazione ─────────────────────────────────────────────────────────────

def _valid_device_name(name: str) -> bool:
    """The device name goes into the MyName header: 1 to 64 printable
    characters, no control characters (a line break would split the header)."""
    return 1 <= len(name) <= 64 and name.isprintable()


async def _test_sip_registration(
    sip_user: str,
    sip_password: str,
    sip_domain: str,
    local_proxy: str,
    local_udp_port: int = DEFAULT_LOCAL_UDP_PORT,
    device_imei: str = "",
    device_uuid: str = "",
    device_name: str = MY_NAME,
    timeout: float = 8.0,
    unregister: bool = False,
) -> tuple[bool, str]:
    """Register over local UDP; returns (success, message).

    The REGISTER carries the same identity the integration will use (MyName,
    Mobile-IMEI, +sip.instance): the intercom binds a pairing to that identity
    and refuses a different one with 503, so a test without it proves nothing.

    unregister: once the test has passed, remove its binding (Expires: 0, same
    Contact). With the integration running, the test registration takes over
    the running one's binding (same +sip.instance) and points it at this
    socket, closed right after: rings would go nowhere until the next renewal.
    """

    def _run() -> tuple[bool, str]:
        call_id  = secrets.token_hex(8)
        from_tag = secrets.token_hex(4)
        uri      = f"sip:{sip_domain}"
        target   = (local_proxy, DEFAULT_LOCAL_SIP_PORT)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            # The configured port, as the integration will use it; when it is
            # taken (the integration itself is running) any port will do.
            try:
                sock.bind(("0.0.0.0", local_udp_port))
            except OSError:
                sock.bind(("0.0.0.0", 0))
            sock.connect(target)
            my_ip, my_port = sock.getsockname()[:2]

            def make_register(auth_hdr: str | None = None, seq: int = 1,
                              expires: int = 60) -> bytes:
                contact = f"<sip:{sip_user}@{my_ip}:{my_port}>"
                if device_uuid:
                    contact += f';+sip.instance="<urn:uuid:{device_uuid}>"'
                lines = [
                    f"REGISTER {uri} SIP/2.0",
                    f"Via: SIP/2.0/UDP {my_ip}:{my_port};branch=z9hG4bK{secrets.token_hex(4)};rport",
                    "Max-Forwards: 70",
                    f"To: <sip:{sip_user}@{sip_domain}>",
                    f"From: <sip:{sip_user}@{sip_domain}>;tag={from_tag}",
                    f"Call-ID: {call_id}",
                    f"CSeq: {seq} REGISTER",
                    f"Contact: {contact}",
                    f"Expires: {expires}",
                    f"User-Agent: {USER_AGENT}",
                    f"MyName: {device_name}",
                ]
                if device_imei:
                    lines.append(f"Mobile-IMEI: {device_imei}")
                if auth_hdr:
                    lines.append(f"Authorization: {auth_hdr}")
                lines += ["Content-Length: 0", "", ""]
                return "\r\n".join(lines).encode()

            def drop_binding(auth_hdr: str | None, seq: int) -> None:
                """Best effort: the test result stands whatever the answer."""
                if not unregister:
                    return
                try:
                    sock.settimeout(min(timeout, 2.0))
                    sock.send(make_register(auth_hdr=auth_hdr, seq=seq, expires=0))
                    read_final()
                except OSError as exc:  # socket.timeout included
                    _LOGGER.debug("SIP test: unregister not confirmed (%s)", exc)

            def read_final() -> str:
                """Skip provisional responses (100 Trying), return the final one."""
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    message = sock.recv(65535).decode(errors="replace")
                    parts = message.split("\r\n", 1)[0].split(" ")
                    if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) >= 200:
                        return message
                raise TimeoutError

            def refused(first: str) -> str:
                if " 503" in first:
                    return (f"{first}: the intercom refuses this identity. It is "
                            "paired to another device name or identifier, or this "
                            "plant does not accept local UDP registrations.")
                return f"Unexpected response: {first}"

            sock.send(make_register(seq=1))
            response = read_final()
            first = response.split("\r\n", 1)[0]
            if " 200" in first:
                drop_binding(None, 2)
                return True, "Registration succeeded (no auth)"
            if " 401" not in first and " 407" not in first:
                return False, refused(first)

            challenge = _parse_challenge(response)
            if not challenge.get("nonce") or not challenge.get("realm"):
                return False, "Challenge without nonce/realm"
            auth = _digest_header(
                user=sip_user, password=sip_password,
                realm=challenge["realm"], nonce=challenge["nonce"], uri=uri,
                qop=challenge.get("qop", ""), opaque=challenge.get("opaque", ""),
                cnonce=secrets.token_hex(4),
            )
            sock.send(make_register(auth_hdr=auth, seq=2))
            first2 = read_final().split("\r\n", 1)[0]
            if " 200" in first2:
                drop_binding(auth, 3)
                return True, "Registration succeeded"
            return False, refused(first2) if " 503" in first2 else f"Authentication refused: {first2}"

        except TimeoutError:
            return False, (
                f"Timeout ({timeout:.0f}s): the intercom does not answer on "
                f"{local_proxy}:{DEFAULT_LOCAL_SIP_PORT}. Check that Home "
                "Assistant and the intercom are on the same network."
            )
        except OSError as exc:
            return False, f"Socket error: {exc}"
        finally:
            sock.close()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


def _parse_challenge(response: str) -> dict[str, str]:
    """The Digest challenge parameters of a 401/407 response."""
    for line in response.split("\r\n"):
        name, sep, value = line.partition(":")
        if not sep or name.strip().lower() not in ("www-authenticate", "proxy-authenticate"):
            continue
        scheme, sep, _params = value.strip().partition(" ")
        if not sep or scheme.lower() != "digest":
            continue
        return _challenge_params(value)
    return {}


def _digest_header(
    *, user: str, password: str, realm: str, nonce: str, uri: str,
    method: str = "REGISTER", qop: str = "", opaque: str = "", cnonce: str = "",
) -> str:
    """Authorization header for an MD5 Digest challenge."""
    nc = "00000001"
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    offered = [item.strip().lower() for item in qop.split(",")] if qop else []
    if "auth" in offered:
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        header = (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                  f'uri="{uri}", response="{response}", algorithm=MD5, '
                  f'qop=auth, nc={nc}, cnonce="{cnonce}"')
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        header = (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                  f'uri="{uri}", response="{response}", algorithm=MD5')
    if opaque:
        header += f', opaque="{opaque}"'
    return header


async def _test_cloud_registration(
    sip_user: str,
    sip_password: str,
    cloud_domain: str,
    cloud_proxy: str = DEFAULT_CLOUD_PROXY,
    device_imei: str = "",
    device_uuid: str = "",
    device_name: str = MY_NAME,
    timeout: float = 12.0,
) -> tuple[bool, str]:
    """Register on the cloud relay over TLS; returns (success, message)."""
    if not cloud_domain:
        return False, ("The credentials carry no cloud domain: set the "
                       "integration up again from the pairing QR.")

    def _run() -> tuple[bool, str]:
        call_id = secrets.token_hex(8)
        from_tag = secrets.token_hex(4)
        uri = f"sip:{cloud_domain}"
        context = ssl.create_default_context()
        if os.path.exists(CA_PATH):
            context.load_verify_locations(CA_PATH)

        last_error = "no proxy reachable"
        for host, port in _resolve_sip_targets(cloud_proxy, CLOUD_SIP_PORT):
            try:
                raw = socket.create_connection((host, port), timeout=timeout)
            except OSError as exc:
                last_error = f"{host}:{port}: {exc}"
                continue
            try:
                with context.wrap_socket(raw, server_hostname=cloud_proxy) as sock:
                    sock.settimeout(timeout)
                    my_ip, my_port = sock.getsockname()[:2]

                    def make_register(auth_hdr=None, seq=1):
                        # Called only in this iteration: my_ip/my_port are the current socket's.
                        contact = f"<sip:{sip_user}@{my_ip}:{my_port};transport=tls>"  # noqa: B023
                        if device_uuid:
                            contact += f';+sip.instance="<urn:uuid:{device_uuid}>"'
                        lines = [
                            f"REGISTER {uri} SIP/2.0",
                            f"Via: SIP/2.0/TLS {my_ip}:{my_port};alias;"  # noqa: B023
                            f"branch=z9hG4bK{secrets.token_hex(4)};rport",
                            f"Route: <sip:{cloud_proxy};transport=tls;lr>",
                            "Max-Forwards: 70",
                            f"To: <sip:{sip_user}@{cloud_domain}>",
                            f"From: <sip:{sip_user}@{cloud_domain}>;tag={from_tag}",
                            f"Call-ID: {call_id}",
                            f"CSeq: {seq} REGISTER",
                            f"Contact: {contact}",
                            "Expires: 60",
                            f"User-Agent: {USER_AGENT}",
                            f"MyName: {device_name}",
                        ]
                        if device_imei:
                            lines.append(f"Mobile-IMEI: {device_imei}")
                        if auth_hdr:
                            lines.append(f"Authorization: {auth_hdr}")
                        lines += ["Content-Length: 0", "", ""]
                        return "\r\n".join(lines).encode()

                    def read_final() -> str:
                        """Skip provisional responses, return the final one."""
                        buffer = b""
                        deadline = time.monotonic() + timeout
                        while time.monotonic() < deadline:
                            chunk = sock.recv(65535)
                            if not chunk:
                                raise OSError("connection closed by the relay")
                            buffer += chunk
                            while b"\r\n\r\n" in buffer:
                                head, _, buffer = buffer.partition(b"\r\n\r\n")
                                message = head.decode(errors="replace")
                                parts = message.split("\r\n", 1)[0].split(" ")
                                if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) >= 200:
                                    return message
                        raise TimeoutError

                    sock.sendall(make_register())
                    response = read_final()
                    first = response.split("\r\n", 1)[0]
                    if " 200" in first:
                        return True, f"Cloud registration succeeded via {host}"
                    if " 401" not in first and " 407" not in first:
                        return False, f"The relay refused the registration: {first}"
                    challenge = _parse_challenge(response)
                    if not challenge.get("nonce") or not challenge.get("realm"):
                        return False, "Cloud challenge without nonce/realm"
                    auth = _digest_header(
                        user=sip_user, password=sip_password,
                        realm=challenge["realm"], nonce=challenge["nonce"], uri=uri,
                        qop=challenge.get("qop", ""), opaque=challenge.get("opaque", ""),
                        cnonce=secrets.token_hex(4),
                    )
                    sock.sendall(make_register(auth_hdr=auth, seq=2))
                    final = read_final().split("\r\n", 1)[0]
                    if " 200" in final:
                        return True, f"Cloud registration succeeded via {host}"
                    return False, f"Cloud authentication refused: {final}"
            except (OSError, ssl.SSLError) as exc:
                last_error = f"{host}:{port}: {exc or type(exc).__name__}"
                continue
        return False, f"Cloud registration failed ({last_error})"

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


async def _probe_transport(
    credentials: dict, *, local_proxy: str, local_udp_port: int,
    identity: dict, prefer_local: bool, local_domain: str = "",
) -> tuple[bool, bool, str]:
    """Find out which transport really works: (use_local_udp, ok, message).

    Start from what the plant profile suggests, and when that fails try the
    other path before giving up: the plant type in the QR is an indication,
    not a guarantee, and a new firmware can contradict it. Whatever passes is
    what gets stored.
    """

    async def _local() -> tuple[bool, str]:
        return await _test_sip_registration(
            sip_user=credentials["sip_user"],
            sip_password=credentials["sip_password"],
            # The domain runtime.configure uses in local UDP mode.
            sip_domain=local_domain or credentials["sip_domain"],
            local_proxy=local_proxy,
            local_udp_port=local_udp_port,
            device_imei=identity.get("device_imei", ""),
            device_uuid=identity.get("device_uuid", ""),
            device_name=identity.get("device_name") or MY_NAME,
        )

    async def _cloud() -> tuple[bool, str]:
        return await _test_cloud_registration(
            sip_user=credentials["sip_user"],
            sip_password=credentials["sip_password"],
            cloud_domain=credentials.get("cloud_domain", ""),
            cloud_proxy=credentials.get("cloud_proxy") or DEFAULT_CLOUD_PROXY,
            device_imei=identity.get("device_imei", ""),
            device_uuid=identity.get("device_uuid", ""),
            device_name=identity.get("device_name") or MY_NAME,
        )

    tries = [(True, "local UDP", _local), (False, "cloud TLS", _cloud)]
    if not prefer_local:
        tries.reverse()
    (first_local, first_name, first), (_, second_name, second) = tries

    ok, msg = await first()
    _LOGGER.info("SIP test (%s): ok=%s msg=%s", first_name, ok, msg)
    if ok:
        return first_local, True, msg
    ok2, msg2 = await second()
    _LOGGER.info("SIP test (%s, fallback): ok=%s msg=%s", second_name, ok2, msg2)
    if ok2:
        return (not first_local), True, (
            f"{first_name} not available ({msg}); registered via {second_name}")
    return first_local, False, f"{first_name}: {msg}. {second_name}: {msg2}"


def _entry_loaded(entry) -> bool:
    """True when the entry is set up: only then its update listener exists and reloads it.

    After ConfigEntryNotReady or a setup error there is no listener: whoever changes
    the entry has to reload it."""
    state = getattr(entry, "state", None)
    return str(getattr(state, "value", state)) == "loaded"


# ─── Config Flow ─────────────────────────────────────────────────────────────


def _default(value: str | None) -> dict:
    """`{"default": value}` solo se c'è un valore: un default "" precompila il campo
    con una stringa vuota invece di lasciarlo vuoto."""
    return {"default": value} if value else {}


class VimarIntercomConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gestisce l'onboarding dell'integrazione Vimar Intercom."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict = {}
        self._identity: dict = {}
        self._qr_error: str | None = None
        # Dati del record mDNS quando il flusso parte dal discovery (issue #6).
        # Vuoto se l'utente ha avviato il flusso a mano.
        self._discovered: dict[str, str] = {}
        # (entry, new address) when a configured Tab announced another address.
        self._moved: tuple | None = None

    # ─── Discovery mDNS (_eipvdes._tcp) ─────────────────────────────────────
    # Nessun import di ZeroconfServiceInfo (helpers.service_info.zeroconf): non serve
    # il tipo e i test, che girano senza HA, non hanno bisogno di un altro stub.
    # Servono solo .host e .properties.

    async def async_step_zeroconf(self, discovery_info) -> FlowResult:
        """Il Tab si è annunciato in LAN. Il record porta indirizzo, MAC e il dominio
        SIP che il Tab si aspetta; le credenziali no: il flusso prosegue come sempre
        (QR o manuale) con questi campi già compilati."""
        info = discovery.extract_discovery(
            str(discovery_info.host), getattr(discovery_info, "properties", None))
        _LOGGER.debug("Zeroconf: host=%s proxy=%s domain=%s model=%s fw=%s",
                      discovery_info.host, info["local_proxy"], info["sip_domain"],
                      info["model"], info["firmware"])
        mac = info["mac_normalized"]
        if not mac:
            # Senza MAC non si riconosce lo stesso Tab al discovery successivo:
            # si aprirebbe un flusso nuovo a ogni riavvio.
            return self.async_abort(reason="no_mac")

        # Già configurato? Le installazioni hanno unique_id «utente@dominio»: si
        # riconoscono dal MAC salvato (dal QR, con separatori diversi) o dall'IP.
        for entry in self._async_current_entries(include_ignore=False):
            conf = {**entry.data, **entry.options}
            same_mac = discovery.normalize_mac(conf.get(KEY_MAC)) == mac
            if same_mac or conf.get(KEY_LOCAL_PROXY) == info["local_proxy"]:
                moved = self._moved_proxy(conf, info["local_proxy"], str(discovery_info.host))
                if same_mac and moved:
                    # One flow per Tab: a second announcement joins this one.
                    await self.async_set_unique_id(mac)
                    self._abort_if_unique_id_configured()   # the user's "Ignore" holds
                    self._moved = (entry, moved)
                    self.context["title_placeholders"] = {"name": "Vimar Intercom", "host": moved}
                    return await self.async_step_zeroconf_moved()
                return self.async_abort(reason="already_configured")
        # Un altro Tab con un'installazione già presente: una sola entry.
        if self._has_entry():
            return self.async_abort(reason="single_instance_allowed")

        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()   # un «ignora» dell'utente vale
        self._discovered = info
        self.context["title_placeholders"] = {
            "name": f"Vimar {info['model']}" if info["model"] else "Vimar Intercom",
            "host": info["local_proxy"],
        }
        return await self.async_step_zeroconf_confirm()

    def _has_entry(self) -> bool:
        """Una sola installazione: lo stato SIP e media è a livello di modulo, quindi
        una seconda entry condividerebbe registrazione e chiamata con la prima.

        Il limite sta qui e non in `single_config_entry` del manifest: con quel flag
        Home Assistant ferma ogni flusso nuovo prima di chiamarci, compreso il
        discovery mDNS, e il Tab che cambia IP in UDP locale non veniva più seguito
        (`_update_proxy`, issue #6)."""
        return bool(self._async_current_entries(include_ignore=False))

    @staticmethod
    def _moved_proxy(conf: dict, proxy: str, host: str) -> str | None:
        """The new address of a configured Tab, if this announcement may propose one.

        Nothing in an mDNS record is authenticated: the MAC it is matched by is
        public (the Tab announces it), and the host is the address record the
        announcer wrote, not the packet's source. The checks below only keep the
        record consistent with itself (the `proxy` it carries is its own address)
        and the address a private IPv4 one, like a Tab's; what protects the entry is
        that the address is applied only after the user confirms it (#46). An
        authenticated probe of the new address would have kept this automatic, but
        HTTP Digest and SIP both hand the unconfirmed host a response it can attack
        offline for the SIP password. Only in local mode, where the address is used.
        """
        if not conf.get(KEY_USE_LOCAL_UDP, True) or not proxy or proxy == conf.get(KEY_LOCAL_PROXY):
            return None
        try:
            ip = ipaddress.ip_address(proxy)
        except ValueError:
            ip = None
        if proxy != host or ip is None or ip.version != 4 or not ip.is_private or ip.is_link_local:
            _LOGGER.warning("Ignored an mDNS announcement for the configured intercom: address %s, "
                            "host %s. Only a private IPv4 address, matching the record's host, "
                            "is offered.", proxy, host)
            return None
        return proxy

    async def async_step_zeroconf_moved(self, user_input: dict | None = None) -> FlowResult:
        """The configured Tab announced itself at another address: apply it on confirmation."""
        entry, proxy = self._moved
        if user_input is not None:
            self._update_proxy(entry, proxy)
            return self.async_abort(reason="address_updated")
        conf = {**entry.data, **entry.options}
        return self.async_show_form(
            step_id="zeroconf_moved",
            data_schema=vol.Schema({}),
            description_placeholders={"old": conf.get(KEY_LOCAL_PROXY) or "n/d", "new": proxy},
        )

    @callback
    def _update_proxy(self, entry, proxy: str) -> None:
        """Il DHCP ha dato un altro IP al Tab: si aggiorna l'entry (il listener ricarica).
        Solo in modalità locale, dove l'IP serve davvero."""
        conf = {**entry.data, **entry.options}
        if not proxy or conf.get(KEY_LOCAL_PROXY) == proxy or not conf.get(KEY_USE_LOCAL_UDP, True):
            return
        _LOGGER.info("Il citofono ha cambiato indirizzo: %s → %s", conf.get(KEY_LOCAL_PROXY), proxy)
        kwargs = {"data": {**entry.data, KEY_LOCAL_PROXY: proxy}}
        if KEY_LOCAL_PROXY in entry.options:
            kwargs["options"] = {**entry.options, KEY_LOCAL_PROXY: proxy}
        # The update listener reloads a loaded entry (local_proxy is connection data,
        # __init__._connection); one that failed to set up has no listener.
        self.hass.config_entries.async_update_entry(entry, **kwargs)
        if not _entry_loaded(entry):
            self.hass.config_entries.async_schedule_reload(entry.entry_id)

    async def async_step_zeroconf_confirm(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Conferma del Tab trovato, poi si prosegue con QR o credenziali."""
        if user_input is not None:
            return await self.async_step_user()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "host":     self._discovered.get("local_proxy", ""),
                "mac":      self._discovered.get("mac", ""),
                "model":    self._discovered.get("model") or "n/d",
                "firmware": self._discovered.get("firmware") or "n/d",
            },
        )

    def _apply_discovered(self) -> None:
        """Dopo QR o credenziali: il dominio locale e il MAC annunciati dal Tab.

        Il `domain` del record è quello che il Tab si aspetta in modalità locale
        (il proprio IP sul 40515, il dominio cloud sul 40507). Il QR del 40515 porta
        `domain=127.0.0.1` e la registrazione locale col dominio cloud riceve 503.
        """
        d = self._discovered
        if not d:
            return
        if d.get("sip_domain"):
            self._credentials[KEY_LOCAL_DOMAIN] = d["sip_domain"]
        if d.get("mac") and not self._credentials.get(KEY_MAC):
            self._credentials[KEY_MAC] = d["mac"]

    def _local_test_domain(self) -> str:
        """Il dominio che runtime.configure userà in UDP locale (LOCAL_DOMAIN se c'è)."""
        return self._credentials.get(KEY_LOCAL_DOMAIN) or self._credentials["sip_domain"]

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Step iniziale: scelta modalità (QR o manuale)."""
        if self._has_entry():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            if user_input.get("mode") == "qr":
                return await self.async_step_qr()
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("mode", default="qr"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["qr", "manual"], translation_key="mode",
                        mode=selector.SelectSelectorMode.LIST)),
            }),
        )

    async def async_step_qr(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Step 2a: incolla il testo del QR di abbinamento."""
        errors: dict[str, str] = {}

        if user_input is not None:
            qr_text = user_input.get("qr_text", "").strip()
            try:
                fields = await self.hass.async_add_executor_job(
                    qr_decoder.decode, qr_text
                )
                self._credentials = qr_decoder.extract_sip_credentials(fields)
                self._apply_discovered()
                return await self.async_step_network()
            except qr_decoder.QRDecodeError as exc:
                _LOGGER.warning("QR decode error: %s", exc)
                errors["qr_text"] = "qr_invalid"
                self._qr_error = str(exc)

        return self.async_show_form(
            step_id="qr",
            data_schema=vol.Schema({
                vol.Required("qr_text"): str,
            }),
            errors=errors,
            description_placeholders={
                "error_detail": self._qr_error or "",
            },
        )

    async def async_step_manual(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Step 2b: inserimento manuale delle credenziali SIP."""
        errors: dict[str, str] = {}

        if user_input is not None:
            sip_user     = user_input.get("sip_user", "").strip()
            sip_password = user_input.get("sip_password", "").strip()
            sip_domain   = user_input.get("sip_domain", "").strip()
            cloud_proxy  = user_input.get("cloud_proxy", DEFAULT_CLOUD_PROXY).strip()

            if not sip_user:
                errors["sip_user"] = "required"
            if not sip_password:
                errors["sip_password"] = "required"
            if not sip_domain:
                errors["sip_domain"] = "required"

            if not errors:
                sip_ha1 = hashlib.md5(
                    f"{sip_user}:{sip_domain}:{sip_password}".encode()
                ).hexdigest()
                self._credentials = {
                    KEY_SIP_USER:     sip_user,
                    KEY_SIP_PASSWORD: sip_password,
                    KEY_SIP_DOMAIN:   sip_domain,
                    KEY_SIP_HA1:      sip_ha1,
                    KEY_CLOUD_PROXY:  cloud_proxy,
                    KEY_GID: "", KEY_PLANT_TYPE: "", KEY_MAC: "",
                }
                self._apply_discovered()
                return await self.async_step_network()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({
                vol.Required("sip_user"):     str,
                vol.Required("sip_password"): str,
                # Il dominio annunciato dal Tab, se è un nome (il dominio cloud del
                # 40507); un IP (40515) è solo il dominio locale, non quello dell'account.
                vol.Required("sip_domain", **_default(
                    "" if discovery.is_ip(self._discovered.get("sip_domain"))
                    else self._discovered.get("sip_domain"))): str,
                vol.Optional("cloud_proxy", default=DEFAULT_CLOUD_PROXY): str,
            }),
            errors=errors,
        )

    async def async_step_network(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Step 3: intercom IP and a real registration on the working transport."""
        errors: dict[str, str] = {}
        profile = profiles.profile_for(self._credentials.get(KEY_PLANT_TYPE))

        if user_input is not None:
            # Drop a duplicate BEFORE probing: the probe really registers, and
            # on a credential already in use it would disturb the entry using it.
            await self.async_set_unique_id(
                f"{self._credentials['sip_user']}@{self._credentials['sip_domain']}")
            self._abort_if_unique_id_configured()

            fallback: str | None = None
            local_proxy    = user_input.get("local_proxy", "").strip()
            use_local_udp  = user_input.get("use_local_udp", profile.prefers_local_udp)
            local_udp_port = int(user_input.get("local_udp_port", DEFAULT_LOCAL_UDP_PORT))
            device_name    = str(user_input.get("device_name") or MY_NAME).strip()

            if not device_name:
                errors["device_name"] = "required"
            elif not _valid_device_name(device_name):
                errors["device_name"] = "invalid_device_name"
            if not local_proxy:
                errors["local_proxy"] = "required"
            elif not discovery.is_ip(local_proxy):
                errors["local_proxy"] = "invalid_ip"
            elif not errors:
                # The probe registers with the identity the integration will
                # use from then on, so it is created here and stored. On a new
                # credential that registration is what fixes the pairing,
                # name included.
                if not self._identity:
                    self._identity = runtime.new_device_identity()
                self._identity["device_name"] = device_name
                use_local_wanted = use_local_udp
                use_local_udp, ok, msg = await _probe_transport(
                    self._credentials,
                    local_proxy=local_proxy,
                    local_udp_port=local_udp_port,
                    identity=self._identity,
                    prefer_local=use_local_udp,
                    local_domain=self._local_test_domain(),
                )
                self._qr_error = msg
                if not ok:
                    errors["local_proxy"] = "sip_registration_failed"
                elif use_local_udp != use_local_wanted:
                    # The path asked for did not work and the other one did:
                    # the entry works, but the user must know it is not the
                    # transport they chose.
                    fallback = msg

            if not errors:
                data = {
                    **self._credentials,
                    **self._identity,
                    KEY_LOCAL_PROXY:    local_proxy,
                    KEY_USE_LOCAL_UDP:  use_local_udp,
                    KEY_LOCAL_UDP_PORT: local_udp_port,
                }
                extra = {}
                if fallback:
                    _LOGGER.warning("Setup: %s", fallback)
                    data[KEY_SETUP_NOTE] = fallback
                    names = {True: "local UDP", False: "cloud TLS"}
                    extra = {"description": "transport_fallback",
                             "description_placeholders": {
                                 "tried": names[use_local_wanted],
                                 "used": names[use_local_udp],
                                 "detail": fallback}}
                return self.async_create_entry(
                    title=f"Vimar Intercom ({local_proxy})",
                    data=data,
                    **extra,
                )

        return self.async_show_form(
            step_id="network",
            data_schema=vol.Schema({
                vol.Required("local_proxy", **_default(self._discovered.get("local_proxy"))): str,
                vol.Optional("use_local_udp", default=profile.prefers_local_udp): bool,
                vol.Optional(
                    "local_udp_port", default=DEFAULT_LOCAL_UDP_PORT
                ): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
                vol.Optional("device_name", default=MY_NAME): str,
            }),
            errors=errors,
            description_placeholders={
                "sip_user":   self._credentials.get("sip_user", ""),
                "sip_domain": self._credentials.get("sip_domain", ""),
                "mac":        self._credentials.get("mac", ""),
                "plant":      profiles.describe(
                    self._credentials.get(KEY_PLANT_TYPE),
                    self._credentials.get("product_code"),
                ),
                "error_detail": self._qr_error or "",
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """A new pairing QR for the entry already set up (the intercom refuses the old one).

        Only the SIP credentials change: the entry, its device identity, network
        settings and options stay, so entities, automations and dashboards keep
        working. The new QR is first tried with a real registration, as at setup."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                fields = await self.hass.async_add_executor_job(
                    qr_decoder.decode, user_input.get("qr_text", "").strip()
                )
                credentials = qr_decoder.extract_sip_credentials(fields)
            except qr_decoder.QRDecodeError as exc:
                _LOGGER.warning("QR decode error: %s", exc)
                errors["qr_text"] = "qr_invalid"
                self._qr_error = str(exc)
            else:
                # The new QR may belong to another entry: stop before the probe, which
                # would register on that entry's slot.
                new_uid = f"{credentials['sip_user']}@{credentials['sip_domain']}"
                if any(e.unique_id == new_uid and e.entry_id != entry.entry_id
                       for e in self._async_current_entries(include_ignore=False)):
                    return self.async_abort(reason="already_configured")
                # What setup learned and a new QR may lack: the Tab's own address as
                # local domain where the QR says 127.0.0.1 (40515, from mDNS), and the MAC.
                if (entry.data.get(KEY_LOCAL_DOMAIN)
                        and not discovery.is_routable_domain(credentials.get(KEY_LOCAL_DOMAIN))):
                    credentials[KEY_LOCAL_DOMAIN] = entry.data[KEY_LOCAL_DOMAIN]
                if not credentials.get(KEY_MAC) and entry.data.get(KEY_MAC):
                    credentials[KEY_MAC] = entry.data[KEY_MAC]
                self._credentials = credentials
                identity = {k: entry.data[k] for k in ("device_imei", "device_uuid", "device_name")
                            if entry.data.get(k)}
                wanted_local = entry.data.get(KEY_USE_LOCAL_UDP, True)
                use_local_udp, ok, msg = await _probe_transport(
                    credentials,
                    local_proxy=entry.data.get(KEY_LOCAL_PROXY, ""),
                    local_udp_port=int(entry.data.get(KEY_LOCAL_UDP_PORT, DEFAULT_LOCAL_UDP_PORT)),
                    identity=identity,
                    prefer_local=wanted_local,
                    local_domain=self._local_test_domain(),
                )
                self._qr_error = msg
                if not ok:
                    errors["qr_text"] = "sip_registration_failed"
                else:
                    # Only the other transport worked: the entry moves to it, and says so
                    # (as setup does with transport_fallback), never silently.
                    moved = use_local_udp != wanted_local
                    if moved:
                        _LOGGER.warning("Reconfigure: %s", msg)
                    # A loaded entry is saved without reloading: its update listener reloads on
                    # new credentials (HA 2026.12). One that is not set up has no listener.
                    finish = (self.async_update_and_abort if _entry_loaded(entry)
                              else self.async_update_reload_and_abort)
                    return finish(
                        entry,
                        unique_id=f"{credentials['sip_user']}@{credentials['sip_domain']}",
                        data_updates={**credentials, KEY_USE_LOCAL_UDP: use_local_udp,
                                      KEY_SETUP_NOTE: msg if moved else ""},
                        reason="reconfigure_transport_changed" if moved else "reconfigure_successful",
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Required("qr_text"): str}),
            errors=errors,
            description_placeholders={"error_detail": self._qr_error or ""},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowHandler:
        # Imported here: options_flow.py imports this module.
        from .options_flow import OptionsFlowHandler

        return OptionsFlowHandler(config_entry)
