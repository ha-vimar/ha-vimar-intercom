"""After a TLS drop the reader must hand reconnection off, not wait for it.

The old reader awaited reconnect() itself. reconnect() sends a REGISTER and
waits for the answer, which only the reader delivers: every attempt timed out
and incoming rings were lost for minutes until the hub's keepalive stepped in.
"""
import asyncio

import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")


class FakeStream:
    """A TLS reader that yields scripted chunks, then EOF."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()

    async def read(self, _n):
        return await self.q.get()


class FakeWriter:
    """Closing it ends its stream, as closing an asyncio transport does."""

    def __init__(self, stream):
        self.stream = stream
        self.closed = False

    def write(self, _data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.closed = True
        self.stream.q.put_nowait(b"")

    def is_closing(self):
        return self.closed


def ok_for(cid):
    return (f"SIP/2.0 200 OK\r\nCall-ID: {cid}\r\nCSeq: 2 REGISTER\r\n"
            f"Contact: <sip:x@1.2.3.4>;expires=600\r\nContent-Length: 0\r\n\r\n").encode()


@pytest.fixture
def tls(monkeypatch):
    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(sip, "_conn_ready", None)
    monkeypatch.setattr(sip, "_conn_gen", 0)
    monkeypatch.setattr(sip, "_reconnect_task", None)
    monkeypatch.setattr(sip, "_reconnect_lock", None)
    monkeypatch.setattr(sip, "registered", False)
    monkeypatch.setattr(sip, "incoming_requests", None)
    monkeypatch.setattr(sip, "pending_responses", {})

    streams = []

    async def fake_connect():
        stream = FakeStream()
        streams.append(stream)
        sip.reader, sip.writer, sip.lock = stream, FakeWriter(stream), asyncio.Lock()
        sip._conn_gen += 1
        sip._conn_event().set()

    async def fake_register():
        # The real thing: a request whose answer only the reader can deliver.
        cid = "reg-test"
        q = sip.pending_responses.setdefault(cid, asyncio.Queue())
        streams[-1].q.put_nowait(ok_for(cid))
        raw = await asyncio.wait_for(q.get(), timeout=2)
        sip._set_registered(sip._parse(raw)[0] == 200)
        return sip.registered

    async def no_profiles():
        return None

    monkeypatch.setattr(sip, "connect", fake_connect)
    monkeypatch.setattr(sip, "do_register", fake_register)
    monkeypatch.setattr(sip, "do_connect_profiles", no_profiles)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(sip.asyncio, "sleep", lambda d, *a: real_sleep(0 if d >= 1 else d, *a))
    return streams


def test_a_dropped_connection_is_rebuilt_and_registered(tls):
    streams = tls

    async def scenario():
        await sip.connect()
        reader = asyncio.create_task(sip.reader_task())
        await asyncio.sleep(0.01)
        streams[0].q.put_nowait(b"")                 # the relay hangs up
        for _ in range(200):
            if sip.registered:
                break
            await asyncio.sleep(0.01)
        reader.cancel()
        return sip.registered

    assert asyncio.run(scenario()) is True
    assert len(streams) == 2, "one new connection, not one per caller"


def test_concurrent_reconnects_share_one_attempt(tls):
    streams = tls

    async def scenario():
        await sip.connect()
        reader = asyncio.create_task(sip.reader_task())
        results = await asyncio.gather(sip.reconnect(), sip.reconnect(), sip.reconnect())
        reader.cancel()
        return results

    assert asyncio.run(scenario()) == [True, True, True]
    assert len(streams) == 2
