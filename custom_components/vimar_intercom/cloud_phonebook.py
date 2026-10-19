"""cloud_phonebook.py — la rubrica scaricata dal cloud Vimar col `token` (issue #5).

Modulo **puro**: nessun import di Home Assistant (vedi `tests/test_smoke.py`).

Ricetta verificata da @CPietro su un Tab 5S Up 40515 / 2FV2 in cloud; il file che
torna è identico byte per byte al `rubrica.db` che tiene l'app VIEW, quindi lo
legge `rubrica_import` così com'è:

    GET https://<cproxy>/phonebook/domains/<cdomain senza ".<cproxy>">/<rubrica_ver>
    Digest: utente = <cdomain COMPLETO>, password = <token>
    User-Agent: TOGA/2.4.0   (quello dell'app)

Il `cdomain` compare in **due forme nella stessa richiesta**: completo come utente
del Digest, senza il suffisso `.<cproxy>` nel percorso. Sbagliarne una dà 401 o 404.

`cdomain` e `cproxy` vengono dal QR (config entry); `token` e `rubrica_ver` dalla
risposta **lunga** a GET_INIT_STATUS, che non tutti gli impianti mandano (il 40507
manda quella corta, senza token). Il token vale come una password: non si scrive
nei log né nel config entry, si rilegge da un GET_INIT_STATUS nuovo.
"""

from __future__ import annotations

import requests
from requests.auth import HTTPDigestAuth

USER_AGENT = "TOGA/2.4.0"
TIMEOUT = (6.0, 60.0)   # (connect, read): la rubrica è ~200 KB


class CloudPhonebookError(Exception):
    """Download non riuscito; il messaggio è per l'utente e non contiene il token."""


class CloudAuthError(CloudPhonebookError):
    """401/403: token scaduto o non valido per questo impianto."""


def path_domain(cdomain: str, cproxy: str) -> str:
    """Il `cdomain` senza il suffisso `.<cproxy>`, come vuole il percorso."""
    cdomain, cproxy = cdomain.strip(), cproxy.strip()
    suffix = "." + cproxy
    return cdomain[: -len(suffix)] if cproxy and cdomain.endswith(suffix) else cdomain


def phonebook_url(cdomain: str, cproxy: str, rubrica_ver: str) -> str:
    return f"https://{cproxy.strip()}/phonebook/domains/{path_domain(cdomain, cproxy)}/{rubrica_ver.strip()}"


def check_inputs(cdomain: str | None, cproxy: str | None, token: str | None,
                 rubrica_ver: str | None) -> str | None:
    """Il primo dato mancante, per dire all'utente cosa manca; None se c'è tutto."""
    for name, value in (("cdomain", cdomain), ("cproxy", cproxy),
                        ("token", token), ("rubrica_ver", rubrica_ver)):
        if not value or not str(value).strip():
            return name
    # Niente caratteri che cambiano il senso dell'URL: vengono dal QR e dall'impianto.
    for value in (cdomain, cproxy, rubrica_ver):
        if any(c in str(value) for c in "/?#@ \r\n"):
            return "format"
    return None


def download(cdomain: str, cproxy: str, token: str, rubrica_ver: str, *,
             session: requests.Session | None = None, timeout=TIMEOUT) -> bytes:
    """Scarica la rubrica. Bloccante: dal config flow va in executor."""
    missing = check_inputs(cdomain, cproxy, token, rubrica_ver)
    if missing:
        raise CloudPhonebookError(f"missing or invalid value: {missing}")
    url = phonebook_url(cdomain, cproxy, rubrica_ver)
    http = session or requests
    try:
        resp = http.get(url, auth=HTTPDigestAuth(cdomain.strip(), token.strip()),
                        headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException as exc:
        raise CloudPhonebookError(f"{cproxy} unreachable: {type(exc).__name__}") from None
    if resp.status_code in (401, 403):
        raise CloudAuthError(f"{cproxy} rejected the token (HTTP {resp.status_code})")
    if resp.status_code == 404:
        raise CloudPhonebookError(f"{cproxy}: no phonebook for this version (HTTP 404)")
    if resp.status_code != 200:
        raise CloudPhonebookError(f"{cproxy} answered HTTP {resp.status_code}")
    data = resp.content or b""
    if not data.startswith(b"SQLite format 3"):
        raise CloudPhonebookError(
            f"{cproxy} answered 200 but the content is not an SQLite database ({len(data)} bytes)")
    return data
