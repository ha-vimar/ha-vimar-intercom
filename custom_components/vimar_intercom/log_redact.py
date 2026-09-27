"""Oscuramento delle credenziali nelle righe di log bufferizzate.

Modulo **puro**: nessun import di Home Assistant, così resta testabile senza HA
(vedi `tests/test_smoke.py`).

Serve a `log_buffer.py` (buffer interno e inoltro al log di HA). Il buffer che alimenta
`/api/vimar_intercom/debug` cattura i record a livello DEBUG a prescindere da
come è configurato `logger:`, quindi una riga di diagnostica scritta senza
pensarci diventa una credenziale leggibile via HTTP. Questa è la rete di
sicurezza, non la regola: **le credenziali non vanno loggate**, punto. Se
qualcosa arriva fin qui è comunque un bug da correggere all'origine.

Cosa viene oscurato:

* assegnazioni `chiave=valore` / `chiave: valore` per i nomi che nel protocollo
  Vimar portano segreti (`pwd`, `password`, `ha1`, `token`, `pn-tok`, …) —
  la forma del payload del QR di abbinamento;
* gli header `Authorization` e `Proxy-Authorization`, che contengono il digest
  della password SIP: chi li intercetta può attaccarla offline;
* i campi `response=` e `cnonce=` di una challenge Digest lasciati soli;
* la forma `{"PARAM":"token","VALUE":"..."}` di `GET_INIT_STATUS_REPLY`, in
  entrambi gli ordini (il Tab 40507 manda VALUE prima di PARAM): il token
  della rubrica va trattato come la password SIP;
* le stesse chiavi tra virgolette, cioè JSON (`"token": "…"`, come in
  `action=status`) e repr di un dict (`'token': '…'`);
* un header Authorization finito dentro una riga sola (messaggio loggato con %r);
* la chiave SRTP di un SDP (`inline:…`) e le stesse chiavi dentro un dict
  (`'crypto_key': '…'`).

Il nome della chiave resta visibile — serve a capire cosa stava succedendo —
mentre il valore diventa `***`.
"""

from __future__ import annotations

import re

MASK = "***"

# Nomi di campo che nel protocollo Vimar portano un segreto.
_SECRET_KEYS = ("pwd", "passwd", "password", "secret", "ha1", "token", "pn-tok", "apikey", "api_key",
                "crypto_key", "srtp_key", "a_srtp_key", "v_srtp_key", "key_b64")

# La chiave può essere tra virgolette (JSON `"token": "…"`, repr di un dict
# `'token': '…'`): la virgoletta di chiusura della chiave fa parte del gruppo 1.
_ASSIGN = re.compile(
    r"(?i)\b((?:" + "|".join(re.escape(k) for k in _SECRET_KEYS) + r")[\"']?)(\s*[:=]\s*)([\"']?)([^\"'\s,;&}]+)\3"
)

# Authorization / Proxy-Authorization: tutto il valore, fino a fine riga.
_AUTH_HEADER = re.compile(r"(?im)^(\s*(?:proxy-)?authorization\s*:\s*).*$")

# Lo stesso header dentro una riga sola, tipicamente un messaggio SIP loggato
# con %r (i CRLF diventano `\\r\\n` letterali): fino al primo CRLF, reale o
# scritto, o alla virgoletta che chiude il repr.
_AUTH_INLINE = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*:\s*)(?:digest|basic|bearer)\b.*?(?=\\r|\\n|[\r\n']|$)"
)

# Digest sparso in una riga che non è un header completo.
_DIGEST_FIELD = re.compile(r"(?i)\b(response|cnonce)(\s*=\s*)(\"?)([0-9a-fA-F]{8,})\3")

# Chiave master SRTP in un SDP: `a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:<base64>`.
# Con quella chiave chi legge il registro decifra audio e video della chiamata.
_SRTP_INLINE = re.compile(r"(?i)(inline:)([A-Za-z0-9+/=]+)")

# GET_INIT_STATUS_REPLY: {"PARAM":"token","VALUE":"..."}
_PARAM_VALUE = re.compile(
    r"(?i)(\"PARAM\"\s*:\s*\"(?:token|pwd|password)\"\s*,\s*\"VALUE\"\s*:\s*\")([^\"]*)(\")"
)

# Stessa forma con l'ordine invertito, come la manda davvero il Tab 40507:
# {"VALUE": "...", "PARAM": "token"}
_VALUE_PARAM = re.compile(
    r"(?i)(\"VALUE\"\s*:\s*\")([^\"]*)(\"\s*,\s*\"PARAM\"\s*:\s*\"(?:token|pwd|password)\")"
)


def redact(text: str) -> str:
    """Restituisce `text` con i valori sensibili sostituiti da `***`.

    Non solleva mai: è chiamata dentro un handler di logging, dove un'eccezione
    farebbe perdere la riga (o peggio, la lascerebbe passare in chiaro).
    """
    if not text:
        return text
    try:
        out = _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}{m.group(3)}", text)
        out = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{MASK}", out)
        out = _AUTH_INLINE.sub(lambda m: f"{m.group(1)}{MASK}", out)
        out = _DIGEST_FIELD.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}{m.group(3)}", out)
        out = _PARAM_VALUE.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
        out = _VALUE_PARAM.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
        out = _SRTP_INLINE.sub(lambda m: f"{m.group(1)}{MASK}", out)
        return out
    except Exception:  # noqa: BLE001 - mai far fallire il logging
        return MASK
