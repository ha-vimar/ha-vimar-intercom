"""Discovery mDNS del citofono — parsing del record `_eipvdes._tcp`.

Modulo **puro**: nessun import di Home Assistant, così resta testabile senza HA
(vedi `tests/test_smoke.py`). Il config flow gli passa i campi grezzi del record
e riceve indietro un dizionario normalizzato.

Il servizio è quello che usa l'app ufficiale VIEW per trovare il Tab in LAN —
nelle stringhe dell'APK: «[mDns] Start discovery for _eipvdes._tcp. and MAC».

Il TXT **non ha la stessa forma su tutti gli impianti** [VERIFICATO 18/09/2026]:

- Tab 7S 2F+ (40507), quattro chiavi: `mac`, `domain`, `proxy`, `timestemp` (sic)
- Tab 5S UP (40515) fw 2.1.0203, undici: le precedenti più `dev`, `rec`, `sta`,
  `man`, `fver`, `rver`, `sver`, `hws`

Quindi solo `mac`, `proxy` e `domain` si possono considerare presenti ovunque;
tutto il resto è opzionale e non deve mai essere obbligatorio per il flusso.
`rec`, `sta` e `man` non sono documentati da nessuna parte e restano ignorati:
finché non si sa cosa significano non entrano nel comportamento.
"""

from __future__ import annotations

SERVICE_TYPE = "_eipvdes._tcp.local."

# Chiavi presenti su tutti gli impianti osservati.
TXT_MAC = "mac"
TXT_PROXY = "proxy"
TXT_DOMAIN = "domain"
# Opzionali, solo su firmware recenti.
TXT_MODEL = "dev"
TXT_FIRMWARE = "fver"

# Valori che non identificano un host raggiungibile: un `domain` così non è
# utilizzabile come dominio SIP. Li usa anche qr_decoder.
_NON_ROUTABLE = frozenset({
    "", "127.0.0.1", "::1", "0.0.0.0", "localhost", "localhost.localdomain",
})


def normalize_mac(mac: str | None) -> str:
    """MAC in forma confrontabile: solo cifre esadecimali maiuscole.

    Serve perché lo stesso indirizzo compare con separatori diversi a seconda
    della fonte (QR, record mDNS, header SIP).
    """
    if not mac:
        return ""
    return "".join(c for c in str(mac).upper() if c in "0123456789ABCDEF")


def _text(value) -> str:
    """Un valore del TXT come stringa: python-zeroconf li consegna in bytes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return str(value).strip()


def parse_service_txt(properties: dict | None) -> dict[str, str]:
    """Normalizza il TXT record in un dizionario di stringhe.

    Chiavi e valori arrivano in bytes da python-zeroconf e in str da altre
    fonti; qui diventano `str` in entrambi i casi. Le chiavi sono confrontate
    in minuscolo.
    """
    out: dict[str, str] = {}
    for key, value in (properties or {}).items():
        k = _text(key).lower()
        if k:
            out[k] = _text(value)
    return out


def is_ip(value: str | None) -> bool:
    """True se `value` è un indirizzo IP (il `domain` del 40515), non un nome."""
    import ipaddress
    try:
        ipaddress.ip_address(_text(value))
    except ValueError:
        return False
    return True


def is_routable_domain(domain: str | None) -> bool:
    """False se il dominio annunciato non è utilizzabile da Home Assistant."""
    return _text(domain).lower() not in _NON_ROUTABLE


def extract_discovery(host: str, properties: dict | None) -> dict[str, str]:
    """Dati utili per il config flow, dal record di servizio.

    `host` è l'indirizzo da cui arriva l'annuncio; viene usato come proxy
    locale quando il TXT non ne dichiara uno (o ne dichiara uno inutilizzabile).

    Restituisce sempre le chiavi `mac`, `mac_normalized`, `local_proxy`,
    `sip_domain`, `model`, `firmware`: quelle che il record non porta restano
    stringhe vuote, così il chiamante non deve gestire assenze.
    """
    txt = parse_service_txt(properties)

    mac = txt.get(TXT_MAC, "")
    proxy = txt.get(TXT_PROXY, "")
    domain = txt.get(TXT_DOMAIN, "")

    if not is_routable_domain(proxy):
        proxy = _text(host)

    return {
        "mac": mac,
        "mac_normalized": normalize_mac(mac),
        "local_proxy": proxy or _text(host),
        # Il dominio che il Tab dichiara per sé: su alcuni impianti è il
        # dominio cloud, su altri il proprio indirizzo LAN. È il valore che
        # quel Tab si aspetta di vedere nelle richieste SIP, e non coincide
        # sempre con il `domain` del QR di abbinamento.
        "sip_domain": domain if is_routable_domain(domain) else "",
        "model": txt.get(TXT_MODEL, ""),
        "firmware": txt.get(TXT_FIRMWARE, ""),
    }
