"""Vimar Intercom: the SIP message layer.

Parsing a message (first line, headers, body), the header helpers, cutting the
TLS stream into messages, and the Digest response to a 401/407 challenge.
No sockets and no call state here: that stays in sip_client.
"""

import hashlib
import logging
import re
import secrets

from . import runtime as R

_LOGGER = logging.getLogger(__name__)


def _parse(msg):
    parts = msg.split("\r\n\r\n", 1)
    body = parts[1] if len(parts) > 1 else ""
    lines = parts[0].split("\r\n")
    first = lines[0]
    code = method = None
    if first.startswith("SIP/2.0"):
        try:
            code = int(first.split()[1])
        except (ValueError, IndexError):
            pass
    else:
        method = first.split()[0] if first else None
    hdrs = {}
    via_list = []
    rr_list = []   # Record-Route: più header, l'ordine conta (route set)
    contact_list = []  # a registrar lists one Contact per binding
    reason_list = []  # RFC 3326: one Reason header per protocol (SIP, Q.850)
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip().lower()
            if key == "via":
                via_list.append(v.strip())
            elif key == "record-route":
                rr_list.append(v.strip())
            elif key == "contact":
                contact_list.append(v.strip())
            elif key == "reason":
                reason_list.append(v.strip())
            hdrs[key] = v.strip()
    if via_list:
        hdrs["_via_all"] = via_list
    if rr_list:
        hdrs["_rr_all"] = rr_list
    if contact_list:
        hdrs["_contact_all"] = contact_list
    if reason_list:
        hdrs["_reason_all"] = reason_list
    return (code or method), hdrs, body, first


def _split_unquoted(text: str, sep: str) -> list[str]:
    """`text` split on `sep` outside double quotes, each part stripped. Inside quotes a
    backslash escapes the next character (`\\"` does not close them, RFC 3261)."""
    parts, current, quoted, escaped = [], [], False, False
    for char in text:
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        if char == sep and not quoted:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    return parts


def _reasons(hdrs) -> list[tuple[str, dict[str, str]]]:
    """Every reason-value of the Reason headers as (protocol, params), protocol upper-case
    and param names lower-case. A header may list several values separated by commas,
    and a quoted `text` may hold commas and semicolons (RFC 3326)."""
    # _parse always fills _reason_all; the plain "reason" key is for header dicts built by hand.
    raw = hdrs.get("_reason_all") or ([hdrs["reason"]] if hdrs.get("reason") else [])
    out = []
    for line in raw:
        for value in _split_unquoted(line, ","):
            protocol, *params = _split_unquoted(value, ";")
            if not protocol:
                continue
            pairs = (p.split("=", 1) for p in params if "=" in p)
            out.append((protocol.upper(), {k.strip().lower(): v.strip() for k, v in pairs}))
    return out


def _split_contacts(hdrs) -> list[str]:
    """Each binding as its own entry.

    A registrar may list contacts on several Contact lines or on one line
    separated by commas (and commas also appear inside quoted parameters, so
    a blind split does not work).
    """
    raw = hdrs.get("_contact_all") or ([hdrs["contact"]] if hdrs.get("contact") else [])
    values: list[str] = []
    for line in raw:
        current, depth, quoted = [], 0, False
        for char in line:
            if char == '"':
                quoted = not quoted
            elif not quoted and char in "<[":
                depth += 1
            elif not quoted and char in ">]":
                depth = max(0, depth - 1)
            if char == "," and depth == 0 and not quoted:
                values.append("".join(current).strip())
                current = []
            else:
                current.append(char)
        if current:
            values.append("".join(current).strip())
    return [v for v in values if v]


def _call_id(hdrs):
    return hdrs.get("call-id", "")


def _tag(header_val):
    for part in header_val.split(";"):
        part = part.strip()
        if part.startswith("tag="):
            return part[4:]
    return ""


def _angle_uri(hdr: str) -> str:
    """L'URI di un header To/From/Contact: quello fra <...>, o tutto fino al ';'."""
    return hdr[hdr.index("<") + 1:hdr.index(">")] if "<" in hdr and ">" in hdr else hdr.split(";")[0]


def _contact_uri(hdrs: dict) -> str:
    return _angle_uri(hdrs.get("contact", ""))


def _host_of(uri_or_via: str) -> str:
    """L'host di un URI SIP («sip:55001@192.168.0.5:5060;x») o di un Via
    («SIP/2.0/UDP 192.168.0.5:5060;branch=…»)."""
    s = uri_or_via.split(";")[0].strip()
    s = s.rsplit("@", 1)[-1].split()[-1]
    return s.rsplit(":", 1)[0] if s.count(":") == 1 else s


def _via_block(hdrs):
    via_all = hdrs.get("_via_all", [])
    if via_all:
        return "".join(f"Via: {v}\r\n" for v in via_all)
    return f"Via: {hdrs.get('via', '')}\r\n"


# Largest body the framer accepts: the reader's buffer guard is the same 1 MB.
MAX_SIP_BODY = 1_000_000


def _split_stream(buf: bytes) -> tuple[list[str], bytes | None]:
    """Cut the TLS stream into complete SIP messages; return them and the rest.

    CRLFs between messages are keepalive pongs (RFC 5626 §4.4.1). Left in the
    buffer they ended up in front of the next message, which then lost its
    first line and was dropped as unrecognised.

    Content-Length may also come as the compact header `l:` (RFC 3261
    section 7.3.3). A value that is not a number, or one above MAX_SIP_BODY,
    means the stream can no longer be framed: waiting for a body that size
    held every later message forever. The rest is then None, and the reader
    drops the connection and reconnects. A negative value counts as 0.
    """
    messages: list[str] = []
    while True:
        buf = buf.lstrip(b"\r\n")
        end = buf.find(b"\r\n\r\n")
        if end < 0:
            return messages, buf
        hdr_end = end + 4
        cl = 0
        for line in buf[:hdr_end].decode(errors="replace").split("\r\n"):
            name, sep, value = line.partition(":")
            if not sep or name.strip().lower() not in ("content-length", "l"):
                continue
            try:
                # max(0): a negative Content-Length did not advance the
                # buffer and the loop spun forever on the event loop.
                cl = max(0, int(value.strip()))
            except ValueError:
                cl = None
            if cl is None or cl > MAX_SIP_BODY:
                _LOGGER.error("SIP: invalid Content-Length (%r): the stream can no "
                              "longer be framed, reconnecting", value.strip()[:32])
                return messages, None
        if len(buf) < hdr_end + cl:
            return messages, buf
        messages.append(buf[:hdr_end + cl].decode(errors="replace"))
        buf = buf[hdr_end + cl:]


def _clen(body: str) -> int:
    """`Content-Length` di un corpo: in **byte**, non in caratteri.

    Fino alla 1.0.6 era `len(body)`: con un corpo non ASCII (`NICK;Cucina è`) si
    dichiaravano 13 byte e se ne spedivano 14. In UDP il corpo arrivava troncato; in
    TCP/TLS il byte in più veniva letto come inizio del messaggio successivo.
    """
    return len(body.encode("utf-8"))


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


# One name=value of a challenge: a quoted value may hold commas (a realm, a
# qop list), so the value is matched whole instead of splitting on commas.
_CHALLENGE_PARAM = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:"([^"]*)"|([^,\s]+))')


def _challenge_params(challenge: str) -> dict[str, str]:
    """Parameters of a WWW-Authenticate / Proxy-Authenticate value.

    `Digest realm="a, b", nonce="n"` gives {"realm": "a, b", "nonce": "n"};
    names are lower-cased. Another scheme (Basic) gives {}.
    """
    scheme, sep, params = (challenge or "").strip().partition(" ")
    if "=" in scheme:
        params = challenge  # no scheme name: parameters only
    elif not sep or scheme.lower() != "digest":
        return {}
    return {
        m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
        for m in _CHALLENGE_PARAM.finditer(params)
    }


# nc per nonce (RFC 2617 3.2.2): fixed at 1, a reused challenge looks like a replay.
_nonce_count: dict[str, int] = {}


def _next_nc(nonce: str) -> str:
    if nonce not in _nonce_count and len(_nonce_count) >= 16:
        del _nonce_count[next(iter(_nonce_count))]  # the oldest nonce
    _nonce_count[nonce] = _nonce_count.get(nonce, 0) + 1
    return f"{_nonce_count[nonce]:08x}"


def _make_auth(method, uri, challenge):
    p = _challenge_params(challenge)
    nonce = p.get("nonce", "")
    realm = p.get("realm", R.SIP_DOMAIN)
    opaque = p.get("opaque", "")
    qop = p.get("qop", "")
    cnonce = secrets.token_hex(8)
    if "auth" in [item.strip().lower() for item in qop.split(",")]:
        nc = _next_nc(nonce)
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
