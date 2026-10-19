"""simulate_ring (#33 item 14): a whole test ring with no SIP traffic. The state
goes to ringing and back by itself, with the doorbell event and both webhooks;
nothing answers it, and a real ring takes over."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.vimar_intercom import button, webhook
from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import runtime as R

sip = hub_mod.sip
START, END = "http://hook.invalid/on", "http://hook.invalid/off"


@pytest.fixture
def log(hub, monkeypatch):
    """Webhooks, doorbell events and every SIP send or answer, in order."""
    out: list[str] = []

    async def fire(url):
        out.append(url)

    async def send(msg):
        out.append("SIP " + msg.split(" ", 1)[0])

    async def answer():
        out.append("SIP answer")
        return True, "200"

    monkeypatch.setattr(webhook, "fire", fire)
    monkeypatch.setattr(sip, "send", send)
    monkeypatch.setattr(sip, "do_answer_incoming", answer)
    monkeypatch.setattr(R, "RING_WEBHOOK_URL", START)
    monkeypatch.setattr(R, "RING_END_WEBHOOK_URL", END)
    hub.register_ring_callback(lambda: out.append("doorbell"))
    return out


def test_it_rings_then_ends_by_itself_with_both_webhooks_and_no_sip(hub, log):
    async def run():
        assert hub.simulate_ring(0.05)
        await asyncio.sleep(0.01)
        assert hub.status == "ringing" and hub.is_ringing
        assert log == ["doorbell", START]
        await asyncio.sleep(0.1)
        assert hub.status == "idle" and not hub.is_ringing
    asyncio.run(run())
    assert log == ["doorbell", START, END]


def test_the_away_message_never_answers_it(hub, log, monkeypatch):
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "/x/message.mp3")
    monkeypatch.setattr(R, "AWAY_MESSAGE_DELAY", 0.01)

    async def run():
        hub.simulate_ring(0.1)
        await asyncio.sleep(0.15)
    asyncio.run(run())
    assert log == ["doorbell", START, END]  # no "SIP answer"


@pytest.mark.parametrize("action, result", [("async_answer", False), ("async_decline", True)])
def test_answer_or_decline_ends_it_without_sip(hub, log, action, result):
    async def run():
        hub.simulate_ring(10)
        await asyncio.sleep(0)
        ok, _ = await getattr(hub, action)()
        await asyncio.sleep(0)
        return ok
    assert asyncio.run(run()) is result
    assert not hub.is_ringing and log == ["doorbell", START, END]


def test_a_real_ring_takes_over(hub, log, monkeypatch):
    async def noop():
        pass
    monkeypatch.setattr(sip, "send_keyframe_request", noop)

    async def run():
        hub.simulate_ring(0.05)
        await asyncio.sleep(0)
        monkeypatch.setitem(sip.pending_incoming, "active", True)  # the real INVITE
        await hub._handle_broadcast("ring", "Chiamata da: 55001")
        assert hub.status == "ringing"
        assert not hub.simulate_ring(5)  # busy: a real ring is on
        await asyncio.sleep(0.1)         # the simulation's own end never comes
        assert hub.is_ringing
        sip.pending_incoming["active"] = False
        await hub._handle_broadcast("ring_ended", "Chiamata cancellata")
        await asyncio.sleep(0)
    asyncio.run(run())
    # The doorbell callback runs inline, the webhooks are tasks: check them apart.
    assert [x for x in log if x != "doorbell"] == [START, END, START, END]
    assert log.count("doorbell") == 2


@pytest.mark.parametrize("busy", ["in_call", "calling", "ringing", "test_ring"])
def test_the_test_ring_button_cannot_fire_during_a_real_call_or_ring(hub, log, monkeypatch, busy):
    """The button goes through the same guard as the service: while a call or a ring is
    on it raises, and nothing rings, fires a webhook or touches SIP."""
    class _Err(Exception):
        def __init__(self, *a, **kw):
            super().__init__(*a)
            self.translation_key = kw.get("translation_key")
    monkeypatch.setattr(button, "HomeAssistantError", _Err)
    b = button.VimarTestRingButton(hub, "e1")
    b._context = None  # no user, as from an automation

    async def run():
        if busy == "test_ring":
            await b.async_press()           # the first press rings
            assert hub.is_ringing
            await asyncio.sleep(0.01)       # let the start webhook go out
            log.clear()
        elif busy == "ringing":
            monkeypatch.setitem(sip.pending_incoming, "active", True)
        else:
            monkeypatch.setattr(sip, busy, True)
        before = hub._sim_ring
        with pytest.raises(_Err) as err:
            await b.async_press()
        assert err.value.translation_key == "test_ring_busy"
        assert hub._sim_ring is before      # no second simulation
        if busy == "test_ring":
            hub._stop_simulated_ring()
    asyncio.run(run())
    assert log == ([] if busy != "test_ring" else [END])
