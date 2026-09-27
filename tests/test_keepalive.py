"""Keeping the connection alive is not the same job as renewing the binding.

The hub used to re-REGISTER every 120s while sip_client separately renewed on
the granted lifetime: two mechanisms doing the same thing with different
Call-IDs. The hub now only keeps the connection warm and recovers a lost
registration; renewal belongs to the lifetime the registrar granted.
"""
import asyncio

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")
sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")


class FakeSip:
    def __init__(self, registered=True):
        self.registered = registered
        self.registrations = 0
        self.keepalives = 0

    async def do_register(self):
        self.registrations += 1
        self.registered = True
        return True

    async def reconnect(self):
        return await self.do_register()

    async def send_keepalive(self):
        self.keepalives += 1
        return True


@pytest.fixture
def hub(monkeypatch):
    instance = hub_mod.VimarIntercomHub.__new__(hub_mod.VimarIntercomHub)
    instance._running = True
    instance._init_status_sent = True
    instance.stats = {"last_register_time": None, "register_failures": 0}
    instance._touch = lambda: None
    monkeypatch.setattr(hub_mod, "KEEPALIVE_INTERVAL", 0.01)
    return instance


def run_one_tick(hub_instance):
    async def main():
        task = asyncio.create_task(hub_instance._keepalive_loop())
        await asyncio.sleep(0.05)
        hub_instance._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())


def test_a_healthy_registration_is_not_re_registered(monkeypatch, hub):
    fake = FakeSip(registered=True)
    monkeypatch.setattr(hub_mod, "sip", fake)
    run_one_tick(hub)
    assert fake.registrations == 0, "renewal is driven by the granted lifetime, not here"
    assert fake.keepalives >= 1, "the connection must still be kept warm"


def test_a_lost_registration_is_recovered(monkeypatch, hub):
    fake = FakeSip(registered=False)
    monkeypatch.setattr(hub_mod, "sip", fake)
    run_one_tick(hub)
    assert fake.registrations >= 1
    assert hub.stats["last_register_time"] is not None


def test_the_keepalive_is_a_crlf_ping_not_a_registration():
    """A full REGISTER to keep a socket warm is expensive and duplicates renewal."""
    assert asyncio.iscoroutinefunction(sip.send_keepalive)


def test_no_keepalive_is_sent_on_the_local_transport(monkeypatch):
    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", True)
    assert asyncio.run(sip.send_keepalive()) is False, "no NAT to keep open on the LAN"
