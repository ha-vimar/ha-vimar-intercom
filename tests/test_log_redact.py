"""Oscuramento delle credenziali nel buffer di debug.

Il buffer che alimenta `/api/vimar_intercom/debug` cattura i record DEBUG a
prescindere da come è configurato `logger:`. Una riga di diagnostica scritta
senza pensarci diventerebbe una credenziale leggibile via HTTP: questi test
fissano la rete di sicurezza che sta in mezzo.
"""
from __future__ import annotations

import pytest

from custom_components.vimar_intercom import log_redact as lr

# ─── la forma del payload del QR di abbinamento ──────────────────────────

def test_password_del_qr_non_sopravvive():
    out = lr.redact("QR payload: ID=12345\nPWD=segretissima\nDOMAIN=x.y")
    assert "segretissima" not in out
    assert "ID=12345" in out, "gli altri campi restano leggibili"


@pytest.mark.parametrize("chiave", ["PWD", "pwd", "password", "passwd", "ha1", "secret"])
def test_tutti_i_nomi_che_portano_un_segreto(chiave):
    assert "valoresegreto" not in lr.redact(f"{chiave}=valoresegreto")


def test_il_nome_della_chiave_resta_visibile():
    """Serve a capire cosa stava succedendo: si oscura il valore, non il campo."""
    assert "ha1" in lr.redact("ha1=0123456789abcdef0123456789abcdef")


# ─── digest SIP: da lì si attacca la password offline ──────────────────────

@pytest.mark.parametrize("header", ["Authorization", "authorization", "Proxy-Authorization"])
def test_header_di_autorizzazione(header):
    riga = f'{header}: Digest username="101", realm="r", response="a1b2c3d4e5f6a1b2"'
    out = lr.redact(riga)
    assert "a1b2c3d4e5f6a1b2" not in out
    assert header.split(":")[0].lower() in out.lower()


def test_response_digest_isolata():
    assert "deadbeefcafe1234" not in lr.redact('response="deadbeefcafe1234"')


# ─── token della rubrica: va trattato come la password SIP ─────────────────

def test_token_in_get_init_status_reply():
    body = 'GET_INIT_STATUS_REPLY;[{"PARAM":"dnd","VALUE":"1"},{"PARAM":"token","VALUE":"abc123def"}]'
    out = lr.redact(body)
    assert "abc123def" not in out
    assert '"PARAM":"dnd","VALUE":"1"' in out, "i parametri non sensibili restano"


# ─── niente falsi positivi: il log deve restare utile ─────────────────────

def test_una_riga_innocua_non_viene_toccata():
    riga = "Door command: uri=sip:55001@x.ipvdes.vimar.cloud body=OPEN_2F registered=True"
    assert lr.redact(riga) == riga


def test_la_parola_token_in_prosa_non_attiva_l_oscuramento():
    riga = "Registered push token for iPhone di Lorenzo"
    assert lr.redact(riga) == riga


# ─── robustezza: gira dentro un logging handler ─────────────────────────

def test_non_solleva_mai(monkeypatch):
    """Un'eccezione qui farebbe perdere la riga, o peggio la lascerebbe passare."""
    monkeypatch.setattr(lr, "_ASSIGN", None)  # rompe la regex di proposito
    assert lr.redact("PWD=x") == lr.MASK


@pytest.mark.parametrize("vuoto", ["", None])
def test_input_vuoto(vuoto):
    assert lr.redact(vuoto) == vuoto


# ─── i quattro casi che passavano in chiaro (review del 27/09) ────────────

@pytest.mark.parametrize("riga, segreto", [
    ("sip_password=abc123", "abc123"),
    ('password="my secret"', "my secret"),
    ("token: 'x y z'", "x y z"),
    ("{'sip_password': 'secret1'}", "secret1"),
    ('{"sip_password": "secret 2", "sip_user": "12345"}', "secret 2"),
    ("pn_token=abcd", "abcd"),
])
def test_casi_che_passavano_in_chiaro(riga, segreto):
    out = lr.redact(riga)
    assert segreto not in out
    assert "***" in out


def test_le_virgolette_restano_e_gli_altri_campi_pure():
    out = lr.redact('{"sip_password": "a b", "sip_user": "12345"}')
    assert out == '{"sip_password": "***", "sip_user": "12345"}'


@pytest.mark.parametrize("riga", ["mypassword_hint=ok", "tokens=5", "sip_user=12345"])
def test_non_oscura_quello_che_non_e_un_segreto(riga):
    assert lr.redact(riga) == riga

# ─── SRTP keys ────────────────────────────────────────────────────────────────

SRTP_KEY = "WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz"


def test_the_srtp_key_in_an_sdp_line_is_hidden():
    out = lr.redact(f"[SDP <<<]   a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{SRTP_KEY}")
    assert SRTP_KEY not in out
    assert "AES_CM_128_HMAC_SHA1_80 inline:" in out, "the line stays readable"


@pytest.mark.parametrize("name", ["crypto_key", "a_srtp_key", "v_srtp_key"])
def test_the_srtp_key_in_a_parsed_sdp_dict_is_hidden(name):
    out = lr.redact(f"SDP: audio={{'port': 4000, '{name}': '{SRTP_KEY}'}}")
    assert SRTP_KEY not in out and "'port': 4000" in out


@pytest.mark.parametrize("name", ["key", "srtp-key", "master_key"])
def test_any_value_under_a_key_named_key_is_hidden(name):
    out = lr.redact(f"crypto=[{{'tag': '1', '{name}': 'abc123'}}] \"{name}\": \"xyz789\"")
    assert "abc123" not in out and "xyz789" not in out and "'tag': '1'" in out


def test_a_bare_srtp_sized_base64_after_key_is_hidden():
    assert SRTP_KEY not in lr.redact(f"key: {SRTP_KEY}")
    assert SRTP_KEY not in lr.redact(f"key={SRTP_KEY}")


def test_words_that_merely_contain_key_stay_readable():
    assert lr.redact("'keyframe': 12, 'monkey': 'x'") == "'keyframe': 12, 'monkey': 'x'"


# ─── crafted input cannot stall the event loop (#46) ─────────────────────

def _elapsed(text: str) -> float:
    import time
    start = time.perf_counter()
    lr.redact(text)
    return time.perf_counter() - start


def test_bare_line_feeds_take_linear_time():
    """When the Authorization prefix could cross newlines, 16000 bare LF after
    a status line took about 3 s, 64 KB of them tens of seconds. The limit is
    loose on purpose: a slow CI runner must pass, a quadratic pattern must not."""
    assert _elapsed("SIP/2.0 200 OK\r\n" + "\n" * 16000) < 0.5


def test_a_long_text_is_cut_after_the_patterns_ran():
    out = lr.redact("password=hidden " + "x" * 100_000)
    assert "hidden" not in out
    assert len(out) < lr.MAX_LEN + 100 and out.endswith("characters cut]")
    assert _elapsed("\n" * 100_000) < 1.0  # quadratic would take minutes


@pytest.mark.parametrize("shape", ['password="{}"', "{{'token': '{}'}}", '{{"PARAM":"token","VALUE":"{}"}}',
                                   "{{'srtp_key': '{}'}}"])
@pytest.mark.parametrize("shift", [-20, -8, -1, 0, 1])
def test_a_quoted_secret_across_the_cut_is_still_hidden(shape, shift):
    """The text is cut after the patterns ran: cut first, a quoted value lost its
    closing quote and its first characters came out in clear."""
    line = shape.format("TOPSECRET1234")
    pad = lr.MAX_LEN + shift - line.index("TOPSECRET")
    out = lr.redact(" " * pad + line + " " * 50)
    assert "TOPSECR" not in out


def test_long_hostile_text_stays_fast_without_an_input_cap():
    for text in ("\n" * 65_000, "\r\n " * 20_000, 'password="' * 6_000,
                 "authorization" + " " * 65_000):
        assert _elapsed(text) < 1.0  # quadratic would take tens of seconds


def test_the_header_is_still_hidden_with_spaces_or_tabs_before_it():
    out = lr.redact("INVITE sip:x SIP/2.0\r\n \tAuthorization: Digest response=\"abcdef0123\"\r\n"
                    "Proxy-Authorization:\tDigest nonce=\"n\"\r\n")
    assert "abcdef0123" not in out and "nonce" not in out
    assert "Authorization: ***" in out and "Proxy-Authorization:\t***" in out


# ─── plant data in the logs (#146): what users paste in issues ─────────────

@pytest.fixture
def plant():
    """The identity runtime.configure registers, forgotten after the test."""
    lr.set_plant_values([("id", "7712345"), ("imei", "358240051111110"),
                         ("uuid", "0f8fad5b-d9cb-469f-a165-70867728950e"), ("name", "Casa Rossi HA")])
    yield
    lr.set_plant_values([])


def test_the_account_values_are_masked_wherever_they_appear(plant):
    out = lr.redact_plant("REGISTER sip:7712345@example.invalid target=7712345 "
                          "+sip.instance=\"<urn:uuid:0f8fad5b-d9cb-469f-a165-70867728950e>\" "
                          "from Casa Rossi HA, imei 358240051111110")
    for value in ("7712345", "358240051111110", "0f8fad5b", "Casa Rossi"):
        assert value not in out
    assert "REGISTER sip:" in out


def test_a_masked_value_stays_recognisable_and_keeps_its_last_digits(plant):
    first, second = lr.redact_plant("id 7712345"), lr.redact_plant("again 7712345")
    tag = first.removeprefix("id ")
    assert tag == second.removeprefix("again ") and tag.startswith("id…45#")


def test_a_registered_number_inside_a_longer_one_is_left_alone(plant):
    assert lr.redact_plant("cseq 977123456") == "cseq 977123456"


def test_short_values_and_the_default_name_are_not_registered():
    lr.set_plant_values([("id", "101"), ("name", "Home Assistant"), ("name", "")])
    try:
        assert lr.redact_plant("101 Home Assistant") == "101 Home Assistant"
    finally:
        lr.set_plant_values([])


@pytest.mark.parametrize("ip, masked", [("192.168.1.23", "192.x.x.23"), ("10.0.0.5", "10.x.x.5"),
                                        ("172.20.4.7", "172.x.x.7"), ("100.72.3.9", "100.x.x.9")])
def test_private_addresses_keep_only_the_first_and_last_octet(ip, masked):
    assert lr.redact_plant(f"Via: SIP/2.0/UDP {ip}:5060;rport") == f"Via: SIP/2.0/UDP {masked}:5060;rport"


@pytest.mark.parametrize("text", ["127.0.0.1:8123", "8.8.8.8", "fw 2.15.0.3", "172.32.0.1", "1192.168.1.1"])
def test_loopback_public_addresses_and_versions_stay(text):
    assert lr.redact_plant(text) == text


def test_the_identity_headers_are_masked_without_registration():
    lr.set_plant_values([])
    raw = "MESSAGE sip:55001@d SIP/2.0\r\nMobile-IMEI: 123456789012345\r\nMyName: Telefono di Anna\r\n"
    out = lr.redact_plant(raw)
    assert "123456789012345" not in out and "Anna" not in out
    assert "Mobile-IMEI: imei…45#" in out and "MyName: name#" in out
    one_line = lr.redact_plant(repr(raw))
    assert "123456789012345" not in one_line and "Anna" not in one_line


@pytest.mark.parametrize("text, hidden", [
    ('GET_NICKS_REPLY;[{"ROLE": "PICG", "EXT": "55001", "NAME": "Casa Bianchi"}]', "Bianchi"),
    ("{'name': 'Mario Verdi', 'msg': 'OPEN_3'}", "Verdi"),
    ('MISSED_CALL;{"SIP_ID": "7798765", "TS": 1}', "7798765"),
    ('{"NICK": "Papà"}', "Papà"),
    ('From: "Giulia Neri" <sip:7798765@d>;tag=1', "Neri"),
])
def test_names_and_caller_ids_in_plant_messages(text, hidden):
    assert hidden not in lr.redact_plant(text)


def test_what_debugging_needs_stays_readable(plant):
    text = ("SIP/2.0 404 Not Found\r\nCSeq: 2 MESSAGE\r\nINVITE sip:55001@d SIP/2.0\r\n"
            'GET_NICKS_REPLY;[{"ROLE": "PICG", "EXT": "55001"}] NAL 5 IDR 1280x720 rtt=35 ms port 5060 '
            "OPEN_2F to 55002, code 200, after 1.25 s")
    assert lr.redact_plant(text) == text


def test_empty_text_passes_through():
    assert lr.redact_plant("") == "" and lr.redact_plant(None) is None


def test_redact_plant_never_raises(monkeypatch):
    monkeypatch.setattr(lr, "_PRIVATE_IP", None)
    assert lr.redact_plant("192.168.1.1") == lr.MASK


def test_hostile_text_stays_fast_through_the_plant_masking(plant):
    """redact_plant runs after redact() has cut the line to MAX_LEN, on the logging thread."""
    import time
    for text in ('"name": "' * 1_800, '"x" ' * 4_000, "MyName:" * 2_300, "192.168." * 2_000,
                 "\nMobile-IMEI: " * 1_100, "7712345" * 2_300):
        text = lr.redact(text)
        start = time.perf_counter()
        lr.redact_plant(text)
        assert time.perf_counter() - start < 1.0  # quadratic would take seconds


def test_a_tag_is_keyed_so_it_cannot_be_brute_forced_back(monkeypatch):
    """A plain 16-bit hash plus the last two digits gave a 7-digit SIP id back in a second."""
    import hashlib
    plain = hashlib.sha256(b"7712345").hexdigest()[:4]
    monkeypatch.setattr(lr, "_TAG_KEY", b"k" * 16)
    first = lr._tag("id", "7712345")
    monkeypatch.setattr(lr, "_TAG_KEY", b"j" * 16)
    assert lr._tag("id", "7712345") != first and not first.endswith(plain)


@pytest.mark.parametrize("text, hidden", [
    ("Via: SIP/2.0/TLS 192.0.2.20:5061;rport=4100;received=203.0.113.77", "203.0.113.77"),
    ("Contact: <sip:7798765@198.51.100.4:39012;transport=tls>;expires=600", "198.51.100.4"),
    ('Contact: <sip:x@h>;+sip.instance="<urn:uuid:9d1e7a52-3b5c-4e2f-8a61-0c4f5e6d7b8a>"', "9d1e7a52"),
    ("{'sip_id': 7798765, 'ts': 1}", "7798765"),
    ('"SIP_ID": 7798765', "7798765"),
    (repr("MESSAGE x\r\nMyName: Casa dell'Anna\r\n"), "Anna"),
])
def test_public_addresses_instance_ids_and_unquoted_ids(text, hidden):
    assert hidden not in lr.redact_plant(text)


@pytest.mark.parametrize("text", ['MISSED_CALL;{"SIP_ID": "55001", "TS": 1}', "{'sip_id': 55001}",
                                  "MyName: Home Assistant", 'From: "Home Assistant" <sip:x@d>',
                                  "Route: <sip:ipvdes.vimar.cloud;transport=tls;lr>"])
def test_panel_extensions_the_default_name_and_the_relay_stay(text):
    assert lr.redact_plant(text) == text


def test_the_closing_quote_of_a_repr_survives_the_header_tag():
    out = lr.redact_plant(repr("MyName: Anna"))
    assert out.startswith("'MyName: name#") and out.endswith("'")


def test_a_header_followed_by_a_long_run_of_spaces_stays_fast():
    import time
    text = "MyName: a" + " " * 16_000 + "x"
    start = time.perf_counter()
    lr.redact_plant(text)
    assert time.perf_counter() - start < 0.5  # the old lookahead took ~10^8 steps


# ─── follow-up of #146: other phones' ids, public addresses in Via, the GID ──

@pytest.mark.parametrize("text, hidden", [
    ("Contact: <sip:7798765@h.invalid;transport=tls>;expires=600", "7798765"),
    ("To: <sips:44556677@d.invalid>", "44556677"),
    ("INVITE sip:880011@d.invalid SIP/2.0", "880011"),
])
def test_another_phones_id_in_a_uri_is_masked(text, hidden):
    lr.set_plant_values([])
    out = lr.redact_plant(text)
    assert hidden not in out and f"…{hidden[-2:]}#" in out


def test_another_phones_id_gets_the_same_tag_on_every_line():
    first = lr.redact_plant("To: <sip:7798765@d.invalid>")
    assert first == lr.redact_plant("To: <sip:7798765@d.invalid>")


@pytest.mark.parametrize("text", ["INVITE sip:55001@d.invalid SIP/2.0", "To: <sip:55002@d.invalid>",
                                  "SIP/2.0 200 OK", "Via: SIP/2.0/UDP h.invalid:5060", "sip:abc@d.invalid"])
def test_panel_extensions_codes_and_ports_in_uris_stay(text):
    assert lr.redact_plant(text) == text


@pytest.mark.parametrize("text, masked", [
    ("Via: SIP/2.0/TLS 9.9.9.23:5061;rport;branch=z9hG4bK1", "Via: SIP/2.0/TLS 9.x.x.23:5061;rport;branch=z9hG4bK1"),
    ("v: SIP/2.0/UDP 8.8.4.56;rport", "v: SIP/2.0/UDP 8.x.x.56;rport"),
    ("Route: <sip:9.9.9.23:5060;lr>", "Route: <sip:9.x.x.23:5060;lr>"),
    ("REGISTER sips:8.8.4.56:5061 SIP/2.0", "REGISTER sips:8.x.x.56:5061 SIP/2.0"),
    ("Via: SIP/2.0/UDP 192.168.1.23:5060", "Via: SIP/2.0/UDP 192.x.x.23:5060"),
])
def test_public_addresses_in_via_and_in_uris_without_a_user_are_masked(text, masked):
    assert lr.redact_plant(text) == masked


@pytest.mark.parametrize("text", ["Via: SIP/2.0/UDP 127.0.0.1:5070;rport", "Route: <sip:127.0.0.1:5060;lr>",
                                  "Via: SIP/2.0/UDP 192.0.2.10:5060", "Route: <sip:198.51.100.7;lr>",
                                  "Via: SIP/2.0/TLS 203.0.113.5:5061", "Via: SIP/2.0/UDP 0.0.0.0:5060",
                                  "sip:300.1.2.3:5060", "sip:8.8.8.8888"])
def test_loopback_documentation_and_non_addresses_stay_in_via_and_uris(text):
    assert lr.redact_plant(text) == text


@pytest.mark.parametrize("text, hidden", [
    ('GET_INIT_STATUS_REPLY;[{"PARAM":"GID","VALUE":"731"}]', "731"),
    ('[{"VALUE": "731", "PARAM": "GID"}]', "731"),
    ("NEW_PHONEBOOK gid=731 ver=abc", "731"),
    ("NEW_PHONEBOOK;0a1b2c;731", "731"),
    ("{'apt_gid': '731', 'dnd': False}", "731"),
    ('{"gid": "731", "rubrica_ver": "x"}', "731"),
    ("pwd=*** gid=731 mac=x", "731"),
    ("{'gid_appartamento': 731}", "731"),
    ("Apartment GID: 731", "731"),
])
def test_the_apartment_gid_is_masked(text, hidden):
    out = lr.redact_plant(text)
    assert hidden not in out and "gid#" in out


@pytest.mark.parametrize("text", ["GID_PE=55100", "ACTUATOR_RULES.GA_GID = ?", "{'gid': None}", "gid=",
                                  "NEW_PHONEBOOK;0a1b2c", "rigid=731"])
def test_panel_gids_and_empty_gids_stay(text):
    assert lr.redact_plant(text) == text


def test_hostile_text_stays_fast_through_the_follow_up_patterns():
    import time
    for text in ("sip:" * 4_000, "sip:1234567" * 1_400, "SIP/2.0/UDP " * 1_300, "SIP/2.0/UDP 1.2.3." * 900,
                 "gid=" * 4_000, "'gid'" * 3_200, '{"PARAM":"GID",' * 1_000, "NEW_PHONEBOOK;" * 1_100,
                 "sip:1.2.3.4" * 1_400):
        text = lr.redact(text)
        start = time.perf_counter()
        lr.redact_plant(text)
        assert time.perf_counter() - start < 1.0  # quadratic would take seconds


def test_our_own_id_and_a_public_host_in_one_uri_keep_their_masks(plant):
    """The registered id is cut before the URI-user pattern runs: same tag either way,
    and the host after the `@` is still shortened."""
    out = lr.redact_plant("Contact: <sip:7712345@9.9.9.23:5060>")
    assert out == f"Contact: <sip:{lr._tag('id', '7712345')}@9.x.x.23:5060>"


# ─── #163: other phones' 5-digit ids (6xxxx), as long as a panel's extension ──

@pytest.mark.parametrize("text, hidden", [
    ("INVITE sip:60941@d.invalid SIP/2.0", "60941"),
    ("From: <sip:62345@d.invalid>;tag=1", "62345"),
    ('MISSED_CALL;{"SIP_ID": "60937", "TS": 1}', "60937"),
    ("{'sip_id': 60958}", "60958"),
    ('GET_NICKS_REPLY;[{"ROLE": "PIM", "EXT": "60963", "NAME": "x"}]', "60963"),
])
def test_another_phones_five_digit_id_is_masked(text, hidden):
    """On cloud 2FV2 plants the phones have 5-digit ids, the length of a panel's
    extension: they stayed readable in an INVITE or a MESSAGE from another phone (#163)."""
    lr.set_plant_values([])
    assert hidden not in lr.redact_plant(text)


@pytest.mark.parametrize("text", [
    "INVITE sip:60001@d.invalid SIP/2.0", "To: <sip:60002@d.invalid>", "To: <sip:61000@d.invalid>",
    "To: <sip:60000@d.invalid>", 'MISSED_CALL;{"SIP_ID": "60001", "TS": 1}', "INVITE sip:40001@d.invalid SIP/2.0",
    "To: <sip:70001@d.invalid>", 'GET_NICKS_REPLY;[{"ROLE": "PICG", "EXT": "55001"}]',
])
def test_service_addresses_and_panels_stay_readable(text):
    """60000-60002 and 61000 are the protocol's own addresses (default PICG, OPEN_CURRENT,
    the 2FV2 SGA): reading the log needs them. A 5-digit id outside 6xxxx is a panel's."""
    lr.set_plant_values([])
    assert lr.redact_plant(text) == text
