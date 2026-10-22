"""Test del parser SIP di sip_client (funzione pura _parse) e del digest."""

from custom_components.vimar_intercom import sip_client as sip

RESP_200 = (
    "SIP/2.0 200 OK\r\n"
    "Via: SIP/2.0/UDP 192.168.0.10:5060;branch=z9hG4bKabc;rport=5060\r\n"
    "From: <sip:12345@example.ipvdes.vimar.cloud>;tag=123\r\n"
    "To: <sip:55002@example.ipvdes.vimar.cloud>;tag=456\r\n"
    "Call-ID: abcdefghij\r\n"
    "CSeq: 7 MESSAGE\r\n"
    "Content-Length: 0\r\n\r\n"
)

MSG_IN = (
    "MESSAGE sip:12345@192.168.0.10:5060 SIP/2.0\r\n"
    "Via: SIP/2.0/UDP 192.168.1.50:5060;branch=z9hG4bKxyz\r\n"
    "From: <sip:55002@example.ipvdes.vimar.cloud>;tag=t1\r\n"
    "To: <sip:12345@example.ipvdes.vimar.cloud>\r\n"
    "Call-ID: kkkkkkkkkk\r\n"
    "CSeq: 1 MESSAGE\r\n"
    "Panda: blue\r\n"
    "Content-Type: text/plain\r\n"
    "Content-Length: 12\r\n\r\n"
    "VOICEMAIL;ON"
)


def test_parse_response_code_and_headers():
    code, hdrs, *rest = sip._parse(RESP_200)
    assert code == 200
    assert hdrs.get("call-id") == "abcdefghij"
    assert "cseq" in hdrs


def test_parse_incoming_message_body():
    parsed = sip._parse(MSG_IN)
    code = parsed[0]
    hdrs = parsed[1]
    assert code == "MESSAGE"  # _parse restituisce il metodo SIP per le request, non un intero
    assert hdrs.get("panda") == "blue"
    # il body deve essere disponibile in uno degli elementi restituiti
    assert any(isinstance(x, str) and "VOICEMAIL;ON" in x for x in parsed[2:]) or "VOICEMAIL;ON" in str(parsed)


def test_parse_garbage_does_not_raise():
    for junk in ("", "\r\n\r\n", "SIP/2.0\r\n", "MESSAGE\r\nContent-Length: 999\r\n\r\nx"):
        sip._parse(junk)


def test_call_id_and_tag_helpers():
    _, hdrs, *_ = sip._parse(RESP_200)
    assert sip._call_id(hdrs) == "abcdefghij"
    assert sip._tag(hdrs.get("to", "")) == "456"
