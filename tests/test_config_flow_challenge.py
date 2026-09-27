"""Digest challenge parsing in the setup registration test.

A real challenge arrives as one header line whose first parameter follows the
scheme name. Splitting the raw line on commas loses that first parameter —
usually realm — and a valid challenge is reported as malformed.
"""
import pytest

cf = pytest.importorskip("custom_components.vimar_intercom.config_flow")


def response(header):
    return (
        "SIP/2.0 401 Unauthorized\r\n"
        "Via: SIP/2.0/UDP 192.168.1.10:5060;branch=z9hG4bK1;rport=5060\r\n"
        f"{header}\r\n"
        "Server: Vimar IP-PBX (aarch64/linux)\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def test_the_first_parameter_is_not_lost():
    parsed = cf._parse_challenge(response(
        'WWW-Authenticate: Digest realm="127.0.0.1", nonce="abc123"'
    ))
    assert parsed["realm"] == "127.0.0.1"
    assert parsed["nonce"] == "abc123"


def test_a_quoted_qop_list_containing_commas_is_kept_whole():
    parsed = cf._parse_challenge(response(
        'WWW-Authenticate: Digest realm="r", nonce="n", qop="auth,auth-int", algorithm=MD5'
    ))
    assert parsed["qop"] == "auth,auth-int"
    assert parsed["algorithm"] == "MD5"


def test_unquoted_values_and_opaque_are_read():
    parsed = cf._parse_challenge(response(
        'WWW-Authenticate: Digest realm="r", nonce="n", opaque="o", stale=true'
    ))
    assert parsed["opaque"] == "o"
    assert parsed["stale"] == "true"


def test_a_proxy_challenge_is_accepted_too():
    parsed = cf._parse_challenge(response('Proxy-Authenticate: Digest realm="r", nonce="n"'))
    assert parsed["realm"] == "r"


def test_a_response_without_a_digest_challenge_yields_nothing():
    assert cf._parse_challenge(response("Server: something")) == {}
    assert cf._parse_challenge(response("WWW-Authenticate: Basic realm=\"r\"")) == {}


class TestDigestHeader:
    """The builder is shared by the local and cloud setup tests."""

    def test_matches_the_published_rfc_2617_vector(self):
        header = cf._digest_header(
            user="Mufasa", password="Circle Of Life", realm="testrealm@host.com",
            nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", uri="/dir/index.html",
            method="GET", qop="auth", cnonce="0a4f113b",
        )
        assert 'response="6629fae49393a05397450978507c4ef1"' in header
        assert "qop=auth" in header and "nc=00000001" in header

    def test_legacy_form_is_used_when_no_qop_is_offered(self):
        header = cf._digest_header(
            user="u", password="p", realm="r", nonce="n", uri="sip:d",
        )
        assert "qop" not in header and "cnonce" not in header

    def test_a_qop_list_selects_auth_and_opaque_is_echoed(self):
        header = cf._digest_header(
            user="u", password="p", realm="r", nonce="n", uri="sip:d",
            qop="auth-int,auth", opaque="xyz", cnonce="c",
        )
        assert "qop=auth" in header
        assert 'opaque="xyz"' in header

    def test_an_unsupported_qop_falls_back_rather_than_claiming_auth(self):
        header = cf._digest_header(
            user="u", password="p", realm="r", nonce="n", uri="sip:d", qop="auth-int",
        )
        assert "qop" not in header


class TestCloudTargets:
    def test_known_relays_are_used_when_srv_lookup_is_unavailable(self, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "dns.resolver", None)
        targets = cf._cloud_targets("ipvdes.vimar.cloud")
        assert targets, "the known relay list must be the fallback"
        assert all(port == cf.CLOUD_SIP_PORT for _host, port in targets)
        assert any("flexiprod" in host for host, _port in targets)

    def test_an_unknown_proxy_has_no_guessed_fallback(self, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "dns.resolver", None)
        assert cf._cloud_targets("relay.example.test") == []
