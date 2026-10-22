"""The TLS framer and the relay's keepalive pongs.

A lone CRLF (the keepalive pong, RFC 5626 §4.4.1) used to stay in the buffer
in front of the next message, which then lost its first line and was dropped.
"""
import pytest

from custom_components.vimar_intercom import sip_client as sip

OPTIONS = b"OPTIONS sip:a SIP/2.0\r\nCall-ID: x\r\nContent-Length: 0\r\n\r\n"


def msg(body=b""):
    return (b"MESSAGE sip:a SIP/2.0\r\nCall-ID: m\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\n\r\n" + body)


def test_a_keepalive_pong_does_not_eat_the_next_message():
    out, rest = sip._split_stream(b"\r\n" + OPTIONS)
    assert out and out[0].startswith("OPTIONS") and rest == b""


def test_pongs_between_messages_are_skipped():
    out, _rest = sip._split_stream(OPTIONS + b"\r\n\r\n" + msg(b"hello"))
    assert [m.split(" ", 1)[0] for m in out] == ["OPTIONS", "MESSAGE"]


def test_a_partial_message_waits_for_the_rest():
    out, rest = sip._split_stream(msg(b"0123456789")[:-3])
    assert out == [] and rest.endswith(b"0123456")


def test_the_compact_content_length_header_is_honoured():
    body = b"hello world"
    compact = b"MESSAGE sip:a SIP/2.0\r\nCall-ID: m\r\nl: 11\r\n\r\n" + body
    out, rest = sip._split_stream(compact + OPTIONS)
    assert [m.split(" ", 1)[0] for m in out] == ["MESSAGE", "OPTIONS"]
    assert out[0].endswith("hello world") and rest == b""


def test_a_header_ending_in_l_is_not_content_length():
    """Only the header named `l` is the compact form, not any name ending in l."""
    m = b"MESSAGE sip:a SIP/2.0\r\nCall-ID: m\r\nX-Url: 99\r\nContent-Length: 0\r\n\r\n"
    out, rest = sip._split_stream(m)
    assert len(out) == 1 and rest == b""


@pytest.mark.parametrize("value", [b"abc", b"12x", b"", b"1000001"])
def test_an_invalid_content_length_breaks_the_stream(value):
    """Waiting for a body that never comes held every later message forever."""
    bad = b"MESSAGE sip:a SIP/2.0\r\nCall-ID: m\r\nContent-Length: " + value + b"\r\n\r\n"
    out, rest = sip._split_stream(OPTIONS + bad + OPTIONS)
    assert [m.split(" ", 1)[0] for m in out] == ["OPTIONS"]
    assert rest is None


def test_a_negative_content_length_counts_as_zero():
    neg = b"OPTIONS sip:a SIP/2.0\r\nCall-ID: n\r\nContent-Length: -1000\r\n\r\n"
    out, rest = sip._split_stream(neg + OPTIONS)
    assert len(out) == 2 and rest == b""


def test_the_reader_reconnects_on_a_broken_stream(monkeypatch):
    import asyncio

    bad = b"MESSAGE sip:a SIP/2.0\r\nCall-ID: m\r\nContent-Length: nope\r\n\r\n"
    chunks = [OPTIONS + bad]
    dispatched, reconnects = [], []

    class _Reader:
        async def read(self, _n):
            if chunks:
                return chunks.pop(0)
            await asyncio.sleep(3600)

    async def _dispatch(raw):
        dispatched.append(raw)

    async def _reconnect():
        reconnects.append(True)
        raise asyncio.CancelledError  # ends the reader loop for the test

    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(sip, "reader", _Reader())
    monkeypatch.setattr(sip, "_dispatch_message", _dispatch)
    monkeypatch.setattr(sip, "_reconnect_from_reader", _reconnect)

    async def _run():
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(sip.reader_task(), 5)

    asyncio.run(_run())
    assert len(dispatched) == 1 and dispatched[0].startswith("OPTIONS")
    assert reconnects == [True]


def test_framing_error_reconnects_are_rate_limited(monkeypatch):
    """A peer sending unframeable data had us reconnect in a tight loop."""
    import asyncio as _asyncio
    reconnects, sleeps = [], []

    async def reconnect():
        reconnects.append(True)

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(sip, "_reconnect_from_reader", reconnect)
    monkeypatch.setattr(sip, "_last_framing_reconnect", -1e9)
    monkeypatch.setattr(sip, "framing_errors", 0)
    monkeypatch.setattr(sip.asyncio, "sleep", sleep)

    async def main():
        await sip._reconnect_after_framing_error()
        await sip._reconnect_after_framing_error()

    _asyncio.run(main())
    assert len(reconnects) == 2 and sip.framing_errors == 2
    assert len(sleeps) == 1 and 1.5 < sleeps[0] <= sip.FRAMING_RECONNECT_MIN_S
