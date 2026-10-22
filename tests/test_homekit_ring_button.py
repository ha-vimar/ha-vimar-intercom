"""The ring as a programmable button for Home automations: optional, off by default.

Turning it on or off must not renumber the other services: the Home app keys
rooms, names and automations on the IIDs, and the gate would have to be set up
again in the Home app.
"""
import types

import pytest

from custom_components.vimar_intercom import const
from custom_components.vimar_intercom import homekit_accessory as hk

loader = pytest.importorskip("pyhap.loader")


def build(**kwargs):
    driver = types.SimpleNamespace(loader=loader.get_loader(), safe_mode=False)
    return hk.VimarDoorbell(driver, "Citofono", hass=None, hub=types.SimpleNamespace(),
                            stream_address="127.0.0.1", serial="x", **kwargs)


def iids(acc):
    out = {}
    for s in acc.services:
        out[(s.display_name, None)] = acc.iid_manager.get_iid(s)
        for c in s.characteristics:
            out[(s.display_name, c.display_name)] = acc.iid_manager.get_iid(c)
    return out


def names(acc):
    return [s.display_name for s in acc.services]


def test_the_button_is_off_by_default():
    assert const.DEFAULT_HOMEKIT_RING_BUTTON is False
    assert "StatelessProgrammableSwitch" not in names(build())
    assert "Doorbell" in names(build())


def test_the_button_is_there_when_turned_on():
    assert "StatelessProgrammableSwitch" in names(build(ring_button=True))


def test_turning_it_on_or_off_keeps_every_other_iid():
    on, off = iids(build(ring_button=True)), iids(build(ring_button=False))
    assert off, "the accessory has services"
    assert all(on[key] == iid for key, iid in off.items())
    assert ("LockMechanism", "LockTargetState") in off


def test_a_ring_without_the_button_still_rings_the_doorbell():
    acc = build()
    acc._hub = types.SimpleNamespace(in_call=True)
    acc._smooth = False
    pressed = []
    acc._char_ring = types.SimpleNamespace(set_value=pressed.append)
    acc.ring()
    assert pressed == [0]


def test_a_ring_presses_the_button_when_it_is_on():
    acc = build(ring_button=True)
    acc._hub = types.SimpleNamespace(in_call=True)
    acc._smooth = False
    pressed = []
    acc._char_ring = types.SimpleNamespace(set_value=lambda v: None)
    acc._char_ring_switch = types.SimpleNamespace(set_value=pressed.append)
    acc.ring()
    assert pressed == [0]
