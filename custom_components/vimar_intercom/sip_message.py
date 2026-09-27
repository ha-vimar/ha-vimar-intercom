"""Messaggi SIP e SDP come testo: leggerli, tagliarli dal flusso, misurarli.

Niente rete e niente stato: sip_client usa queste funzioni, e i test le
provano da sole.
"""

import logging
import re

_LOGGER = logging.getLogger(__name__)


# Un corpo SIP più grande di così non esiste su questo impianto: l'SDP più
# lungo è sotto i 2 KB, la rubrica viaggia via HTTP.
MAX_SIP_BODY = 1_000_000


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
    contact_list = []
    rr_list = []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip().lower()
            if key == "via":
                via_list.append(v.strip())
            elif key == "contact":
                contact_list.append(v.strip())
            elif key == "record-route":
                # Possono essercene parecchie — qui ne arrivano cinque — e
                # contano tutte, in ordine: sono la strada del dialogo.
                rr_list.extend(x.strip() for x in v.split(",") if x.strip())
            hdrs[key] = v.strip()
    if via_list:
        hdrs["_via_all"] = via_list
    if contact_list:
        hdrs["_contact_all"] = contact_list
    if rr_list:
        hdrs["_record_route_all"] = rr_list
    return (code or method), hdrs, body, first


def _call_id(hdrs):
    return hdrs.get("call-id", "")


def _tag(header_val):
    for part in header_val.split(";"):
        part = part.strip()
        if part.startswith("tag="):
            return part[4:]
    return ""


def _angle(value: str) -> str:
    """Il contenuto fra parentesi angolari, se ci sono."""
    if "<" in value and ">" in value:
        return value[value.index("<") + 1:value.index(">")]
    return value.strip()


def _split_stream(buf: bytes) -> tuple[list[str], bytes]:
    """Taglia il flusso TLS in messaggi SIP completi; restituisce anche l'avanzo.

    * I CRLF fra un messaggio e l'altro sono i pong del keepalive (RFC 5626
      §4.4.1): vanno scartati, altrimenti finiscono in testa al messaggio
      successivo, che senza la sua prima riga non si riconosce più.
    * Un Content-Length negativo faceva girare questo ciclo per sempre
      sull'event loop (totale fermo sull'intestazione), e uno enorme teneva
      il buffer in attesa di un corpo che non arriva: un solo messaggio
      malformato del relay bloccava Home Assistant. Il flusso non è più
      affidabile, quindi lo si butta.
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
            name, _, value = line.partition(":")
            if name.strip().lower() in ("content-length", "l"):
                try:
                    cl = int(value.strip())
                except ValueError:
                    cl = -1
        if not 0 <= cl <= MAX_SIP_BODY:
            _LOGGER.error("SIP: Content-Length non valido (%r): flusso scartato", cl)
            return messages, b""
        total = hdr_end + cl
        if len(buf) < total:
            return messages, buf
        messages.append(buf[:total].decode(errors="replace"))
        buf = buf[total:]


def _clen(body: str) -> int:
    """`Content-Length` di un corpo: in **byte**, non in caratteri.

    Fino alla 1.0.6 era `len(body)`: con un corpo non ASCII (`NICK;Cucina è`) si
    dichiaravano 13 byte e se ne spedivano 14. In UDP il corpo arrivava troncato; in
    TCP/TLS il byte in più veniva letto come inizio del messaggio successivo.
    """
    return len(body.encode("utf-8"))


def _via_block(hdrs):
    via_all = hdrs.get("_via_all", [])
    if via_all:
        return "".join(f"Via: {v}\r\n" for v in via_all)
    return f"Via: {hdrs.get('via', '')}\r\n"


def _challenge_nonce(challenge: str) -> str:
    m = re.search(r'nonce\s*=\s*"([^"]*)"', challenge)
    return m.group(1) if m else ""


def _split_contacts(hdrs) -> list[str]:
    """Ogni binding come voce separata.

    Un registrar può elencare i contatti su più righe Contact oppure su una
    sola separati da virgola (e la virgola compare anche dentro i parametri
    fra virgolette, quindi non si può splittare alla cieca).
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


def parse_sdp(sdp_text):
    result = {"audio": {}, "video": {}, "conn": ""}
    m = None
    for line in sdp_text.split("\n"):
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
            if m:
                result[m]["ip"] = ip
            else:
                result["conn"] = ip
        elif line.startswith("m=audio"):
            m = "audio"
            parts = line.split()
            result["audio"]["port"] = int(parts[1])
        elif line.startswith("m=video"):
            m = "video"
            parts = line.split()
            result["video"]["port"] = int(parts[1])
        elif line.startswith("a=rtpmap:") and m:
            result[m].setdefault("rtpmap", []).append(line)
        elif line.startswith("a=fmtp:") and m:
            result[m].setdefault("fmtp", []).append(line)
        elif line.startswith("a=crypto:") and m:
            parts = line.split()
            for p in parts:
                if p.startswith("inline:"):
                    result[m]["crypto_key"] = p[7:]
                    break
    for section in ("audio", "video"):
        if section in result and "ip" not in result[section]:
            result[section]["ip"] = result["conn"]
    return result
