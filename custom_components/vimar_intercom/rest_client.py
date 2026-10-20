"""rest_client.py — l'API HTTP che il citofono espone in rete locale.

Modulo **puro**: nessun import di Home Assistant, così resta testabile senza HA
(vedi `tests/test_smoke.py`).

Il citofono serve tre endpoint su HTTP semplice, porta 80, autenticati con
Digest MD5 usando **le stesse credenziali SIP** che l'integrazione ha già nel
config entry — nessun token, nessun account Vimar, nessun accesso al cloud:

    GET /rest/get_info.php?action=status     → stato dell'appartamento in JSON
    GET /rest/get_info.php?action=nickname   → ruoli e interni configurati
    GET /rest/get_file.php?name=rubrica      → rubrica.db (SQLite)
    GET /rest/get_file.php?name=mailbox      → mailbox.db (SQLite)

Sono le stesse chiamate che fa l'app ufficiale quando è sulla rete di casa; la
via SIP (`GET_INIT_STATUS`, `GET_NICKS`) resta quella per gli impianti che si
raggiungono solo dal cloud.

Tre particolarità del server del citofono, verificate sul campo, che questo
modulo tiene a bada:

* Il `nonce` del Digest arriva come `nonce="b'MTc4OTg1NTExNA=='"`: è il
  `repr()` di un oggetto `bytes` di Python finito nell'header per sbaglio.
  Va rimandato **testualmente**, apici compresi. `requests` ricopia il nonce
  così com'è, quindi funziona senza accorgimenti — ma non provare a
  normalizzarlo.
* La risposta porta `Content-Encoding: none`, che non è un valore legale.
  Per questo il corpo si legge da `raw` con `decode_content=False`: lasciando
  fare a urllib3, un giorno potrebbe tentare una decompressione che non esiste.
* A una risorsa che non conosce il citofono risponde **401, non 404**. Quindi
  un 401 non significa per forza «credenziali sbagliate»: vedi `RestAuthError`.
"""

from __future__ import annotations

import json
import re
from typing import Any

import requests
from requests.auth import HTTPDigestAuth

# Percorso base dell'API sul citofono. Non configurabile: è il firmware.
_BASE_PATH = "/rest"

# Nomi accettati da get_file.php. Il ".db" **non** va messo: l'app lo toglie
# prima di comporre l'URL, e con ".db" il citofono risponde 401 (verificato).
DB_RUBRICA = "rubrica"
DB_MAILBOX = "mailbox"

# Valori di `action` accettati da get_info.php.
ACTION_STATUS = "status"
ACTION_NICKNAME = "nickname"

# Il citofono è un dispositivo domestico su rete locale: se non risponde in
# pochi secondi non risponderà affatto. La rubrica è ~200 KB, quindi il
# download ha un timeout di lettura più lungo.
DEFAULT_TIMEOUT = (4.0, 10.0)      # (connect, read)
DOWNLOAD_TIMEOUT = (4.0, 60.0)

# Ruolo del capogruppo nella risposta di `action=nickname`. È il PICG, cioè il
# destinatario di GET_INIT_STATUS e — su tutti gli impianti visti finora —
# anche l'SGA a cui vanno segreteria, DND e attuatori.
ROLE_PICG = "PICG"


class RestError(Exception):
    """L'API locale non ha risposto come previsto."""


class RestAuthError(RestError):
    """401 dal citofono.

    Attenzione: il citofono risponde 401 anche a un `name` o a un `action` che
    non conosce, quindi questo errore significa «credenziali rifiutate **o**
    risorsa inesistente». Non è possibile distinguere i due casi dal protocollo.
    """


class RestUnavailable(RestError):
    """Il citofono non è raggiungibile su HTTP (rete, porta chiusa, timeout)."""


def base_url(host: str) -> str:
    """URL base dell'API, dato l'indirizzo del citofono."""
    return f"http://{host}{_BASE_PATH}"


def _request(
    host: str,
    user: str,
    password: str,
    path: str,
    params: dict[str, str],
    *,
    timeout: tuple[float, float],
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """Una GET autenticata Digest, con gli errori tradotti nelle nostre eccezioni."""
    url = f"{base_url(host)}/{path}"
    try:
        resp = requests.get(
            url,
            params=params,
            auth=HTTPDigestAuth(user, password),
            timeout=timeout,
            stream=True,
            # Il server non parla gzip e dichiara un Content-Encoding illegale:
            # meglio non chiedere nulla.
            headers={"Accept-Encoding": "identity", **(headers or {})},
        )
    except requests.exceptions.RequestException as err:
        # Il messaggio di requests contiene l'URL ma non le credenziali
        # (stanno nell'header Authorization, non nell'URL).
        raise RestUnavailable(f"{host} unreachable: {err}") from err

    if resp.status_code == 401:
        raise RestAuthError(
            f"401 from {host}: SIP credentials rejected, or "
            f"{params} does not exist on this system"
        )
    if resp.status_code == 304:
        return resp
    if resp.status_code != 200:
        raise RestError(f"{host} answered {resp.status_code} to {path} {params}")
    return resp


def _body(resp: requests.Response) -> bytes:
    """Il corpo grezzo, senza lasciare che urllib3 provi a decodificarlo."""
    try:
        return resp.raw.read(decode_content=False)
    except Exception:  # noqa: BLE001 — raw può essere già consumato nei test
        return resp.content


def probe(host: str, timeout: float = 3.0) -> bool:
    """`True` se a quell'indirizzo c'è l'API del citofono.

    Non servono credenziali: basta la sfida Digest. Un impianto raggiungibile
    solo dal cloud, o con la porta 80 chiusa, restituisce `False` — e in quel
    caso l'integrazione deve usare la via SIP.
    """
    try:
        resp = requests.get(
            f"{base_url(host)}/get_info.php",
            params={"action": ACTION_STATUS},
            timeout=timeout,
        )
    except requests.exceptions.RequestException:
        return False
    if resp.status_code != 401:
        return False
    return "digest" in resp.headers.get("WWW-Authenticate", "").lower()


def get_status(
    host: str, user: str, password: str, timeout: tuple[float, float] = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Lo stato dell'appartamento, come `{param: valore}`.

    È lo stesso contenuto del `GET_INIT_STATUS_REPLY` SIP, servito in JSON e
    **senza troncamenti**: `rubrica_ver`, `vm_ver`, `vm_level`, `dnd`,
    `voicemail`, e sugli impianti che la mandano anche `vm_timeout`,
    `vm_timeout_values`, `apt_names`, `GID`, `media_enc`, `token`.

    I nomi dei parametri sono restituiti così come arrivano: il chiamante
    legge quelli che gli servono e ignora gli altri, perché variano col modello.
    """
    resp = _request(
        host, user, password, "get_info.php", {"action": ACTION_STATUS}, timeout=timeout
    )
    return parse_status(_body(resp))


def get_nicknames(
    host: str, user: str, password: str, timeout: tuple[float, float] = DEFAULT_TIMEOUT
) -> list[dict[str, str]]:
    """Gli interni configurati, come lista di `{"ROLE","EXT","NAME"}`.

    Equivalente HTTP di `GET_NICKS`. Attenzione: elenca i **nickname**
    configurati (capogruppo e posti interni), non la rubrica: le targhe
    normalmente non compaiono.
    """
    resp = _request(
        host, user, password, "get_info.php", {"action": ACTION_NICKNAME}, timeout=timeout
    )
    return parse_nicknames(_body(resp))


def download_db(
    host: str,
    user: str,
    password: str,
    name: str = DB_RUBRICA,
    *,
    dest: str | None = None,
    if_modified_since: str | None = None,
    timeout: tuple[float, float] = DOWNLOAD_TIMEOUT,
) -> tuple[bytes | None, str | None]:
    """Scarica `rubrica` o `mailbox` dal citofono.

    Restituisce `(contenuto, last_modified)`. Se `if_modified_since` è
    valorizzato con un `Last-Modified` ricevuto in precedenza e il file non è
    cambiato, il citofono risponde 304 e il contenuto è `None`: è il modo più
    economico di sapere se la rubrica è stata modificata, meglio che
    riscaricare 200 KB o calcolarne l'MD5.

    Con `dest` il contenuto viene anche scritto su quel percorso.
    """
    if name not in (DB_RUBRICA, DB_MAILBOX):
        raise ValueError(f"invalid name: {name!r}")

    headers = {"If-Modified-Since": if_modified_since} if if_modified_since else None
    resp = _request(
        host, user, password, "get_file.php", {"name": name}, timeout=timeout, headers=headers
    )
    last_modified = resp.headers.get("Last-Modified")
    if resp.status_code == 304:
        return None, last_modified

    data = _body(resp)
    if not data.startswith(b"SQLite format 3"):
        raise RestError(
            f"{host} answered 200 but the content is not an SQLite database "
            f"({len(data)} bytes)"
        )
    if dest:
        with open(dest, "wb") as fh:
            fh.write(data)
    return data, last_modified


# ─── Parser (puri: nessuna rete, testabili da soli) ──────────────────────────


def parse_status(raw: bytes | str) -> dict[str, Any]:
    """Da `[{"PARAM": "dnd", "VALUE": "0"}, …]` a `{"dnd": "0", …}`.

    Il formato è lo stesso del corpo di `GET_INIT_STATUS_REPLY;` (senza il
    prefisso), quindi questa funzione serve a entrambe le vie.
    """
    try:
        items = json.loads(raw)
    except (ValueError, TypeError) as err:
        raise RestError(f"not a JSON answer: {err}") from err
    if not isinstance(items, list):
        raise RestError("unexpected answer: a JSON array was expected")

    out: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        param = item.get("PARAM")
        if param is None:
            continue
        out[str(param)] = item.get("VALUE")
    return out


def parse_nicknames(raw: bytes | str) -> list[dict[str, str]]:
    """La lista di `action=nickname`, ripulita dalle voci malformate."""
    try:
        items = json.loads(raw)
    except (ValueError, TypeError) as err:
        raise RestError(f"not a JSON answer: {err}") from err
    if not isinstance(items, list):
        raise RestError("unexpected answer: a JSON array was expected")

    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict) or "EXT" not in item:
            continue
        out.append(
            {
                "role": str(item.get("ROLE", "")).strip(),
                "ext": str(item.get("EXT", "")).strip(),
                "name": str(item.get("NAME", "")).strip(),
            }
        )
    return out


def parse_nicks_reply(body: str) -> list[dict[str, str]]:
    """Il corpo di un `GET_NICKS_REPLY;[...]` arrivato via SIP.

    Stesso JSON di `action=nickname`, preceduto dal nome del comando. A
    differenza della via HTTP qui il testo può arrivare troncato (log, sensori
    che tagliano a 200 caratteri, o un MESSAGE spezzato): se l'array intero non
    è JSON valido si recuperano uno per uno gli oggetti completi, così la voce
    `PICG` — che il citofono manda per prima — non va persa. Non solleva mai.
    """
    raw = (body or "").strip()
    head, sep, rest = raw.partition(";")
    payload = rest if sep and head.strip().upper() == "GET_NICKS_REPLY" else raw
    try:
        return parse_nicknames(payload)
    except RestError:
        pass
    out: list[dict[str, str]] = []
    for chunk in re.findall(r"\{[^{}]*\}", payload):
        try:
            out.extend(parse_nicknames("[" + chunk + "]"))
        except RestError:
            continue
    return out


def find_picg(nicknames: list[dict[str, str]]) -> str | None:
    """L'interno del capogruppo, o `None` se non è dichiarato.

    È il valore da usare come `picg_target` (e, salvo impianti particolari,
    anche come `sga_target`): lo dichiara il citofono stesso, quindi non serve
    più né la rubrica né una scansione a tentativi.
    """
    for nick in nicknames:
        if nick.get("role", "").upper() == ROLE_PICG:
            ext = nick.get("ext", "")
            return ext or None
    return None


def vm_level(status: dict[str, Any]) -> tuple[int, int] | None:
    """`vm_level` come `(usati, capienza)`.

    Il citofono lo manda nella forma `"0/100"`: è il numero di videomessaggi
    registrati sul totale, non lo spazio libero.
    """
    raw = status.get("vm_level")
    if not isinstance(raw, str) or "/" not in raw:
        return None
    used, _, total = raw.partition("/")
    try:
        return int(used.strip()), int(total.strip())
    except ValueError:
        return None
