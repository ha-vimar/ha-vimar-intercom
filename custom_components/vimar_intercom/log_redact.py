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
* un header Authorization finito dentro una riga sola (messaggio loggato con %r).
* SRTP keys: `inline:<key>` in an SDP line, any dict/JSON value under a key
  named `key` or `*_key`, and a 40-character base64 blob after `key:`/`key=`.

Il nome della chiave resta visibile — serve a capire cosa stava succedendo —
mentre il valore diventa `***`.

`redact_plant()` is the second layer, for the log only (#146): the plant data users
would otherwise remove by hand before pasting a log in an issue. Each value becomes a
short tag that is the same on every line of a run (`id…45#9f1c`, `name#71aa`), so a log
can still be followed:

* the account's SIP id, IMEI, UUID, MyName and SIP domains, registered by
  `runtime.configure()` and masked wherever they appear;
* the `MyName` and `Mobile-IMEI` headers, whatever their value;
* names and caller ids in plant messages (`"NAME"`, `"NICK"`, `"SIP_ID"`, a dict's
  `'name'`) and the display name before a `<sip:` URI;
* a device's `urn:uuid:` instance id;
* private IPv4 addresses, down to their first and last octet (`192.x.x.23`), and any
  address after `received=`, as the host of a SIP URI (the home's public address, the
  other phones' Contacts), at the hop of a `Via` or as a URI without a user (loopback,
  0.0.0.0 and the RFC 5737 documentation blocks stay there);
* the other phones' and devices' ids as the user of a SIP URI (`sip:7798765@…`), the
  phones' 5-digit ids (`6xxxx`) too, also as a `SIP_ID` or an `EXT` (#163);
* the apartment's GID, by its shape (`"PARAM":"GID"`, `gid=`, `apt_gid`, `NEW_PHONEBOOK`).

The panels' extensions (`55001`, also as a `SIP_ID`), the protocol's own addresses
(60000, 60001, 60002, 61000), SIP methods and codes, ports and timings stay: they are what
a log is read for. `redact()` alone still feeds the *Last Received Message* sensor, which
shows the plant's own message.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from .const import MY_NAME

MASK = "***"

# Longest text redact() returns. A SIP message with its SDP is a few KB; a longer
# log line is cut, after the patterns have run (cutting first could leave the start
# of a quoted secret without the closing quote its pattern needs).
MAX_LEN = 16384

# Nomi di campo che nel protocollo Vimar portano un segreto.
_SECRET_KEYS = ("pwd", "passwd", "password", "secret", "ha1", "token", "auth", "pn-tok", "apikey", "api_key",
                "crypto_key", "srtp_key", "a_srtp_key", "v_srtp_key", "key_b64")

# La chiave può essere tra virgolette (JSON `"token": "…"`, repr di un dict
# `'token': '…'`): la virgoletta di chiusura della chiave fa parte del gruppo 1.
# Prima della chiave non si usa `\b`: `_` è un carattere di parola, e con `\b`
# `sip_password=…` passava in chiaro. Basta che non la preceda una lettera o una
# cifra: `sip_password`, `pn_token`, `"password"` sono coperti, `mypassword` no.
# Il valore tra virgolette arriva fino alla virgoletta di chiusura, spazi
# compresi (`password="my secret"`); senza virgolette, fino al primo separatore.
_ASSIGN = re.compile(
    r"(?i)(?<![A-Za-z0-9])((?:" + "|".join(re.escape(k) for k in _SECRET_KEYS) + r")[\"']?)(\s*[:=]\s*)"
    r"(?:\"([^\"]*)\"|'([^']*)'|([^\"'\s,;&}]+))"
)


def _mask_assign(m: re.Match) -> str:
    if m.group(3) is not None:
        val = f'"{MASK}"'
    elif m.group(4) is not None:
        val = f"'{MASK}'"
    else:
        val = MASK
    return f"{m.group(1)}{m.group(2)}{val}"


# Authorization / Proxy-Authorization: tutto il valore, fino a fine riga.
# Only horizontal whitespace around the name: with `\s*` the prefix crossed newlines,
# so a text of n bare line feeds cost O(n^2) (seconds for one crafted SIP datagram,
# on the event loop, #46).
_AUTH_HEADER = re.compile(r"(?im)^([ \t]*(?:proxy-)?authorization[ \t]*:[ \t]*).*$")

# Lo stesso header dentro una riga sola, tipicamente un messaggio SIP loggato
# con %r (i CRLF diventano `\\r\\n` letterali): fino al primo CRLF, reale o
# scritto, o alla virgoletta che chiude il repr.
_AUTH_INLINE = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*:\s*)(?:digest|basic|bearer)\b.*?(?=\\r|\\n|[\r\n']|$)"
)

# Digest sparso in una riga che non è un header completo.
_DIGEST_FIELD = re.compile(r"(?i)\b(response|cnonce)(\s*=\s*)(\"?)([0-9a-fA-F]{8,})\3")

# SRTP master key in an SDP line: `a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:<base64>`.
# Whoever reads a log with that key can decrypt the call's audio and video.
_SRTP_INLINE = re.compile(r"(?i)(inline:)([A-Za-z0-9+/=]+)")

# A dict or JSON value under a key named `key` or ending in `_key`/`-key`
# (`{'suite': ..., 'key': '...'}`, `"srtp_key": "..."`): whatever the value.
_DICT_KEY = re.compile(r"(?i)(([\"'])(?:[\w-]*[_-])?key\2\s*:\s*)([\"'])(.*?)\3")

# A bare base64 blob of SRTP-key length (30 bytes = 40 characters) after
# `key:` or `key=`, quoted or not.
_KEY_B64 = re.compile(r"(?i)(\bkey[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9+/]{40}[A-Za-z0-9+/=]*)")

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
        out = _ASSIGN.sub(_mask_assign, text)
        out = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{MASK}", out)
        out = _AUTH_INLINE.sub(lambda m: f"{m.group(1)}{MASK}", out)
        out = _DIGEST_FIELD.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}{m.group(3)}", out)
        out = _PARAM_VALUE.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
        out = _VALUE_PARAM.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
        out = _SRTP_INLINE.sub(lambda m: f"{m.group(1)}{MASK}", out)
        out = _DICT_KEY.sub(lambda m: f"{m.group(1)}{m.group(3)}{MASK}{m.group(3)}", out)
        out = _KEY_B64.sub(lambda m: f"{m.group(1)}{MASK}", out)
        if len(out) > MAX_LEN:
            out = f"{out[:MAX_LEN]}... [{len(out) - MAX_LEN} characters cut]"
        return out
    except Exception:  # noqa: BLE001 - mai far fallire il logging
        return MASK


# ─── plant data (#146) ───────────────────────────────────────────────────────

# Shorter values would match too much (a registered "101" is every 101 in the log).
_MIN_PLANT_VALUE = 4

# A SIP_ID this short is a panel's or a monitor's extension (55001): it stays readable.
_PANEL_EXT_MAX = 5

# ...except another phone's: the phones seen so far have 5-digit ids 6xxxx (a local 2F
# and cloud 2FV2 plants, #163). That is a shape seen on plants, not a protocol rule
# (ADR-13): the tag keeps the last two digits, so a 6xxxx panel would still be told apart.
# These stay readable: the protocol's own addresses (60000, the default PICG 60001,
# OPEN_CURRENT's 60002, the 2FV2 SGA 61000) and every panel or porter (4xxxx, 5xxxx).
_SERVICE_EXTS = frozenset({"60000", "60001", "60002", "61000"})


def _is_private_id(value: str) -> bool:
    """A numeric SIP id that is someone's: longer than an extension, or a 5-digit phone's."""
    if len(value) > _PANEL_EXT_MAX:
        return True
    return len(value) == _PANEL_EXT_MAX and value[0] == "6" and value not in _SERVICE_EXTS

# Tags are keyed with a key drawn at start-up: a value gets the same tag for the whole run,
# but a tag can't be turned back into a 7-digit SIP id or an IMEI by trying every value,
# as 16 bits of a plain hash could. After a restart the tags change.
_TAG_KEY = secrets.token_bytes(16)


def _tag(kind: str, value: str, *, keep_tail: bool = True) -> str:
    """Stable short tag. Ids keep their last two digits, to tell two of them apart
    (not a GID: two digits of a three-digit number would give it away)."""
    digest = hmac.new(_TAG_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()[:4]
    tail = f"…{value[-2:]}" if keep_tail and value.isdigit() else ""
    return f"{kind}{tail}#{digest}"


# value -> tag, and one pattern over all of them (longest first), swapped as a pair so
# a logging thread never sees one without the other.
_plant: tuple[dict[str, str], re.Pattern | None] = ({}, None)


def set_plant_values(pairs, *, keep: bool = False) -> None:
    """Mask these `(kind, value)` pairs in every later log line, in place of the ones
    before (or on top of them with `keep`), swapped in one go: no line in between goes
    out with nothing masked. Short values and the default MyName are left out."""
    global _plant
    tags = dict(_plant[0]) if keep else {}
    for kind, value in pairs:
        value = str(value or "").strip()
        if len(value) >= _MIN_PLANT_VALUE and value != MY_NAME:
            tags.setdefault(value, _tag(kind, value))
    if not tags:
        _plant = ({}, None)
        return
    alts = "|".join(re.escape(v) for v in sorted(tags, key=len, reverse=True))
    _plant = (tags, re.compile(rf"(?<![0-9A-Za-z])(?:{alts})(?![0-9A-Za-z])"))


def remember_plant_value(kind: str, value: str | None) -> None:
    """Mask `value` too, keeping what is registered."""
    set_plant_values([(kind, value)], keep=True)


def forget_plant_values() -> None:
    global _plant
    _plant = ({}, None)


_IPV4 = r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"


def _short_ip(ip: str) -> str:
    parts = ip.split(".")
    return f"{parts[0]}.x.x.{parts[3]}"


# Any address, public too, where it can only be a phone's or the home's: the address the
# relay saw us from (`received=`) and the host of a SIP URI (`sip:7712345@203.0.113.9`,
# the other phones' Contacts in a REGISTER answer).
_RECEIVED = re.compile(rf"(?i)(\breceived=)({_IPV4})")
_URI_HOST = re.compile(rf"(?i)(\bsips?:[^@\s<>\"';,]{{1,64}}@)({_IPV4})")

# Elsewhere only private addresses: 10/8, 100.64/10 (carrier NAT), 169.254/16, 172.16/12,
# 192.168/16, checked in _mask_ip. Not after or before a digit or a dot, so `1192.168.1.1`
# and `2.15.0.3.1` are not addresses.
_PRIVATE_IP = re.compile(r"(?<![\d.])(10|100|169|172|192)\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\.?\d)")


# Another phone's or device's id as the user of a SIP URI (`<sip:7798765@…>`): digits,
# the whole user, up to the `@`. Which ones are masked: _is_private_id.
_URI_USER = re.compile(rf"(?i)(\bsips?:)(\d{{{_PANEL_EXT_MAX},20}})(?=@)")

# Any address, public too, at the hop of a Via (`SIP/2.0/UDP 9.9.9.23:5060`) or as a
# URI without a user (`<sip:9.9.9.23:5060;lr>`): the home's or the relay peer's.
_VIA_HOST = re.compile(rf"(?i)(\bSIP/2\.0/[A-Z]{{2,4}}[ \t]+)({_IPV4})(?![\d.])")
_URI_ADDR = re.compile(rf"(?i)(\bsips?:)({_IPV4})(?![\d.])")

# Left as they are there: loopback (the Tab 5S's 127.0.0.1, the test harness), the
# unspecified 0.0.0.0, and the documentation blocks of RFC 5737 the tests and docs use.
_KEEP_ADDR = re.compile(r"127\.|0\.0\.0\.0$|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.")


def _mask_any_ip(m: re.Match) -> str:
    ip = m.group(2)
    if _KEEP_ADDR.match(ip) or max(int(o) for o in ip.split(".")) > 255:
        return m.group(0)
    return f"{m.group(1)}{_short_ip(ip)}"


def _mask_ip(m: re.Match) -> str:
    first, second = int(m.group(1)), int(m.group(2))
    private = {10: True, 100: 64 <= second <= 127, 169: second == 254, 172: 16 <= second <= 31,
               192: second == 168}[first]
    if not private or max(int(g) for g in m.groups()) > 255:
        return m.group(0)
    return _short_ip(m.group(0))


# A device's instance id, in a Contact (`+sip.instance="<urn:uuid:…>"`).
_URN_UUID = re.compile(r"(?i)(urn:uuid:)([0-9a-f-]{8,64})")

# `MyName: …` / `Mobile-IMEI: …`, as a header line or inside a message logged with %r
# (there it follows a written `\n`, a letter for `\b`). The value runs to the end of the
# line or to a backslash; the closing quote of a %r is put back after the tag.
_IDENTITY_HEADER = re.compile(r"(?im)(?:(?<=\\n)|(?<![\w-]))(myname|mobile-imei)([ \t]*:[ \t]*)([^\r\n\\]+)")


def _mask_header(m: re.Match) -> str:
    value = m.group(3)
    core = value.rstrip(" \t'\"")
    if not core or core == MY_NAME:
        return m.group(0)
    kind = "imei" if m.group(1).lower() == "mobile-imei" else "name"
    return f"{m.group(1)}{m.group(2)}{_tag(kind, core)}{value[len(core):]}"


# A name or caller id in a plant message: `"NAME": "…"`, `'name': '…'`, `"SIP_ID": 7798765`,
# and the extension of a place in GET_NICKS_REPLY (`"EXT": "…"`), where the phones appear.
_NAME_FIELD = re.compile(
    r"(?i)([\"'])(name|nick|nickname|myname|sip_id|ext)\1(\s*:\s*)(?:([\"'])(.*?)\4|(\d+))"
)


def _mask_field(m: re.Match) -> str:
    quote, value = (m.group(4), m.group(5)) if m.group(6) is None else ("", m.group(6))
    is_id = m.group(2).lower() in ("sip_id", "ext")
    if not value or value == MY_NAME or (is_id and value.isdigit() and not _is_private_id(value)):
        return m.group(0)
    return (f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}"
            f"{quote}{_tag('id' if is_id else 'name', value)}{quote}")


# The display name of a SIP address: `From: "Anna" <sip:…>`.
_DISPLAY_NAME = re.compile(r"\"([^\"\r\n]+)\"(?=\s*<sips?:)")


# The apartment's GID: in the status answer (`{"PARAM":"GID","VALUE":"731"}`, both
# orders), after a key named `gid`, `apt_gid`, `gid_appartamento` or `Apartment GID`
# (`gid=731`, `'apt_gid': '731'`), and last in `NEW_PHONEBOOK;<ver>;<gid>`. Matched by
# its shape only: a small integer, as a registered value it would hit every 731 in a log.
# A phonebook row's `"GID": N` is masked too, even a panel's: it may be ours.
# A panel's `GID_PE` or `GA_GID` is not a key named like these and stays.
_GID_PARAM = re.compile(r"(?i)(\"PARAM\"\s*:\s*\"GID\"\s*,\s*\"VALUE\"\s*:\s*\"?)([^\",}\s]+)")
_GID_VALUE = re.compile(r"(?i)(\"VALUE\"\s*:\s*\"?)([^\",}\s]+)(\"?\s*,\s*\"PARAM\"\s*:\s*\"GID\")")
_GID_KEY = re.compile(
    r"(?i)(?<![\w-])([\"']?(?:apt_|apartment[ _])?gid(?:_appartamento)?[\"']?[ \t]*[:=][ \t]*)"
    r"(?:([\"'])([^\"'\r\n]{1,32})\2|([^\s\"',;&}\]]+))"
)
_GID_PHONEBOOK = re.compile(r"(?i)(\bNEW_PHONEBOOK;[^;\s]*;)([^;\s\"',}\]\\]+)")


def _gid(value: str) -> str:
    return _tag("gid", value, keep_tail=False)


def _mask_gid_key(m: re.Match) -> str:
    quote, value = (m.group(2), m.group(3)) if m.group(4) is None else ("", m.group(4))
    if value.lower() in ("none", "null"):
        return m.group(0)
    return f"{m.group(1)}{quote}{_gid(value)}{quote}"


def redact_plant(text: str) -> str:
    """`text` with the plant data above replaced by tags. Never raises."""
    if not text:
        return text
    try:
        # The shapes first: a registered value they tag gets the same tag as below.
        out = _IDENTITY_HEADER.sub(_mask_header, text)
        out = _NAME_FIELD.sub(_mask_field, out)
        out = _DISPLAY_NAME.sub(lambda m: m.group(0) if m.group(1) == MY_NAME
                                else f'"{_tag("name", m.group(1))}"', out)
        out = _URN_UUID.sub(lambda m: f"{m.group(1)}{_tag('uuid', m.group(2))}", out)
        out = _GID_PARAM.sub(lambda m: f"{m.group(1)}{_gid(m.group(2))}", out)
        out = _GID_VALUE.sub(lambda m: f"{m.group(1)}{_gid(m.group(2))}{m.group(3)}", out)
        out = _GID_KEY.sub(_mask_gid_key, out)
        out = _GID_PHONEBOOK.sub(lambda m: f"{m.group(1)}{_gid(m.group(2))}", out)
        tags, known = _plant
        if known:
            out = known.sub(lambda m: tags[m.group(0)], out)
        out = _URI_USER.sub(lambda m: f"{m.group(1)}{_tag('id', m.group(2))}"
                            if _is_private_id(m.group(2)) else m.group(0), out)
        out = _RECEIVED.sub(lambda m: f"{m.group(1)}{_short_ip(m.group(2))}", out)
        out = _URI_HOST.sub(lambda m: f"{m.group(1)}{_short_ip(m.group(2))}", out)
        out = _VIA_HOST.sub(_mask_any_ip, out)
        out = _URI_ADDR.sub(_mask_any_ip, out)
        return _PRIVATE_IP.sub(_mask_ip, out)
    except Exception:  # noqa: BLE001 - mai far fallire il logging
        return MASK
