"""Digest challenges and responses, in the setup flow and in the SIP client.

* The response is checked against the vector published in RFC 2617 §3.5.
* A quoted value may hold commas (a realm, a qop list): the SIP client used to
  split the raw header on commas and read such a realm wrong.
* A qop list selects auth and echoes opaque; without qop, or with only a qop we
  do not implement, the RFC 2069 form is used instead of claiming auth.
* When the SRV lookup fails, the known cloud relays are the fallback.
"""
from __future__ import annotations

import sys

import pytest

from custom_components.vimar_intercom import config_flow as cf
from custom_components.vimar_intercom import sip_client as sip

R = sip.R

RFC = dict(realm="testrealm@host.com", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093",
           uri="/dir/index.html", cnonce="0a4f113b")
RFC_RESPONSE = "6629fae49393a05397450978507c4ef1"


def response(header):
    return (
        "SIP/2.0 401 Unauthorized\r\n"
        "Via: SIP/2.0/UDP 192.168.1.10:5060;branch=z9hG4bK1;rport=5060\r\n"
        f"{header}\r\n"
        "Server: Vimar IP-PBX (aarch64/linux)\r\n"
        "Content-Length: 0\r\n\r\n"
    )


# ─── Parsing ──────────────────────────────────────────────────────────────────

def test_the_first_parameter_is_not_lost():
    parsed = cf._parse_challenge(response(
        'WWW-Authenticate: Digest realm="127.0.0.1", nonce="abc123"'))
    assert parsed == {"realm": "127.0.0.1", "nonce": "abc123"}


def test_a_quoted_qop_list_containing_commas_is_kept_whole():
    parsed = cf._parse_challenge(response(
        'WWW-Authenticate: Digest realm="r", nonce="n", qop="auth,auth-int", algorithm=MD5'))
    assert parsed["qop"] == "auth,auth-int"
    assert parsed["algorithm"] == "MD5"


def test_unquoted_values_and_opaque_are_read():
    parsed = cf._parse_challenge(response(
        'WWW-Authenticate: Digest realm="r", nonce="n", opaque="o", stale=true'))
    assert parsed["opaque"] == "o"
    assert parsed["stale"] == "true"


def test_a_proxy_challenge_is_accepted_too():
    parsed = cf._parse_challenge(response('Proxy-Authenticate: Digest realm="r", nonce="n"'))
    assert parsed["realm"] == "r"


def test_a_response_without_a_digest_challenge_yields_nothing():
    assert cf._parse_challenge(response("Server: something")) == {}
    assert cf._parse_challenge(response('WWW-Authenticate: Basic realm="r"')) == {}


def test_the_sip_client_keeps_a_quoted_realm_with_a_comma():
    params = sip._challenge_params(
        'Digest realm="plant, north", nonce="n1", qop="auth,auth-int", opaque="o"')
    assert params == {"realm": "plant, north", "nonce": "n1",
                      "qop": "auth,auth-int", "opaque": "o"}


def test_the_sip_client_ignores_another_scheme():
    assert sip._challenge_params('Basic realm="r"') == {}
    assert sip._challenge_params("") == {}


# ─── Setup flow: _digest_header ──────────────────────────────────────────────

class TestDigestHeader:
    def test_matches_the_published_rfc_2617_vector(self):
        header = cf._digest_header(user="Mufasa", password="Circle Of Life",
                                   method="GET", qop="auth", **RFC)
        assert f'response="{RFC_RESPONSE}"' in header
        assert "qop=auth" in header and "nc=00000001" in header

    def test_legacy_form_is_used_when_no_qop_is_offered(self):
        header = cf._digest_header(user="u", password="p", realm="r", nonce="n", uri="sip:d")
        assert "qop" not in header and "cnonce" not in header

    def test_a_qop_list_selects_auth_and_opaque_is_echoed(self):
        header = cf._digest_header(user="u", password="p", realm="r", nonce="n", uri="sip:d",
                                   qop="auth-int,auth", opaque="xyz", cnonce="c")
        assert "qop=auth," in header
        assert 'opaque="xyz"' in header

    def test_an_unsupported_qop_falls_back_rather_than_claiming_auth(self):
        header = cf._digest_header(user="u", password="p", realm="r", nonce="n",
                                   uri="sip:d", qop="auth-int")
        assert "qop" not in header


# ─── SIP client: _make_auth ──────────────────────────────────────────────────

@pytest.fixture
def mufasa(monkeypatch):
    monkeypatch.setattr(R, "SIP_USER", "Mufasa")
    monkeypatch.setattr(R, "SIP_PASSWORD", "Circle Of Life")
    monkeypatch.setattr(R, "SIP_DOMAIN", "other.domain")
    monkeypatch.setattr(sip.secrets, "token_hex", lambda n: RFC["cnonce"])


def test_the_sip_client_matches_the_rfc_2617_vector(mufasa):
    challenge = (f'Digest realm="{RFC["realm"]}", nonce="{RFC["nonce"]}", '
                 'qop="auth,auth-int", opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    header = sip._make_auth("GET", RFC["uri"], challenge)
    assert f'response="{RFC_RESPONSE}"' in header
    assert "qop=auth," in header and "nc=00000001" in header
    assert 'opaque="5ccc069c403ebaf9f0171e9517f40e41"' in header


def test_the_sip_client_signs_with_a_realm_holding_a_comma(mufasa):
    header = sip._make_auth("REGISTER", "sip:d", 'Digest realm="a, b", nonce="n"')
    assert 'realm="a, b"' in header
    assert f'response="{sip._digest_resp("REGISTER", "sip:d", "n", "a, b")}"' in header


def test_the_sip_client_does_not_claim_auth_for_auth_int_only(mufasa):
    header = sip._make_auth("REGISTER", "sip:d", 'Digest realm="r", nonce="n", qop="auth-int"')
    assert "qop" not in header and "cnonce" not in header


def test_the_sip_client_uses_the_rfc_2069_form_without_qop(mufasa):
    header = sip._make_auth("REGISTER", "sip:d", 'Digest realm="r", nonce="n"')
    assert "qop" not in header
    assert f'response="{sip._digest_resp("REGISTER", "sip:d", "n", "r")}"' in header


# ─── Cloud relays when SRV fails ─────────────────────────────────────────────

class TestCloudTargets:
    def test_known_relays_are_used_when_srv_lookup_is_unavailable(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "dns.resolver", None)
        targets = sip._resolve_sip_targets("ipvdes.vimar.cloud", cf.CLOUD_SIP_PORT)
        relays = targets[:-1]
        assert relays and all("flexiprod" in host for host, _port in relays)
        assert targets[-1] == ("ipvdes.vimar.cloud", cf.CLOUD_SIP_PORT)

    def test_an_unknown_proxy_has_no_guessed_fallback(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "dns.resolver", None)
        assert sip._resolve_sip_targets("relay.example.test", 5061) == [
            ("relay.example.test", 5061)]
