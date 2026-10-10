"""1.0.10: «Rispondi» solo durante lo squillo, pulsanti che segnalano l'errore (#23),
sensori "Ultimo ..." ripristinati dopo un riavvio, entità della card dalla camera."""
from __future__ import annotations

import asyncio
import importlib
import re
import sys
import types
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.vimar_intercom import button

# camera.py e sensor.py usano al caricamento enum e dataclass di HA: negli stub (HA non
# installato) servono versioni minime vere, non i jolly.
if getattr(sys.modules.get("homeassistant"), "_is_stub", False):
    sys.modules["homeassistant.components.camera"].CameraEntityFeature = types.SimpleNamespace(STREAM=2)

    @dataclass(frozen=True, kw_only=True)
    class _SensorEntityDescription:
        key: str
        name: str | None = None
        icon: str | None = None
        device_class: str | None = None
        options: list | None = None
        state_class: str | None = None
        native_unit_of_measurement: str | None = None

    class _RestoreSensor:
        pass

    _s = sys.modules["homeassistant.components.sensor"]
    _s.SensorEntityDescription = _SensorEntityDescription
    _s.RestoreSensor = _RestoreSensor
    _s.SensorDeviceClass = types.SimpleNamespace(ENUM="enum", TIMESTAMP="timestamp", DURATION="duration")
    _s.SensorStateClass = types.SimpleNamespace(TOTAL_INCREASING="total_increasing", MEASUREMENT="measurement")
    sys.modules["homeassistant.const"].UnitOfTime = types.SimpleNamespace(SECONDS="s")
camera = importlib.import_module("custom_components.vimar_intercom.camera")
sensor = importlib.import_module("custom_components.vimar_intercom.sensor")

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "vimar_intercom"


from homeassistant.exceptions import HomeAssistantError as _HAError  # noqa: E402


@pytest.fixture(autouse=True)
def _errore_vero(monkeypatch):
    # Negli stub HomeAssistantError è un jolly, non un'eccezione: serve una classe vera.
    monkeypatch.setattr(button, "HomeAssistantError", _HAError)


class _Hub:
    registered = True
    is_ringing = False

    def __init__(self, ok=False):
        self.ok = ok
        self.callbacks = []
        self.stats = {}

    async def async_answer(self):
        return self.ok, "Nessuna chiamata in arrivo"

    async def async_door(self, target=None, command=None):
        return self.ok, "timeout", None

    async def async_call(self, target=None):
        return self.ok, "486"

    async def async_send_command(self, **kw):
        return self.ok, "Timeout"

    def register_state_callback(self, cb):
        self.callbacks.append(cb)

    def unregister_state_callback(self, cb):
        self.callbacks.remove(cb)


# --- Rispondi ---------------------------------------------------------------------

def test_rispondi_disponibile_solo_durante_lo_squillo():
    hub = _Hub()
    b = button.VimarAnswerButton(hub, "e1")
    assert b.available is False
    hub.is_ringing = True
    assert b.available is True
    hub.registered = False
    assert b.available is False


def test_rispondi_scrive_lo_stato_solo_quando_cambia():
    hub = _Hub()
    b = button.VimarAnswerButton(hub, "e1")
    scritture = []
    b.async_write_ha_state = lambda: scritture.append(b.available)
    asyncio.run(b.async_added_to_hass())
    hub.callbacks[0]()          # primo giro: stato iniziale
    hub.callbacks[0]()          # niente di nuovo: nessuna scrittura
    hub.is_ringing = True
    hub.callbacks[0]()
    hub.callbacks[0]()
    hub.is_ringing = False
    hub.callbacks[0]()
    assert scritture == [False, True, False]


# --- Pulsanti che falliscono (#23) --------------------------------------------------

@pytest.mark.parametrize("make", [
    lambda h: button.VimarAnswerButton(h, "e1"),
    lambda h: button.VimarDoorButton(h, "e1", None, "Apri Porta", "door_street", "mdi:door-open"),
    lambda h: button.VimarCallButton(h, "e1"),
    lambda h: button.VimarCallTargetButton(h, "e1", "55100", "Chiama Video (esterno)", "call_ext"),
    lambda h: button.VimarActuatorButton(h, "e1", {"name": "Luce scala", "msg": "OPEN_2", "target": "55001"}),
])
def test_un_pulsante_che_fallisce_solleva_un_errore(make):
    with pytest.raises(_HAError):
        asyncio.run(make(_Hub(ok=False)).async_press())


def test_un_pulsante_riuscito_non_solleva():
    asyncio.run(button.VimarDoorButton(_Hub(ok=True), "e1", None, "Apri Porta", "door_street", "x").async_press())


def test_nessun_pulsante_fallisce_in_silenzio():
    src = (COMPONENT / "button.py").read_text(encoding="utf-8")
    assert not re.search(r"_LOGGER\.error\(", src)


# --- Sensori ripristinati -------------------------------------------------------------

def _desc(key):
    return next(d for d in sensor.SENSORS if d.key == key)


def test_ripristinati_solo_i_sensori_ultimo():
    ripristinati = {d.key for d in sensor.SENSORS if d.restore}
    assert ripristinati == {"last_caller", "last_ring", "last_call_duration", "last_door", "last_missed_call"}


def _sensore(key, hub, *, valore, attributi):
    s = sensor.VimarStatSensor(hub, "e1", _desc(key))

    async def last_data():
        return types.SimpleNamespace(native_value=valore)

    async def last_state():
        return types.SimpleNamespace(attributes=attributi)

    s.async_get_last_sensor_data = last_data
    s.async_get_last_state = last_state
    return s


def _hub_statistiche(**stats):
    hub = _Hub()
    hub.stats = {"last_door_time": None, "last_door_target": None, "last_door_result": None,
                 "door_count": 0, **stats}
    return hub


def test_ultima_apertura_sopravvive_al_riavvio():
    ieri = datetime(2026, 9, 28, 7, 37, 34, tzinfo=UTC)
    hub = _hub_statistiche()
    s = _sensore("last_door", hub, valore=ieri, attributi={
        "target": "Targa", "target_id": "55001", "esito": "OK (200)", "aperture": 1,
        "friendly_name": "Vimar Intercom Intercom Ultima Apertura", "icon": "mdi:door-open"})
    asyncio.run(s.async_added_to_hass())
    assert s.native_value == ieri
    # Solo i nostri attributi: nome e icona li rimette HA.
    assert s.extra_state_attributes == {"target": "Targa", "target_id": "55001",
                                        "esito": "OK (200)", "aperture": 1}


def test_il_valore_nuovo_vince_su_quello_ripristinato():
    ieri = datetime(2026, 9, 27, tzinfo=UTC)
    oggi = datetime(2026, 9, 28, 16, 0, tzinfo=UTC)
    hub = _hub_statistiche()
    s = _sensore("last_door", hub, valore=ieri, attributi={"aperture": 5})
    asyncio.run(s.async_added_to_hass())
    hub.stats.update(last_door_time=oggi, last_door_target="55001", last_door_result="OK (200)", door_count=1)
    assert s.native_value == oggi
    assert s.extra_state_attributes["aperture"] == 1


def test_i_contatori_non_si_ripristinano():
    hub = _hub_statistiche(ring_count=0)
    s = _sensore("ring_count", hub, valore=12, attributi={})
    asyncio.run(s.async_added_to_hass())
    assert s.native_value == 0


# --- Entità della card -------------------------------------------------------------------

def test_la_camera_dice_alla_card_le_entita_vere(monkeypatch):
    registro = {
        ("sensor", "e1_status"): "sensor.ufficio_vimar_intercom_intercom_stato",
        ("sensor", "e1_last_ring"): "sensor.ufficio_vimar_intercom_intercom_ultimo_squillo",
        ("lock", "e1_lock"): "lock.vimar_intercom_street_gate",
        ("switch", "e1_dnd"): "switch.non_disturbare",
        ("switch", "e1_segreteria"): "switch.segreteria",
        ("select", "e1_vm_timeout"): "select.ritardo",
        ("select", "e1_away_file"): "select.file",
        ("text", "e1_away_text"): "text.testo",
    }

    class _Reg:
        def async_get_entity_id(self, domain, platform, unique_id):
            assert platform == "vimar_intercom"
            return registro.get((domain, unique_id))

    monkeypatch.setattr(camera, "er", types.SimpleNamespace(async_get=lambda hass: _Reg()))
    cam = camera.VimarIntercomCamera(_Hub(), "e1", object())
    assert cam.extra_state_attributes == {"card_entities": {
        "status": "sensor.ufficio_vimar_intercom_intercom_stato",
        "last_ring": "sensor.ufficio_vimar_intercom_intercom_ultimo_squillo",
        "lock": "lock.vimar_intercom_street_gate",
        "dnd": "switch.non_disturbare", "segreteria": "switch.segreteria",
        "delay": "select.ritardo", "file": "select.file", "text": "text.testo",
    }}


def test_la_card_non_legge_piu_gli_entity_id_fissi():
    js = (COMPONENT / "www" / "vimar-intercom-card.js").read_text(encoding="utf-8")
    assert not re.search(r"this\._cfg\.(camera|status|lock|last_ring)\b", js)
    assert "card_entities" in js
    assert 'reg[id].platform === "vimar_intercom"' in js


# --- Livello della voce in uscita ----------------------------------------------------------

def test_il_livello_della_voce_distingue_il_silenzio(monkeypatch, caplog):
    import logging
    import struct

    from custom_components.vimar_intercom import media_handler as mh

    caplog.set_level(logging.DEBUG, logger=mh._LOGGER.name)
    orologio = iter([10.0, 12.5, 15.0])
    monkeypatch.setattr(mh.time, "monotonic", lambda: next(orologio))
    monkeypatch.setattr(mh, "_tx_last_log", 0.0)
    monkeypatch.setattr(mh, "_tx_peak", 0)
    mh._note_tx_level(struct.pack("<4h", 5, -40, 12, 0))
    mh._note_tx_level(struct.pack("<3h", 100, -9000, 4000))
    mh._note_tx_level(struct.pack("<2h", 0, 0))
    righe = [r.getMessage() for r in caplog.records if "Voce verso la targa" in r.getMessage()]
    assert righe == ["Voce verso la targa: picco 40/32767 (silenzio)",
                     "Voce verso la targa: picco 9000/32767 (voce)",
                     "Voce verso la targa: picco 0/32767 (silenzio)"]


# --- Rifiuta (603, come l'app) ----------------------------------------------------

def test_rifiuta_disponibile_solo_durante_lo_squillo():
    hub = _Hub()
    b = button.VimarDeclineButton(hub, "e1")
    assert b.available is False and b._attr_unique_id == "e1_decline"
    hub.is_ringing = True
    assert b.available is True


def test_rifiuta_chiama_l_hub_e_segnala_l_errore():
    hub = _Hub()
    chiamate = []

    async def decline():
        chiamate.append(1)
        return hub.ok, "Nessuna chiamata in arrivo"

    hub.async_decline = decline
    b = button.VimarDeclineButton(hub, "e1")
    with pytest.raises(_HAError):
        asyncio.run(b.async_press())
    hub.ok = True
    asyncio.run(b.async_press())
    assert len(chiamate) == 2


def test_hub_decline_risponde_603_solo_se_squilla(monkeypatch):
    """Stesso 603 dell'app (Via/To;tag/From/Call-ID/CSeq, senza corpo), inviato una volta."""
    from custom_components.vimar_intercom import hub as hub_mod
    from custom_components.vimar_intercom import sip_client as sip
    inviati = []

    async def send(m):
        inviati.append(m)

    async def bc(*a, **k):
        pass

    monkeypatch.setattr(sip, "send", send)
    monkeypatch.setattr(sip, "broadcast", bc)
    monkeypatch.setattr(sip.media, "stop_media", bc)
    monkeypatch.setattr(sip, "pending_incoming", {
        "active": True, "via_block": "Via: v\r\n", "to_hdr": "<sip:a>", "from_hdr": "<sip:b>;tag=x",
        "cid": "c1", "cseq": "1 INVITE", "my_tag": "t"})
    h = object.__new__(hub_mod.VimarIntercomHub)
    h._touch = lambda: None
    h._ring_opened = False
    ok, _ = asyncio.run(h.async_decline())
    assert ok and inviati[0].startswith("SIP/2.0 603 Decline\r\n") and "Content-Length: 0" in inviati[0]
    ok, _ = asyncio.run(h.async_decline())   # ormai non squilla più
    assert not ok and len(inviati) == 1


def test_un_attuatore_in_coda_sul_relay_non_e_riuscito():
    """202 dal relay (#120 per la porta): errore tradotto, non «OK»."""
    hub = _Hub()

    async def queued(**_kw):
        return False, button.QUEUED

    hub.async_send_command = queued
    b = button.VimarActuatorButton(hub, "e1", {"name": "Luce scala", "msg": "OPEN_2", "target": "55001"})
    with pytest.raises(_HAError) as err:
        asyncio.run(b.async_press())
    assert err.value.translation_key == "command_queued"
