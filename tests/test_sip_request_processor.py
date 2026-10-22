"""One reply that cannot be sent must not stop the processing of SIP requests."""
import asyncio

import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")


def message(cid):
    return (f"MESSAGE sip:a SIP/2.0\r\nVia: SIP/2.0/TLS 1.2.3.4;branch=z9hG4bK{cid}\r\n"
            f"From: <sip:1@x>;tag=a\r\nTo: <sip:2@x>\r\nCall-ID: {cid}\r\n"
            f"CSeq: 1 MESSAGE\r\nContent-Length: 4\r\n\r\nPING")


def test_a_failed_reply_does_not_stop_the_processor(monkeypatch):
    sent = []

    async def send(m):
        sent.append(m)
        if len(sent) == 1:
            raise ConnectionError("writer closed during a reconnect")

    async def broadcast(*_a):
        return None

    monkeypatch.setattr(sip, "send", send)
    monkeypatch.setattr(sip, "broadcast", broadcast)

    async def scenario():
        monkeypatch.setattr(sip, "incoming_requests", asyncio.Queue())
        task = asyncio.create_task(sip.request_processor())
        await sip.incoming_requests.put(message("first"))
        await sip.incoming_requests.put(message("second"))
        await asyncio.sleep(0.05)
        task.cancel()
        return task

    asyncio.run(scenario())
    assert len(sent) == 2, "the second request was still answered"


def request(kind):
    return (f"{kind} sip:a SIP/2.0\r\nVia: SIP/2.0/TLS 1.2.3.4;branch=z9hG4bK{kind}\r\n"
            f"From: <sip:1@x>;tag=a\r\nTo: <sip:2@x>\r\nCall-ID: {kind}\r\n"
            f"CSeq: 1 {kind}\r\nContent-Length: 0\r\n\r\n")


@pytest.mark.parametrize("kind, handler", [
    ("INVITE", "handle_incoming_invite"), ("CANCEL", "handle_incoming_cancel"),
    ("BYE", "handle_incoming_bye"), ("OPTIONS", "handle_incoming_options")])
def test_a_request_handler_is_kept_referenced_and_its_failure_logged(monkeypatch, caplog, kind, handler):
    # The loop holds only a weak reference to a task: a handler nobody else
    # references can be garbage-collected before it ends (asyncio docs).
    async def _handle(raw):
        await release.wait()
        raise RuntimeError("boom")

    monkeypatch.setattr(sip, handler, _handle)
    monkeypatch.setattr(sip, "_background", set())

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        monkeypatch.setattr(sip, "incoming_requests", asyncio.Queue())
        task = asyncio.create_task(sip.request_processor())
        await sip.incoming_requests.put(request(kind))
        await asyncio.sleep(0.01)
        held = [t for t in sip._background if not t.done() and t.get_coro().__name__ == "_handle"]
        release.set()
        await asyncio.gather(*held, return_exceptions=True)
        await asyncio.sleep(0)
        task.cancel()
        return held

    release = None
    assert len(asyncio.run(scenario())) == 1
    assert f"{kind} handler error: boom" in caplog.text


def test_sip_identifiers_come_from_secrets(monkeypatch):
    import random
    monkeypatch.setattr(random, "randint", lambda *_a: 0)
    assert sip._gen("") != sip._gen(""), "not the module-level PRNG"
