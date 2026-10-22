"""The hub's quick-reopen pause is for reflexes, not for people.

go2rtc and the stream worker reopen /av on their own right after a call ends;
the pause stops them from placing a new call. A HomeKit view is always a
person, so it passes ``reflex_guard=False``. Pure hub test: no pyhap needed.
"""
import asyncio

from custom_components.vimar_intercom import hub as hub_mod


def test_a_person_reopening_a_view_is_not_taken_for_a_reflex(monkeypatch):
    """The quick-reopen pause refused a HomeKit view reopened 4 s after the
    last one closed (seen live at 21:07:53 and 21:11:28)."""
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    for name, value in (("in_call", False), ("calling", False), ("registered", True)):
        monkeypatch.setattr(hub_mod.sip, name, value, raising=False)
    monkeypatch.setattr(hub_mod.sip, "ringing", lambda: False)
    calls = []

    async def do_call(target=None, **_kw):
        calls.append(target)
        return True, "200"

    monkeypatch.setattr(hub_mod.sip, "do_call", do_call)
    now = hub_mod.time.monotonic()
    h._viewers_left_at = h._auto_ended_at = now - 1

    async def main():
        reflex = await h.stream_opened()
        h._stream_viewers = 0
        person = await h.stream_opened(reflex_guard=False)
        await asyncio.sleep(0)
        return reflex, person

    assert asyncio.run(main()) == (False, True)
    assert calls == [None]


def test_whether_the_last_viewer_leaving_ends_the_call(monkeypatch):
    """The public question a HomeKit view asks instead of reading the hub's
    private counters."""
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(hub_mod.sip, "in_call", True, raising=False)
    monkeypatch.setattr(hub_mod.sip, "calling", False, raising=False)
    h._stream_viewers, h._auto_called = 0, False
    assert not h.should_hang_up_for_viewers(), "a call someone else placed"
    assert h.should_hang_up_for_viewers(answered_for_them=True)
    h._auto_called = True
    assert h.should_hang_up_for_viewers()
    h._stream_viewers = 1
    assert not h.should_hang_up_for_viewers(answered_for_them=True), "someone still watches"
    h._stream_viewers = 0
    monkeypatch.setattr(hub_mod.sip, "in_call", False, raising=False)
    assert not h.should_hang_up_for_viewers(), "nothing left to hang up"
