"""The door opens at the panel that owns the relay, not always at the SGA.

On the reference 2F plant the SGA and the door panel are the same address, so
sending OPEN to the SGA worked. On a 2FV2 the SGA is 61000 and the door
belongs to panel 55001: the phonebook says so (ACTUATOR_LIST.GID_PE).
"""
import pytest

runtime = pytest.importorskip("custom_components.vimar_intercom.runtime")

BASE = {"sip_user": "1", "sip_password": "p", "sip_domain": "d", "use_local_udp": True}


def test_the_configured_door_panel_receives_the_open_command():
    runtime.configure({**BASE, "sga_target": "61000", "door_target": "55001"})
    assert runtime.DOOR_ESTERNO == "sip:55001@d"
    assert runtime.INTERCOM == "sip:61000@d", "status commands still go to the SGA"


def test_without_a_door_panel_the_sga_is_used_as_before():
    runtime.configure({**BASE, "sga_target": "61000"})
    assert runtime.DOOR_ESTERNO == "sip:61000@d"


def test_the_phonebook_names_the_door_panel():
    actuators = [
        {"name": "Luce scala", "msg": "ATTUATORE_01", "target": "55001", "icon": "light"},
        {"name": "Serratura", "msg": "OPEN", "target": "55001", "icon": "door"},
    ]
    assert runtime.door_from_actuators(actuators) == "55001"


def test_a_phonebook_without_a_door_says_nothing():
    assert runtime.door_from_actuators([{"name": "x", "target": "AUTO", "icon": "door"}]) == ""
    assert runtime.door_from_actuators([]) == ""


def test_entries_imported_before_the_option_existed_use_the_phonebook():
    """Actuators saved by 1.0.7, no door_target: not the SGA."""
    runtime.configure({**BASE, "sga_target": "61000", "actuators": [
        {"name": "Serratura", "msg": "OPEN", "target": "55001", "icon": "door"}]})
    assert runtime.DOOR_ESTERNO == "sip:55001@d"
