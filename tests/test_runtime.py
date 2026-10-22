"""Test per runtime.py: risoluzione di sga_target/picg_target da options flow.

runtime.py è un modulo puro (nessun import homeassistant), popolato da
configure() con i dati salvati nel config entry. Qui si verifica solo la
logica di risoluzione SGA/PICG (fallback al default storico in const.py
quando l'utente non ha impostato nulla in options), non l'intero modulo.
"""
from __future__ import annotations

from custom_components.vimar_intercom import const, runtime


def _base_data(**overrides) -> dict:
    data = {
        "sip_user": "u",
        "sip_password": "p",
        "sip_domain": "example.test",
    }
    data.update(overrides)
    return data


def test_default_fallback_when_not_set():
    runtime.configure(_base_data())
    assert runtime.SGA_TARGET == const.SGA_TARGET
    assert runtime.PICG_TARGET == const.PICG_TARGET


def test_sga_target_override_only():
    runtime.configure(_base_data(sga_target="12345"))
    assert runtime.SGA_TARGET == "12345"
    # picg_target non impostato → resta il default storico
    assert runtime.PICG_TARGET == const.PICG_TARGET


def test_sga_and_picg_target_independent_override():
    runtime.configure(_base_data(sga_target="12345", picg_target="67890"))
    assert runtime.SGA_TARGET == "12345"
    assert runtime.PICG_TARGET == "67890"


def test_blank_values_fall_back_to_default():
    # Stringa vuota o solo spazi = "non impostato", non un valore valido.
    runtime.configure(_base_data(sga_target="   ", picg_target=""))
    assert runtime.SGA_TARGET == const.SGA_TARGET
    assert runtime.PICG_TARGET == const.PICG_TARGET


def test_door_esterno_uses_resolved_sga_target():
    runtime.configure(_base_data(sga_target="99999", sip_domain="sip.example.test"))
    assert runtime.DOOR_ESTERNO == "sip:99999@sip.example.test"


def test_intercom_calls_the_camera_target_not_the_sga():
    """Issue #3: le chiamate vanno alla targa video. L'SGA riceve i comandi ma
    non accetta un INVITE su tutti gli impianti (488 qui, 488/408 sul 40515)."""
    runtime.configure(_base_data(sga_target="61000", camera_target="55001",
                                 sip_domain="sip.example.test"))
    assert runtime.INTERCOM == "sip:55001@sip.example.test"
    assert runtime.DOOR_ESTERNO == "sip:61000@sip.example.test"


def test_a_blank_internal_panel_follows_the_configured_picg():
    runtime.configure(_base_data(picg_target="60001", internal_panel_target=" "))
    assert runtime.INTERNAL_PANEL_TARGET == "60001"


def test_camera_and_internal_panel_targets_override():
    runtime.configure(_base_data(camera_target="55001", internal_panel_target="60001"))
    assert runtime.CAMERA_TARGET == "55001"
    assert runtime.INTERNAL_PANEL_TARGET == "60001"


def test_camera_and_internal_panel_targets_blank_fall_back():
    runtime.configure(_base_data(camera_target="  ", internal_panel_target=""))
    assert runtime.CAMERA_TARGET == const.CAMERA_TARGET
    # «Chiama Casa» without a target calls the apartment's PICG, not a fixed 55002
    assert runtime.INTERNAL_PANEL_TARGET == runtime.PICG_TARGET
    assert runtime.INTERCOM.startswith(f"sip:{const.CAMERA_TARGET}@")


def test_reconfigure_resets_previous_override():
    """Un configure() successivo senza sga_target non deve trascinarsi dietro
    il valore della chiamata precedente: deve tornare al default."""
    runtime.configure(_base_data(sga_target="11111"))
    assert runtime.SGA_TARGET == "11111"
    runtime.configure(_base_data())
    assert runtime.SGA_TARGET == const.SGA_TARGET




def test_configure_hands_the_identity_to_the_log_masking():
    """#146: the account's SIP id, IMEI, UUID and name never reach a log line in clear."""
    from custom_components.vimar_intercom import log_redact
    runtime.configure({"sip_user": "7712345", "sip_domain": "d", "device_imei": "358240051111110",
                       "device_uuid": "0f8fad5b-d9cb-469f-a165-70867728950e", "device_name": "Casa Rossi HA"})
    try:
        out = log_redact.redact_plant("7712345 358240051111110 0f8fad5b-d9cb-469f-a165-70867728950e Casa Rossi HA")
        for value in ("7712345", "358240051111110", "0f8fad5b", "Rossi"):
            assert value not in out
        runtime.configure({"sip_user": "7799999", "sip_domain": "d"})
        assert "7712345" in log_redact.redact_plant("7712345"), "a reconfigure forgets the old identity"
    finally:
        log_redact.set_plant_values([])


def test_configure_masks_the_sip_domain_in_its_three_forms():
    """PROTOCOL §4-bis: the cloud domain whole, without `.<cproxy>`, and with `.` → `_`."""
    from custom_components.vimar_intercom import log_redact
    runtime.configure({"sip_user": "7712345", "sip_domain": "d", "cloud_proxy": "relay.example",
                       "cloud_domain": "home42.plant.relay.example", "local_domain": "192.168.1.50"})
    out = log_redact.redact_plant("sip:x@home42.plant.relay.example /domains/home42.plant/ "
                                  "home42_plant_rubrica via relay.example")
    assert "home42" not in out and "relay.example" in out


def test_a_local_domain_that_is_an_address_is_left_to_the_address_masking():
    """Some Tab 5S have 127.0.0.1 as their local domain: it must not turn every loopback
    line into a domain tag, and the Tab's IP keeps its address shape."""
    from custom_components.vimar_intercom import log_redact
    runtime.configure({"sip_user": "7712345", "sip_domain": "d", "local_domain": "127.0.0.1"})
    assert log_redact.redact_plant("http://127.0.0.1:8123") == "http://127.0.0.1:8123"
    runtime.configure({"sip_user": "7712345", "sip_domain": "d", "local_domain": "192.168.1.50"})
    assert log_redact.redact_plant("sip:55001@192.168.1.50") == "sip:55001@192.x.x.50"


def test_configure_masks_the_intercom_mac_however_it_is_written():
    from custom_components.vimar_intercom import log_redact
    runtime.configure({"sip_user": "7712345", "sip_domain": "d", "mac": "00:1A:2B:3C:4D:5E"})
    out = log_redact.redact_plant("mac 00:1A:2B:3C:4D:5E 00:1a:2b:3c:4d:5e 001A2B3C4D5E 001a2b3c4d5e")
    assert "4D:5E" not in out and "4d:5e" not in out and "3C4D5E" not in out and "3c4d5e" not in out


def test_a_reconfigure_swaps_the_values_in_one_go(monkeypatch):
    """Forget-then-remember left a moment with nothing masked: one assignment now."""
    from custom_components.vimar_intercom import log_redact
    runtime.configure({"sip_user": "7712345", "sip_domain": "d"})
    seen = []
    real = log_redact.set_plant_values
    monkeypatch.setattr(log_redact, "set_plant_values", lambda *a, **k: (seen.append(a), real(*a, **k)))
    runtime.configure({"sip_user": "7799999", "sip_domain": "d"})
    assert len(seen) == 1
    assert "7799999" not in log_redact.redact_plant("7799999")
