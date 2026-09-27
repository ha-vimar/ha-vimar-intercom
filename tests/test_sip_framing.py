"""The TLS framer: one malformed message from the relay must not hang HA.

A negative Content-Length used to keep the loop on the same header forever,
on the event loop. A lone CRLF (the keepalive pong, RFC 5626) used to end up
in front of the next message, which then lost its first line.
"""
import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")

OPTIONS = b"OPTIONS sip:a SIP/2.0\r\nCall-ID: x\r\nContent-Length: 0\r\n\r\n"


def msg(body=b"", length=None):
    n = len(body) if length is None else length
    return (b"MESSAGE sip:a SIP/2.0\r\nCall-ID: m\r\n"
            b"Content-Length: " + str(n).encode() + b"\r\n\r\n" + body)


def test_two_messages_and_a_partial_one():
    out, rest = sip._split_stream(OPTIONS + msg(b"hello") + msg(b"late")[:30])
    assert [m.split(" ", 1)[0] for m in out] == ["OPTIONS", "MESSAGE"]
    assert out[1].endswith("hello")
    assert rest == msg(b"late")[:30]


def test_a_keepalive_pong_does_not_eat_the_next_message():
    out, _ = sip._split_stream(b"\r\n" + OPTIONS)
    assert out and out[0].startswith("OPTIONS")


@pytest.mark.parametrize("length", [-1, -5000, "abc", sip.MAX_SIP_BODY + 1])
def test_a_bad_content_length_drops_the_stream_instead_of_spinning(length):
    out, rest = sip._split_stream(OPTIONS + msg(b"x", length=length) + OPTIONS)
    assert [m.split(" ", 1)[0] for m in out] == ["OPTIONS"]
    assert rest == b""


def test_a_body_that_has_not_arrived_yet_is_waited_for():
    out, rest = sip._split_stream(msg(b"0123456789")[:-3])
    assert out == [] and rest.endswith(b"0123456")
