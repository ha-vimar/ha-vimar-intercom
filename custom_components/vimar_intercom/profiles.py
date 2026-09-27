"""Profili degli impianti: cosa aspettarsi prima ancora di provarci.

Gli impianti Vimar non si comportano allo stesso modo, e le differenze non sono
dettagli: cambiano il trasporto da usare e la cifratura del media. Questo
modulo raccoglie ciò che sappiamo per famiglia e lo usa come **punto di
partenza**, non come verità: il config flow prova davvero il trasporto e, se il
profilo aveva torto, vince la prova sul campo (vedi ``config_flow``).

Verificato sul campo:

``2F`` — Due Fili Plus, es. Tab 7S 40507
    Registrazione SIP in UDP locale verso il citofono, media RTP in chiaro: la
    targa baresip con SRTP non risponde affatto. È l'impianto su cui è nata
    l'integrazione originale.

``2FV2`` — Due Fili Plus EVO, es. Tab 7S Up 40517 (firmware 2.1.0203)
    L'UDP locale è rifiutato con ``503 You're not allowed to make this
    operation``. Il TCP locale sulla 5060 accetta la registrazione — e anzi è
    così che un dispositivo si abbina, senza passare dall'app — ma il PBX non
    consegna mai le chiamate su quella via: verificato con cattura pacchetti,
    con la registrazione locale viva e attiva mentre la chiamata la ignorava.
    Le chiamate arrivano solo attraverso il relay cloud in TLS, che offre media
    ``RTP/SAVP`` con ``a=crypto``.

``IP`` — impianti IP; non ancora verificato, trattato come 2F.
"""

from __future__ import annotations

from dataclasses import dataclass

# Come ci si aspetta di parlare con l'impianto.
TRANSPORT_LOCAL_UDP = "local_udp"
TRANSPORT_CLOUD_TLS = "cloud_tls"


@dataclass(frozen=True)
class PlantProfile:
    """Cosa aspettarsi da una famiglia di impianti."""

    plant_type: str
    label: str
    transport: str
    #: ``None`` significa "decidilo dall'offerta": è sempre la scelta migliore
    #: quando rispondiamo, e l'unica corretta su impianti misti.
    media_encryption: bool | None
    verified: bool
    notes: str

    @property
    def prefers_local_udp(self) -> bool:
        return self.transport == TRANSPORT_LOCAL_UDP


_DUE_FILI = PlantProfile(
    plant_type="2F",
    label="Due Fili Plus",
    transport=TRANSPORT_LOCAL_UDP,
    media_encryption=False,
    verified=True,
    notes=(
        "Registrazione in UDP locale verso il citofono; media RTP in chiaro "
        "(la targa non risponde se le si offre SRTP)."
    ),
)

_DUE_FILI_EVO = PlantProfile(
    plant_type="2FV2",
    label="Due Fili Plus EVO",
    transport=TRANSPORT_CLOUD_TLS,
    media_encryption=True,
    verified=True,
    notes=(
        "L'UDP locale è rifiutato con 503. Il TCP locale accetta la "
        "registrazione ma il PBX non consegna le chiamate su quella via: "
        "passano dal relay cloud in TLS, con media SRTP."
    ),
)

_IP = PlantProfile(
    plant_type="IP",
    label="Impianto IP",
    transport=TRANSPORT_LOCAL_UDP,
    media_encryption=None,
    verified=False,
    notes="Non ancora verificato: si parte come Due Fili e si corregge provando.",
)

_UNKNOWN = PlantProfile(
    plant_type="",
    label="Impianto sconosciuto",
    transport=TRANSPORT_LOCAL_UDP,
    media_encryption=None,
    verified=False,
    notes=(
        "Tipo di impianto non riconosciuto: si prova prima il locale, poi il "
        "cloud, e si tiene quello che funziona."
    ),
)

PROFILES: dict[str, PlantProfile] = {
    p.plant_type: p for p in (_DUE_FILI, _DUE_FILI_EVO, _IP)
}

# Modelli visti sul campo, solo per dare un nome leggibile a chi configura.
KNOWN_MODELS: dict[str, str] = {
    "40507": "Elvox Tab 7S (Due Fili Plus)",
    "40517": "Elvox Tab 7S Up (Due Fili Plus EVO)",
    "40515": "Elvox Tab 5S Up (Due Fili Plus EVO)",
}


def profile_for(plant_type: str | None) -> PlantProfile:
    """Il profilo della famiglia, o quello prudente se non la riconosciamo."""
    return PROFILES.get((plant_type or "").strip().upper(), _UNKNOWN)


def model_name(product_code: str | None) -> str:
    """Nome leggibile del modello dal codice prodotto del QR (campo ``pc``)."""
    return KNOWN_MODELS.get((product_code or "").strip(), "")


def describe(plant_type: str | None, product_code: str | None = None) -> str:
    """Riga di riepilogo per il config flow e la diagnostica."""
    profile = profile_for(plant_type)
    model = model_name(product_code)
    head = f"{model} — {profile.label}" if model else profile.label
    transport = (
        "UDP locale" if profile.prefers_local_udp else "cloud TLS"
    )
    confidence = "verificato" if profile.verified else "da verificare"
    return f"{head}: trasporto atteso {transport} ({confidence})"
