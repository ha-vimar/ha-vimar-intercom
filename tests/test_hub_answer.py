"""Opening the video while the entrance is ringing must answer, not dial out.

Observed live: the entrance called, the stream was opened nine seconds later,
and the integration placed a second outgoing INVITE instead of picking up. The
ring then went unanswered until it was cancelled.
"""
import asyncio
import types

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")


class FakeSip:
    def __init__(self, ringing=False, in_call=False):
        self.registered = True
        self.in_call = in_call
        self.calling = False
        self.pending_incoming = {"active": ringing}
        self.answered = False
        self.called = None

    async def do_answer_incoming(self):
        self.answered = True
        return True, "OK"

    async def do_call(self, target=None):
        self.called = target
        return True, "OK"


@pytest.fixture
def hub(monkeypatch):
    instance = hub_mod.VimarIntercomHub.__new__(hub_mod.VimarIntercomHub)
    instance._stream_viewers = 0
    instance._hangup_task = None
    instance._auto_called = False
    instance._auto_call_target = None
    return instance


def run(coro):
    async def main():
        result = await coro
        await asyncio.sleep(0)  # let the background task run
        return result
    return asyncio.run(main())


def test_a_ringing_call_is_answered(monkeypatch, hub):
    fake = FakeSip(ringing=True)
    monkeypatch.setattr(hub_mod, "sip", fake)
    run(hub.stream_opened())
    assert fake.answered is True
    assert fake.called is None, "must not place a second call while one is ringing"
    assert hub._auto_called is True, "closing the stream must still hang up"


def test_without_a_ringing_call_the_video_entrance_is_dialled(monkeypatch, hub):
    fake = FakeSip(ringing=False)
    monkeypatch.setattr(hub_mod, "sip", fake)
    monkeypatch.setattr(hub_mod.R, "SIP_DOMAIN", "plant.test")
    monkeypatch.setattr(hub_mod.R, "CAMERA_TARGET", "55001")
    run(hub.stream_opened())
    assert fake.answered is False
    assert fake.called == "sip:55001@plant.test"


def test_an_established_call_is_left_alone(monkeypatch, hub):
    fake = FakeSip(ringing=True, in_call=True)
    monkeypatch.setattr(hub_mod, "sip", fake)
    run(hub.stream_opened())
    assert fake.answered is False and fake.called is None
