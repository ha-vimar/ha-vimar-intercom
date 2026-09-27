"""Config flow per Vimar Intercom — onboarding via QR o credenziali manuali."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import re
import socket
import ssl
import time
import tempfile

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DOMAIN, SGA_TARGET, PICG_TARGET, USER_AGENT, CA_PATH,
    SIP_PORT as CLOUD_SIP_PORT, MY_NAME as DEFAULT_DEVICE_NAME,
    CONF_HOMEKIT_ACCESSORY, CONF_HOMEKIT_SMOOTH,
    DEFAULT_HOMEKIT_ACCESSORY, DEFAULT_HOMEKIT_SMOOTH,
)
from . import qr_decoder
from . import profiles
from . import rest_client
from . import rubrica_import
from . import runtime

_LOGGER = logging.getLogger(__name__)

# ─── Chiavi config entry ─────────────────────────────────────────────────────
KEY_SIP_USER      = "sip_user"
KEY_SIP_PASSWORD  = "sip_password"
KEY_SIP_DOMAIN    = "sip_domain"
KEY_DEVICE_NAME   = "device_name"
KEY_SIP_HA1       = "sip_ha1"
KEY_CLOUD_PROXY   = "cloud_proxy"
KEY_LOCAL_PROXY   = "local_proxy"
KEY_GID           = "gid"
KEY_PLANT_TYPE    = "plant_type"
KEY_MAC           = "mac"
KEY_USE_LOCAL_UDP  = "use_local_udp"
KEY_LOCAL_UDP_PORT = "local_udp_port"
KEY_ACTUATORS      = "actuators"
KEY_MEDIA_ENC      = "media_enc"
KEY_SGA_TARGET     = "sga_target"
KEY_PICG_TARGET    = "picg_target"
KEY_CAMERA_TARGET  = "camera_target"
KEY_DOOR_TARGET    = "door_target"

DEFAULT_CLOUD_PROXY    = "ipvdes.vimar.cloud"
DEFAULT_LOCAL_SIP_PORT = 5060
DEFAULT_LOCAL_UDP_PORT = 5060

# Icone ammesse per gli attuatori dinamici (vedi tools/parse_rubrica.py)
ALLOWED_ACTUATOR_ICONS = ("door", "light", "switch")


def _parse_actuators(raw: str) -> list[dict]:
    """Valida la lista attuatori incollata come JSON nell'options flow.

    Solleva ValueError con un messaggio parlante se il JSON non è una lista di
    dict con le chiavi ``name``, ``msg``, ``target``, ``icon`` (icon nel set
    ammesso). Stringa vuota → lista vuota (nessun bottone).
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"JSON non valido: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("La radice deve essere una lista JSON")
    result: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Elemento #{i} non è un oggetto JSON")
        missing = [k for k in ("name", "msg", "target", "icon") if k not in item]
        if missing:
            raise ValueError(f"Elemento #{i}: chiavi mancanti {missing}")
        icon = item["icon"]
        if icon not in ALLOWED_ACTUATOR_ICONS:
            raise ValueError(
                f"Elemento #{i}: icon '{icon}' non valida "
                f"(ammesse: {', '.join(ALLOWED_ACTUATOR_ICONS)})"
            )
        result.append({
            "name":   str(item["name"]),
            "msg":    str(item["msg"]),
            "target": str(item["target"]),
            "icon":   str(icon),
        })
    return result


# ─── Validazione ─────────────────────────────────────────────────────────────

def _validate_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


# Parametri di un challenge Digest: valori quotati (che possono contenere
# virgole) oppure token nudi. Va analizzato dopo aver tolto nome header e
# schema, altrimenti il primo parametro — di solito realm — viene perso.
_CHALLENGE_PARAM = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:"([^"]*)"|([^,\s]+))')


def _parse_challenge(response: str) -> dict[str, str]:
    """Estrae i parametri del challenge Digest da una risposta 401/407."""
    for line in response.split("\r\n"):
        name, sep, value = line.partition(":")
        if not sep or name.strip().lower() not in ("www-authenticate", "proxy-authenticate"):
            continue
        scheme, sep, params = value.strip().partition(" ")
        if not sep or scheme.lower() != "digest":
            continue
        return {
            m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
            for m in _CHALLENGE_PARAM.finditer(params)
        }
    return {}


def _digest_header(
    *, user: str, password: str, realm: str, nonce: str, uri: str,
    method: str = "REGISTER", qop: str = "", opaque: str = "", cnonce: str = "",
) -> str:
    """Costruisce l'header Authorization per un challenge Digest MD5."""
    nc = "00000001"
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    offered = [item.strip().lower() for item in qop.split(",")] if qop else []
    if "auth" in offered:
        response = hashlib.md5(
            f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()
        ).hexdigest()
        header = (
            f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}", algorithm=MD5, '
            f'qop=auth, nc={nc}, cnonce="{cnonce}"'
        )
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        header = (
            f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}", algorithm=MD5'
        )
    if opaque:
        header += f', opaque="{opaque}"'
    return header


def _cloud_targets(cloud_proxy: str) -> list[tuple[str, int]]:
    """Proxy SIP cloud, da SRV _sips._tcp.<proxy> con fallback noti."""
    targets: list[tuple[str, int]] = []
    try:
        import dns.resolver  # dnspython, presente in HA

        answers = dns.resolver.resolve(f"_sips._tcp.{cloud_proxy}", "SRV")
        for record in sorted(answers, key=lambda r: (r.priority, -r.weight)):
            targets.append((str(record.target).rstrip("."), int(record.port)))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("SRV lookup _sips._tcp.%s fallito: %s", cloud_proxy, exc)
    if not targets and cloud_proxy.endswith("ipvdes.vimar.cloud"):
        targets = [(f"flexiprod{i}.ipvdes2.vimarsso.cloud", CLOUD_SIP_PORT) for i in (1, 2, 3)]
    return targets


async def _test_cloud_registration(
    sip_user: str,
    sip_password: str,
    cloud_domain: str,
    cloud_proxy: str = DEFAULT_CLOUD_PROXY,
    device_imei: str = "",
    device_uuid: str = "",
    device_name: str = DEFAULT_DEVICE_NAME,
    timeout: float = 12.0,
) -> tuple[bool, str]:
    """Registra sul relay cloud in TLS e restituisce (successo, messaggio)."""
    if not cloud_domain:
        return False, (
            "Le credenziali non contengono il dominio cloud: riconfigura "
            "l'integrazione partendo dal QR di abbinamento."
        )

    def _run() -> tuple[bool, str]:
        def rand_hex(n: int = 8) -> str:
            return f"{secrets.randbelow(16**n):0{n}x}"

        call_id  = rand_hex(16)
        from_tag = rand_hex(8)
        uri      = f"sip:{cloud_domain}"

        context = ssl.create_default_context()
        if os.path.exists(CA_PATH):
            context.load_verify_locations(CA_PATH)

        last_error = "nessun proxy raggiungibile"
        for host, port in _cloud_targets(cloud_proxy):
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
                        contact = f"<sip:{sip_user}@{my_ip}:{my_port};transport=tls>"
                        if device_uuid:
                            contact += f';+sip.instance="<urn:uuid:{device_uuid}>"'
                        lines = [
                            f"REGISTER {uri} SIP/2.0",
                            f"Via: SIP/2.0/TLS {my_ip}:{my_port};alias;branch=z9hG4bK{rand_hex()};rport",
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
                        """Scarta le risposte provvisorie e restituisce la finale."""
                        buffer = b""
                        deadline = time.monotonic() + timeout
                        while time.monotonic() < deadline:
                            chunk = sock.recv(65535)
                            if not chunk:
                                raise OSError("connessione chiusa dal relay")
                            buffer += chunk
                            while b"\r\n\r\n" in buffer:
                                head, _, buffer = buffer.partition(b"\r\n\r\n")
                                message = head.decode(errors="replace")
                                first = message.split("\r\n", 1)[0]
                                code = first.split(" ")[1] if first.count(" ") >= 1 else ""
                                if code.isdigit() and int(code) >= 200:
                                    return message
                        raise socket.timeout

                    sock.sendall(make_register())
                    response = read_final()
                    first = response.split("\r\n", 1)[0]
                    if " 200" in first:
                        return True, "Registrazione cloud riuscita (senza auth)"
                    if " 401" not in first and " 407" not in first:
                        return False, f"Il relay ha rifiutato la registrazione: {first}"

                    challenge = _parse_challenge(response)
                    if not challenge.get("nonce") or not challenge.get("realm"):
                        return False, "Challenge cloud senza nonce/realm"
                    auth = _digest_header(
                        user=sip_user, password=sip_password,
                        realm=challenge["realm"], nonce=challenge["nonce"], uri=uri,
                        qop=challenge.get("qop", ""), opaque=challenge.get("opaque", ""),
                        cnonce=rand_hex(8),
                    )
                    sock.sendall(make_register(auth_hdr=auth, seq=2))
                    final = read_final().split("\r\n", 1)[0]
                    if " 200" in final:
                        return True, f"Registrazione cloud riuscita tramite {host}"
                    return False, f"Autenticazione cloud rifiutata: {final}"
            except (OSError, ssl.SSLError, socket.timeout) as exc:
                last_error = f"{host}:{port}: {exc}"
                continue
        return False, f"Registrazione cloud fallita ({last_error})"

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


async def _probe_transport(
    credentials: dict, *, local_proxy: str, local_udp_port: int,
    device_imei: str, device_uuid: str, device_name: str, prefer_local: bool,
) -> tuple[bool, bool, str]:
    """Stabilisce quale trasporto funziona davvero.

    Restituisce ``(use_local_udp, ok, messaggio)``. Si parte da ciò che il
    profilo dell'impianto suggerisce, ma se fallisce si prova l'altra via prima
    di arrendersi: il tipo di impianto dichiarato nel QR è un'indicazione, non
    una garanzia, e su un firmware nuovo può essere smentito. Quello che passa
    la prova è ciò che viene salvato.
    """

    async def _local() -> tuple[bool, str]:
        return await _test_sip_registration(
            sip_user=credentials["sip_user"],
            sip_password=credentials["sip_password"],
            sip_domain=credentials.get("local_domain") or credentials["sip_domain"],
            local_proxy=local_proxy,
            local_udp_port=local_udp_port,
            device_imei=device_imei,
            device_uuid=device_uuid,
            device_name=device_name,
        )

    async def _cloud() -> tuple[bool, str]:
        return await _test_cloud_registration(
            sip_user=credentials["sip_user"],
            sip_password=credentials["sip_password"],
            cloud_domain=credentials.get("cloud_domain", ""),
            cloud_proxy=credentials.get("cloud_proxy", DEFAULT_CLOUD_PROXY),
            device_imei=device_imei,
            device_uuid=device_uuid,
            device_name=device_name,
        )

    first_local = bool(prefer_local)
    first = _local if first_local else _cloud
    second = _cloud if first_local else _local
    first_name = "UDP locale" if first_local else "cloud TLS"
    second_name = "cloud TLS" if first_local else "UDP locale"

    ok, msg = await first()
    _LOGGER.info("SIP test (%s): ok=%s msg=%s", first_name, ok, msg)
    if ok:
        return first_local, True, msg

    ok2, msg2 = await second()
    _LOGGER.info("SIP test (%s, ripiego): ok=%s msg=%s", second_name, ok2, msg2)
    if ok2:
        return (not first_local), True, (
            f"{first_name} non disponibile ({msg}); registrazione riuscita via {second_name}"
        )
    return first_local, False, f"{first_name}: {msg} — {second_name}: {msg2}"


async def _test_sip_registration(
    sip_user: str,
    sip_password: str,
    sip_domain: str,
    local_proxy: str,
    local_udp_port: int = DEFAULT_LOCAL_UDP_PORT,
    device_imei: str = "",
    device_uuid: str = "",
    device_name: str = DEFAULT_DEVICE_NAME,
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """Tenta una registrazione SIP UDP e restituisce (successo, messaggio).

    Usa la stessa identità che userà il runtime: il citofono lega la coppia
    (identificativo, nome) alle credenziali al momento dell'abbinamento, quindi
    un test con un'identità diversa proverebbe qualcosa di inutile.
    """

    def _run() -> tuple[bool, str]:
        def rand_hex(n: int = 8) -> str:
            return f"{secrets.randbelow(16**n):0{n}x}"

        call_id  = rand_hex(16)
        from_tag = rand_hex(8)
        uri      = f"sip:{sip_domain}"

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            # Via e Contact devono annunciare la porta realmente in ascolto:
            # annunciarne un'altra manda le risposte in un socket inesistente.
            try:
                sock.bind(("0.0.0.0", local_udp_port))
            except OSError:
                sock.bind(("0.0.0.0", 0))
            my_port = sock.getsockname()[1]

            my_ip = "0.0.0.0"
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                probe.connect((local_proxy, DEFAULT_LOCAL_SIP_PORT))
                my_ip = probe.getsockname()[0]
                probe.close()
            except OSError:
                pass

            def make_register(auth_hdr: str | None = None, seq: int = 1) -> bytes:
                branch = f"z9hG4bK{rand_hex()}"
                lines = [
                    f"REGISTER {uri} SIP/2.0",
                    f"Via: SIP/2.0/UDP {my_ip}:{my_port};branch={branch};rport",
                    "Max-Forwards: 70",
                    f"To: <sip:{sip_user}@{sip_domain}>",
                    f"From: <sip:{sip_user}@{sip_domain}>;tag={from_tag}",
                    f"Call-ID: {call_id}",
                    f"CSeq: {seq} REGISTER",
                    f"Contact: <sip:{sip_user}@{my_ip}:{my_port}>"
                    + (f';+sip.instance="<urn:uuid:{device_uuid}>"' if device_uuid else ""),
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

            def status(response: str) -> str:
                return response.split("\r\n", 1)[0]

            def rejected(first: str) -> tuple[bool, str]:
                if " 503" in first:
                    return False, (
                        f"Il citofono ha rifiutato la registrazione UDP locale ({first.strip()}). "
                        "Su alcuni firmware l'UDP locale è disabilitato: disattiva «Usa UDP locale» "
                        "per registrarti tramite il cloud."
                    )
                return False, f"Risposta inattesa: {first}"

            sock.sendto(make_register(seq=1), (local_proxy, DEFAULT_LOCAL_SIP_PORT))
            data, _ = sock.recvfrom(65535)
            resp  = data.decode(errors="replace")
            first = status(resp)

            if " 200" in first:
                return True, "Registrazione riuscita (senza auth)"
            if " 401" not in first and " 407" not in first:
                return rejected(first)

            challenge = _parse_challenge(resp)
            nonce  = challenge.get("nonce", "")
            realm  = challenge.get("realm", "")
            opaque = challenge.get("opaque", "")
            qop    = challenge.get("qop", "")
            if not nonce or not realm:
                return False, "Challenge 401 senza nonce/realm"

            auth = _digest_header(
                user=sip_user, password=sip_password, realm=realm, nonce=nonce,
                uri=uri, qop=qop, opaque=opaque, cnonce=rand_hex(8),
            )

            sock.sendto(make_register(auth_hdr=auth, seq=2), (local_proxy, DEFAULT_LOCAL_SIP_PORT))
            data2, _ = sock.recvfrom(65535)
            resp2  = data2.decode(errors="replace")
            first2 = status(resp2)

            if " 200" in first2:
                return True, "Registrazione riuscita"
            return rejected(first2)

        except socket.timeout:
            return False, (
                f"Timeout ({timeout:.0f}s) — il citofono non e' raggiungibile "
                f"su {local_proxy}:{DEFAULT_LOCAL_SIP_PORT}. "
                "Verifica che HA e il citofono siano sulla stessa rete."
            )
        except OSError as exc:
            return False, f"Errore socket: {exc}"
        finally:
            sock.close()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


# ─── Config Flow ─────────────────────────────────────────────────────────────

class VimarIntercomConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gestisce l'onboarding dell'integrazione Vimar Intercom."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict = {}
        self._qr_error: str | None = None
        # Generato una volta sola: il test di registrazione lo usa e, se
        # l'abbinamento viene creato in quel momento, il citofono lo lega alle
        # credenziali. Il valore salvato dev'essere lo stesso che è stato testato.
        self._identity: dict[str, str] = runtime.new_device_identity()

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Step iniziale: scelta modalità (QR o manuale)."""
        if user_input is not None:
            if user_input.get("mode") == "qr":
                return await self.async_step_qr()
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("mode", default="qr"): vol.In({
                    "qr":     "Scansiona il QR di abbinamento (consigliato)",
                    "manual": "Inserimento manuale credenziali SIP",
                }),
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
                return await self.async_step_network()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({
                vol.Required("sip_user"):     str,
                vol.Required("sip_password"): str,
                vol.Required("sip_domain"):   str,
                vol.Optional("cloud_proxy", default=DEFAULT_CLOUD_PROXY): str,
            }),
            errors=errors,
        )

    async def async_step_network(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Step 3: IP citofono, porta UDP, test registrazione."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Il doppione si scarta PRIMA della prova: la prova registra
            # davvero sul citofono, e su una credenziale già in uso poteva
            # disturbare l'integrazione che la sta usando.
            await self.async_set_unique_id(
                f"{self._credentials['sip_user']}@{self._credentials['sip_domain']}")
            self._abort_if_unique_id_configured()

            local_proxy    = user_input.get("local_proxy", "").strip()
            use_local_udp  = user_input.get("use_local_udp", True)
            local_udp_port = int(user_input.get("local_udp_port", DEFAULT_LOCAL_UDP_PORT))
            device_name    = user_input.get("device_name", DEFAULT_DEVICE_NAME).strip()

            if not device_name:
                errors["device_name"] = "required"
            if not local_proxy:
                errors["local_proxy"] = "required"
            elif not _validate_ip(local_proxy):
                errors["local_proxy"] = "invalid_ip"
            elif not errors:
                # Solo con i dati validi: la prova registra davvero, e su una
                # credenziale nuova è la registrazione stessa a fissare
                # l'abbinamento, nome compreso.
                # Si verifica il trasporto che verrà davvero usato, e se quello
                # scelto non risponde si prova l'altro invece di fallire: è
                # l'introspezione che evita di dare per scontato il modello.
                use_local_udp, ok, msg = await _probe_transport(
                    self._credentials,
                    local_proxy=local_proxy,
                    local_udp_port=local_udp_port,
                    **self._identity,
                    device_name=device_name,
                    prefer_local=use_local_udp,
                )
                if not ok:
                    errors["base"] = "sip_registration_failed"
                    self._qr_error = msg
                else:
                    self._qr_error = msg

            if not errors:
                # L'abbinamento lega (identificativo, nome) alle credenziali: il
                # citofono rifiuta poi con 503 ogni registrazione che cambi uno
                # dei due. L'identificativo è casuale e per-installazione, così
                # due istanze di HA sullo stesso impianto non collidono, e va
                # conservato: perderlo significa dover rigenerare il QR.
                data = {
                    **self._credentials,
                    KEY_LOCAL_PROXY:    local_proxy,
                    KEY_USE_LOCAL_UDP:  use_local_udp,
                    KEY_LOCAL_UDP_PORT: local_udp_port,
                    **self._identity,
                    KEY_DEVICE_NAME:    device_name,
                    # L'impostazione iniziale della cifratura media segue il
                    # profilo dell'impianto; in risposta si rispecchia comunque
                    # ciò che offre l'altro capo.
                    KEY_MEDIA_ENC: bool(
                        profiles.profile_for(
                            self._credentials.get("plant_type")
                        ).media_encryption
                    ),
                }

                return self.async_create_entry(
                    title=f"Vimar Intercom ({local_proxy})",
                    data=data,
                )

        return self.async_show_form(
            step_id="network",
            data_schema=vol.Schema({
                vol.Required("local_proxy"): str,
                vol.Optional(
                    "use_local_udp",
                    default=profiles.profile_for(
                        self._credentials.get("plant_type")
                    ).prefers_local_udp,
                ): bool,
                vol.Optional(
                    "local_udp_port", default=DEFAULT_LOCAL_UDP_PORT
                ): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
                vol.Optional("device_name", default=DEFAULT_DEVICE_NAME): str,
            }),
            errors=errors,
            description_placeholders={
                "sip_user":   self._credentials.get("sip_user", ""),
                "sip_domain": self._credentials.get("sip_domain", ""),
                "mac":        self._credentials.get("mac", ""),
                "plant":      profiles.describe(
                    self._credentials.get("plant_type"),
                    self._credentials.get("product_code"),
                ),
                "error_detail": self._qr_error or "",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowHandler:
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Modifica le impostazioni di rete senza re-inserire le credenziali."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._actuators_error: str | None = None
        self._rubrica_error: str | None = None
        self._imported: dict | None = None
        self._imported_gid: str = "101"
        # PICG dichiarato dal citofono stesso (get_info.php?action=nickname).
        # Resta None quando la rubrica arriva da un file caricato a mano.
        self._picg_from_rest: str | None = None

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Menu: impostazioni a mano, rubrica dal citofono, o file rubrica.db."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "homekit", "fetch_rubrica", "import_rubrica"],
        )

    async def async_step_settings(
        self, user_input: dict | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        # Le options già salvate hanno precedenza sui dati iniziali dell'entry.
        current = {**self._entry.data, **self._entry.options}

        # Valore di default del campo attuatori: la lista già salvata, serializzata
        # in JSON leggibile così l'utente la ritrova e può modificarla.
        actuators_default = json.dumps(
            current.get(KEY_ACTUATORS, []), ensure_ascii=False, indent=2
        )

        if user_input is not None:
            local_proxy    = user_input.get("local_proxy", "").strip()
            use_local_udp  = user_input.get("use_local_udp", True)
            local_udp_port = int(user_input.get("local_udp_port", DEFAULT_LOCAL_UDP_PORT))
            media_enc      = bool(user_input.get(KEY_MEDIA_ENC, False))
            actuators_raw  = user_input.get(KEY_ACTUATORS, "")
            actuators_default = actuators_raw  # rimostra ciò che l'utente ha scritto

            # SGA/PICG: id SIP numerici (es. "55001"). Campo vuoto → fallback al
            # default storico in const.py (gestito da runtime.configure()), quindi
            # qui basta validare il formato quando l'utente scrive qualcosa.
            sga_target_raw  = str(user_input.get(KEY_SGA_TARGET, "")).strip()
            picg_target_raw = str(user_input.get(KEY_PICG_TARGET, "")).strip()
            if sga_target_raw and not sga_target_raw.isdigit():
                errors[KEY_SGA_TARGET] = "invalid_target"
            if picg_target_raw and not picg_target_raw.isdigit():
                errors[KEY_PICG_TARGET] = "invalid_target"
            camera_target_raw = str(user_input.get(KEY_CAMERA_TARGET, "")).strip()
            if camera_target_raw and not camera_target_raw.isdigit():
                errors[KEY_CAMERA_TARGET] = "invalid_target"
            door_target_raw = str(user_input.get(KEY_DOOR_TARGET, "")).strip()
            if door_target_raw and not door_target_raw.isdigit():
                errors[KEY_DOOR_TARGET] = "invalid_target"

            actuators: list[dict] = []
            try:
                actuators = _parse_actuators(actuators_raw)
            except ValueError as exc:
                errors[KEY_ACTUATORS] = "invalid_actuators"
                self._actuators_error = str(exc)

            # Il test SIP live va fatto solo se cambiano davvero i parametri SIP:
            # rifarlo a ogni salvataggio (es. modifica solo attuatori) fallirebbe per
            # conflitto con l'integrazione già registrata e bloccherebbe il salvataggio.
            sip_changed = (
                local_proxy    != current.get(KEY_LOCAL_PROXY, "")
                or use_local_udp  != current.get(KEY_USE_LOCAL_UDP, True)
                or local_udp_port != current.get(KEY_LOCAL_UDP_PORT, DEFAULT_LOCAL_UDP_PORT)
            )
            if not _validate_ip(local_proxy):
                errors["local_proxy"] = "invalid_ip"
            elif use_local_udp and sip_changed:
                # Con l'identità già abbinata: il citofono lega (identificativo,
                # nome) alle credenziali, e una prova senza quella veniva
                # rifiutata con 503 bloccando il salvataggio.
                ok, msg = await _test_sip_registration(
                    sip_user      = current["sip_user"],
                    sip_password  = current["sip_password"],
                    sip_domain    = current.get("local_domain") or current["sip_domain"],
                    local_proxy   = local_proxy,
                    local_udp_port = local_udp_port,
                    device_imei   = current.get("device_imei", ""),
                    device_uuid   = current.get("device_uuid", ""),
                    device_name   = current.get("device_name") or DEFAULT_DEVICE_NAME,
                )
                if not ok:
                    errors["local_proxy"] = "sip_registration_failed"

            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        # Le opzioni che questa pagina non gestisce (HomeKit)
                        # restano com'erano: senza, salvare la rete le azzerava.
                        **self._entry.options,
                        KEY_LOCAL_PROXY:    local_proxy,
                        KEY_USE_LOCAL_UDP:  use_local_udp,
                        KEY_LOCAL_UDP_PORT: local_udp_port,
                        KEY_MEDIA_ENC:      media_enc,
                        KEY_ACTUATORS:      actuators,
                        KEY_SGA_TARGET:     sga_target_raw,
                        KEY_PICG_TARGET:    picg_target_raw,
                        KEY_CAMERA_TARGET:  camera_target_raw,
                        KEY_DOOR_TARGET:    door_target_raw,
                    },
                )

        # Il form si ricompone su ciò che l'utente ha appena inviato, non solo
        # sui valori salvati: ricostruirlo da `current` scarterebbe in silenzio
        # tutte le altre modifiche fatte insieme a quella che non ha passato la
        # validazione. `current` resta intatto perché serve ai confronti sopra
        # (sip_changed) per capire cosa è davvero cambiato.
        form = {**current, **(user_input or {})}

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema({
                vol.Required(
                    "local_proxy",
                    default=form.get(KEY_LOCAL_PROXY, "")
                ): str,
                vol.Optional(
                    "use_local_udp",
                    default=form.get(KEY_USE_LOCAL_UDP, True)
                ): bool,
                vol.Optional(
                    "local_udp_port",
                    default=form.get(KEY_LOCAL_UDP_PORT, DEFAULT_LOCAL_UDP_PORT)
                ): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
                vol.Optional(
                    KEY_MEDIA_ENC,
                    default=form.get(KEY_MEDIA_ENC, False)
                ): bool,
                vol.Optional(
                    KEY_ACTUATORS,
                    default=actuators_default,
                ): str,
                vol.Optional(
                    KEY_SGA_TARGET,
                    default=form.get(KEY_SGA_TARGET) or SGA_TARGET,
                ): str,
                vol.Optional(
                    KEY_PICG_TARGET,
                    default=form.get(KEY_PICG_TARGET) or PICG_TARGET,
                ): str,
                vol.Optional(
                    KEY_CAMERA_TARGET,
                    # Vuoto se nessuno l'ha scelta: precompilarla col default
                    # la farebbe sembrare una scelta, e l'hub smetterebbe di
                    # impararla dalla targa che suona.
                    default=form.get(KEY_CAMERA_TARGET) or "",
                ): str,
                vol.Optional(
                    KEY_DOOR_TARGET,
                    default=form.get(KEY_DOOR_TARGET) or "",
                ): str,
            }),
            errors=errors,
            description_placeholders={
                "actuators_error": getattr(self, "_actuators_error", "") or "",
            },
        )

    async def async_step_homekit(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Il videocitofono in HomeKit: acceso o spento, video diretto o ricodificato."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self._entry.options,
                    CONF_HOMEKIT_ACCESSORY: bool(user_input.get(CONF_HOMEKIT_ACCESSORY)),
                    CONF_HOMEKIT_SMOOTH: bool(user_input.get(CONF_HOMEKIT_SMOOTH)),
                },
            )
        current = self._entry.options
        return self.async_show_form(
            step_id="homekit",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_HOMEKIT_ACCESSORY,
                    default=current.get(CONF_HOMEKIT_ACCESSORY, DEFAULT_HOMEKIT_ACCESSORY),
                ): bool,
                vol.Optional(
                    CONF_HOMEKIT_SMOOTH,
                    default=current.get(CONF_HOMEKIT_SMOOTH, DEFAULT_HOMEKIT_SMOOTH),
                ): bool,
            }),
        )

    async def async_step_fetch_rubrica(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Scarica la rubrica **dal citofono**, senza doverla estrarre a mano.

        Usa l'API HTTP che il citofono espone in rete locale (`rest_client`)
        con le stesse credenziali SIP già nel config entry: niente token,
        niente account Vimar, niente cloud. Nella stessa occasione chiede i
        nickname, così il PICG lo **dichiara il citofono** invece di doverlo
        indovinare o leggere dalla rubrica.

        Funziona solo se il citofono è raggiungibile in LAN sulla porta 80; per
        gli impianti che si raggiungono solo dal cloud resta la voce «importa
        da file».
        """
        errors: dict[str, str] = {}
        current = {**self._entry.data, **self._entry.options}
        host = (current.get(KEY_LOCAL_PROXY) or "").strip()
        default_gid = str(current.get(KEY_GID) or "101")

        if not host:
            # Senza indirizzo del citofono non c'è niente da contattare.
            errors["base"] = "no_local_proxy"
        elif user_input is not None:
            gid = (user_input.get("rubrica_gid") or default_gid).strip() or default_gid
            sip_user = current.get(KEY_SIP_USER, "")
            sip_password = current.get(KEY_SIP_PASSWORD, "")

            def _fetch() -> tuple[dict, str | None]:
                """Scarica, legge, ripulisce. Tutto bloccante, quindi executor."""
                fd, path = tempfile.mkstemp(suffix=".db", prefix="vimar_rubrica_")
                os.close(fd)
                try:
                    rest_client.download_db(
                        host, sip_user, sip_password, rest_client.DB_RUBRICA, dest=path
                    )
                    parsed = rubrica_import.parse_rubrica_file(path, gid)
                finally:
                    try:
                        os.unlink(path)
                    except OSError:  # pragma: no cover — file già rimosso
                        pass

                # I nickname sono un di più: se non arrivano, l'import della
                # rubrica resta valido e il PICG rimane quello configurato.
                picg: str | None = None
                try:
                    picg = rest_client.find_picg(
                        rest_client.get_nicknames(host, sip_user, sip_password)
                    )
                except rest_client.RestError:
                    _LOGGER.debug("nickname non disponibili da %s", host)
                return parsed, picg

            try:
                result, picg = await self.hass.async_add_executor_job(_fetch)
            except rest_client.RestAuthError as exc:
                errors["base"] = "rest_auth_failed"
                self._rubrica_error = str(exc)
            except rest_client.RestUnavailable as exc:
                errors["base"] = "rest_unavailable"
                self._rubrica_error = str(exc)
            except (rest_client.RestError, rubrica_import.RubricaImportError,
                    ValueError, OSError) as exc:
                errors["base"] = "rubrica_import_failed"
                self._rubrica_error = str(exc)
            else:
                if not result["actuators"]:
                    errors["base"] = "rubrica_no_actuators"
                    self._rubrica_error = (
                        f"Rubrica scaricata, ma nessun attuatore per il GID {gid}."
                    )
                else:
                    self._imported = result
                    self._imported_gid = gid
                    self._picg_from_rest = picg
                    return await self.async_step_import_confirm()

        return self.async_show_form(
            step_id="fetch_rubrica",
            data_schema=vol.Schema({
                vol.Optional(
                    "rubrica_gid",
                    default=(user_input or {}).get("rubrica_gid") or default_gid,
                ): str,
            }),
            errors=errors,
            description_placeholders={
                "host": host or "—",
                "rubrica_error": self._rubrica_error or "",
            },
        )

    async def async_step_import_rubrica(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Carica un file rubrica.db ed estrae attuatori + SGA/parametri SYSTEM,
        sostituendo la necessità di girare tools/parse_rubrica.py a mano e
        incollarne l'output nel campo Attuatori (JSON)."""
        errors: dict[str, str] = {}
        current = {**self._entry.data, **self._entry.options}
        default_gid = str(current.get(KEY_GID) or "101")

        if user_input is not None:
            gid = (user_input.get("rubrica_gid") or default_gid).strip() or default_gid
            upload_id = user_input.get("rubrica_file")

            def _load() -> dict:
                # process_uploaded_file è un context manager sincrono: la lettura
                # del file (SQLite) è I/O bloccante, quindi tutto il blocco gira
                # nell'executor, mai nel loop asyncio.
                with process_uploaded_file(self.hass, upload_id) as file_path:
                    return rubrica_import.parse_rubrica_file(str(file_path), gid)

            try:
                result = await self.hass.async_add_executor_job(_load)
            except (rubrica_import.RubricaImportError, ValueError, OSError) as exc:
                errors["rubrica_file"] = "rubrica_import_failed"
                self._rubrica_error = str(exc)
            else:
                if not result["actuators"]:
                    errors["rubrica_file"] = "rubrica_no_actuators"
                    self._rubrica_error = (
                        f"Nessun attuatore trovato per il GID appartamento {gid}."
                    )
                else:
                    self._imported = result
                    self._imported_gid = gid
                    return await self.async_step_import_confirm()

        return self.async_show_form(
            step_id="import_rubrica",
            data_schema=vol.Schema({
                vol.Required("rubrica_file"): selector.FileSelector(
                    selector.FileSelectorConfig(accept=".db")
                ),
                # Come sopra: se l'import fallisce, il GID digitato resta nel
                # campo invece di tornare al valore salvato.
                vol.Optional(
                    "rubrica_gid",
                    default=(user_input or {}).get("rubrica_gid") or default_gid,
                ): str,
            }),
            errors=errors,
            description_placeholders={
                "rubrica_error": self._rubrica_error or "",
            },
        )

    def _confirm_picg(self, current: dict, sga: str | None) -> str:
        """Il picg_target che la conferma salverà — unica fonte per riepilogo e salvataggio.

        In ordine: il PICG dichiarato dal citofono (rubrica scaricata, nickname
        disponibili); altrimenti quello già configurato, che un import da file
        non deve toccare perché il file non dice nulla sul PICG; solo se non ne
        è configurato nessuno, l'SGA della rubrica (sugli impianti visti finora
        coincidono), e infine il default.

        Prima della correzione il riepilogo prometteva «resta quello già
        configurato» mentre il salvataggio lo sovrascriveva con l'SGA: un 60001
        messo a mano per un 2FV2 diventava 55001.
        """
        return (
            self._picg_from_rest
            or current.get(KEY_PICG_TARGET)
            or sga
            or PICG_TARGET
        )

    async def async_step_import_confirm(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Riepilogo di ciò che è stato letto da rubrica.db; confermando si
        sostituisce la lista attuatori corrente con quella importata."""
        current = {**self._entry.data, **self._entry.options}
        result = self._imported or {"actuators": [], "sga": None}
        actuators: list[dict] = result["actuators"]
        sga = result.get("sga")

        if user_input is not None:
            # Due fonti distinte per due valori distinti:
            #   sga_target  ← SYSTEM.MAGIC_APT_INTERCOM della rubrica
            #   picg_target ← il ruolo PICG dichiarato dal citofono nei nickname
            new_sga  = sga or current.get(KEY_SGA_TARGET) or SGA_TARGET
            new_picg = self._confirm_picg(current, sga)
            new_door = runtime.door_from_actuators(actuators) or current.get(KEY_DOOR_TARGET, "")
            return self.async_create_entry(
                title="",
                data={
                    **self._entry.options,
                    KEY_LOCAL_PROXY:    current.get(KEY_LOCAL_PROXY, ""),
                    KEY_USE_LOCAL_UDP:  current.get(KEY_USE_LOCAL_UDP, True),
                    KEY_LOCAL_UDP_PORT: current.get(KEY_LOCAL_UDP_PORT, DEFAULT_LOCAL_UDP_PORT),
                    KEY_MEDIA_ENC:      current.get(KEY_MEDIA_ENC, False),
                    KEY_ACTUATORS:      actuators,
                    KEY_SGA_TARGET:     new_sga,
                    KEY_PICG_TARGET:    new_picg,
                    KEY_DOOR_TARGET:    new_door,
                },
            )

        current_sga = current.get(KEY_SGA_TARGET) or SGA_TARGET
        if sga and sga != current_sga:
            sga_info = (
                f"{sga} — diverso da quello attualmente configurato ({current_sga}); "
                "confermando verrà impostato come nuovo sga_target/picg_target."
            )
        elif sga:
            sga_info = f"{sga} — coincide con quello già in uso."
        else:
            sga_info = "non trovato (tabella SYSTEM assente o senza MAGIC_APT_INTERCOM); resta invariato quello già configurato."

        configured_picg = current.get(KEY_PICG_TARGET)
        new_picg = self._confirm_picg(current, sga)
        if self._picg_from_rest and self._picg_from_rest != (configured_picg or PICG_TARGET):
            picg_info = (
                f"{self._picg_from_rest} — dichiarato dal citofono stesso, "
                f"diverso da quello configurato ({configured_picg or PICG_TARGET}); "
                "confermando verrà impostato come nuovo picg_target."
            )
        elif self._picg_from_rest:
            picg_info = f"{self._picg_from_rest} — dichiarato dal citofono, coincide con quello già in uso."
        elif configured_picg:
            picg_info = (
                f"non richiesto (rubrica da file): resta quello già configurato ({configured_picg})."
            )
        else:
            picg_info = (
                f"non richiesto (rubrica da file) e non ancora configurato: verrà impostato "
                f"uguale all'SGA ({new_picg})."
            )

        return self.async_show_form(
            step_id="import_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "count": str(len(actuators)),
                "names": ", ".join(a["name"] for a in actuators) or "—",
                "gid": self._imported_gid,
                "sga_info": sga_info,
                "picg_info": picg_info,
            },
        )
