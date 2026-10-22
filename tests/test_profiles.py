"""Plant profiles: an expectation to start from, never an assumption to keep.

Two families behave differently in ways that decide the transport and the media
profile, and a QR's declared type is an indication, not a guarantee : a new
firmware can contradict it, which is why setup probes rather than trusts.
"""
import pytest

from custom_components.vimar_intercom import profiles
from custom_components.vimar_intercom import qr_decoder as qr


class TestKnownFamilies:
    def test_due_fili_expects_the_local_path(self):
        profile = profiles.profile_for("2F")
        assert profile.prefers_local_udp is True
        assert profile.verified is True

    def test_due_fili_evo_expects_the_cloud_path(self):
        profile = profiles.profile_for("2FV2")
        assert profile.prefers_local_udp is False
        assert profile.transport == profiles.TRANSPORT_CLOUD_TLS
        assert profile.verified is True

    def test_the_type_is_matched_regardless_of_spacing_or_case(self):
        assert profiles.profile_for(" 2fv2 ").plant_type == "2FV2"

    @pytest.mark.parametrize("value", ["", None, "SOMETHING_NEW"])
    def test_an_unknown_plant_falls_back_without_claiming_certainty(self, value):
        profile = profiles.profile_for(value)
        assert profile.verified is False
        assert profile.prefers_local_udp is True, "try the local path first, then the cloud"


class TestDescription:
    def test_a_known_model_is_named(self):
        text = profiles.describe("2FV2", "40517")
        assert "Tab 7S Up" in text
        assert "cloud TLS" in text
        assert "verified" in text

    def test_an_unknown_model_still_describes_the_family(self):
        text = profiles.describe("2F", "99999")
        assert "Due Fili Plus" in text
        assert "local UDP" in text

    def test_an_unverified_family_says_so(self):
        assert "not verified yet" in profiles.describe("IP", "")


class TestFromTheQr:
    def test_the_product_code_reaches_the_profile(self):
        creds = qr.extract_sip_credentials({
            "id": "60999", "pwd": "x", "domain": "127.0.0.1",
            "planttype": "2FV2", "pc": "40517",
        })
        assert creds["plant_type"] == "2FV2"
        assert creds["product_code"] == "40517"
        assert "Tab 7S Up" in profiles.describe(creds["plant_type"], creds["product_code"])
