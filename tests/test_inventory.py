"""Device inventory built from the SIP traffic the integration already sees.

Every mobile device on these plants shares one SIP user, so the user cannot
otherwise tell which phones are paired — and that matters, because generating a
new pairing QR rotates the shared credential and unpairs the others.

The header shapes below are taken from real traffic captured on a Tab 7S Up.
"""
import pytest

inv = pytest.importorskip("custom_components.vimar_intercom.inventory")

OWN = "0123456789abcdef"

IPHONE_MESSAGE = {
    "from": "<sip:60901@127.0.0.1>;tag=sNnYYRfd8",
    "to": "<sip:61000@127.0.0.1>",
    "myname": "Kitchen iPhone",
    "mobile-imei": "E4380B63-A490-4B0E-9A24-02820BEC1C56",
    "user-agent": "TOGA_iPhone16,1_iOS27.0/5.4.73|AppVer:2.4.5|ProtVer:1.0|",
    "_via_all": [
        "SIP/2.0/TLS 63.34.36.117:7042;rport;branch=z9hG4bK.4cc",
        "SIP/2.0/TLS 192.0.2.17:5091;rport=35197;branch=z9hG4bK.Da4;received=198.51.100.65",
        "SIP/2.0/TLS 192.0.2.97:64406;branch=z9hG4bK.55o;rport=64406;received=192.0.2.97",
    ],
}

ENTRANCE_INVITE = {
    "from": "<sip:55001@127.0.0.1>;tag=IMWQLeahd",
    "user-agent": "Linphonec/3.8.0-linphone-daemon (belle-sip/1.4.0)",
    "via": "SIP/2.0/TLS 63.34.36.117:7042;rport=7042;received=63.34.36.117",
}


class TestSipId:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("<sip:60901@127.0.0.1>;tag=x", "60901"),
            ("sip:55001@plant.example;transport=tls", "55001"),
            ("<sips:60999@1.2.3.4:5060>", "60999"),
            ("", ""),
            ("not a uri", ""),
        ],
    )
    def test_extracts_the_user(self, value, expected):
        assert inv.sip_id(value) == expected


class TestBindings:
    def test_a_registrar_contact_becomes_a_registered_device(self):
        inventory = inv.DeviceInventory()
        device = inventory.note_binding(
            '<sip:60999@198.51.100.65:53884;transport=tls>'
            ';+sip.instance="<urn:uuid:0123456789abcdef>";expires=3600',
            own_device_id=OWN,
        )
        assert device.sip_id == "60999"
        assert device.device_id == OWN
        assert device.expires == 3600
        assert device.registered is True
        assert device.is_self is True, "our own binding should be recognisable"

    def test_another_phone_on_the_shared_account_is_a_separate_device(self):
        inventory = inv.DeviceInventory()
        inventory.note_binding(
            '<sip:60999@1.1.1.1:100;transport=tls>;+sip.instance="<urn:uuid:aaa>";expires=900',
            own_device_id=OWN,
        )
        inventory.note_binding(
            '<sip:60999@2.2.2.2:200;transport=tls>;+sip.instance="<urn:uuid:bbb>";expires=900',
            own_device_id=OWN,
        )
        assert len(inventory) == 2, "same SIP user, different instances, different devices"
        assert not any(d["is_self"] for d in inventory.snapshot())

    def test_the_same_binding_seen_twice_is_one_device(self):
        inventory = inv.DeviceInventory()
        contact = '<sip:60999@1.1.1.1:100>;+sip.instance="<urn:uuid:aaa>";expires=900'
        inventory.note_binding(contact)
        inventory.note_binding(contact)
        assert len(inventory) == 1

    def test_nonsense_is_ignored(self):
        inventory = inv.DeviceInventory()
        assert inventory.note_binding("") is None
        assert inventory.note_binding("<sip:@>") is None
        assert len(inventory) == 0

    def test_registration_state_can_be_cleared_before_a_refresh(self):
        inventory = inv.DeviceInventory()
        inventory.note_binding('<sip:60999@1.1.1.1>;+sip.instance="<urn:uuid:aaa>";expires=900')
        inventory.forget_bindings()
        assert inventory.snapshot()[0]["registered"] is False


class TestPeers:
    def test_an_incoming_message_identifies_the_phone_that_sent_it(self):
        inventory = inv.DeviceInventory()
        device = inventory.note_peer(IPHONE_MESSAGE, own_device_id=OWN)
        assert device.sip_id == "60901"
        assert device.name == "Kitchen iPhone"
        assert device.user_agent.startswith("TOGA_iPhone16,1_iOS27.0")
        assert device.address == "192.0.2.97:64406", "the last Via hop is the sender"
        assert device.is_self is False

    def test_a_device_without_identity_headers_is_still_recorded(self):
        inventory = inv.DeviceInventory()
        device = inventory.note_peer(ENTRANCE_INVITE)
        assert device.sip_id == "55001"
        assert device.user_agent.startswith("Linphonec")
        assert device.address == "63.34.36.117:7042"

    def test_a_later_message_enriches_rather_than_erases(self):
        """A request that omits MyName must not wipe a name we already know."""
        inventory = inv.DeviceInventory()
        inventory.note_peer(IPHONE_MESSAGE)
        inventory.note_peer({
            "from": IPHONE_MESSAGE["from"],
            "mobile-imei": IPHONE_MESSAGE["mobile-imei"],
        })
        assert len(inventory) == 1
        assert inventory.snapshot()[0]["name"] == "Kitchen iPhone"

    def test_a_peer_and_its_binding_merge_into_one_device(self):
        inventory = inv.DeviceInventory()
        inventory.note_peer({
            "from": "<sip:60999@127.0.0.1>",
            "mobile-imei": "aaa",
            "myname": "pixel 9",
        })
        inventory.note_binding('<sip:60999@1.1.1.1>;+sip.instance="<urn:uuid:aaa>";expires=900')
        assert len(inventory) == 1
        device = inventory.snapshot()[0]
        assert device["name"] == "pixel 9" and device["registered"] is True

    def test_nonsense_is_ignored(self):
        inventory = inv.DeviceInventory()
        assert inventory.note_peer({}) is None
        assert inventory.note_peer({"from": "garbage"}) is None


class TestReporting:
    def test_the_newest_device_comes_first(self):
        inventory = inv.DeviceInventory()
        inventory.note_peer(ENTRANCE_INVITE)
        inventory.note_peer(IPHONE_MESSAGE)
        assert [d["sip_id"] for d in inventory.snapshot()] == ["60901", "55001"]

    def test_lines_are_readable_and_fall_back_to_known_names(self):
        inventory = inv.DeviceInventory()
        inventory.note_peer(ENTRANCE_INVITE)
        line = inventory.describe({"55001": "Targa Esterna"})[0]
        assert line.startswith("Targa Esterna (55001)")
        assert "63.34.36.117:7042" in line
        assert "|" not in line, "the user-agent tail is noise"

    def test_the_inventory_cannot_grow_without_bound(self):
        inventory = inv.DeviceInventory(max_devices=3)
        for index in range(10):
            inventory.note_peer({"from": f"<sip:6000{index}@x>", "mobile-imei": f"id{index}"})
        assert len(inventory) == 3


def test_our_own_binding_is_labelled_with_the_paired_name():
    """A Contact carries no name, and an unlabelled id helps nobody."""
    inventory = inv.DeviceInventory()
    inventory.note_binding(
        f'<sip:60999@1.1.1.1>;+sip.instance="<urn:uuid:{OWN}>";expires=3600',
        own_device_id=OWN,
        own_name="Home Assistant",
    )
    line = inventory.describe()[0]
    assert line.startswith("Home Assistant (60999)")
    assert "questa installazione" in line


def test_another_devices_binding_is_not_given_our_name():
    inventory = inv.DeviceInventory()
    inventory.note_binding(
        '<sip:60999@2.2.2.2>;+sip.instance="<urn:uuid:someone-else>";expires=900',
        own_device_id=OWN,
        own_name="Home Assistant",
    )
    assert inventory.snapshot()[0]["name"] == ""
