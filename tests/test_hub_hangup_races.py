"""Hang-up races in the hub, found by the review of the 1.1.0 changes."""
import asyncio

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")
sip, media = hub_mod.sip, hub_mod.media


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    monkeypatch.setattr(sip, "in_call", True)
    h._auto_called = True
    h._stream_viewers = 0
    return h


def test_a_new_view_keeps_waiting_while_the_bye_is_in_flight(hub, monkeypatch):
    """Cancelling the delayed hang-up must not clear _hanging_up early."""
    monkeypatch.setattr(hub_mod, "STREAM_HANGUP_DELAY", 0)
    bye_done = {}

    async def slow_hangup():
        await asyncio.sleep(0.2)
        bye_done["at"] = True

    monkeypatch.setattr(sip, "do_hangup", slow_hangup)

    async def scenario():
        task = asyncio.create_task(hub._delayed_hangup())
        await asyncio.sleep(0.05)          # the BYE is going out
        task.cancel()                      # a new view opens
        await asyncio.sleep(0.01)
        during = hub._hanging_up
        await asyncio.sleep(0.3)
        return during, hub._hanging_up

    during, after = asyncio.run(scenario())
    assert during is True, "the new view must still wait for the BYE"
    assert after is False and bye_done


def test_a_view_that_never_started_still_ends_the_auto_call(hub, monkeypatch):
    """Closed while the call was connecting: no ffmpeg ever appears."""
    monkeypatch.setattr(hub_mod, "HOMEKIT_WATCH_START", 0.4)
    monkeypatch.setattr(media, "homekit_ffmpeg_pids", lambda _sdp=None: [])
    monkeypatch.setattr(media, "homekit_session_count", lambda: 0)
    monkeypatch.setattr(media, "release_homekit_session", lambda _sid: None)
    hung = []

    async def hangup():
        hung.append(True)

    monkeypatch.setattr(sip, "do_hangup", hangup)
    asyncio.run(hub.watch_homekit_session({"id": "s", "sdp": "/tmp/x.sdp"}))
    assert hung == [True]


def test_closing_the_view_of_an_answered_call_hangs_up_promptly(hub, monkeypatch):
    """A ring answered by opening the view ends when the view closes, not 30 s
    later: every view reopened meanwhile attached to the old call (the indoor
    monitor's, which has no video)."""
    hub._answered_call = True
    hung = []

    async def hangup():
        hung.append(True)

    monkeypatch.setattr(sip, "do_hangup", hangup)

    async def scenario():
        await hub._delayed_hangup()

    import time
    t0 = time.monotonic()
    asyncio.run(scenario())
    assert hung == [True]
    assert time.monotonic() - t0 < hub_mod.STREAM_HANGUP_DELAY / 2
