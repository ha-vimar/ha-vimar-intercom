"""Rilevamento automatico del modello di citofono dagli header SIP.

Il citofono (e gli altri endpoint dell'impianto) si identificano negli header
SIP ``User-Agent`` (richieste) e ``Server`` (risposte). Questo modulo mappa
quelle stringhe sul nome commerciale Vimar/Elvox corrispondente.

Nessun valore è hardcodato nelle entità: ``sip_client`` chiama :func:`match`
per ogni peer SIP incontrato e propaga il risultato al device registry di HA.
"""

from __future__ import annotations

import re

# ─── Peer che NON identificano il modello ────────────────────────────────────
# Flexisip è il proxy SIP che gira *sul* citofono: risponde a tutto ciò che non
# viene inoltrato a un endpoint, quindi il suo banner non dice nulla sul modello.
IGNORE_PATTERNS: tuple[str, ...] = (
    r"flexisip",
    r"sofia-sip",
    r"belle-sip",
    r"linphone",
    r"toga_",          # user-agent dell'app Vimar (e di questa integrazione)
    r"video\.door_",   # user-agent dell'app Video Door (Elvox)
    r"home\s*assistant",
)

# ─── Mappa User-Agent → modello commerciale ─────────────────────────────────
# Ordine = priorità: il primo pattern che matcha vince, quindi i più specifici
# stanno in cima. Il gruppo di cattura, se presente, non viene usato: serve
# solo a rendere il pattern leggibile.
MODEL_PATTERNS: tuple[tuple[str, str], ...] = (
    # ── Posti interni serie Tab (touch) ──
    (r"tab[\s_-]*7s[\s_-]*(?:up[\s_-]*)?plus",  "Elvox Tab 7S Plus"),
    (r"tab[\s_-]*7s",                           "Elvox Tab 7S"),
    (r"tab[\s_-]*5s[\s_-]*(?:up[\s_-]*)?plus",  "Elvox Tab 5S Plus"),
    (r"tab[\s_-]*5s",                           "Elvox Tab 5S"),
    (r"tab[\s_-]*4s",                           "Elvox Tab 4S"),
    # ── Codici prodotto Vimar (compaiono in alcuni firmware) ──
    (r"\b(?:k)?40517(?:\.\d+)?\b",              "Elvox Tab 7S Up"),
    (r"\b(?:k)?40515(?:\.\d+)?\b",              "Elvox Tab 5S Up"),
    (r"\b(?:k)?40945(?:\.\d+)?\b",              "Elvox Tab 5S Plus"),
    (r"\b(?:k)?40607(?:\.\d+)?\b",              "Elvox Tab 7S IP"),
    # ── Targhe esterne / altri endpoint dell'impianto ──
    (r"pixel",                                  "Elvox Pixel"),
    (r"\b41018\b|\b41019\b",                    "Elvox Pixel"),
    # ── Fallback generico: è un Tab, ma non sappiamo quale ──
    (r"\btab\b",                                "Elvox Tab IP"),
)

# Versione firmware: "qualcosa/1.2.3" oppure "FwVer:1.2.3" / "AppVer:2.4.0"
_FW_PATTERNS: tuple[str, ...] = (
    r"(?:fw|sw|fwver|swver|version)[:/ ]\s*v?(\d+(?:\.\d+){1,3})",
    r"/\s*v?(\d+(?:\.\d+){1,3})",
)


# Longest User-Agent looked at: real ones are under 100 characters.
MAX_UA_LEN = 256


def is_ignored(user_agent: str) -> bool:
    """True se lo User-Agent è di un proxy/app e non di un dispositivo."""
    ua = user_agent.lower()
    return any(re.search(p, ua) for p in IGNORE_PATTERNS)


def match(user_agent: str) -> tuple[str | None, str | None, int]:
    """Estrae (modello, versione firmware, priorità) da uno User-Agent SIP.

    ``priorità`` è l'indice del pattern che ha matchato: più basso = più
    specifico. Vale ``-1`` quando non c'è alcun match, così il chiamante può
    ignorare il risultato o preferire un match precedente più preciso.
    """
    if not user_agent:
        return None, None, -1

    # A User-Agent comes from any SIP peer: only the start of it can name a model.
    ua = user_agent.strip()[:MAX_UA_LEN]
    if is_ignored(ua):
        return None, None, -1

    ua_low = ua.lower()
    model = None
    priority = -1
    for idx, (pattern, name) in enumerate(MODEL_PATTERNS):
        if re.search(pattern, ua_low):
            model = name
            priority = idx
            break

    if model is None:
        return None, None, -1

    return model, _extract_fw(ua), priority


def _extract_fw(user_agent: str) -> str | None:
    for pattern in _FW_PATTERNS:
        m = re.search(pattern, user_agent, re.IGNORECASE)
        if m:
            return m.group(1)
    return None
