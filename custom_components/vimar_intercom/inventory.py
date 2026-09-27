"""Inventario dei dispositivi dell'impianto, ricavato dal traffico SIP.

Su questi impianti tutti i dispositivi mobili condividono un unico utente SIP e
si distinguono solo per identificativo e nome. Chi usa l'integrazione non ha
quindi modo di sapere quali telefoni siano abbinati, cosa che conta parecchio:
generare un nuovo QR ruota la credenziale condivisa e sgancia gli altri.

Le informazioni passano già nel traffico che l'integrazione gestisce:

* il ``200 OK`` a un ``REGISTER`` elenca un ``Contact`` per ogni binding
  dell'account, con il suo ``+sip.instance`` e l'``expires`` concesso;
* le richieste in arrivo portano ``MyName``, ``Mobile-IMEI``, ``User-Agent`` e
  una catena ``Via`` i cui ``received=``/``rport=`` rivelano l'indirizzo reale.

Questo modulo raccoglie quei frammenti in un elenco unico. È volutamente puro:
niente Home Assistant, niente socket, così è testabile da solo.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict

# "<sip:60901@127.0.0.1:5060;transport=tls>;tag=abc" → "60901"
_SIP_ID = re.compile(r"sips?:([^@;>\s]+)@")
_URI_HOST = re.compile(r"sips?:[^@;>\s]+@([^;>\s]+)")
_INSTANCE = re.compile(r'\+sip\.instance\s*=\s*"?<?urn:uuid:([^">\s;]+)', re.I)
_EXPIRES = re.compile(r";\s*expires\s*=\s*(\d+)", re.I)
_RECEIVED = re.compile(r";\s*received\s*=\s*([^;\s]+)", re.I)
_RPORT = re.compile(r";\s*rport\s*=\s*(\d+)", re.I)


def sip_id(value: str) -> str:
    """L'utente SIP dentro un URI o un header From/To."""
    match = _SIP_ID.search(value or "")
    return match.group(1) if match else ""


def _uri_host(value: str) -> str:
    match = _URI_HOST.search(value or "")
    return match.group(1) if match else ""


def _first(pattern, value: str) -> str:
    match = pattern.search(value or "")
    return match.group(1) if match else ""


@dataclass
class Device:
    """Un dispositivo visto sull'impianto."""

    sip_id: str
    device_id: str = ""
    name: str = ""
    user_agent: str = ""
    address: str = ""
    expires: int | None = None
    is_self: bool = False
    registered: bool = False
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["first_seen"] = round(self.first_seen)
        data["last_seen"] = round(self.last_seen)
        return data


class DeviceInventory:
    """Raccoglie i dispositivi osservati, senza duplicarli."""

    def __init__(self, max_devices: int = 64):
        self._devices: dict[str, Device] = {}
        self._max_devices = max_devices

    # ─── Raccolta ────────────────────────────────────────────────────

    def note_binding(
        self, contact: str, *, own_device_id: str = "", own_name: str = "",
    ) -> Device | None:
        """Registra un binding preso da un ``Contact`` di una risposta 200.

        Un binding dice che quel dispositivo è registrato adesso, e con quale
        durata: è l'unica fonte che elenca anche i dispositivi che in questo
        momento non stanno parlando.
        """
        if not contact:
            return None
        user = sip_id(contact)
        if not user:
            return None
        instance = _first(_INSTANCE, contact)
        expires = _first(_EXPIRES, contact)
        is_self = bool(own_device_id and instance == own_device_id)
        device = self._merge(
            key=instance or f"{user}@{_uri_host(contact)}",
            sip_id=user,
            device_id=instance,
            address=_uri_host(contact),
            expires=int(expires) if expires else None,
            registered=True,
            is_self=is_self,
            # Un binding non porta un nome: per il nostro usiamo quello con cui
            # ci siamo abbinati, così l'elenco non mostra un id nudo.
            name=own_name if is_self else "",
        )
        return device

    def note_peer(self, headers: dict, *, own_device_id: str = "") -> Device | None:
        """Registra un dispositivo da una richiesta SIP in arrivo.

        ``headers`` sono quelli già analizzati da ``sip_client._parse``: il
        mittente, le sue intestazioni di identità e la catena Via, da cui si
        ricava l'indirizzo con cui si è davvero presentato.
        """
        if not headers:
            return None
        user = sip_id(headers.get("from", ""))
        if not user:
            return None
        device_id = (headers.get("mobile-imei") or "").strip()
        via_chain = headers.get("_via_all") or ([headers["via"]] if headers.get("via") else [])
        address = ""
        for via in via_chain:
            # L'ultimo Via della catena è quello del mittente originale, ed è
            # lì che compare il suo indirizzo pubblico o di LAN.
            received, rport = _first(_RECEIVED, via), _first(_RPORT, via)
            if received:
                address = f"{received}:{rport}" if rport else received
        return self._merge(
            key=device_id or f"{user}@{address}" or user,
            sip_id=user,
            device_id=device_id,
            name=(headers.get("myname") or "").strip(),
            user_agent=(headers.get("user-agent") or "").strip(),
            address=address,
            is_self=bool(own_device_id and device_id == own_device_id),
        )

    def _merge(self, key: str, **values) -> Device:
        device = self._devices.get(key)
        if device is None:
            if len(self._devices) >= self._max_devices:
                # Impianto anomalo o traffico inatteso: meglio dimenticare il
                # più vecchio che crescere senza limite.
                oldest = min(self._devices, key=lambda k: self._devices[k].last_seen)
                del self._devices[oldest]
            device = Device(sip_id=values.get("sip_id", ""))
            self._devices[key] = device
        for name, value in values.items():
            # Un dato assente in questo messaggio non deve cancellare quello
            # che sappiamo già da un messaggio precedente.
            if value in ("", None, False) and getattr(device, name, None):
                continue
            setattr(device, name, value)
        device.last_seen = time.time()
        return device

    def forget_bindings(self) -> None:
        """Dimentica lo stato di registrazione prima di rileggerlo."""
        for device in self._devices.values():
            device.registered = False

    # ─── Lettura ─────────────────────────────────────────────────────

    def snapshot(self) -> list[dict]:
        """I dispositivi noti, i più recenti per primi."""
        return [
            device.as_dict()
            for device in sorted(
                self._devices.values(), key=lambda d: d.last_seen, reverse=True
            )
        ]

    def describe(self, names: dict[str, str] | None = None) -> list[str]:
        """Righe leggibili, per diagnostica e log."""
        lines = []
        for device in sorted(self._devices.values(), key=lambda d: d.last_seen, reverse=True):
            label = device.name or (names or {}).get(device.sip_id) or device.sip_id
            parts = [f"{label} ({device.sip_id})"]
            if device.address:
                parts.append(device.address)
            if device.user_agent:
                parts.append(device.user_agent.split("|")[0])
            if device.registered and device.expires:
                parts.append(f"registrato, {device.expires}s")
            if device.is_self:
                parts.append("questa installazione")
            lines.append(" — ".join(parts))
        return lines

    def __len__(self) -> int:
        return len(self._devices)
