"""get_local_ip runs in the executor, not on the event loop.

In cloud mode it connects a UDP socket to the proxy's host NAME, which
resolves it first: a blocking DNS lookup, seconds long while the network is
down (Home Assistant starting before the router), at setup and at every
reconnect. On the loop it froze every integration meanwhile.
"""
from __future__ import annotations

import asyncio
import threading

from custom_components.vimar_intercom import hub as hub_mod

sip = hub_mod.sip
media = hub_mod.media


def _recording_get_local_ip(monkeypatch):
    threads = []

    def get_local_ip():
        threads.append(threading.current_thread())
        return "192.0.2.9"

    monkeypatch.setattr(sip, "get_local_ip", get_local_ip)
    return threads


def test_connect_resolves_the_local_ip_off_the_loop(monkeypatch):
    threads = _recording_get_local_ip(monkeypatch)
    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(sip.R, "LOCAL_PROXY", "127.0.0.1")
    monkeypatch.setattr(sip.R, "LOCAL_UDP_PORT", 0)
    try:
        asyncio.run(sip.connect())
    finally:
        if sip._udp_sock is not None:
            sip._udp_sock.close()
            sip._udp_sock = None
    assert sip.MY_IP == "192.0.2.9"
    assert threads and all(t is not threading.main_thread() for t in threads)


def test_hub_start_resolves_the_local_ip_off_the_loop(monkeypatch):
    threads = _recording_get_local_ip(monkeypatch)
    hub = hub_mod.VimarIntercomHub()
    for name in ("init", "set_state_callback", "set_model_callback"):
        monkeypatch.setattr(sip, name, lambda *a: None)
    monkeypatch.setattr(media, "init", lambda fn: None)
    monkeypatch.setattr(sip, "MY_IP", None)

    async def nothing(*a):
        pass

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(media, "setup_transports", nothing)
    monkeypatch.setattr(sip, "connect", nothing)
    for name in ("reader_task", "request_processor"):
        monkeypatch.setattr(sip, name, idle)
    for name in ("_auto_startup", "_keepalive_loop"):
        monkeypatch.setattr(hub, name, idle)

    async def run():
        await hub.async_start()
        for t in hub._tasks:
            t.cancel()
        await asyncio.gather(*hub._tasks, return_exceptions=True)

    asyncio.run(run())
    assert sip.MY_IP == "192.0.2.9"
    assert threads and all(t is not threading.main_thread() for t in threads)
