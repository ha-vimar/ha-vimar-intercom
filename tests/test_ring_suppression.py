"""A real ring must never be declined because of a call that already ended.

Observed live: after an auto-call ended (the far end sent BYE), the hub kept
believing it had a call of its own and answered every subsequent incoming
INVITE with 603 Decline — the log said it plainly: auto_called=True,
in_call=False, calling=False.
"""
import asyncio

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")


class FakeSip:
    def __init__(self, in_call=False, calling=False):
        self.in_call = in_call
        self.calling = calling
        self.registered = True
        self.declines = 0
        self.pending_incoming = {"caller_uri": "sip:55001@127.0.0.1"}

    async def do_decline_incoming(self):
        self.declines += 1


@pytest.fixture
def hub(monkeypatch):
    instance = hub_mod.VimarIntercomHub.__new__(hub_mod.VimarIntercomHub)
    instance._auto_called = False
    instance._stream_viewers = 0
    instance._ring_callbacks = []
    instance._ws_broadcast_fn = None
    instance._ring_answered = False
    instance._call_timeout_task = None
    instance._keyframe_task = None
    instance._hangup_task = None
    instance.stats = {
        "last_ring_time": None, "last_caller_uri": None, "last_caller_id": None,
        "ring_count": 0, "missed_calls": 0,
    }
    instance._touch = lambda: None
    instance._now = lambda: "now"
    return instance


def deliver(hub_instance, event, message=""):
    async def main():
        await hub_instance._handle_broadcast(event, message)
        await asyncio.sleep(0)
    asyncio.run(main())


def deliver_ring(hub_instance):
    deliver(hub_instance, "ring", "Chiamata da: sip:55001@127.0.0.1")


def test_a_ring_after_a_finished_auto_call_is_a_real_ring(monkeypatch, hub):
    """The exact failure: the flag outlived the call and rejected the doorbell."""
    fake = FakeSip(in_call=False, calling=False)
    monkeypatch.setattr(hub_mod, "sip", fake)
    hub._auto_called = True  # left over from an auto-call the far end ended

    rung = []
    hub._ring_callbacks = [lambda: rung.append(True)]
    deliver_ring(hub)

    assert fake.declines == 0, "a call we are not in must never be declined"
    assert rung == [True]
    assert hub._auto_called is False, "the stale flag should be cleared, not kept"
    assert hub.stats["ring_count"] == 1


def test_the_echo_of_our_own_call_is_still_suppressed(monkeypatch, hub):
    fake = FakeSip(in_call=True)
    monkeypatch.setattr(hub_mod, "sip", fake)
    hub._auto_called = True

    rung = []
    hub._ring_callbacks = [lambda: rung.append(True)]
    deliver_ring(hub)

    assert fake.declines == 1
    assert rung == [], "this INVITE is the PBX echoing our own call"
    assert hub.stats["ring_count"] == 0


def test_a_ring_while_dialling_is_also_suppressed(monkeypatch, hub):
    fake = FakeSip(calling=True)
    monkeypatch.setattr(hub_mod, "sip", fake)
    deliver_ring(hub)
    assert fake.declines == 1


def test_the_flag_dies_with_the_call(monkeypatch, hub):
    """Previously cleared only when we hung up, never when the far end did."""
    fake = FakeSip(in_call=False)
    monkeypatch.setattr(hub_mod, "sip", fake)
    hub._auto_called = True

    deliver(hub, "call_ended", "Chiamata terminata")

    assert hub._auto_called is False
