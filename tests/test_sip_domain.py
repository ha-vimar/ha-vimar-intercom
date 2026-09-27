"""The Digest realm is the domain of the registrar in use.

A QR carries two different domains: the intercom's local PBX domain and the
cloud relay domain. Using one where the other is required produces an HA1 that
cannot authenticate (upstream issue #1).
"""
import hashlib

import pytest

qr = pytest.importorskip("custom_components.vimar_intercom.qr_decoder")
runtime = pytest.importorskip("custom_components.vimar_intercom.runtime")

FIELDS = {
    "id": "60999",
    "pwd": "synthetic-secret",
    "domain": "127.0.0.1",
    "cdomain": "020000000001.FFFFFFFFFF.ipvdes.vimar.cloud",
    "proxy": "192.0.2.17",
    "cproxy": "ipvdes.vimar.cloud",
    "mac": "02:00:00:00:00:01",
}


def ha1(domain):
    return hashlib.md5(f"60999:{domain}:synthetic-secret".encode()).hexdigest()


def test_both_domains_are_extracted_separately():
    creds = qr.extract_sip_credentials(FIELDS)
    assert creds["local_domain"] == "127.0.0.1"
    assert creds["cloud_domain"] == "020000000001.FFFFFFFFFF.ipvdes.vimar.cloud"


def test_a_cloud_only_qr_still_yields_a_domain():
    creds = qr.extract_sip_credentials({k: v for k, v in FIELDS.items() if k != "domain"})
    assert creds["sip_domain"] == FIELDS["cdomain"]


def configure(**overrides):
    data = dict(qr.extract_sip_credentials(FIELDS), local_proxy="192.0.2.17", **overrides)
    runtime.configure(data)


def test_cloud_mode_authenticates_against_the_cloud_realm():
    configure(use_local_udp=False)
    assert runtime.SIP_DOMAIN == FIELDS["cdomain"]
    assert runtime.SIP_HA1 == ha1(FIELDS["cdomain"])


def test_local_mode_authenticates_against_the_local_realm():
    configure(use_local_udp=True)
    assert runtime.SIP_DOMAIN == "127.0.0.1"
    assert runtime.SIP_HA1 == ha1("127.0.0.1")


def test_a_stale_stored_ha1_is_recomputed_for_the_domain_in_use():
    """An entry saved before this fix holds an HA1 for the wrong realm."""
    configure(use_local_udp=False, sip_ha1="0" * 32)
    assert runtime.SIP_HA1 == ha1(FIELDS["cdomain"])


def test_a_legacy_entry_without_a_cloud_domain_warns_and_does_not_crash(caplog):
    runtime.configure({
        "sip_user": "60999", "sip_password": "synthetic-secret",
        "sip_domain": "127.0.0.1", "use_local_udp": False,
    })
    assert runtime.SIP_DOMAIN == "127.0.0.1"
    assert "cloud" in caplog.text.lower()


def test_targets_follow_the_selected_domain():
    configure(use_local_udp=False)
    assert runtime.INTERCOM.endswith(f"@{FIELDS['cdomain']}")
    configure(use_local_udp=True)
    assert runtime.INTERCOM.endswith("@127.0.0.1")


def test_device_identity_comes_from_the_entry():
    """The pairing binds (identifier, name); both must survive a restart."""
    configure(use_local_udp=False, device_imei="351234567890123",
              device_uuid="0123456789abcdef", device_name="Test")
    assert runtime.DEVICE_IMEI == "351234567890123"
    assert runtime.DEVICE_UUID == "0123456789abcdef"
    assert runtime.DEVICE_NAME == "Test"


def test_a_blank_device_name_falls_back_to_the_default():
    const = pytest.importorskip("custom_components.vimar_intercom.const")
    configure(use_local_udp=False, device_name="   ")
    assert runtime.DEVICE_NAME == const.MY_NAME


def test_entries_from_the_fork_keep_their_cloud_domain():
    """Entries saved by the m4r1k fork call the cloud domain sip_cloud_domain."""
    data = dict(qr.extract_sip_credentials(FIELDS), use_local_udp=False)
    data["sip_cloud_domain"] = data.pop("cloud_domain")
    data["sip_domain"] = "127.0.0.1"
    runtime.configure(data)
    assert runtime.SIP_DOMAIN == FIELDS["cdomain"]


def test_auto_call_targets_use_runtime_values_not_constants():
    """C.SIP_DOMAIN does not exist; using it broke every auto-call."""
    const = pytest.importorskip("custom_components.vimar_intercom.const")
    assert not hasattr(const, "SIP_DOMAIN")

    configure(use_local_udp=False)
    assert runtime.SIP_DOMAIN and runtime.CAMERA_TARGET
    assert runtime.CAMERA_TARGET == const.CAMERA_TARGET


def test_the_camera_target_is_installation_specific():
    configure(use_local_udp=False, camera_target="55200")
    assert runtime.CAMERA_TARGET == "55200"
    configure(use_local_udp=False, camera_target="  ")
    assert runtime.CAMERA_TARGET == pytest.importorskip(
        "custom_components.vimar_intercom.const"
    ).CAMERA_TARGET
