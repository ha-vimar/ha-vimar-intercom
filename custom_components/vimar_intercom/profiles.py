"""Plant profiles: what to expect before trying.

Vimar plants do not all behave the same way, and the differences decide the
transport and the media encryption. This module collects what is known per
family and uses it as a starting point, not as the truth: setup probes the
transport for real and, when the profile was wrong, the probe wins (see
``config_flow._probe_transport``).

Verified in the field:

``2F``: Due Fili Plus, e.g. Tab 7S 40507
    SIP registration over local UDP to the intercom, plain RTP media: the
    entrance panel does not answer at all when offered SRTP. This is the plant
    the integration was first written for.

``2FV2``: Due Fili Plus EVO, e.g. Tab 7S Up 40517 (firmware 2.1.0203)
    Local UDP is refused with ``503 You're not allowed to make this
    operation``. Local TCP on 5060 accepts the registration, but the PBX never
    delivers calls that way (checked with a packet capture while the local
    registration was alive). Calls only arrive through the cloud relay over
    TLS, which offers ``RTP/SAVP`` media with ``a=crypto``.

``IP``: IP plants; not verified yet, treated like 2F.
"""

from __future__ import annotations

from dataclasses import dataclass

TRANSPORT_LOCAL_UDP = "local_udp"
TRANSPORT_CLOUD_TLS = "cloud_tls"


@dataclass(frozen=True)
class PlantProfile:
    """What to expect from a family of plants."""

    plant_type: str
    label: str
    transport: str
    verified: bool

    @property
    def prefers_local_udp(self) -> bool:
        return self.transport == TRANSPORT_LOCAL_UDP


_DUE_FILI = PlantProfile("2F", "Due Fili Plus", TRANSPORT_LOCAL_UDP, True)
_DUE_FILI_EVO = PlantProfile("2FV2", "Due Fili Plus EVO", TRANSPORT_CLOUD_TLS, True)
_IP = PlantProfile("IP", "IP plant", TRANSPORT_LOCAL_UDP, False)
_UNKNOWN = PlantProfile("", "Unknown plant", TRANSPORT_LOCAL_UDP, False)

PROFILES: dict[str, PlantProfile] = {
    p.plant_type: p for p in (_DUE_FILI, _DUE_FILI_EVO, _IP)
}

# Models seen in the field, only to show a readable name during setup.
KNOWN_MODELS: dict[str, str] = {
    "40507": "Elvox Tab 7S (Due Fili Plus)",
    "40517": "Elvox Tab 7S Up (Due Fili Plus EVO)",
    "40515": "Elvox Tab 5S Up (Due Fili Plus EVO)",
}


def profile_for(plant_type: str | None) -> PlantProfile:
    """The family's profile, or the cautious one when it is not recognised."""
    return PROFILES.get((plant_type or "").strip().upper(), _UNKNOWN)


def model_name(product_code: str | None) -> str:
    """Readable model name from the QR's product code (field ``pc``)."""
    return KNOWN_MODELS.get((product_code or "").strip(), "")


def describe(plant_type: str | None, product_code: str | None = None) -> str:
    """One summary line for the setup form and diagnostics."""
    profile = profile_for(plant_type)
    model = model_name(product_code)
    head = f"{model}, {profile.label}" if model else profile.label
    transport = "local UDP" if profile.prefers_local_udp else "cloud TLS"
    confidence = "verified" if profile.verified else "not verified yet"
    return f"{head}: expected transport {transport} ({confidence})"
