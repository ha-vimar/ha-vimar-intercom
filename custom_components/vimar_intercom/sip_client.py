"""Vimar Intercom — SIP signaling: transport, auth, operations."""

import asyncio
import hashlib
import os
import secrets
import re
import socket
import ssl
import string
import time
import logging

from . import const as C
from . import runtime as R
from .sip_message import (  # noqa: F401 — usati anche da fuori come sip._parse, ...
    MAX_SIP_BODY, _angle, _call_id, _challenge_nonce, _clen, _parse,
    _split_contacts, _split_stream, _tag, _via_block, parse_sdp,
)
from . import media_handler as media
from . import model_detect
from .inventory import DeviceInventory

_LOGGER = logging.getLogger(__name__)

# ─── Broadcast callback (set by hub) ────────────────────────────────
_broadcast = None


def init(broadcast_fn):
    global _broadcast
    _broadcast = broadcast_fn


_suppress_broadcast = False

async def broadcast(msg_type, msg):
    if _suppress_broadcast:
        _LOGGER.debug("Broadcast suppressed: %s %s", msg_type, msg)
        return
    if _broadcast:
        await _broadcast(msg_type, msg)


# ─── State ──────────────────────────────────────────────────────────
reader = None
writer = None
lock = None
_udp_sock = None     # UDP socket (local mode)
_udp_target = None   # (host, port) target for UDP sendto
registered = False
# Dispositivi dell'impianto osservati sul canale SIP (vedi inventory.py).
DEVICES = DeviceInventory()
# Lifetime granted by the registrar on the last successful REGISTER, in seconds.
# It may be shorter than requested, and renewal must follow what it grants.
granted_expiry = 0
in_call = False
calling = False
cseq_counter = 0
local_tag = None
MY_IP = None

# State change callback — hub sets this to notify entities
_state_change_callback = None

call_state = {
    "call_id": None, "from_tag": None, "to_tag": None,
    "remote_contact": None, "remote_sdp": None, "original_target": None,
    # L'SDP che abbiamo mandato noi: a un re-INVITE si risponde con questo.
    "local_sdp": None,
    # La transazione INVITE in corso, per poterla annullare con un CANCEL.
    "invite_branch": None, "invite_cseq": None, "cancelled": False,
}

_DIALOG_RESET = dict(call_id=None, from_tag=None, to_tag=None,
                     remote_contact=None, remote_sdp=None, original_target=None,
                     route_set=None, local_sdp=None, invite_branch=None,
                     invite_cseq=None, cancelled=False)

pending_responses: dict[str, asyncio.Queue] = {}
incoming_requests: asyncio.Queue = None


def set_state_callback(cb):
    """Set callback that fires on registered/in_call changes."""
    global _state_change_callback
    _state_change_callback = cb


def _notify_state_change():
    """Notify hub that SIP state changed."""
    if _state_change_callback:
        try:
            _state_change_callback()
        except Exception:
            _LOGGER.exception("State change callback error")


def _set_registered(val: bool):
    global registered
    if registered != val:
        registered = val
        _notify_state_change()


def _set_in_call(val: bool):
    global in_call
    if in_call != val:
        in_call = val
        _notify_state_change()


def _set_calling(val: bool):
    global calling
    if calling != val:
        calling = val
        _notify_state_change()


def get_local_ip():
    """Detect local IP by routing toward the SIP target."""
    target = (R.LOCAL_PROXY, C.LOCAL_SIP_PORT) if R.USE_LOCAL_UDP else (R.SIP_PROXY, C.SIP_PORT)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(target)
        ip = s.getsockname()[0]
        _LOGGER.info("Detected local IP: %s (via %s)", ip, target[0])
        return ip
    except Exception:
        _LOGGER.warning("IP detection failed, using 0.0.0.0")
        return "0.0.0.0"
    finally:
        s.close()


# ─── Transport helpers ───────────────────────────────────────────────

def _transport():
    return "UDP" if R.USE_LOCAL_UDP else "TLS"


def _my_port():
    """La porta su cui ascoltiamo **davvero**.

    connect() ripiega su una porta effimera quando LOCAL_UDP_PORT e' occupata.
    Fino alla 1.0.5 questa funzione restituiva comunque la porta configurata, e
    quel valore finiva in Via, nel Contact della REGISTER e in _simple_contact():
    la registrazione riusciva lo stesso (le risposte tornano al source port) ma
    l'INVITE in arrivo veniva instradato verso una porta dove non ascolta
    nessuno. Campanello muto, nessun errore, nessun log.
    """
    if not R.USE_LOCAL_UDP:
        return 5070
    if _udp_sock is not None:
        try:
            return int(_udp_sock.getsockname()[1])
        except OSError:
            pass
    return R.LOCAL_UDP_PORT


def _via_line(branch):
    return (f"Via: SIP/2.0/{_transport()} {MY_IP}:{_my_port()};"
            f"branch={branch};rport\r\n")


def _contact_hdr(include_pn=True):
    """Return the full Contact header value (no 'Contact:' prefix)."""
    port = _my_port()
    if R.USE_LOCAL_UDP:
        contact = f"<sip:{R.SIP_USER}@{MY_IP}:{port}>"
        contact += f';+sip.instance="<urn:uuid:{R.DEVICE_UUID}>"'
        contact += ";expires=3600"
        return contact
    # TLS/cloud mode
    contact_uri = f"sip:{R.SIP_USER}@{MY_IP}:{port};transport=tls"
    if include_pn and C.PN_TOKEN:
        contact_uri += (f";app-id={C.PN_APP_ID}"
                        f";pn-type={C.PN_TYPE}"
                        f";pn-tok={C.PN_TOKEN}"
                        f";pn-msg-str=IM_MSG;pn-msg-snd=msg.caf"
                        f";pn-call-str=IC_MSG;pn-call-snd=notes_of_the_optimistic.caf"
                        f";q=0.00;domain-name={R.SIP_DOMAIN}")
    contact = f"<{contact_uri}>"
    contact += f';+sip.instance="<urn:uuid:{R.DEVICE_UUID}>"'
    contact += f";expires={'5184000' if C.PN_TOKEN else '3600'}"
    return contact


def _route_line(route_set=None):
    """Le intestazioni Route da mettere in una richiesta.

    Fuori da un dialogo si usa il proxy della registrazione. DENTRO un dialogo
    no: serve la strada che il dialogo stesso ha registrato nei Record-Route
    della risposta, presi al contrario (RFC 3261 §12.1.2). Qui ne arrivano
    cinque — il relay, l'indirizzo pubblico, il PBX del citofono e due interni
    — e senza di quelle una richiesta in-dialogo non arriva a destinazione: il
    BYE finiva nel vuoto, nessuno rispondeva, e la chiamata restava aperta
    fino al timeout del citofono. Due minuti, con la luce del posto esterno
    accesa e nessuno che potesse più suonare.
    """
    if route_set:
        return "".join(f"Route: {r}\r\n" for r in route_set)
    if R.USE_LOCAL_UDP:
        return ""
    return f"Route: <sip:{R.SIP_PROXY};transport=tls;lr>\r\n"


def _simple_contact():
    """Contact URI for in-dialog responses (180/200 to incoming INVITE)."""
    port = _my_port()
    if R.USE_LOCAL_UDP:
        return f"<sip:{R.SIP_USER}@{MY_IP}:{port}>"
    return f"<sip:{R.SIP_USER}@{MY_IP}:{port};transport=tls>"


def _gen(prefix="z9hG4bK"):
    return f"{prefix}{secrets.token_hex(4)}"


def _next_cseq():
    global cseq_counter
    cseq_counter += 1
    return cseq_counter


# ─── Digest Auth ────────────────────────────────────────────────────

def _compute_ha1(realm):
    if realm == R.SIP_DOMAIN:
        return R.SIP_HA1
    return hashlib.md5(f"{R.SIP_USER}:{realm}:{R.SIP_PASSWORD}".encode()).hexdigest()


def _digest_resp(method, uri, nonce, realm=None, qop=None, nc=None, cnonce=None):
    ha1 = _compute_ha1(realm or R.SIP_DOMAIN)
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if qop == "auth":
        return hashlib.md5(
            f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
        ).hexdigest()
    return hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()


def _make_auth(method, uri, challenge):
    p = {}
    for item in challenge.replace("Digest ", "").split(","):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            p[k.strip()] = v.strip().strip('"')
    nonce = p.get("nonce", "")
    realm = p.get("realm", R.SIP_DOMAIN)
    opaque = p.get("opaque", "")
    qop = p.get("qop", "")
    nc = "00000001"
    cnonce = secrets.token_hex(8)
    if "auth" in qop:
        resp = _digest_resp(method, uri, nonce, realm, "auth", nc, cnonce)
        hdr = (f'Digest username="{R.SIP_USER}", realm="{realm}", '
               f'nonce="{nonce}", uri="{uri}", response="{resp}", '
               f'algorithm=MD5, qop=auth, nc={nc}, cnonce="{cnonce}"')
    else:
        resp = _digest_resp(method, uri, nonce, realm)
        hdr = (f'Digest username="{R.SIP_USER}", realm="{realm}", '
               f'nonce="{nonce}", uri="{uri}", response="{resp}", '
               f'algorithm=MD5')
    if opaque:
        hdr += f', opaque="{opaque}"'
    return hdr


# ─── Transport ──────────────────────────────────────────────────────

def _create_ssl_context(verify: bool = True):
    """Contesto TLS per la modalita' cloud.

    Con il CA di Vimar presente (`vimar_rootca.pem`) si verifica contro quello.
    Senza, e con verify=True, si verifica contro il trust store di sistema.
    verify=False disattiva ogni verifica: fino alla 1.0.5 era il comportamento
    di **tutte** le installazioni, perche' il CA non e' nel repo e il ramo else
    era l'unico attivo — in silenzio. Chi si fosse messo in mezzo avrebbe
    raccolto la REGISTER con il digest della password SIP.

    connect() ora prova prima verificando e ripiega qui solo se l'handshake
    fallisce per il certificato, scrivendolo nel log.
    """
    ctx = ssl.create_default_context()
    if os.path.exists(C.CA_PATH):
        ctx.load_verify_locations(C.CA_PATH)
    elif not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _resolve_sip_targets(proxy: str, default_port: int) -> list[tuple[str, int]]:
    """Restituisce [(host, port)] per il proxy SIP cloud: SRV _sips._tcp.<proxy>,
    poi fallback noti, poi il proxy stesso."""
    targets: list[tuple[str, int]] = []
    try:
        import dns.resolver  # dnspython (presente in HA)
        ans = dns.resolver.resolve(f"_sips._tcp.{proxy}", "SRV")
        recs = sorted(ans, key=lambda r: (r.priority, -r.weight))
        for r in recs:
            targets.append((str(r.target).rstrip("."), int(r.port)))
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("SRV lookup _sips._tcp.%s failed: %s", proxy, e)
    if not targets and proxy.endswith("ipvdes.vimar.cloud"):
        targets = [(f"flexiprod{i}.ipvdes2.vimarsso.cloud", 7042) for i in (1, 2, 3)]
    targets.append((proxy, default_port))
    return targets


async def connect():
    global reader, writer, lock, _udp_sock, _udp_target, MY_IP
    MY_IP = get_local_ip()

    if R.USE_LOCAL_UDP:
        # ── UDP locale: apre un socket UDP verso il citofono ───────
        _udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            _udp_sock.bind(("0.0.0.0", R.LOCAL_UDP_PORT))
        except OSError as e:
            # Porta occupata (restart di HA, un altro servizio SIP, una seconda
            # entry): si ripiega su una effimera. _my_port() la rilegge dalla
            # socket, quindi Via e Contact restano veri — ma va detto, perche'
            # se qualcun altro tiene la porta standard puo' intercettare le
            # chiamate in arrivo al posto nostro.
            _udp_sock.bind(("0.0.0.0", 0))
            _LOGGER.warning(
                "Porta UDP %d occupata (%s): in ascolto sulla %d. Se un altro "
                "servizio SIP tiene la %d, le chiamate in arrivo potrebbero "
                "non raggiungere Home Assistant.",
                R.LOCAL_UDP_PORT, e, _udp_sock.getsockname()[1], R.LOCAL_UDP_PORT)
        _udp_sock.setblocking(False)
        _udp_target = (R.LOCAL_PROXY, C.LOCAL_SIP_PORT)
        lock = asyncio.Lock()
        _LOGGER.info("SIP UDP socket bound, target %s:%d",
                     R.LOCAL_PROXY, C.LOCAL_SIP_PORT)
    else:
        # ── TLS/TCP cloud mode ─────────────────────────────────────
        loop = asyncio.get_running_loop()
        # Il proxy cloud reale è pubblicato via DNS SRV (_sips._tcp.<cproxy>):
        # es. flexiprod1/2/3.ipvdes2.vimarsso.cloud:7042. <cproxy> stesso è un
        # CDN HTTPS e NON parla SIP → senza SRV la connessione resta appesa.
        candidates = await loop.run_in_executor(None, _resolve_sip_targets, R.SIP_PROXY, C.SIP_PORT)
        last_err = None
        # L'SNI è il nome del servizio cloud (<cproxy> del QR), non l'host SRV a
        # cui ci si connette. Va preso dal config entry: su un impianto con un
        # cproxy diverso da quello di default, una costante qui manderebbe in
        # handshake TLS il nome sbagliato.
        # Due giri: prima verificando il certificato, poi — solo se a fermarci
        # e' stato il certificato e non la rete — senza verifica, dicendolo.
        # L'ordine conta: chi ha un impianto che presenta un certificato
        # verificabile ottiene una connessione sicura senza dover configurare
        # niente, e chi non ce l'ha continua a funzionare come prima, ma con una
        # riga nel log invece che in silenzio.
        cert_error = None
        for verify in (True, False):
            ctx = await loop.run_in_executor(None, _create_ssl_context, verify)
            if not verify:
                _LOGGER.warning(
                    "TLS: certificato del proxy cloud non verificabile (%s). "
                    "Riprovo SENZA verifica: la connessione resta cifrata ma non "
                    "autenticata, quindi un intermediario potrebbe leggere la "
                    "REGISTER e ricavare offline la password SIP. Per chiudere "
                    "questo buco serve il CA di Vimar in %s.",
                    cert_error, C.CA_PATH)
            for host, port in candidates:
                _LOGGER.info("Connecting to SIP proxy %s:%d (SNI %s, verify=%s)...",
                             host, port, R.SIP_PROXY, verify)
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(host, port, ssl=ctx, server_hostname=R.SIP_PROXY),
                        timeout=12)
                    break
                except ssl.SSLCertVerificationError as e:
                    cert_error = e
                    last_err = e
                    _LOGGER.warning("SIP TLS %s:%d: certificato rifiutato: %s", host, port, e)
                    reader = writer = None
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    _LOGGER.warning("SIP TLS connect to %s:%d failed: %s", host, port, e)
                    reader = writer = None
            if writer is not None or cert_error is None:
                # Riuscito, oppure fallito per motivi che il secondo giro non
                # risolverebbe (host irraggiungibile, timeout, rete assente).
                break
        if writer is None:
            raise ConnectionError(f"Nessun proxy SIP cloud raggiungibile: {last_err}")
        lock = asyncio.Lock()
        global _conn_gen
        _conn_gen += 1
        _conn_event().set()
        _LOGGER.info("SIP TLS connected")


async def reconnect():
    """Riconnette e ri-registra, con attese crescenti. Una alla volta.

    La chiedono in tanti (il lettore, il keepalive dell'hub, il rinnovo, il
    pulsante nella scheda): chi arriva mentre una è in corso ne aspetta
    l'esito invece di aprire una seconda connessione in parallelo.
    """
    global _reconnect_lock
    if _reconnect_lock is None:
        _reconnect_lock = asyncio.Lock()
    if _reconnect_lock.locked():
        async with _reconnect_lock:
            return registered
    async with _reconnect_lock:
        return await _reconnect_locked()


async def _reconnect_locked():
    _set_registered(False)
    delays = [2, 4, 8, 16, 32]
    for attempt, delay in enumerate(delays, 1):
        _LOGGER.warning("SIP reconnect attempt %d/%d in %ds...", attempt, len(delays), delay)
        await asyncio.sleep(delay)
        try:
            if R.USE_LOCAL_UDP:
                # UDP: non serve riconnettersi, solo re-registrarsi
                ok = await do_register()
            else:
                if writer:
                    try:
                        writer.close()
                    except Exception:
                        pass
                await connect()
                _LOGGER.info("SIP reconnected, re-registering...")
                ok = await do_register()
                if ok:
                    try:
                        await do_connect_profiles()
                    except Exception:
                        pass
            if ok:
                return True
        except Exception as e:
            _LOGGER.error("Reconnect attempt %d failed: %s", attempt, e)
    _LOGGER.error("All reconnect attempts failed")
    return False


async def send(msg: str):
    first_line = msg.split("\r\n", 1)[0]
    _LOGGER.debug("[SIP >>>] %s", first_line)
    try:
        if R.USE_LOCAL_UDP:
            loop = asyncio.get_running_loop()
            async with lock:
                await loop.sock_sendto(_udp_sock, msg.encode(), _udp_target)
        else:
            async with lock:
                writer.write(msg.encode())
                await writer.drain()
    except Exception as e:
        _LOGGER.error("[SIP >>>] send failed: %s", e)
        raise


async def send_keepalive() -> bool:
    """Ping CRLF sulla connessione (RFC 5626 §3.5.1, "double CRLF").

    Serve a tenere viva la connessione e il binding NAT verso il relay senza
    rifare una REGISTER completa: il rinnovo della registrazione ha una sua
    scadenza, decisa da quanto concede il registrar, ed è un'altra cosa.
    In UDP locale non c'è NAT di mezzo e il ping non serve.
    """
    if R.USE_LOCAL_UDP:
        return False
    if not writer or writer.is_closing():
        return False
    try:
        async with lock:
            writer.write(b"\r\n\r\n")
            await writer.drain()
        return True
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("SIP keepalive failed: %s", e)
        return False


# ─── Rilevamento modello dal SIP ────────────────────────────────────────────
# Ogni messaggio SIP ricevuto può portare l'identità del peer: «User-Agent»
# nelle richieste (INVITE/OPTIONS/MESSAGE dal citofono) e «Server» nelle
# risposte. model_detect.match() traduce quella stringa nel nome commerciale.

_seen_uas: set[str] = set()
_model_callback = None


def set_model_callback(cb):
    """Callback(model, fw, user_agent) invocata quando il modello cambia."""
    global _model_callback
    _model_callback = cb


def _learn_peer(hdrs: dict, first: str = "") -> None:
    """Impara il modello del citofono dagli header identificativi."""
    for key in ("user-agent", "server"):
        ua = (hdrs.get(key) or "").strip()
        if not ua or ua == C.USER_AGENT:
            continue
        if ua not in _seen_uas:
            _seen_uas.add(ua)
            _LOGGER.info("SIP peer identificato — %s: %s  [%s]", key, ua, first[:60])
        _apply_ua(ua)


def _apply_ua(ua: str) -> None:
    model, fw, priority = model_detect.match(ua)
    if not model:
        return
    # Un match più specifico (priorità più bassa) sovrascrive quello corrente
    if model == R.DETECTED_MODEL and (fw or "") == R.DETECTED_FW:
        return
    if priority > R.DETECTED_PRIORITY:
        return

    _LOGGER.info("Modello citofono rilevato: %s (fw=%s) da User-Agent «%s»",
                 model, fw or "n/d", ua)
    R.DETECTED_MODEL    = model
    R.DETECTED_FW       = fw or ""
    R.DETECTED_UA       = ua
    R.DETECTED_PRIORITY = priority

    if _model_callback:
        try:
            _model_callback(model, fw or "", ua, priority)
        except Exception:
            _LOGGER.exception("Model callback error")


# ─── Reader tasks ───────────────────────────────────────────────────

async def _send_options_ping():
    """Invia OPTIONS al citofono come keepalive UDP."""
    if not registered:
        return
    cid = _gen("ping-")
    ftag = _gen("")
    branch = _gen()
    target = f"sip:{R.SIP_USER}@{R.SIP_DOMAIN}"
    msg = (f"OPTIONS {target} SIP/2.0\r\n"
           f"{_via_line(branch)}"
           f"Max-Forwards: 70\r\n"
           f"To: <{target}>\r\n"
           f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
           f"Call-ID: {cid}\r\n"
           f"CSeq: 1 OPTIONS\r\n"
           f"User-Agent: {C.USER_AGENT}\r\n"
           f"Content-Length: 0\r\n\r\n")
    try:
        await send(msg)
    except Exception as e:
        _LOGGER.debug("OPTIONS ping failed: %s", e)


def _log_sdp(direction: str, first: str, body: str) -> None:
    """Scrive l'SDP ricevuto, riga per riga, nel registro di diagnostica.

    Finora del corpo non restava traccia: si registrava solo la riga di
    richiesta. Ma è nell'SDP che l'altro capo dichiara cosa sa fare — quali
    ritorni RTCP accetta, se vuole i pacchetti compatti, se multiplexa RTP e
    RTCP sulla stessa porta — e senza vederlo si finisce a dedurre dalle
    proprie offerte, che sono tutt'altra cosa.
    """
    if not body or not body.lstrip().startswith("v=0"):
        return
    _LOGGER.debug("[SDP %s] per %s:", direction, first)
    for line in body.strip().splitlines():
        _LOGGER.debug("[SDP %s]   %s", direction, line.strip())


async def _dispatch_message(raw: str):
    """Smista un messaggio SIP ricevuto (sia UDP che TCP)."""
    kind, hdrs, body, first = _parse(raw)
    cid = _call_id(hdrs)
    _LOGGER.debug("[SIP <<<] %s", first)
    _log_sdp("<<<", first, body)
    _learn_peer(hdrs, first)

    if isinstance(kind, int):
        if cid in pending_responses:
            _LOGGER.debug("reader: queuing response %d for cid=%s", kind, cid[:24])
            await pending_responses[cid].put(raw)
        elif cid.startswith("ping-"):
            # Risposta (es. 407) all'OPTIONS keepalive: _send_options_ping non
            # registra il proprio Call-ID in pending_responses per costruzione,
            # quindi è l'esito normale del keepalive, non un errore da segnalare.
            _LOGGER.debug("Keepalive response %d for cid=%s", kind, cid[:24])
        else:
            # Copie del relay (che manda tutto due volte) o risposte arrivate
            # dopo la fine della loro transazione: normali, non un guasto.
            _LOGGER.debug("Stale response %d for cid=%s", kind, cid[:24])
    elif isinstance(kind, str):
        await incoming_requests.put(raw)


async def _udp_reader_task():
    """Loop di lettura per modalità UDP locale."""
    loop = asyncio.get_running_loop()
    _LOGGER.info("SIP UDP reader started")
    last_ping = time.time()

    while True:
        try:
            data, addr = await asyncio.wait_for(
                loop.sock_recvfrom(_udp_sock, 65535), timeout=20)
            if _udp_target and addr[0] != _udp_target[0]:
                # In UDP chiunque in rete può mandare un datagramma: un finto
                # squillo, o un SDP che dirotta il media altrove. Parliamo solo
                # con il citofono configurato.
                _LOGGER.debug("SIP UDP da %s ignorato: non è il citofono", addr[0])
                continue
            raw = data.decode(errors="replace")
            await _dispatch_message(raw)
            last_ping = time.time()
        except asyncio.TimeoutError:
            # Keepalive: OPTIONS ogni 20 s se registrato
            if registered and (time.time() - last_ping) >= 20:
                await _send_options_ping()
                last_ping = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOGGER.error("SIP UDP reader error: %s", e)
            await asyncio.sleep(2)


# La connessione TLS in uso: connect() la sostituisce e alza l'evento, il
# lettore la legge finché muore. Il numero distingue una connessione nuova da
# quella appena caduta.
_conn_ready: asyncio.Event | None = None
_conn_gen = 0
_reconnect_task: asyncio.Task | None = None
_reconnect_lock: asyncio.Lock | None = None


def _conn_event() -> asyncio.Event:
    global _conn_ready
    if _conn_ready is None:
        _conn_ready = asyncio.Event()
    return _conn_ready


def reset_state() -> None:
    """Riporta il modulo allo stato di partenza (scaricamento dell'integrazione).

    Lo stato SIP sta in variabili di modulo, che sopravvivono a un ricaricamento:
    senza questo il nuovo hub partiva convinto di essere ancora in chiamata.
    """
    global _reconnect_task, _invite_idle_event
    if _reconnect_task is not None and not _reconnect_task.done():
        _reconnect_task.cancel()
    _reconnect_task = None
    _set_in_call(False)
    _set_calling(False)
    _set_registered(False)
    call_state.update(_DIALOG_RESET)
    pending_incoming["active"] = False
    pending_incoming["early"], pending_incoming["early_sdp"] = False, None
    pending_responses.clear()
    _invite_idle_event = None
    if _conn_ready is not None:
        _conn_ready.clear()


def _spawn_reconnect() -> None:
    """Fa partire una riconnessione in un task suo, se non ce n'è già una."""
    global _reconnect_task
    if _reconnect_task is None or _reconnect_task.done():
        _reconnect_task = asyncio.create_task(reconnect())


async def reader_task():
    """Legge la connessione TLS in uso; quando muore, chiede di rifarla.

    Il lettore non aspetta mai la riconnessione. Prima la aspettava: dentro
    c'è una REGISTER, la cui risposta arriva proprio da questo lettore, che
    però era fermo ad aspettare. Cinque tentativi da 15 s andavano a vuoto e
    si restava due o tre minuti senza squilli, fino al keepalive dell'hub.
    """
    if R.USE_LOCAL_UDP:
        await _udp_reader_task()
        return

    while True:
        await _conn_event().wait()
        gen, conn = _conn_gen, reader
        await _read_connection(conn)
        if gen == _conn_gen:
            # È caduta quella che stavamo leggendo, e nessuno l'ha già rifatta.
            _conn_event().clear()
            _set_registered(False)
            _spawn_reconnect()


async def _read_connection(conn) -> None:
    """Legge un flusso TLS finché si chiude, si rompe o manda spazzatura."""
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(conn.read(8192), timeout=30)
        except asyncio.TimeoutError:
            # Ping CRLF (RFC 5626): il proxy non deve credere morta la connessione.
            try:
                async with lock:
                    writer.write(b"\r\n\r\n")
                    await writer.drain()
            except Exception:  # noqa: BLE001
                _LOGGER.warning("CRLF keepalive failed, reconnecting...")
                return
            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("SIP reader error: %s, reconnecting...", e)
            return
        if not chunk:
            _LOGGER.warning("SIP connection closed by server, reconnecting...")
            return
        buf += chunk
        if len(buf) > MAX_SIP_BODY:
            _LOGGER.error("SIP TCP buffer overflow (>1 MB); reset connessione")
            return
        messages, buf = _split_stream(buf)
        for raw in messages:
            await _dispatch_message(raw)


# Ritrasmissione delle richieste non-INVITE su UDP (RFC 3261 §17.1.2.2, Timer E).
# L'app ufficiale non ne ha bisogno perché in locale usa TCP; noi usiamo UDP, e
# fino alla 1.0.6 ogni richiesta partiva una volta sola: un datagramma perso dava
# «REGISTER: nessuna risposta finale utile (0 risposte)» e due minuti di
# «Non registrato» fino al giro successivo del keepalive.
_T1 = 0.5   # primo intervallo di ritrasmissione (s)
_T2 = 4.0   # intervallo massimo (s)


async def _send_request(msg: str, cid: str, timeout: float = 15) -> list[str]:
    """Invia una richiesta non-INVITE e ne raccoglie le risposte fino alla finale.

    Due differenze rispetto a `send()` + `_wait_final()`:

    * la coda delle risposte esiste **prima** dell'invio: il reader scarta come
      «stale» le risposte di un Call-ID senza coda, e il citofono risponde in
      poche decine di millisecondi;
    * su UDP la richiesta viene **ritrasmessa identica** (stesso branch, quindi la
      stessa transazione per il server) a 0,5 - 1 - 2 - 4 - 4 … secondi, finché non
      arriva una risposta qualsiasi. Su TCP/TLS il trasporto è affidabile e non si
      ritrasmette.
    """
    q = pending_responses.setdefault(cid, asyncio.Queue())
    loop = asyncio.get_running_loop()
    await send(msg)
    deadline = loop.time() + timeout
    retransmit = bool(R.USE_LOCAL_UDP)
    interval = _T1
    next_tx = loop.time() + interval
    results: list[str] = []
    try:
        while True:
            now = loop.time()
            if now >= deadline:
                break
            wake = min(deadline, next_tx) if retransmit else deadline
            try:
                raw = await asyncio.wait_for(q.get(), timeout=max(0.0, wake - now))
            except asyncio.TimeoutError:
                if retransmit and loop.time() >= next_tx and loop.time() < deadline:
                    interval = min(interval * 2, _T2)
                    next_tx = loop.time() + interval
                    _LOGGER.debug("ritrasmissione cid=%s (prossima tra %.1fs)", cid[:24], interval)
                    await send(msg)
                continue
            results.append(raw)
            # Una risposta, anche provvisoria, dice che la richiesta è arrivata.
            retransmit = False
            kind = _parse(raw)[0]
            if isinstance(kind, int) and kind >= 200:
                break
    finally:
        pending_responses.pop(cid, None)
    return results


async def _wait_final(cid, timeout=15):
    q = pending_responses.setdefault(cid, asyncio.Queue())
    results = []
    deadline = time.time() + timeout
    while True:
        rem = deadline - time.time()
        if rem <= 0:
            break
        try:
            raw = await asyncio.wait_for(q.get(), timeout=min(rem, 3))
            results.append(raw)
            kind, *_ = _parse(raw)
            if isinstance(kind, int) and kind >= 200:
                break
        except asyncio.TimeoutError:
            continue
    pending_responses.pop(cid, None)
    return results


# ─── SDP ────────────────────────────────────────────────────────────

_local_crypto_key = None
_local_video_crypto_key = None


def build_sdp(enc: bool | None = None, *, video: bool = True, decline_video: bool = False):
    """Costruisce l'offerta/risposta SDP.

    ``enc`` decide fra SRTP (``RTP/SAVP`` + ``a=crypto``) e RTP in chiaro.
    Lasciato a ``None`` vale l'impostazione dell'impianto (``R.MEDIA_ENC``), che
    è ciò che serve quando siamo noi a fare l'offerta.

    Quando invece stiamo **rispondendo**, va passato ciò che l'altro capo ha
    offerto: gli impianti non sono uguali fra loro. Su 2F verificato in campo
    la targa baresip vuole RTP in chiaro e con SRTP non risponde affatto; su
    2FV2 l'INVITE arriva con ``RTP/SAVP`` e ``a=crypto``. Rispondere con un
    profilo diverso da quello offerto significa dire di aver accettato qualcosa
    che non useremo: qui il risultato era ricevere cifrato e trasmettere in
    chiaro.

    ``video=False`` toglie del tutto la sezione video, per i posti esterni che
    hanno solo microfono e altoparlante. ``decline_video=True`` la tiene ma con
    porta 0: è il modo previsto da RFC 3264 per rifiutare un flusso che ci è
    stato offerto, e va usato rispondendo, perché una risposta deve avere le
    stesse sezioni dell'offerta nello stesso ordine.
    """
    global _local_crypto_key, _local_video_crypto_key
    sid = str(int(time.time()))
    import base64 as _b64

    enc = bool(getattr(R, "MEDIA_ENC", False)) if enc is None else bool(enc)
    proto = "RTP/SAVP" if enc else "RTP/AVP"

    if enc:
        _local_crypto_key = _b64.b64encode(os.urandom(30)).decode()
        _local_video_crypto_key = _b64.b64encode(os.urandom(30)).decode()
        audio_crypto = f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_local_crypto_key}\r\n"
        video_crypto = f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_local_video_crypto_key}\r\n"
    else:
        # RTP in chiaro: nessuna chiave locale → setup_media non crea srtp_tx/rx.
        _local_crypto_key = None
        _local_video_crypto_key = None
        audio_crypto = ""
        video_crypto = ""

    sdp = (
        f"v=0\r\n"
        f"o=- {sid} {sid} IN IP4 {MY_IP}\r\n"
        f"s=Talk\r\n"
        f"c=IN IP4 {MY_IP}\r\n"
        f"b=AS:512\r\n"
        f"t=0 0\r\n"
        f"a=rtcp-xr:rcvr-rtt=all:10000 stat-summary=loss,dup,jitt,TTL voip-metrics\r\n"
        f"m=audio {C.RTP_AUDIO_PORT} {proto} 0 8 101\r\n"
        f"a=rtpmap:0 PCMU/8000\r\n"
        f"a=rtpmap:8 PCMA/8000\r\n"
        f"a=rtpmap:101 telephone-event/8000\r\n"
        f"a=fmtp:101 0-15\r\n"
        f"a=ptime:20\r\n"
        f"a=sendrecv\r\n"
        f"{audio_crypto}"
    )

    if decline_video:
        # Porta 0 = "questo flusso non lo voglio", mantenendo la sezione.
        return sdp + f"m=video 0 {proto} 96\r\na=rtpmap:96 H264/90000\r\n"
    if not video:
        return sdp
    return sdp + (
        f"m=video {C.RTP_VIDEO_PORT} {proto} 96\r\n"
        f"b=AS:256\r\n"
        f"a=rtpmap:96 H264/90000\r\n"
        f"a=fmtp:96 profile-level-id=42801F;packetization-mode=1\r\n"
        f"a=rtcp-fb:96 ccm fir\r\n"
        f"a=rtcp-fb:96 nack\r\n"
        f"a=rtcp-fb:96 nack pli\r\n"
        f"a=sendrecv\r\n"
        f"{video_crypto}"
    )


# ─── Operations ─────────────────────────────────────────────────────

def _record_bindings(hdrs) -> None:
    """Annota chi risulta registrato sull'account, noi compresi.

    È l'unico momento in cui il registrar elenca anche i dispositivi che in
    questo momento non stanno inviando nulla.
    """
    try:
        DEVICES.forget_bindings()
        for contact in _split_contacts(hdrs):
            DEVICES.note_binding(
                contact, own_device_id=R.DEVICE_IMEI, own_name=R.DEVICE_NAME,
            )
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("Device inventory (bindings) skipped: %s", e)


def _remember_granted_expiry(hdrs) -> None:
    """Record the lifetime the registrar granted, preferring our own contact."""
    global granted_expiry
    granted_expiry = 0
    values = _split_contacts(hdrs)
    instance = f"urn:uuid:{R.DEVICE_UUID}" if R.DEVICE_UUID else ""
    ours = f"{MY_IP}:{_my_port()}" if MY_IP else ""
    matching = [
        v for v in values
        if (instance and instance in v) or (ours and ours in v)
    ]
    offered = [
        int(m.group(1))
        for m in (re.search(r";\s*expires\s*=\s*(\d+)", v or "") for v in matching)
        if m
    ]
    if offered:
        granted_expiry = offered[0]
    elif values:
        # Più dispositivi condividono questo utente SIP. Se non riconosciamo il
        # nostro binding, la durata residua degli altri non dice nulla di noi:
        # prenderla porterebbe a rinnovare ogni pochi secondi, a raffica, per
        # sempre. Meglio l'intestazione Expires, e in mancanza il default.
        _LOGGER.debug("Binding nostro non riconoscibile fra %d contatti", len(values))
    if not granted_expiry and re.fullmatch(r"\s*\d+\s*", str(hdrs.get("expires", ""))):
        granted_expiry = int(str(hdrs["expires"]).strip())
    if not granted_expiry:
        granted_expiry = 3600


async def do_register():
    # In UDP mode writer is always None — skip the TLS connect check
    if R.USE_LOCAL_UDP:
        if _udp_sock is None:
            await connect()
    elif not writer or writer.is_closing():
        await connect()

    global local_tag
    local_tag = _gen("")
    cid = _gen("reg-")
    uri = f"sip:{R.SIP_DOMAIN}"

    def _msg(auth=None, seq=1):
        branch = _gen()
        m = (f"REGISTER {uri} SIP/2.0\r\n"
             f"{_via_line(branch)}"
             f"{_route_line()}"
             f"Max-Forwards: 70\r\n"
             f"To: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>\r\n"
             f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={local_tag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} REGISTER\r\n"
             f"Contact: {_contact_hdr()}\r\n"
             f"User-Agent: {C.USER_AGENT}\r\n"
             f"Mobile-IMEI: {R.DEVICE_IMEI}\r\n"
             f"MyName: {R.DEVICE_NAME}\r\n"
             f"Supported: replaces,outbound,gruu\r\n"
             f"Allow: INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,INFO,MESSAGE,UPDATE\r\n")
        if auth:
            m += f"Authorization: {auth}\r\n"
        return m + "Content-Length: 0\r\n\r\n"

    # Il registrar cambia il nonce fra un rinnovo e l'altro e risponde 401
    # senza stale=true: con un nonce nuovo si riprova. Lo stesso nonce due
    # volte vuol dire credenziali rifiutate, e riprovare girerebbe a vuoto.
    # Ogni uscita negativa azzera `registered`: fino alla 1.0.5 un fallimento
    # lasciava il flag a True e il citofono sembrava raggiungibile.
    seen_nonces: set[str] = set()
    auth = None
    resps: list[str] = []
    for _attempt in range(3):
        resps = await _send_request(_msg(auth=auth, seq=_next_cseq()), cid)
        final = None
        for r in resps:
            code, hdrs, *_ = _parse(r)
            if isinstance(code, int) and code >= 200:
                final = (code, hdrs)
        if final is None:
            break
        code, hdrs = final
        if code == 200:
            _remember_granted_expiry(hdrs)
            _record_bindings(hdrs)
            _set_registered(True)
            _LOGGER.info("SIP registered successfully (expires in %ds)", granted_expiry)
            return True
        if code not in (401, 407):
            _LOGGER.warning("SIP registration rejected: %s", code)
            _set_registered(False)
            return False
        challenge = hdrs.get("www-authenticate" if code == 401 else "proxy-authenticate", "")
        if not challenge:
            _LOGGER.warning("REGISTER: %s senza challenge, registrazione fallita", code)
            _set_registered(False)
            return False
        nonce = _challenge_nonce(challenge)
        if nonce in seen_nonces:
            _LOGGER.warning("SIP registration refused: credentials rejected")
            _set_registered(False)
            return False
        seen_nonces.add(nonce)
        auth = _make_auth("REGISTER", uri, challenge)

    # Diagnostica: "0 risposte" = nulla è tornato nemmeno dopo le ritrasmissioni;
    # altrimenti diciamo quali codici sono arrivati.
    codes = [_parse(r)[0] for r in resps]
    _LOGGER.warning(
        "REGISTER: nessuna risposta finale utile (%d risposte%s) verso %s via %s",
        len(resps), f": {codes}" if codes else "",
        R.LOCAL_PROXY if R.USE_LOCAL_UDP else R.SIP_PROXY,
        "UDP" if R.USE_LOCAL_UDP else "TLS",
    )
    _set_registered(False)
    return False


async def do_system_message(target_uri, body_text, extra_headers=None):
    if not registered:
        _LOGGER.warning("do_system_message: not registered, target=%s body=%s", target_uri, body_text)
        return False, "Non registrato"
    _LOGGER.info("do_system_message: target=%s body=%s headers=%s", target_uri, body_text, extra_headers)
    ftag = _gen("")
    # Come l'app ufficiale (MakeCallModel/SystemMsg): Call-ID = 10 caratteri alfanumerici
    cid = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))

    def _msg(auth=None, seq=1):
        branch = _gen()
        m = (f"MESSAGE {target_uri} SIP/2.0\r\n"
             f"{_via_line(branch)}"
             f"{_route_line()}"
             f"Max-Forwards: 70\r\n"
             f"To: <{target_uri}>\r\n"
             f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} MESSAGE\r\n"
             f"Contact: {_simple_contact()}\r\n"
             f"User-Agent: {C.USER_AGENT}\r\n"
             f"Mobile-IMEI: {R.DEVICE_IMEI}\r\n"
             f"MyName: {R.DEVICE_NAME}\r\n")
        if extra_headers:
            for k, v in extra_headers.items():
                m += f"{k}: {v}\r\n"
        if auth:
            m += f"Proxy-Authorization: {auth}\r\n"
        m += (f"Content-Type: text/plain\r\n"
              f"Content-Length: {_clen(body_text)}\r\n\r\n{body_text}")
        return m

    for r in await _send_request(_msg(seq=_next_cseq()), cid, timeout=15):
        code, hdrs, *_ = _parse(r)
        _LOGGER.info("do_system_message: response %s for %s", code, target_uri)
        if code and code < 200:
            continue
        if code in (401, 407):
            ch = hdrs.get("proxy-authenticate", "") or hdrs.get("www-authenticate", "")
            if not ch:
                return False, f"Auth vuoto ({code})"
            auth = _make_auth("MESSAGE", target_uri, ch)
            for r2 in await _send_request(_msg(auth=auth, seq=_next_cseq()), cid, timeout=15):
                c2 = _parse(r2)[0]
                _LOGGER.info("do_system_message: auth response %s for %s", c2, target_uri)
                if c2 and 200 <= c2 < 300:
                    return True, f"OK ({c2})"
                if c2 and c2 >= 300:
                    return False, f"Errore: {c2}"
            return False, "Timeout"
        if code and 200 <= code < 300:
            return True, f"OK ({code})"
        if code and code >= 300:
            return False, f"Errore: {code}"
    return False, "Timeout"


async def do_call(target=None):
    """INVITE a SIP target, lasciando sempre lo stato coerente.

    Il flag `calling` veniva alzato all'inizio e abbassato solo dai percorsi
    che finiscono bene o in do_hangup. Se la scrittura sul socket TLS falliva
    — e succede, perché il relay chiude la connessione e si riconnette — la
    funzione usciva per eccezione con `calling` ancora alzato, per sempre. Da
    quel momento ogni apertura di vista trovava "già in chiamata", non
    chiamava nessuno e scadeva: HomeKit mostrava "Nessuna risposta" fino a un
    riaggancio manuale o a un riavvio.
    """
    # Una transazione INVITE alla volta. Dopo un CANCEL la vecchia resta
    # aperta finché non arriva il 487 (poche decine di millisecondi): una
    # chiamata nuova partita in quella finestra si vedeva azzerare `calling`
    # dal finally della vecchia, o scrivere nel proprio stato il 200 di
    # quella.
    idle = _invite_idle()
    if not idle.is_set():
        try:
            await asyncio.wait_for(idle.wait(), INVITE_SETTLE)
        except asyncio.TimeoutError:
            return False, "Chiamata precedente ancora in chiusura"
    idle.clear()
    try:
        return await _do_call_inner(target)
    finally:
        idle.set()
        if not in_call:
            _set_calling(False)


# Quanto una chiamata nuova aspetta che la transazione INVITE precedente
# (annullata) si chiuda.
INVITE_SETTLE = 5.0
_invite_idle_event: asyncio.Event | None = None


def _invite_idle() -> asyncio.Event:
    global _invite_idle_event
    if _invite_idle_event is None:
        _invite_idle_event = asyncio.Event()
        _invite_idle_event.set()
    return _invite_idle_event


async def _do_call_inner(target=None):
    """INVITE a SIP target (default: intercom targa 55001)."""
    if not registered:
        _LOGGER.error("do_call: NOT registered")
        return False, "Non registrato"
    if in_call or calling:
        _LOGGER.error("do_call: already in call/calling")
        return False, "Già in chiamata"

    _set_calling(True)
    target_uri = target or R.INTERCOM
    _LOGGER.info("do_call: target=%s", target_uri)
    ftag = _gen("")
    cid = _gen("call-")
    sdp = build_sdp(video=bool(getattr(R, "VIDEO_ENABLED", True)))
    call_state.update(_DIALOG_RESET)
    call_state["call_id"] = cid
    call_state["from_tag"] = ftag
    call_state["original_target"] = target_uri
    call_state["local_sdp"] = sdp

    vimar_callid = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))

    def _inv(auth=None, seq=1):
        branch = _gen()
        call_state["invite_branch"], call_state["invite_cseq"] = branch, seq
        m = (f"INVITE {target_uri} SIP/2.0\r\n"
             f"{_via_line(branch)}"
             f"{_route_line()}"
             f"Max-Forwards: 70\r\n"
             f"To: <{target_uri}>\r\n"
             f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} INVITE\r\n"
             f"Contact: {_simple_contact()}"
             f';+sip.instance="<urn:uuid:{R.DEVICE_UUID}>"\r\n'
             f"User-Agent: {C.USER_AGENT}\r\n"
             f"Supported: replaces,outbound,gruu,timer\r\n"
             f"Allow: INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,INFO,MESSAGE,UPDATE\r\n"
             f"Session-Expires: 600;refresher=uas\r\n"
             f"Min-SE: 90\r\n")
        if auth:
            m += f"Proxy-Authorization: {auth}\r\n"
        m += (f"Mobile-IMEI: {R.DEVICE_IMEI}\r\n"
              f"MyName: {R.DEVICE_NAME}\r\n"
              f"X-Call-ID: {vimar_callid}\r\n"
              f"Content-Type: application/sdp\r\n"
              f"Content-Length: {_clen(sdp)}\r\n\r\n{sdp}")
        return m

    def _ack(to_tag, seq, in_dialog=False):
        """ACK di una risposta. Per un 2xx è una richiesta del dialogo
        (RFC 3261 §13.2.2.4): va al Contact della targa per la strada dei
        Record-Route, come il BYE; per un rifiuto segue l'INVITE."""
        branch = _gen()
        to_hdr = f"<{target_uri}>"
        if to_tag:
            to_hdr += f";tag={to_tag}"
        uri = (call_state.get("remote_contact") or target_uri) if in_dialog else target_uri
        route = call_state.get("route_set") if in_dialog else None
        return (f"ACK {uri} SIP/2.0\r\n"
                f"{_via_line(branch)}"
                f"{_route_line(route)}"
                f"Max-Forwards: 70\r\n"
                f"To: {to_hdr}\r\n"
                f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
                f"Call-ID: {cid}\r\n"
                f"CSeq: {seq} ACK\r\n"
                f"Content-Length: 0\r\n\r\n")

    cur_seq = _next_cseq()
    await send(_inv(seq=cur_seq))
    await broadcast("log", "INVITE inviato...")

    q = pending_responses.setdefault(cid, asyncio.Queue())
    _LOGGER.debug("do_call: cid=%s, q id=%s, pending_keys=%s", cid[:24], id(q), list(pending_responses.keys())[:3])
    deadline = time.time() + 45

    try:
        while time.time() < deadline:
            _LOGGER.debug("do_call: waiting q.get (qsize=%d, cid_in_pending=%s, q_is_same=%s)",
                           q.qsize(), cid in pending_responses, pending_responses.get(cid) is q)
            try:
                raw = await asyncio.wait_for(q.get(), timeout=3)
            except asyncio.TimeoutError:
                _LOGGER.debug("do_call: q.get timeout (qsize=%d)", q.qsize())
                continue

            code, hdrs, body, first = _parse(raw)
            if "INVITE" not in hdrs.get("cseq", "").upper():
                continue    # es. il 200 del nostro CANCEL: non è la risposta all'INVITE
            ttag = _tag(hdrs.get("to", ""))
            _LOGGER.debug("do_call: response %s (body=%dB)", code, len(body) if body else 0)

            if code in (100, 180, 183):
                if code == 183 and body:
                    call_state["remote_sdp"] = parse_sdp(body)
                continue

            if code in (401, 407):
                await send(_ack(ttag, cur_seq))
                ch = hdrs.get("proxy-authenticate", "") or hdrs.get("www-authenticate", "")
                if not ch:
                    pending_responses.pop(cid, None)
                    _set_calling(False)
                    return False, f"Auth vuoto ({code})"
                auth = _make_auth("INVITE", target_uri, ch)
                cur_seq = _next_cseq()
                await send(_inv(auth=auth, seq=cur_seq))
                continue

            if 200 <= code < 300:
                call_state["to_tag"] = ttag
                raw_contact = hdrs.get("contact", "")
                if "<" in raw_contact and ">" in raw_contact:
                    call_state["remote_contact"] = raw_contact[raw_contact.index("<")+1:raw_contact.index(">")]
                else:
                    call_state["remote_contact"] = raw_contact
                # Da chiamanti la strada del dialogo è l'elenco dei Record-Route
                # AL CONTRARIO (RFC 3261 §12.1.2). Va usata per tutto ciò che
                # viaggia dentro il dialogo, BYE compreso.
                call_state["route_set"] = list(
                    reversed(hdrs.get("_record_route_all") or []))
                await send(_ack(ttag, cur_seq, in_dialog=True))

                if call_state.get("cancelled"):
                    # Il 200 ha incrociato il nostro CANCEL: la chiamata c'è,
                    # ma nessuno la vuole più. Si chiude subito con un BYE,
                    # invece di lasciare la luce della targa accesa per niente.
                    _set_in_call(True)
                    await do_hangup()
                    return False, "Annullata"

                if body:
                    remote = parse_sdp(body)
                    call_state["remote_sdp"] = remote
                    _LOGGER.info("SDP: audio=%s video=%s", remote.get('audio', {}), remote.get('video', {}))
                    await media.setup_media(remote, _local_crypto_key, _local_video_crypto_key)

                _set_in_call(True)
                _set_calling(False)
                await broadcast("call_started", "Connesso!")
                # Request keyframe immediately — no delay
                await send_keyframe_request()
                pending_responses.pop(cid, None)
                return True, "Connesso!"

            if code >= 300:
                if code == 487 and call_state.get("cancelled"):
                    _LOGGER.info("Chiamata annullata prima della risposta")
                else:
                    _LOGGER.warning("INVITE rejected: %d", code)
                await send(_ack(ttag, cur_seq))
                pending_responses.pop(cid, None)
                _set_calling(False)
                reason = first.split(" ", 2)[2] if first.count(" ") >= 2 else str(code)
                return False, f"{code} {reason}"

        pending_responses.pop(cid, None)
        _set_calling(False)
        _LOGGER.error("INVITE timeout (45s) for %s", target_uri)
        return False, "Timeout (45s)"
    finally:
        # Ogni uscita dalla transazione — return, timeout o eccezione sollevata
        # da parse_sdp()/setup_media() dopo il 200 OK — deve liberare la coda e
        # azzerare `calling`: senza questo il flag resta True per sempre e la
        # guardia a inizio funzione blocca ogni chiamata successiva.
        pending_responses.pop(cid, None)
        _set_calling(False)


async def send_keyframe_request():
    """Send SIP INFO picture_fast_update to get a video keyframe (SPS/PPS).

    L'INFO è in-dialog: request-URI = Contact del peer (55100/targa), non
    l'hardcode R.INTERCOM (55001). Gestisce la challenge 407/401 rispedendo
    con Proxy-Authorization (come MESSAGE/INVITE) invece di lasciare il proxy
    a rifiutare a ripetizione ("Stale response 407")."""
    if not in_call or not call_state["call_id"]:
        return
    # Request-URI: il Contact reale del peer della chiamata attiva.
    info_target = call_state.get("remote_contact") or call_state.get("original_target") or R.INTERCOM
    to_uri = call_state.get("original_target") or R.INTERCOM
    cid = call_state["call_id"]
    body = ('<?xml version="1.0" encoding="utf-8" ?>'
            '<media_control><vc_primitive><to_encoder>'
            '<picture_fast_update></picture_fast_update>'
            '</to_encoder></vc_primitive></media_control>')

    def _info(auth=None):
        seq = _next_cseq()
        m = (
            f"INFO {info_target} SIP/2.0\r\n"
            f"{_via_line(_gen())}"
            f"{_route_line(call_state.get('route_set'))}"
            f"Max-Forwards: 70\r\n"
            f"To: <{to_uri}>;tag={call_state['to_tag']}\r\n"
            f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={call_state['from_tag']}\r\n"
            f"Call-ID: {cid}\r\n"
            f"CSeq: {seq} INFO\r\n")
        if auth:
            m += f"Proxy-Authorization: {auth}\r\n"
        m += (f"Content-Type: application/media_control+xml\r\n"
              f"Content-Length: {_clen(body)}\r\n\r\n{body}")
        return m

    # Registra la coda PRIMA di inviare: la risposta arriva sul Call-ID del
    # dialog attivo, che _wait_final consuma; senza questo il reader la scarta
    # come "Stale response".
    pending_responses.setdefault(cid, asyncio.Queue())
    await send(_info())
    _LOGGER.debug("Sent INFO picture_fast_update → %s", info_target)

    # Attendi risposta breve; su 407/401 rimanda con auth (una volta sola).
    #
    # Le risposte che non sono per il nostro INFO (es. il 200 di un BYE, se un
    # hangup concorrente condivide la coda del dialog) vanno restituite alla coda,
    # ma solo **dopo** il ciclo. Fino alla 1.0.6 venivano rimesse in coda subito e
    # rilette al giro successivo: da Python 3.12 `asyncio.wait_for` su una coda già
    # piena non sospende, quindi il ciclo girava senza mai cedere l'event loop fino
    # alla scadenza — Home Assistant fermo 3 secondi, ogni 5 secondi, per tutta la
    # chiamata (misurato: 0,01 s su 3.11, 3,01 s su 3.12 e 3.13).
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3
    foreign: list[str] = []
    try:
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(
                    pending_responses[cid].get(), timeout=min(1.0, deadline - loop.time())
                )
            except (asyncio.TimeoutError, KeyError):
                break
            code, hdrs, *_ = _parse(raw)
            if "INFO" not in hdrs.get("cseq", "INFO"):
                foreign.append(raw)
                continue
            if not isinstance(code, int) or code < 200:
                continue
            if code in (401, 407):
                ch = hdrs.get("proxy-authenticate", "") or hdrs.get("www-authenticate", "")
                if ch:
                    auth = _make_auth("INFO", info_target, ch)
                    await send(_info(auth=auth))
                    _LOGGER.debug("Re-sent INFO with Proxy-Authorization after %d", code)
            break
    finally:
        queue = pending_responses.get(cid)
        for raw in foreign:
            if queue is None:
                break
            try:
                queue.put_nowait(raw)
            except asyncio.QueueFull:
                break
    # Nota: NON facciamo pop() qui — un do_call/_wait_final concorrente potrebbe
    # possedere la stessa coda. La coda del dialog viene ripulita a hangup.


async def _cancel_outgoing() -> None:
    """Annulla l'INVITE ancora in squillo (RFC 3261 §9.1).

    Prima un riaggancio durante lo squillo abbassava solo i flag: la chiamata
    si connetteva lo stesso, senza nessuno a guardare, e la targa restava
    accesa fino al suo timeout. Il CANCEL ripete Request-URI, Call-ID, From,
    To e il branch dell'INVITE; la targa risponde 487 e _do_call_inner fa
    l'ACK come per ogni rifiuto.
    """
    cid = call_state.get("call_id")
    branch, seq = call_state.get("invite_branch"), call_state.get("invite_cseq")
    target_uri = call_state.get("original_target")
    if not (cid and branch and seq and target_uri):
        return
    call_state["cancelled"] = True
    await send(
        f"CANCEL {target_uri} SIP/2.0\r\n"
        f"{_via_line(branch)}"
        f"{_route_line()}"
        f"Max-Forwards: 70\r\n"
        f"To: <{target_uri}>\r\n"
        f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={call_state['from_tag']}\r\n"
        f"Call-ID: {cid}\r\n"
        f"CSeq: {seq} CANCEL\r\n"
        f"Content-Length: 0\r\n\r\n")
    _LOGGER.info("CANCEL inviato per la chiamata in squillo %s", cid[:24])


async def do_hangup():
    if calling and not in_call:
        try:
            await _cancel_outgoing()
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("CANCEL non inviato: %s", e)
    _set_calling(False)
    if not in_call or not call_state["call_id"]:
        _set_in_call(False)
        await media.stop_media()
        await broadcast("call_ended", "Chiamata terminata")
        return

    cid = call_state["call_id"]
    ftag = call_state["from_tag"] or local_tag or _gen("")
    ttag = call_state["to_tag"] or ""

    target_uri = call_state.get("remote_contact") or R.INTERCOM
    to_uri = call_state.get("original_target") or R.INTERCOM
    to_hdr = f"<{to_uri}>"
    if ttag:
        to_hdr += f";tag={ttag}"

    bye = (f"BYE {target_uri} SIP/2.0\r\n"
           f"{_via_line(_gen())}"
           f"{_route_line(call_state.get('route_set'))}"
           f"Max-Forwards: 70\r\n"
           f"To: {to_hdr}\r\n"
           f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
           f"Call-ID: {cid}\r\n"
           f"CSeq: {_next_cseq()} BYE\r\n"
           f"User-Agent: {C.USER_AGENT}\r\n"
           f"Content-Length: 0\r\n\r\n")
    await send(bye)
    await _wait_final(cid, timeout=5)

    if call_state.get("call_id") and cid != call_state["call_id"]:
        # Mentre aspettavamo la risposta al BYE è partita un'altra chiamata:
        # lo stato adesso è suo, e non va toccato.
        _LOGGER.debug("Chiamata %s chiusa, ma nel frattempo ne è partita un'altra", cid[:24])
        return

    pending_responses.pop(cid, None)
    _set_in_call(False)
    call_state.update(_DIALOG_RESET)
    await media.stop_media()
    await broadcast("call_ended", "Chiamata terminata")


async def do_options(target=None):
    if not registered:
        return False, "Non registrato"
    target = target or R.INTERCOM
    ftag = _gen("")
    cid = _gen("opt-")

    def _msg(auth=None, seq=1):
        branch = _gen()
        m = (f"OPTIONS {target} SIP/2.0\r\n"
             f"{_via_line(branch)}"
             f"{_route_line()}"
             f"Max-Forwards: 70\r\n"
             f"To: <{target}>\r\n"
             f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} OPTIONS\r\n"
             f"User-Agent: {C.USER_AGENT}\r\n"
             f"Accept: application/sdp\r\n")
        if auth:
            m += f"Proxy-Authorization: {auth}\r\n"
        return m + "Content-Length: 0\r\n\r\n"

    await send(_msg(seq=_next_cseq()))
    for r in await _wait_final(cid):
        code, hdrs, *_ = _parse(r)
        if code and code < 200:
            continue
        if code in (401, 407):
            ch = hdrs.get("proxy-authenticate", "") or hdrs.get("www-authenticate", "")
            if not ch:
                return False, "Auth vuoto"
            await send(_msg(auth=_make_auth("OPTIONS", target, ch), seq=_next_cseq()))
            for r2 in await _wait_final(cid):
                c2 = _parse(r2)[0]
                if c2 and 200 <= c2 < 300:
                    return True, f"OK: {c2}"
                return False, f"Errore: {c2}"
            return False, "Timeout"
        if code and 200 <= code < 300:
            return True, f"OK: {code}"
        if code and code >= 300:
            return False, f"Errore: {code}"
    return False, "Timeout"


async def do_connect_profiles():
    """Register push profile on Vimar cloud. Uses Digest auth (not Basic)."""
    username = f"{R.SIP_USER}@{R.SIP_DOMAIN}"
    body = [{"sipid": R.SIP_USER, "domain": R.SIP_DOMAIN, "pntok": C.PN_TOKEN}]
    if not C.PN_TOKEN:
        return False, "No FCM token"

    import requests as req_lib
    loop = asyncio.get_running_loop()

    def _call(endpoint):
        return req_lib.post(
            f"https://ipvdes.vimar.cloud/eipvdesUtils/{endpoint}",
            json=body,
            auth=req_lib.auth.HTTPDigestAuth(username, C.PN_TOKEN),
            headers={"Accept": "application/json"}, timeout=15)

    try:
        resp = await loop.run_in_executor(None, _call, "connectProfiles")
        _LOGGER.info("connectProfiles: %d", resp.status_code)
        if resp.status_code == 200:
            return True, "Profilo connesso"
        if resp.status_code == 403:
            await loop.run_in_executor(None, _call, "disconnectProfiles")
            resp3 = await loop.run_in_executor(None, _call, "connectProfiles")
            _LOGGER.info("connectProfiles retry: %d", resp3.status_code)
            if resp3.status_code == 200:
                return True, "Profilo connesso"
            return False, f"connectProfiles: {resp3.status_code}"
        return False, f"connectProfiles: {resp.status_code}"
    except Exception as e:
        _LOGGER.error("connectProfiles error: %s", e)
        return False, str(e)


# ─── Incoming SIP ───────────────────────────────────────────────────

pending_incoming = {
    "active": False, "cid": None, "from_hdr": None, "to_hdr": None,
    "cseq": None, "via_block": None, "my_tag": None,
    "caller_uri": None, "caller_tag": None, "body": None,
    "record_route": None, "caller_contact": None,
    "early": False, "early_sdp": None,
}


async def _ack_request(hdrs):
    """200 OK secco a una richiesta duplicata, senza rieseguirla."""
    try:
        await send(
            f"SIP/2.0 200 OK\r\n"
            f"{_via_block(hdrs)}To: {hdrs.get('to', '')}\r\nFrom: {hdrs.get('from', '')}\r\n"
            f"Call-ID: {_call_id(hdrs)}\r\nCSeq: {hdrs.get('cseq', '')}\r\n"
            f"Content-Length: 0\r\n\r\n")
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("Ack alla richiesta duplicata non riuscito: %s", e)


async def _resend_ringing():
    """Ripete il 180 sulla transazione già in squillo, senza rifare lo squillo."""
    p = pending_incoming
    if not p.get("active"):
        return
    try:
        await send(
            f"SIP/2.0 180 Ringing\r\n"
            f"{p['via_block']}To: {p['to_hdr']};tag={p['my_tag']}\r\nFrom: {p['from_hdr']}\r\n"
            f"Call-ID: {p['cid']}\r\nCSeq: {p['cseq']}\r\n"
            f"Contact: {_simple_contact()}\r\n"
            f"Content-Length: 0\r\n\r\n")
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("Ripetizione del 180 non riuscita: %s", e)


async def _answer_reinvite(hdrs) -> None:
    """Un re-INVITE nel dialogo in corso: si conferma la sessione com'è.

    Prima era trattato come uno squillo nuovo: notifica, 180, e lo stato della
    chiamata in corso sovrascritto. Si risponde con lo stesso SDP già mandato,
    così chiavi e porte non cambiano sotto il flusso.
    """
    sdp = call_state.get("local_sdp") or ""
    ctype = "Content-Type: application/sdp\r\n" if sdp else ""
    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{_via_block(hdrs)}To: {hdrs.get('to', '')}\r\nFrom: {hdrs.get('from', '')}\r\n"
        f"Call-ID: {_call_id(hdrs)}\r\nCSeq: {hdrs.get('cseq', '1 INVITE')}\r\n"
        f"Contact: {_simple_contact()}\r\n"
        f"{ctype}Content-Length: {_clen(sdp)}\r\n\r\n{sdp}")
    _LOGGER.info("re-INVITE nel dialogo in corso: sessione confermata")


async def handle_incoming_invite(raw):
    _, hdrs, body, first = _parse(raw)
    from_hdr = hdrs.get("from", "?")
    cid = _call_id(hdrs)
    via_block = _via_block(hdrs)
    to_hdr = hdrs.get("to", "")
    cseq = hdrs.get("cseq", "1 INVITE")

    if in_call and cid and cid == call_state.get("call_id"):
        await _answer_reinvite(hdrs)
        return

    caller_tag = _tag(from_hdr)
    caller_uri = ""
    if "<" in from_hdr and ">" in from_hdr:
        caller_uri = from_hdr[from_hdr.index("<")+1:from_hdr.index(">")]

    _LOGGER.info("Incoming INVITE from %s", caller_uri)

    my_tag = _gen("")

    pending_incoming.update(
        active=True, early=False, early_sdp=None,
        cid=cid, from_hdr=from_hdr, to_hdr=to_hdr,
        cseq=cseq, via_block=via_block, my_tag=my_tag,
        caller_uri=caller_uri, caller_tag=caller_tag, body=body,
        # Il bersaglio del dialogo è il Contact di chi chiama, non il suo
        # From: mandare il BYE al From si prende un "481 Call/transaction
        # does not exist" e la chiamata resta aperta. Misurato.
        caller_contact=_angle(hdrs.get("contact", "")) or caller_uri,
        # La strada del dialogo, per poterlo chiudere davvero più tardi.
        record_route=list(hdrs.get("_record_route_all") or []),
    )

    await send(
        f"SIP/2.0 180 Ringing\r\n"
        f"{via_block}To: {to_hdr};tag={my_tag}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Contact: {_simple_contact()}\r\n"
        f"Content-Length: 0\r\n\r\n")

    await broadcast("ring", f"Chiamata da: {caller_uri}")

    # Lo squillo è il momento buono per cominciare a ricevere: mentre la
    # persona sente la notifica, prende il telefono e apre l'app passano
    # secondi che oggi buttiamo via. Chiedere il media adesso li recupera
    # tutti, senza rispondere e senza occupare il posto esterno.
    # Mai durante un'altra chiamata: build_sdp tira chiavi SRTP nuove e
    # setup_media riscrive i contesti del flusso che si sta guardando.
    if EARLY_MEDIA and not (in_call or calling):
        try:
            await do_early_media()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Media in anticipo non riuscito: %s", err)


# Prova: chiedere alla targa di mandare il flusso già allo squillo.
EARLY_MEDIA = True


async def do_early_media():
    """Chiede il flusso PRIMA che qualcuno risponda, con un 183 che porta l'SDP.

    Un 183 Session Progress con un corpo dice a chi chiama "comincia pure a
    mandare, non ho ancora alzato": è esattamente il caso di chi vuole vedere
    chi ha suonato prima di decidere. Se la targa lo onora, allo squillo il
    flusso è già in arrivo e un keyframe ce l'abbiamo da parte: chi apre il
    video dopo non aspetta né la chiamata (1,08 s dal relay) né il primo
    fotogramma della targa (0,45 s).

    Non è una risposta. La chiamata resta in squillo, nessuno la conta come
    risposta, e il posto esterno non viene occupato: se la targa lo ignora,
    un 183 provvisorio non cambia niente e si continua come prima.
    """
    p = pending_incoming
    if not p.get("active") or p.get("early"):
        return False
    if not p.get("body"):
        # Senza offerta non c'è niente a cui rispondere in anticipo.
        return False
    remote = parse_sdp(p["body"])
    if not (remote.get("video") or {}).get("port"):
        return False

    offered_enc = bool(
        (remote.get("audio") or {}).get("crypto_key")
        or (remote.get("video") or {}).get("crypto_key")
    )
    sdp = build_sdp(enc=offered_enc, video=True)
    await send(
        f"SIP/2.0 183 Session Progress\r\n"
        f"{p['via_block']}To: {p['to_hdr']};tag={p['my_tag']}\r\nFrom: {p['from_hdr']}\r\n"
        f"Call-ID: {p['cid']}\r\nCSeq: {p['cseq']}\r\n"
        f"Contact: {_simple_contact()}\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {_clen(sdp)}\r\n\r\n{sdp}")
    p["early"] = True
    # La risposta va tenuta ALLA LETTERA: il 200 OK dovrà rimandare questa,
    # non una ricostruita. Vedi do_answer_incoming.
    p["early_sdp"] = sdp
    await media.setup_media(remote, _local_crypto_key, _local_video_crypto_key)
    _LOGGER.info("183 con SDP mandato allo squillo: in attesa di media in anticipo")
    return True


async def do_answer_incoming():
    if not pending_incoming["active"]:
        return False, "Nessuna chiamata in arrivo"

    p = pending_incoming
    # Da qui la chiamata non è più "in attesa": va segnato subito, prima di
    # qualunque await, altrimenti la copia dell'INVITE che arriva nel frattempo
    # trova ancora lo stato di squillo e la fa rifiutare.
    pending_incoming["active"] = False
    # L'offerta va letta PRIMA di rispondere: il profilo media della risposta
    # deve essere quello offerto, non quello preferito da noi.
    remote = parse_sdp(p["body"]) if p["body"] else {}
    offered_enc = bool(
        (remote.get("audio") or {}).get("crypto_key")
        or (remote.get("video") or {}).get("crypto_key")
    )
    if p["body"]:
        _LOGGER.info(
            "Incoming offer: %s — answering in kind",
            "SRTP" if offered_enc else "RTP in chiaro",
        )
    # Un posto esterno solo audio non offre video: rispondere con una sezione
    # video inventata significa promettere un flusso che nessuno manderà.
    offered_video = bool((remote.get("video") or {}).get("port"))
    plant_has_video = bool(getattr(R, "VIDEO_ENABLED", True))
    # Senza corpo nell'INVITE la nostra risposta è a tutti gli effetti
    # un'offerta: lì il video lo decidiamo noi, non un'offerta che non c'è.
    want_video = (offered_video and plant_has_video) if p["body"] else plant_has_video
    if p.get("early_sdp") and want_video:
        # La stessa risposta già mandata nel 183, parola per parola. Il 183 e
        # il 200 OK dello stesso dialogo devono portare LA STESSA risposta
        # (RFC 3261 §13.2.1): build_sdp però tira chiavi SRTP nuove a ogni
        # giro, quindi ricostruirla qui significava offrire chiavi DIVERSE da
        # quelle di mezzo minuto prima. La targa se ne accorgeva e rinegoziava
        # il media: in strada la luce del posto esterno si accendeva e
        # spegneva, il video andava a scatti e l'audio non arrivava affatto.
        sdp = p["early_sdp"]
    else:
        sdp = build_sdp(
            enc=offered_enc if p["body"] else None,
            video=want_video,
            decline_video=offered_video and not want_video,
        )
    if p["body"] and not offered_video:
        _LOGGER.info("Chiamata solo audio: nessun flusso video offerto")

    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{p['via_block']}To: {p['to_hdr']};tag={p['my_tag']}\r\nFrom: {p['from_hdr']}\r\n"
        f"Call-ID: {p['cid']}\r\nCSeq: {p['cseq']}\r\n"
        f"Contact: {_simple_contact()}\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {_clen(sdp)}\r\n\r\n{sdp}")

    _set_in_call(True)
    call_state.update(_DIALOG_RESET)
    call_state["local_sdp"] = sdp
    call_state["call_id"] = p["cid"]
    call_state["from_tag"] = p["my_tag"]
    call_state["to_tag"] = p["caller_tag"]
    call_state["remote_contact"] = p.get("caller_contact") or p["caller_uri"]
    # Per il BYE (do_hangup) di una chiamata IN ARRIVO: il To: e la request-URI
    # devono puntare al CHIAMANTE, non alla targa di default (R.INTERCOM).
    call_state["original_target"] = p["caller_uri"]
    # Da chiamati vale l'ordine in cui sono arrivati, non al contrario
    # (RFC 3261 §12.1.1). Senza, il BYE di una chiamata dalla strada non
    # arriva, e il posto esterno resta occupato per due minuti.
    call_state["route_set"] = list(p.get("record_route") or [])

    if p["body"]:
        if not want_video:
            # Evita di aprire la ricezione video per un flusso che non esiste.
            remote = {**remote, "video": {}}
        call_state["remote_sdp"] = remote
        _LOGGER.info("Answer SDP: audio=%s video=%s", remote.get('audio'), remote.get('video'))
        if p.get("early") and p.get("early_sdp") and want_video:
            # Il media è già in piedi dallo squillo, con queste stesse chiavi:
            # rifarlo adesso vuol dire buttare giù i contesti SRTP e riaprirli
            # sotto un flusso che sta già scorrendo, cioè perdere i pacchetti
            # in volo proprio nell'istante in cui si guarda.
            _LOGGER.info("Media già in piedi dal 183: non lo si rifà")
        else:
            await media.setup_media(remote, _local_crypto_key, _local_video_crypto_key)

    await broadcast("call_started", "Chiamata attiva!")
    # Request keyframe for video
    await send_keyframe_request()
    return True, "Risposto!"


async def do_decline_incoming():
    if not pending_incoming["active"]:
        return

    p = pending_incoming
    await send(
        f"SIP/2.0 486 Busy Here\r\n"
        f"{p['via_block']}To: {p['to_hdr']};tag={p['my_tag']}\r\nFrom: {p['from_hdr']}\r\n"
        f"Call-ID: {p['cid']}\r\nCSeq: {p['cseq']}\r\n"
        f"Content-Length: 0\r\n\r\n")
    pending_incoming["active"] = False


async def handle_incoming_bye(raw):
    _, hdrs, *_ = _parse(raw)
    cid = _call_id(hdrs)
    from_hdr = hdrs.get("from", "")
    to_hdr = hdrs.get("to", "")
    cseq = hdrs.get("cseq", "1 BYE")

    if cid != call_state.get("call_id"):
        # Un BYE per un dialogo che non è il nostro (un altro dispositivo
        # dello stesso utente, o una chiamata già chiusa) non deve chiudere
        # quella in corso: RFC 3261 §15.1.2, 481.
        await send(
            f"SIP/2.0 481 Call/Transaction Does Not Exist\r\n"
            f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
            f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
            f"Content-Length: 0\r\n\r\n")
        _LOGGER.debug("BYE per un dialogo sconosciuto (%s): 481", cid[:24])
        return

    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Content-Length: 0\r\n\r\n")

    _set_in_call(False)
    call_state.update(_DIALOG_RESET)
    await media.stop_media()
    await broadcast("call_ended", "Chiamata terminata")


async def handle_incoming_options(raw):
    _, hdrs, *_ = _parse(raw)
    from_hdr = hdrs.get("from", "")
    to_hdr = hdrs.get("to", "")
    cid = _call_id(hdrs)
    cseq = hdrs.get("cseq", "1 OPTIONS")
    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Allow: INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,INFO,MESSAGE,UPDATE\r\n"
        f"Content-Length: 0\r\n\r\n")


async def handle_incoming_cancel(raw):
    _, hdrs, *_ = _parse(raw)
    cid = _call_id(hdrs)
    from_hdr = hdrs.get("from", "")
    to_hdr = hdrs.get("to", "")
    cseq = hdrs.get("cseq", "1 CANCEL")

    _LOGGER.info("Incoming CANCEL for %s", cid[:24])

    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Content-Length: 0\r\n\r\n")

    if pending_incoming["active"] and pending_incoming["cid"] == cid:
        p = pending_incoming
        invite_cseq = p["cseq"]
        await send(
            f"SIP/2.0 487 Request Terminated\r\n"
            f"{p['via_block']}To: {p['to_hdr']};tag={p['my_tag']}\r\nFrom: {p['from_hdr']}\r\n"
            f"Call-ID: {cid}\r\nCSeq: {invite_cseq}\r\n"
            f"Content-Length: 0\r\n\r\n")
        pending_incoming["active"] = False
        if p.get("early") and not in_call:
            # Il media aperto dal 183 non ha più una chiamata dietro.
            await media.stop_media()
        p["early"], p["early_sdp"] = False, None
        await broadcast("ring_ended", "Chiamata cancellata")


# Il relay consegna ogni richiesta DUE volte, a pochi millisecondi di distanza,
# con branch Via diversi ma stessa transazione. Senza filtro ogni squillo
# diventa due notifiche, ogni messaggio conta doppio, e la seconda INVITE
# sovrascrive lo stato della chiamata che stiamo già rispondendo.
_seen_requests: dict = {}
DUPLICATE_WINDOW = 30.0


def _is_duplicate(kind: str, hdrs) -> bool:
    """Vero se questa richiesta è la copia di una già presa in carico."""
    key = (kind, _call_id(hdrs), hdrs.get("cseq", ""))
    if not key[1]:
        return False
    now = time.monotonic()
    for old_key, seen_at in list(_seen_requests.items()):
        if now - seen_at > DUPLICATE_WINDOW:
            del _seen_requests[old_key]
    if key in _seen_requests:
        _seen_requests[key] = now
        return True
    _seen_requests[key] = now
    return False


async def request_processor():
    def _fire(coro, name: str):
        """Lancia un task e loga le eccezioni non gestite (evita eccezioni silenziate)."""
        t = asyncio.create_task(coro)
        t.add_done_callback(
            lambda t, n=name: _LOGGER.error("%s handler error: %s", n, t.exception())
            if not t.cancelled() and t.exception() else None
        )

    while True:
        raw = await incoming_requests.get()
        try:
            await _process_request(raw, _fire)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # Una risposta che non parte (connessione giù durante una
            # riconnessione) non deve fermare per sempre la lettura delle
            # richieste: prima da lì in poi nessuno squillo arrivava più.
            _LOGGER.warning("Richiesta SIP non gestita: %s", e)


async def _process_request(raw, _fire):
    kind, hdrs, body, first = _parse(raw)
    try:
        DEVICES.note_peer(hdrs, own_device_id=R.DEVICE_IMEI)
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("Device inventory (peer) skipped: %s", e)
    duplicate = _is_duplicate(kind, hdrs)
    if duplicate and kind in ("INVITE", "CANCEL", "BYE", "MESSAGE"):
        # Alla copia si risponde comunque — il mittente la ritrasmette
        # finché non riceve qualcosa — ma non la si esegue una seconda volta.
        _LOGGER.debug("Richiesta %s duplicata dal relay: ignorata", kind)
        if kind == "INVITE" and in_call and _call_id(hdrs) == call_state.get("call_id"):
            await _answer_reinvite(hdrs)
        elif kind == "INVITE" and pending_incoming.get("cid") == _call_id(hdrs):
            await _resend_ringing()
        elif kind in ("CANCEL", "BYE", "MESSAGE"):
            await _ack_request(hdrs)
        return
    if kind == "INVITE":
        _fire(handle_incoming_invite(raw), "INVITE")
    elif kind == "CANCEL":
        _fire(handle_incoming_cancel(raw), "CANCEL")
    elif kind == "BYE":
        _fire(handle_incoming_bye(raw), "BYE")
    elif kind == "OPTIONS":
        _fire(handle_incoming_options(raw), "OPTIONS")
    elif kind == "MESSAGE":
        _LOGGER.debug("SIP MESSAGE body=%r from=%s", body, hdrs.get("from",""))
        # Cap generoso (era 200: troncava GET_INIT_STATUS_REPLY ~266B → perdeva dnd/voicemail).
        await broadcast("message", (body or "")[:4096])
        from_hdr = hdrs.get("from", "")
        to_hdr = hdrs.get("to", "")
        msg_cid = hdrs.get("call-id", "")
        msg_cseq = hdrs.get("cseq", "1 MESSAGE")
        await send(
            f"SIP/2.0 200 OK\r\n"
            f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
            f"Call-ID: {msg_cid}\r\nCSeq: {msg_cseq}\r\n"
            f"Content-Length: 0\r\n\r\n")
    elif kind == "INFO":
        from_hdr = hdrs.get("from", "")
        to_hdr = hdrs.get("to", "")
        info_cid = hdrs.get("call-id", "")
        info_cseq = hdrs.get("cseq", "1 INFO")
        await send(
            f"SIP/2.0 200 OK\r\n"
            f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
            f"Call-ID: {info_cid}\r\nCSeq: {info_cseq}\r\n"
            f"Content-Length: 0\r\n\r\n")
    elif kind == "NOTIFY":
        # Il Tab può notificare cambi di stato (segreteria, DND, ...) via
        # NOTIFY. Catturiamo body + evento SIP e rispondiamo 200 OK.
        ev = hdrs.get("event", "")
        _LOGGER.info("SIP NOTIFY event=%s body=%s hdrs=%s",
                     ev, (body or "")[:300], first[:80])
        await broadcast("message", f"NOTIFY {ev}: {(body or '')[:200]}")
        from_hdr = hdrs.get("from", "")
        to_hdr = hdrs.get("to", "")
        n_cid = hdrs.get("call-id", "")
        n_cseq = hdrs.get("cseq", "1 NOTIFY")
        await send(
            f"SIP/2.0 200 OK\r\n"
            f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
            f"Call-ID: {n_cid}\r\nCSeq: {n_cseq}\r\n"
            f"Content-Length: 0\r\n\r\n")
    elif kind != "ACK":
        _LOGGER.debug("Unhandled SIP request: %s", kind)


# Si rinnova con questo anticipo sulla scadenza; su lease brevi l'anticipo si
# riduce in proporzione, e comunque non si rinnova più spesso di così.
REGISTER_MARGIN = 60
MIN_REGISTER_INTERVAL = 5


def _renew_delay() -> float:
    """Seconds to wait before renewing, from what the registrar granted.

    Con lease brevi sottrarre un margine fisso porta il rinnovo esattamente
    sulla scadenza (60 - 60 = 0, poi alzato dal minimo a 60): per questo il
    margine diventa proporzionale quando il lease è corto.
    """
    granted = granted_expiry or 3600
    margin = min(REGISTER_MARGIN, granted * 0.2)
    return max(MIN_REGISTER_INTERVAL, granted - margin)


async def register_refresh_task():
    """Keep the registration alive on every transport.

    Cloud registrations expire exactly like local ones. Without this the binding
    lapses after the granted lifetime and the intercom stops delivering calls
    until something else happens to force a reconnect.
    """
    while True:
        await asyncio.sleep(_renew_delay())
        if not registered:
            continue
        _LOGGER.info("SIP: periodic re-registration...")
        try:
            ok = await do_register()
            if not ok:
                _LOGGER.warning("SIP re-registration failed, reconnecting")
                _spawn_reconnect()
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("SIP re-registration error: %s", e)
