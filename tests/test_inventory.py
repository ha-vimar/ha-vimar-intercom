"""Device inventory built from the SIP traffic the integration already sees.

Every mobile device on these plants shares one SIP user, so the user cannot
otherwise tell which phones are paired, and that matters, because generating a
new pairing QR rotates the shared credential and unpairs the others.

The header shapes below are taken from real traffic captured on a Tab 7S Up.
"""
import importlib

import pytest

from custom_components.vimar_intercom import inventory as inv

OWN = "0123456789abcdef"

IPHONE_MESSAGE = {
    "from": "<sip:60901@127.0.0.1>;tag=tag-iphone",
    "to": "<sip:61000@127.0.0.1>",
    "myname": "Kitchen iPhone",
    "mobile-imei": "00000000-0000-4000-8000-000000000001",
    "user-agent": "TOGA_iPhone16,1_iOS27.0/5.4.73|AppVer:2.4.5|ProtVer:1.0|",
    "_via_all": [
        "SIP/2.0/TLS 203.0.113.10:7042;rport;branch=z9hG4bK.relay",
        "SIP/2.0/TLS 192.0.2.17:5091;rport=40000;branch=z9hG4bK.proxy;received=198.51.100.65",
        "SIP/2.0/TLS 192.0.2.97:40002;branch=z9hG4bK.phone;rport=40002;received=192.0.2.97",
    ],
}

ENTRANCE_INVITE = {
    "from": "<sip:55001@127.0.0.1>;tag=tag-panel",
    "user-agent": "Linphonec/3.8.0-linphone-daemon (belle-sip/1.4.0)",
    "via": "SIP/2.0/TLS 203.0.113.10:7042;rport=7042;received=203.0.113.10",
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
        assert device.address == "192.0.2.97:40002", "the last Via hop is the sender"
        assert device.is_self is False

    def test_a_device_without_identity_headers_is_still_recorded(self):
        inventory = inv.DeviceInventory()
        device = inventory.note_peer(ENTRANCE_INVITE)
        assert device.sip_id == "55001"
        assert device.user_agent.startswith("Linphonec")
        assert device.address == "203.0.113.10:7042"

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
    def test_the_newest_device_comes_first(self, monkeypatch):
        # A clock of our own: two devices noted within one tick of a coarse
        # system clock (about 15 ms on Windows) have the same last_seen.
        ticks = iter(range(1000, 2000))
        monkeypatch.setattr(inv.time, "time", lambda: float(next(ticks)))
        inventory = inv.DeviceInventory()
        inventory.note_peer(ENTRANCE_INVITE)
        inventory.note_peer(IPHONE_MESSAGE)
        assert [d["sip_id"] for d in inventory.snapshot()] == ["60901", "55001"]

    def test_lines_are_readable_and_fall_back_to_known_names(self):
        inventory = inv.DeviceInventory()
        inventory.note_peer(ENTRANCE_INVITE)
        line = inventory.describe({"55001": "Targa Esterna"})[0]
        assert line.startswith("Targa Esterna (55001)")
        assert "203.0.113.10:7042" in line
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
    assert "this installation" in line


def test_another_devices_binding_is_not_given_our_name():
    inventory = inv.DeviceInventory()
    inventory.note_binding(
        '<sip:60999@2.2.2.2>;+sip.instance="<urn:uuid:someone-else>";expires=900',
        own_device_id=OWN,
        own_name="Home Assistant",
    )
    assert inventory.snapshot()[0]["name"] == ""


class TestFromTheRegistrar:
    """The hooks in sip_client: every binding of a 200 OK reaches the list."""

    RESPONSE = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS 10.0.0.2:5061;branch=z9hG4bK1\r\n"
        "Contact: <sip:60999@1.1.1.1:100;transport=tls>;+sip.instance=\"<urn:uuid:aaa>\";expires=900, "
        "<sip:60999@2.2.2.2:200;transport=tls>;+sip.instance=\"<urn:uuid:b,b>\";expires=600\r\n"
        "Contact: <sip:60999@3.3.3.3:300;transport=tls>;+sip.instance=\"<urn:uuid:ours>\";expires=3600\r\n"
        "Content-Length: 0\r\n\r\n"
    )

    def test_every_contact_line_and_comma_separated_binding_is_kept(self):
        sip = importlib.import_module("custom_components.vimar_intercom.sip_client")
        _, hdrs, _, _ = sip._parse(self.RESPONSE)
        contacts = sip._split_contacts(hdrs)
        assert len(contacts) == 3, "a comma inside quotes does not split a binding"
        assert contacts[1].endswith("expires=600")

    def test_our_binding_is_recognised_by_its_instance_uuid(self, monkeypatch):
        sip = importlib.import_module("custom_components.vimar_intercom.sip_client")
        monkeypatch.setattr(sip, "DEVICES", inv.DeviceInventory())
        monkeypatch.setattr(sip.R, "DEVICE_UUID", "ours")
        monkeypatch.setattr(sip.R, "DEVICE_IMEI", "351234567890123")
        _, hdrs, _, _ = sip._parse(self.RESPONSE)
        sip._record_bindings(hdrs)
        devices = sip.DEVICES.snapshot()
        assert len(devices) == 3
        ours = [d for d in devices if d["is_self"]]
        assert len(ours) == 1 and ours[0]["name"] == sip.C.MY_NAME


class TestRestore:
    """Phones only appear when they talk to us: the list has to survive a restart."""

    def test_a_saved_list_comes_back_without_claiming_registration(self):
        before = inv.DeviceInventory()
        before.note_peer(IPHONE_MESSAGE)
        before.note_binding('<sip:60999@1.1.1.1>;+sip.instance="<urn:uuid:ours>";expires=900',
                            own_device_id="ours", own_name="Home Assistant")
        saved = before.snapshot()

        after = inv.DeviceInventory()
        assert after.load(saved) == 2
        restored = {d["sip_id"]: d for d in after.snapshot()}
        assert restored["60901"]["name"] == "Kitchen iPhone"
        assert not any(d["registered"] for d in after.snapshot())

    def test_a_live_device_is_not_overwritten_by_the_saved_one(self):
        live = inv.DeviceInventory()
        live.note_peer(IPHONE_MESSAGE)
        stale = [{"sip_id": "60901", "device_id": IPHONE_MESSAGE["mobile-imei"], "name": "old name"}]
        assert live.load(stale) == 0
        assert live.snapshot()[0]["name"] == "Kitchen iPhone"

    def test_garbage_in_the_saved_state_is_ignored(self):
        assert inv.DeviceInventory().load([None, {}, {"name": "x"}, "text"]) == 0
        assert inv.DeviceInventory().load(None) == 0


class TestHardening:
    def test_long_values_are_capped(self):
        inventory = inv.DeviceInventory()
        device = inventory.note_peer({"from": "<sip:60901@d>", "mobile-imei": "x" * 500,
                                      "myname": "n" * 500, "user-agent": "u" * 500})
        assert len(device.name) == len(device.user_agent) == len(device.device_id) == 128
        loaded = inv.DeviceInventory()
        loaded.load([{"sip_id": "60902", "name": "n" * 500, "address": "a" * 500}])
        d = loaded.snapshot()[0]
        assert len(d["name"]) == len(d["address"]) == 128

    def test_saved_values_of_the_wrong_type_are_ignored(self):
        inventory = inv.DeviceInventory()
        assert inventory.load([{"sip_id": "60901", "name": ["x"], "device_id": 5,
                                "is_self": "yes"}]) == 1
        d = inventory.snapshot()[0]
        assert d["name"] == "" and d["device_id"] == "" and d["is_self"] is False
        assert inventory.load([{"sip_id": {"a": 1}}]) == 0

    def test_one_phone_is_one_device_whatever_the_case(self):
        inventory = inv.DeviceInventory()
        inventory.note_peer({"from": "<sip:60999@d>", "mobile-imei": "AbC-123"})
        inventory.note_binding('<sip:60999@1.1.1.1>;+sip.instance="<urn:uuid:abc-123>";expires=900')
        assert len(inventory) == 1

    def test_an_expired_binding_is_recorded(self):
        inventory = inv.DeviceInventory()
        contact = '<sip:60999@1.1.1.1>;+sip.instance="<urn:uuid:aaa>";expires={}'
        inventory.note_binding(contact.format(900))
        inventory.note_binding(contact.format(0))
        assert inventory.snapshot()[0]["expires"] == 0

    def test_a_missing_boolean_does_not_clear_a_known_one(self):
        inventory = inv.DeviceInventory()
        inventory.note_binding('<sip:60999@1.1.1.1>;+sip.instance="<urn:uuid:ours>";expires=900',
                               own_device_id="ours")
        inventory.note_peer({"from": "<sip:60999@d>", "mobile-imei": "ours"})
        assert inventory.snapshot()[0]["is_self"] is True


def test_a_crafted_sip_header_cannot_stall_the_uri_regexes():
    """"sip:" repeated with no "@" made every start scan to the end of the header."""
    import time

    header = "sip:" * 16_000
    start = time.perf_counter()
    assert inv.sip_id(header) == ""
    assert inv._uri_host(header) == ""
    assert time.perf_counter() - start < 0.5
    assert inv.sip_id("<sip:60901@127.0.0.1>;tag=x") == "60901"
