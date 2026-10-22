"""A HomeKit view that opens and closes in awkward order leaves nothing behind.

Backing out of the live view within a couple of seconds sends the phone's
"stop" while start_stream is still waiting for the call. The old code closed
the sockets under it, then start_stream carried on and attached a dead video
sink for good, or left ffmpeg running. And three parties can close a session
(the phone, the panel hanging up, ffmpeg exiting), sometimes at once.
"""
import asyncio
import os
import socket
from types import SimpleNamespace

import pytest

from custom_components.vimar_intercom import homekit_accessory as hk
from custom_components.vimar_intercom import homekit_files as hkf  # noqa: E402

media = hk.media
from harness.homekit import (  # noqa: E402
    FakeBridge,
    FakeHass,
    FakeProc,
    FakeState,
    FakeTap,
    FakeTranscoder,
    FakeVideo,
    Hub,
    Notes,
    QRAcc,
    Req,
    Response,
    SetupHass,
    SetupHub,
)


def session():
    a, v = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return {
        "id": "s1", "stream_idx": 0, "address": "10.0.0.5", "v_port": 5000, "a_port": 5002,
        "v_srtp_key": "k", "a_srtp_key": "k", "v_ssrc": 1, "a_ssrc": 2,
        "local_v_port": 1, "local_a_port": 2, "a_sock": a, "v_sock": v,
        "created": 0.0, "lock": asyncio.Lock(),
    }


@pytest.fixture
def acc(monkeypatch):
    FakeVideo.instances.clear()
    FakeBridge.instances.clear()
    FakeTap.instances.clear()
    procs = []
    gate = {"open": None}
    sinks = []

    async def spawn_ffmpeg(*_a, **_k):
        procs.append(FakeProc())
        return procs[-1]

    async def no_stderr(*_a):
        return None

    hkm = hk.hkm
    monkeypatch.setattr(hk, "DirectVideo", FakeVideo)
    monkeypatch.setattr(hk, "AudioBridge", FakeBridge)
    monkeypatch.setattr(hk, "log_stderr", no_stderr)
    monkeypatch.setattr(hk, "ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(hk.asyncio, "create_subprocess_exec", spawn_ffmpeg)
    monkeypatch.setattr(hkm, "AudioTap", FakeTap)
    monkeypatch.setattr(hkm, "video_ready", lambda _hub: True)
    monkeypatch.setattr(hkm, "gop_has_keyframe", lambda: True)
    monkeypatch.setattr(hkm, "gop_for_direct_video", lambda: ([], []))
    monkeypatch.setattr(hkm, "add_video_sink", sinks.append)
    # Like the real one: removing a sink that is not there is not an error.
    monkeypatch.setattr(hkm, "remove_video_sink",
                        lambda sink, _proto=None: sink in sinks and sinks.remove(sink))
    monkeypatch.setattr(hk.sip, "ringing", lambda: False)

    gate["open"] = asyncio.Event()
    a = hk.VimarDoorbell.__new__(hk.VimarDoorbell)
    a._hub, a._hass, a._smooth, a._transcoder = Hub(gate), None, False, None
    a._answer_on_open, a._answered, a._tasks, a._last_frame = False, False, set(), None
    a._answering, a._transcoder_lock = False, asyncio.Lock()
    a._call_gen, a._closed = 0, False
    a.sessions = {}
    a.set_streaming_available = lambda _idx: None
    a.sinks = sinks
    return a, procs, gate


def closed(sock):
    return sock.fileno() == -1


def test_a_stop_during_start_closes_what_start_opened(acc):
    a, procs, gate = acc
    info = session()
    a_sock, v_sock = info["a_sock"], info["v_sock"]

    async def scenario():
        gate["open"] = asyncio.Event()
        start = asyncio.create_task(a.start_stream(info, {}))
        await asyncio.sleep(0.01)
        stop = asyncio.create_task(a.stop_stream(info))
        await asyncio.sleep(0.01)
        gate["open"].set()
        return await start, await stop

    started, _ = asyncio.run(scenario())
    # For pyhap it started and was then stopped: reporting a failure made it
    # delete the session that its own stop was about to delete (KeyError).
    assert started is True
    assert closed(a_sock) and closed(v_sock)
    assert a.sinks == [], "no dead video sink left behind"
    assert all(p.returncode is not None for p in procs), "no orphaned ffmpeg"


def test_three_closers_at_once_close_once(acc):
    a, procs, gate = acc
    info = session()

    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        assert await a.start_stream(info, {})
        a._hub.in_call = False
        await asyncio.gather(
            a.stop_stream(info),
            a._end_from_panel(info),
            a._close_session(info),
        )

    asyncio.run(scenario())
    assert [b.stopped for b in FakeBridge.instances] == [1]
    assert [v.stopped for v in FakeVideo.instances] == [1]
    assert procs[0].returncode is not None
    assert a.sinks == []


def test_a_normal_view_opens_and_closes(acc):
    a, procs, gate = acc
    info = session()

    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        ok = await a.start_stream(info, {})
        sinks_while_open = list(a.sinks)
        await a.stop_stream(info)
        return ok, sinks_while_open

    ok, sinks = asyncio.run(scenario())
    assert ok is True and len(sinks) == 1
    assert a.sinks == []
    assert closed(info.get("a_sock") or socket.socket()) or "a_sock" not in info


def test_when_ffmpeg_exits_by_itself_the_phone_is_told(acc):
    """The watcher asks for the close and must survive it to free the slot."""
    a, procs, gate = acc
    info = session()
    freed = []
    a.set_streaming_available = freed.append

    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        assert await a.start_stream(info, {})
        a.sessions[info["id"]] = info
        procs[0].terminate()              # the panel hung up; ffmpeg exits
        for _ in range(100):
            if freed:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert freed == [0]
    assert [b.stopped for b in FakeBridge.instances] == [1]


def test_no_pyhap_internal_is_overridden_by_accident():
    """pyhap's Camera has private methods of its own (``_start_stream`` parses
    the phone's request and then calls ``start_stream``). A helper with the
    same name silently replaced it, and no stream could start at all."""
    from pyhap.camera import Camera
    ours = {n for n in vars(hk.VimarDoorbell) if n.startswith("_") and not n.startswith("__")}
    theirs = {n for klass in Camera.__mro__ for n in vars(klass)
              if n.startswith("_") and not n.startswith("__")}
    assert ours & theirs == set()


def test_a_phone_request_goes_through_pyhap_into_our_stream(acc):
    """End to end on pyhap's own path: SetupEndpoints, then the selected
    stream configuration handed to pyhap's Camera._start_stream."""
    import uuid

    from pyhap import tlv
    from pyhap.camera import SELECTED_STREAM_CONFIGURATION_TYPES, SETUP_TYPES, STREAMING_STATUS
    from test_homekit_endpoints import _Mgmt, phone_request

    a, procs, gate = acc
    a.stream_address, a.stream_address_isv6 = "127.0.0.1", b"\x00"
    a._management = [_Mgmt()]
    a._streaming_status = [STREAMING_STATUS["AVAILABLE"]]
    sid = uuid.uuid4()
    a.set_endpoints(phone_request(sid))
    selected = {SELECTED_STREAM_CONFIGURATION_TYPES["SESSION"]:
                tlv.encode(SETUP_TYPES["SESSION_ID"], sid.bytes)}

    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        await hk.Camera._start_stream(a, selected, False)
        info = a.sessions[sid]
        await a.stop_stream(info)

    asyncio.run(scenario())
    assert a._streaming_status[0] == STREAMING_STATUS["STREAMING"]
    assert procs, "our start_stream ran and launched ffmpeg"


def test_the_gate_never_claims_to_be_locked(monkeypatch):
    """The intercom only pulses the strike: it cannot know the gate is shut.
    After opening, the lock rests at 'unknown', not 'secured' (which iOS
    announced as 'locked again')."""
    a = hk.VimarDoorbell.__new__(hk.VimarDoorbell)

    class Char:
        def __init__(self):
            self.values = []

        def set_value(self, v):
            self.values.append(v)

    class Hub:
        async def async_door(self, **_k):
            return True, "opened", 200

    a._hub, a._char_lock_current, a._char_lock_target = Hub(), Char(), Char()
    monkeypatch.setattr(hk, "GATE_RELOCK_SECONDS", 0)
    asyncio.run(a._open_gate())
    assert a._char_lock_current.values == [hk.LOCK_UNSECURED, hk.LOCK_UNKNOWN]
    assert hk.LOCK_SECURED not in a._char_lock_current.values
    assert a._char_lock_target.values == [hk.LOCK_SECURED], "the next tap opens again"


# ─── answering a ring and hanging up (rebased on v1.0.9) ──────────────────────

def _ringing(monkeypatch, a):
    state = {"ringing": True}
    monkeypatch.setattr(hk.sip, "ringing", lambda: state["ringing"])
    a._hub.in_call = False

    async def answer():
        a._hub.log.append("answer")
        a._hub.in_call, state["ringing"] = True, False
        return True, "ok"

    a._hub.async_answer = answer
    return state


def _open_view(a, gate, info):
    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        assert await a.start_stream(info, {})
        # Answer on open runs beside the opening: let it land before
        # asyncio.run cancels whatever is still pending.
        answers = [t for t in a._tasks if "_answer_for" in t.get_coro().__qualname__]
        await asyncio.gather(*answers)
    asyncio.run(scenario())


def _close(a, info):
    """The phone closes the view; then whatever the close spawned (the
    hang-up is its own task) runs to the end."""
    async def scenario():
        await a.stop_stream(info)
        while a._tasks:
            await asyncio.gather(*a._tasks)
    asyncio.run(scenario())


def test_a_ring_is_previewed_without_answering_until_talk(acc, monkeypatch):
    """Default mode: opening the view never steals the ring from the Tab."""
    a, procs, gate = acc
    _ringing(monkeypatch, a)
    info = session()

    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        assert await a.start_stream(info, {})
        assert "answer" not in a._hub.log, "the view alone does not answer"
        FakeBridge.instances[0].on_first_voice()   # the user presses Talk
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert a._hub.log.count("answer") == 1 and a._answered


def test_answer_on_open_answers_as_the_view_opens(acc, monkeypatch):
    a, procs, gate = acc
    _ringing(monkeypatch, a)
    a._answer_on_open = True
    _open_view(a, gate, session())
    assert a._hub.log.count("answer") == 1


def test_answer_on_open_does_not_hold_the_view(acc, monkeypatch):
    """The answer is a round trip through the relay: the view goes on opening
    meanwhile, and the answer still lands."""
    a, procs, gate = acc
    _ringing(monkeypatch, a)
    a._hub.video_active = True
    a._answer_on_open = True
    release = asyncio.Event()
    answer = a._hub.async_answer

    async def slow_answer():
        await release.wait()
        return await answer()
    monkeypatch.setattr(a._hub, "async_answer", slow_answer)
    info = session()

    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        assert await asyncio.wait_for(a.start_stream(info, {}), 2)
        assert "answer" not in a._hub.log, "the view opened first"
        release.set()
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert a._hub.log.count("answer") == 1 and "answer" in info["timeline"]


def test_closing_the_last_view_of_an_answered_ring_hangs_up(acc, monkeypatch):
    """The Home app has no hang-up button: closing the view ends the call."""
    a, procs, gate = acc
    _ringing(monkeypatch, a)
    a._answer_on_open = True
    info = session()
    _open_view(a, gate, info)
    _close(a, info)
    assert a._hub.log == ["opened", "answer", "closed", "hangup"]


def test_closing_a_preview_leaves_the_ring_alone(acc, monkeypatch):
    """Not answered: the Tab and the app can still pick the ring up."""
    a, procs, gate = acc
    _ringing(monkeypatch, a)
    info = session()
    _open_view(a, gate, info)
    _close(a, info)
    assert a._hub.log == ["opened", "closed"]


def test_an_auto_call_ends_with_its_last_view(acc):
    """v1.0.9 waits 30 s before ending an auto-call; HomeKit ends it at once."""
    a, procs, gate = acc
    a._hub._auto_called = True
    info = session()
    _open_view(a, gate, info)
    _close(a, info)
    assert a._hub.log[-1] == "hangup"


def test_another_viewer_keeps_the_call(acc):
    a, procs, gate = acc
    a._hub._auto_called = True
    info = session()
    _open_view(a, gate, info)
    a._hub._stream_viewers += 1          # someone else still watches /av
    _close(a, info)
    assert "hangup" not in a._hub.log


def test_the_audio_tap_follows_the_view(acc):
    a, procs, gate = acc
    info = session()
    _open_view(a, gate, info)
    tap = FakeTap.instances[0]
    assert tap.attached and not tap.closed
    _close(a, info)
    assert tap.closed


def _failed_start(a, gate, info):
    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        return await a.start_stream(info, {})
    return asyncio.run(scenario())


def test_a_refused_view_gives_its_viewer_place_back(acc):
    """stream_opened counts the viewer even when it says no call is coming (the
    quick-reopen pause, not registered). pyhap drops a failed start without
    calling stop_stream, so the place is given back by the start itself."""
    a, _procs, gate = acc
    a._hub.open_result = False
    info = session()
    assert _failed_start(a, gate, info) is False
    assert a._hub._stream_viewers == 0
    assert a._hub.log == ["opened", "closed"]
    assert "a_sock" not in info and "v_sock" not in info


def test_a_view_that_gets_no_call_releases_everything(acc, monkeypatch):
    a, _procs, gate = acc
    monkeypatch.setattr(hk, "CALL_WAIT", 0.05)
    a._hub.video_active = a._hub.in_call = False
    info = session()
    assert _failed_start(a, gate, info) is False
    assert a._hub._stream_viewers == 0
    assert "a_sock" not in info and "v_sock" not in info


# ─── a start that fails with an exception ─────────────────────────────────────

def _boom(*_a, **_k):
    raise OSError("boom")


async def _aboom(*_a, **_k):
    raise OSError("boom")


@pytest.mark.parametrize("where", ["video", "tap", "bridge", "ffmpeg"])
def test_an_exception_while_opening_releases_everything(acc, monkeypatch, where):
    """Only a cancellation was handled: any other exception (ffmpeg missing, a
    socket refused) left the viewer counted, the sockets open and the video
    sink on the hub's protocol, which lives as long as the hub: every later
    call streamed to that phone."""
    a, procs, gate = acc
    if where == "video":
        monkeypatch.setattr(FakeVideo, "open", _aboom)
    elif where == "tap":
        monkeypatch.setattr(hk.hkm, "AudioTap", _boom)
    elif where == "bridge":
        monkeypatch.setattr(FakeBridge, "start", _aboom)
    else:
        monkeypatch.setattr(hk.asyncio, "create_subprocess_exec", _aboom)
    info = session()
    a_sock, v_sock = info["a_sock"], info["v_sock"]
    assert _failed_start(a, gate, info) is False
    assert a._hub._stream_viewers == 0 and a._hub.log == ["opened", "closed"]
    assert a.sinks == [], "no video sink left on the hub's protocol"
    assert closed(a_sock) and closed(v_sock)
    assert all(v.stopped == 1 for v in FakeVideo.instances)
    assert all(b.stopped == 1 for b in FakeBridge.instances)
    assert all(t.closed for t in FakeTap.instances)


def test_an_encoder_that_fails_to_start_releases_the_view(acc, monkeypatch):
    a, procs, gate = acc
    FakeTranscoder.instances.clear()
    FakeTranscoder.gate = None
    monkeypatch.setattr(hk, "Transcoder", FakeTranscoder)
    monkeypatch.setattr(hk.hkm, "parameter_sets", lambda: (None, None))
    monkeypatch.setattr(FakeTranscoder, "start", _aboom)
    a._smooth = True
    info = session()
    assert _failed_start(a, gate, info) is False
    assert FakeTranscoder.instances[0].stopped, "its ffmpeg may already run"
    assert a._hub._stream_viewers == 0 and a.sinks == []


def test_a_stale_session_is_released_through_the_close(acc):
    """A prepared session that never started goes through the normal close:
    closing its sockets alone left anything else of it (the hub's viewer, a
    video sink) behind."""
    a, procs, gate = acc
    info = session()
    info["viewer"] = True
    a._hub._stream_viewers = 1
    video = FakeVideo()
    info["video"] = video
    a.sinks.append(video.on_live)
    info["video_detach"] = lambda: a.sinks.remove(video.on_live)
    info["created"] = hk.time.monotonic() - 61
    a.sessions[info["id"]] = info

    async def scenario():
        a._drop_stale_sessions()
        await asyncio.gather(*a._tasks)

    asyncio.run(scenario())
    assert a.sessions == {}
    assert a._hub._stream_viewers == 0 and a.sinks == [] and video.stopped == 1
    assert "a_sock" not in info and "v_sock" not in info


def test_a_view_closed_while_the_call_was_placed_does_not_answer(acc, monkeypatch):
    """The phone backed out while the hub waited in stream_opened. With the
    video already there, nothing checked: the view went on to answer the ring
    in answer-on-open mode."""
    a, procs, gate = acc
    _ringing(monkeypatch, a)
    a._hub.video_active = True
    a._answer_on_open = True
    info = session()

    async def scenario():
        gate["open"] = asyncio.Event()
        start = asyncio.create_task(a.start_stream(info, {}))
        await asyncio.sleep(0.01)
        stop = asyncio.create_task(a.stop_stream(info))
        await asyncio.sleep(0.01)
        gate["open"].set()
        await start
        await stop

    asyncio.run(scenario())
    assert "answer" not in a._hub.log
    assert a._hub._stream_viewers == 0 and procs == []


def test_the_close_does_not_wait_for_the_hang_up(acc):
    """The hang-up ran inside the session's close, under its lock: a BYE the
    cloud never answers held the close (and the next view) for ~5 s."""
    a, procs, gate = acc
    a._hub._auto_called = True
    info = session()
    _open_view(a, gate, info)
    never = []

    async def scenario():
        answered = asyncio.Event()

        async def slow_hangup():
            a._hub.log.append("hangup")
            never.append(1)
            await answered.wait()               # the cloud never answers

        a._hub.async_hangup = slow_hangup
        await asyncio.wait_for(a.stop_stream(info), 1.0)
        await asyncio.sleep(0.01)
        for t in a._tasks:
            t.cancel()

    asyncio.run(scenario())
    assert a._hub.log[-1] == "hangup" and never


# ─── the call ends while a view is still starting ─────────────────────────────

def test_the_end_of_the_call_closes_a_view_still_starting(acc):
    """The call ended while the view waited for it: the view came up anyway,
    frozen and silent, and kept its place among the hub's viewers."""
    a, procs, gate = acc
    info = session()
    a.sessions[info["id"]] = info

    async def scenario():
        gate["open"] = asyncio.Event()
        start = asyncio.create_task(a.start_stream(info, {}))
        await asyncio.sleep(0.01)                 # blocked in stream_opened
        assert a._hub._stream_viewers == 1
        a.on_video_ended()
        await asyncio.sleep(0.01)
        gate["open"].set()
        started = await start
        for _ in range(100):
            if "closed" in a._hub.log:
                break
            await asyncio.sleep(0.01)
        return started

    assert asyncio.run(scenario()) is True, "for pyhap: started, then stopped"
    assert procs == [], "no ffmpeg for a call that is over"
    assert a._hub._stream_viewers == 0
    assert FakeTap.instances == [] and a.sinks == []


def test_a_cancelled_start_gives_the_viewer_back(acc):
    a, procs, gate = acc
    info = session()

    async def scenario():
        gate["open"] = asyncio.Event()
        start = asyncio.create_task(a.start_stream(info, {}))
        await asyncio.sleep(0.01)                 # the hub counted, then waits
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start
        for _ in range(100):
            if "closed" in a._hub.log:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert a._hub._stream_viewers == 0
    assert closed(info["a_sock"] if "a_sock" in info else socket.socket()) or "a_sock" not in info


def test_a_failing_close_still_gives_the_viewer_back(acc):
    a, procs, gate = acc
    info = session()

    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        assert await a.start_stream(info, {})

        async def broken():
            raise RuntimeError("bridge stop failed")
        FakeBridge.instances[0].stop = broken
        with pytest.raises(RuntimeError):
            await a.stop_stream(info)

    asyncio.run(scenario())
    assert a._hub._stream_viewers == 0 and a._hub.log[-1] == "closed"


def test_a_view_closed_while_answering_hangs_up(acc, monkeypatch):
    """Talk pressed, then the view closed before the panel confirmed the
    answer: the close found nothing answered, and the call stayed up."""
    a, procs, gate = acc
    state = _ringing(monkeypatch, a)
    release = asyncio.Event
    info = session()

    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        assert await a.start_stream(info, {})
        slow = release()

        async def answer():
            a._hub.log.append("answer")
            await slow.wait()
            a._hub.in_call, state["ringing"] = True, False
            return True, "ok"
        a._hub.async_answer = answer
        FakeBridge.instances[0].on_first_voice()   # Talk
        FakeBridge.instances[0].on_first_voice()   # a second packet: still one answer
        await asyncio.sleep(0.01)
        await a.stop_stream(info)
        slow.set()
        for _ in range(100):
            if "hangup" in a._hub.log:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert a._hub.log == ["opened", "answer", "closed", "hangup"]


def test_sessions_we_closed_are_forgotten(acc, monkeypatch):
    a, procs, gate = acc
    info = session()
    a.sessions[info["id"]] = info
    _open_view(a, gate, info)
    asyncio.run(a._end_from_panel(info))        # the phone never sends its stop
    a._drop_stale_sessions()
    assert info["id"] in a.sessions, "pyhap may still be about to delete it"
    info["closed_at"] -= 61
    a._drop_stale_sessions()
    assert a.sessions == {}


def test_the_stream_start_logs_its_timeline(acc, monkeypatch, caplog):
    """Measurement discipline: one INFO line says where the opening time went,
    and every stage is a DEBUG mark from the same origin."""
    a, procs, gate = acc
    _ringing(monkeypatch, a)
    a._answer_on_open = True
    info = session()
    with caplog.at_level("DEBUG", logger=hk.__name__):
        _open_view(a, gate, info)
    started = [r.getMessage() for r in caplog.records
               if r.levelname == "INFO" and "stream started" in r.getMessage()]
    assert len(started) == 1
    line = started[0]
    assert " ms (stream_opened " in line
    for stage in ("call", "keyframe", "video begin", "ffmpeg"):
        assert f"{stage} " in line and stage in info["timeline"], stage
    # The answer runs beside the opening, so it may land before or after
    # this line (test_answer_on_open_does_not_hold_the_view checks its mark).
    assert a._hub.log.count("answer") == 1
    order = list(info["timeline"])
    assert order.index("call") < order.index("keyframe") < order.index("ffmpeg")
    marks = [r.getMessage() for r in caplog.records
             if r.levelname == "DEBUG" and "timeline" in r.getMessage()]
    assert any("timeline ffmpeg +" in m for m in marks)
    FakeBridge.instances[0].on_first_voice()
    assert "first voice" in info["timeline"]
    caplog.clear()
    with caplog.at_level("INFO", logger=hk.__name__):
        FakeBridge.instances[0].on_first_audio()
    assert "first audio" in info["timeline"]
    assert any("first audio to the phone" in r.getMessage() and "stream_opened " in r.getMessage()
               for r in caplog.records if r.levelname == "INFO")
    _close(a, info)


def test_the_views_ffmpeg_reads_the_tap_on_loopback_only(acc, monkeypatch):
    """ffmpeg 8.1 bound the RTCP port of an SDP input on 0.0.0.0."""
    a, procs, gate = acc
    cmds = []

    async def spawn(*args, **_kw):
        cmds.append(args)
        procs.append(FakeProc())
        return procs[-1]

    monkeypatch.setattr(hk.asyncio, "create_subprocess_exec", spawn)
    info = session()
    _open_view(a, gate, info)
    args = cmds[0]
    i = args.index("/tmp/x.sdp")
    assert args[i - 3:i] == ("-localaddr", "127.0.0.1", "-i")
    _close(a, info)


# ─── the shared encoder follows the call ──────────────────────────────────────

@pytest.fixture
def transcoding(acc, monkeypatch):
    a, procs, gate = acc
    FakeTranscoder.instances.clear()
    FakeTranscoder.gate = None
    monkeypatch.setattr(hk, "Transcoder", FakeTranscoder)
    monkeypatch.setattr(hk.hkm, "parameter_sets", lambda: (None, None))
    monkeypatch.setattr(media, "video_proto", object())
    a._smooth = True
    return a


def test_an_encoder_still_starting_when_the_call_ends_is_stopped(transcoding):
    """It was not in self._transcoder yet, so the end of the call missed it and
    the next call reused it: fed by the old call, showing the old visitor."""
    a = transcoding

    async def scenario():
        FakeTranscoder.gate = asyncio.Event()
        warming = asyncio.create_task(a._ensure_transcoder())
        await asyncio.sleep(0.01)
        a.on_video_ended()
        FakeTranscoder.gate.set()
        await warming
        for _ in range(100):
            if FakeTranscoder.instances[0].stopped:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert FakeTranscoder.instances[0].stopped
    assert a._transcoder is None


def test_the_next_call_gets_its_own_encoder(transcoding):
    """media_handler's video protocol is one per hub: the next call feeds the
    SAME protocol, so the protocol cannot tell the calls apart. The end of the
    call can: an encoder of the previous call shows the previous visitor."""
    a = transcoding
    proto = media.video_proto

    async def scenario():
        first = await a._ensure_transcoder()
        assert await a._ensure_transcoder() is first, "same call: shared"
        a.on_video_ended()                       # the call ends...
        await asyncio.sleep(0.01)
        second = await a._ensure_transcoder()    # ...and the next one starts
        return first, second

    first, second = asyncio.run(scenario())
    assert media.video_proto is proto, "the protocol never changed"
    assert second is not first and first.stopped and not second.stopped


def test_an_encoder_the_end_of_the_call_missed_is_not_reused(transcoding):
    """The stop spawned at the end of the call may not have run yet: the
    generation alone makes the next call build its own encoder."""
    a = transcoding

    async def scenario():
        first = await a._ensure_transcoder()
        a._call_gen += 1                         # the call ended, the stop not run
        second = await a._ensure_transcoder()
        return first, second

    first, second = asyncio.run(scenario())
    assert second is not first and first.stopped


def _ring(a, monkeypatch, video_flowing):
    video = {"ready": video_flowing}
    monkeypatch.setattr(hk.hkm, "video_ready", lambda _hub: video["ready"])
    a._hub.in_call = False
    a._char_ring = a._char_ring_switch = type("C", (), {"set_value": lambda *_: None})()

    async def scenario():
        video["ready"] = True
        first = await a._ensure_transcoder()
        video["ready"] = video_flowing
        a.ring()
        video["ready"] = True                    # the ring's early media arrives
        await asyncio.gather(*a._tasks)          # the ring's prewarm
        return first

    return asyncio.run(scenario())


def test_a_new_ring_does_not_reuse_the_previous_visitors_encoder(transcoding, monkeypatch):
    a = transcoding
    first = _ring(a, monkeypatch, video_flowing=False)
    assert first.stopped and a._transcoder is not first and a._transcoder is not None


def test_a_ring_during_this_visitors_video_keeps_the_encoder(transcoding, monkeypatch):
    """A second press while the preview plays: the open views read this
    encoder, and replacing it would freeze them."""
    a = transcoding
    first = _ring(a, monkeypatch, video_flowing=True)
    assert not first.stopped and a._transcoder is first


def test_no_encoder_without_call_video(transcoding, monkeypatch):
    """The call ended between the view's start and its encoder: an encoder
    started then reads the next call's packets, or nothing."""
    a = transcoding
    monkeypatch.setattr(hk.hkm, "video_ready", lambda _hub: False)
    assert asyncio.run(a._ensure_transcoder()) is None
    assert FakeTranscoder.instances == []


def test_no_encoder_for_a_view_that_is_closing(transcoding):
    a = transcoding
    assert asyncio.run(a._ensure_transcoder({"stopping": True})) is None
    assert FakeTranscoder.instances == []


def test_no_encoder_once_homekit_is_off(transcoding):
    a = transcoding
    a._closed = True
    assert asyncio.run(a._ensure_transcoder()) is None
    assert FakeTranscoder.instances == []


def test_an_encoder_start_cancelled_midway_is_stopped(transcoding):
    """start_stream cancelled while ffmpeg was already spawned: the encoder
    was nobody's, and ran until Home Assistant restarted."""
    a = transcoding

    async def scenario():
        FakeTranscoder.gate = asyncio.Event()
        warming = asyncio.create_task(a._ensure_transcoder())
        await asyncio.sleep(0.01)
        warming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await warming

    asyncio.run(scenario())
    assert FakeTranscoder.instances[0].stopped and a._transcoder is None


# ─── pairing ───────────────────────────────────────────────────────────────────

def test_the_setup_code_never_goes_to_stdout(capsys):
    """pyhap prints the code and a QR to stdout when unpaired: straight into
    Home Assistant's container log."""
    a = hk.VimarDoorbell.__new__(hk.VimarDoorbell)
    a.driver = type("D", (), {"state": type("S", (), {"pincode": b"123-45-678",
                                                      "setup_id": "ABCD"})()})()
    a.category = hk.CATEGORY_VIDEO_DOOR_BELL
    a.setup_message()
    out = capsys.readouterr()
    assert "123-45-678" not in out.out + out.err


def test_unpairing_rotates_the_setup_code(tmp_path, monkeypatch, assert_private_mode):
    """The old code was in the notification, maybe in a screenshot or with a
    previous owner. After the last controller removes the intercom, only a
    new code pairs it again."""
    (tmp_path / ".storage").mkdir()
    hass = FakeHass(tmp_path)
    _state, pin_path = hkf.homekit_files(hass, "e1")
    old = hk._load_or_create_pin(pin_path)
    shown = []
    monkeypatch.setattr(hk, "_show_pairing", lambda _h, _e, _acc, pin: shown.append(pin))
    driver = type("D", (), {})()
    driver.state = type("S", (), {"pincode": old.encode()})()
    changed = hk._pairing_callback(
        hass, {"acc": object(), "driver": driver, "entry_id": "e1"}, pin_path)

    async def scenario():
        changed(False)
        new = driver.state.pincode.decode()      # at once, before any await
        await asyncio.gather(*hass.tasks)
        return new

    new = asyncio.run(scenario())
    assert new != old and shown == [new]
    assert hk._load_or_create_pin(pin_path) == new, "and it survives a restart"
    assert_private_mode(tmp_path / ".storage" / "vimar_intercom.e1.homekit.pin")


def test_the_qr_follows_the_new_code(tmp_path, monkeypatch):
    """pyhap builds the QR payload from driver.state.pincode."""
    pytest.importorskip("base36")
    driver = type("D", (), {})()
    driver.state = type("S", (), {"pincode": b"111-22-333", "setup_id": "ABCD"})()
    a = hk.VimarDoorbell.__new__(hk.VimarDoorbell)
    a.driver, a.category = driver, hk.CATEGORY_VIDEO_DOOR_BELL
    before = a.xhm_uri()
    hk._rotate_pin(driver)
    assert a.xhm_uri() != before


def test_deleting_the_entry_deletes_its_pairing(tmp_path):
    (tmp_path / ".storage").mkdir()
    hass = FakeHass(tmp_path)
    for path in hkf.homekit_files(hass, "e1") + hkf.homekit_files(hass, "e2"):
        open(path, "w").close()
    hkf.remove_homekit_files(hass, "e1")
    hkf.remove_homekit_files(hass, "e1")          # twice: nothing left, no error
    assert sorted(p.name for p in (tmp_path / ".storage").iterdir()) == [
        "vimar_intercom.e2.homekit.pin", "vimar_intercom.e2.homekit.state"]


# ─── pairing stays with administrators ─────────────────────────────────────────

@pytest.fixture
def notes(monkeypatch):
    n = Notes()
    monkeypatch.setattr(hk, "persistent_notification", n)
    monkeypatch.setattr(hk, "_qr_svg", lambda uri: b"<svg>" + uri.encode() + b"</svg>")
    return n


def test_the_setup_code_is_not_in_the_notification(tmp_path, notes):
    """A persistent notification is shown to every user: with the code in it,
    anyone could pair and get video, Talk and the gate, past allowed_users."""
    hass = FakeHass(tmp_path)
    hk._show_pairing(hass, "e1", QRAcc(), "123-45-678")
    (_title, text), = notes.shown.values()
    assert "123-45-678" not in text and "homekit_qr" not in text
    assert "Configure → HomeKit" in text
    info = hass.data[hk.HOMEKIT_DATA]["pairing"]["e1"]
    assert info["pin"] == "123-45-678" and info["svg"] and info["token"]
    hk._hide_pairing(hass, "e1")
    assert notes.shown == {} and hass.data[hk.HOMEKIT_DATA]["pairing"] == {}


def _user(admin):
    return type("U", (), {"is_admin": admin})()


def test_the_qr_is_for_administrators_only(tmp_path, notes, monkeypatch):
    monkeypatch.setattr(hk, "web", type("W", (), {"Response": Response}))
    hass = FakeHass(tmp_path)
    hk._show_pairing(hass, "e1", QRAcc(), "123-45-678")
    token = hass.data[hk.HOMEKIT_DATA]["pairing"]["e1"]["token"]
    view = hk._PairingQRView(hass)
    assert view.requires_auth is True

    def get(user, t):
        return asyncio.run(view.get(Req(user, t)))

    assert get(_user(False), token).status == 403, "a user who is not admin"
    assert get(None, token).status == 403
    assert get(_user(True), "wrong").status == 404
    ok = get(_user(True), token)
    assert ok.status == 200 and ok.body.startswith(b"<svg>")
    hk._hide_pairing(hass, "e1")
    assert get(_user(True), token).status == 404, "paired or turned off: gone"


def _driver(**callbacks):
    d = hk._Driver.__new__(hk._Driver)
    d.state = FakeState()
    d.async_persist = lambda: None
    d._pair_failures = 0
    d._on_pairing_change = callbacks.get("changed", lambda _p: None)
    d._on_pair_failures = callbacks.get("failures")
    return d


def test_unpairing_one_of_two_controllers_keeps_the_code():
    changes = []
    d = _driver(changed=changes.append)
    d.state.clients = 2
    d.unpair("uuid-1")
    assert changes == [], "another controller is still paired: no new code"
    d.unpair("uuid-2")
    assert changes == [False]


def test_ten_wrong_codes_in_a_row_change_the_code():
    """pyhap has no limit on pairing attempts: the eight digits could be tried
    one after another."""
    tripped = []
    d = _driver(failures=tripped.append)
    for _ in range(hk.MAX_PAIR_FAILURES - 1):
        d._pair_attempt(False)
    d._pair_attempt(True)                        # the right code resets the count
    for _ in range(hk.MAX_PAIR_FAILURES - 1):
        d._pair_attempt(False)
    assert tripped == []
    d._pair_attempt(False)
    assert tripped == [hk.MAX_PAIR_FAILURES]
    assert d._pair_failures == 0, "counting starts again with the new code"


def test_pyhaps_verifier_is_the_counting_one():
    """pyhap builds a verifier at every pair-setup M1 and checks the code with
    it at M3: ours reports each check."""
    results = []
    d = _driver()
    d._pair_attempt = results.append
    d.setup_srp_verifier()
    assert isinstance(d.srp_verifier, hk._CountingVerifier)
    salt, _b = d.srp_verifier.get_challenge()   # the rest is pyhap's own
    assert salt
    d.srp_verifier._inner = type("V", (), {"verify": lambda _s, m: b"ok" if m == b"good" else None})()
    assert d.srp_verifier.verify(b"bad") is None and d.srp_verifier.verify(b"good") == b"ok"
    assert results == [False, True]


def test_too_many_wrong_codes_rotate_it_and_warn(tmp_path, notes, caplog):
    (tmp_path / ".storage").mkdir()
    hass = FakeHass(tmp_path)
    _state, pin_path = hkf.homekit_files(hass, "e1")
    driver = _driver()
    old = driver.state.pincode
    too_many = hk._pair_failures_callback(
        hass, {"acc": QRAcc(), "driver": driver, "entry_id": "e1"}, pin_path)

    async def scenario():
        too_many(hk.MAX_PAIR_FAILURES)
        await asyncio.gather(*hass.tasks)

    asyncio.run(scenario())
    new = driver.state.pincode.decode()
    assert new.encode() != old
    assert hass.data[hk.HOMEKIT_DATA]["pairing"]["e1"]["pin"] == new
    assert hk._load_or_create_pin(pin_path) == new
    assert any(r.levelname == "WARNING" and "wrong setup codes" in r.getMessage()
               for r in caplog.records)


def test_the_code_file_is_private_and_replaced_whole(tmp_path, assert_private_mode):
    path = str(tmp_path / "x.homekit.pin")
    with open(path, "w") as f:
        f.write("{}")
    import os
    os.chmod(path, 0o644)
    old_umask = os.umask(0)
    try:
        hk._save_pin(path, "123-45-678")
    finally:
        os.umask(old_umask)
    assert_private_mode(path)
    assert hk._load_or_create_pin(path) == "123-45-678"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.homekit.pin"], "no temp file left"


def test_a_code_that_cannot_be_saved_is_logged(tmp_path, caplog):
    hass = FakeHass(tmp_path)
    asyncio.run(hk._store_pin(hass, str(tmp_path / "missing" / "x.pin"), "123-45-678"))
    assert any(r.levelname == "ERROR" and "not saved" in r.getMessage() for r in caplog.records)


# ─── publishing and turning off ─────────────────────────────────────────────────

@pytest.fixture
def setup_rig(tmp_path, monkeypatch, notes):
    import sys
    (tmp_path / ".storage").mkdir()
    hass = SetupHass(tmp_path)
    hub = SetupHub()
    order = []

    class Driver:
        fail_start = False

        def __init__(self, **kw):
            assert hass.in_executor, "pyhap's driver does file I/O as it is built"
            self.kw = kw
            self.state = FakeState(clients=0)
            self.state.pincode = kw["pincode"]
            Driver.last = self

        def add_accessory(self, acc):
            self.acc = acc

        async def async_start(self):
            order.append(("start", list(hub.rings), list(hub.ends)))
            if Driver.fail_start:
                raise OSError("port in use")

        async def async_stop(self):
            order.append(("stop",))

    class Acc(QRAcc):
        def __init__(self, *_a, **_k):
            self._tasks, self._closed = set(), False
            self._smooth, self._answer_on_open = True, False

        def ring(self):
            pass

        def on_video_ended(self):
            pass

        async def async_close_views(self):
            pass

        async def _stop_transcoder(self):
            order.append(("transcoder stopped", self._closed))

        _spawn = hk.VimarDoorbell._spawn

        async def _keep_last_frame(self):
            await asyncio.Event().wait()   # until HomeKit stops and cancels it

    async def source_ip(_hass):
        return "127.0.0.1"

    async def zc(_hass):
        return None

    comps = sys.modules["homeassistant.components"]
    monkeypatch.setattr(comps, "network", type("N", (), {"async_get_source_ip": staticmethod(source_ip)}),
                        raising=False)
    monkeypatch.setattr(comps, "zeroconf", type("Z", (), {"async_get_async_instance": staticmethod(zc)}),
                        raising=False)
    monkeypatch.setattr(hk, "_Driver", Driver)
    monkeypatch.setattr(hk, "VimarDoorbell", Acc)
    entry = type("E", (), {"entry_id": "e1", "options": {}})()
    return hass, hub, entry, Driver, order


def test_the_hub_callbacks_come_after_the_server_is_up(setup_rig):
    hass, hub, entry, Driver, order = setup_rig

    async def scenario():
        return await hk.async_setup_homekit(hass, entry, hub)

    stop = asyncio.run(scenario())
    assert order[0] == ("start", [], []), "nothing registered before the start"
    assert len(hub.rings) == 1 and len(hub.ends) == 1
    asyncio.run(stop())


def test_a_server_that_does_not_start_registers_nothing(setup_rig):
    hass, hub, entry, Driver, order = setup_rig
    Driver.fail_start = True
    with pytest.raises(OSError):
        asyncio.run(hk.async_setup_homekit(hass, entry, hub))
    assert hub.rings == [] and hub.ends == []
    assert ("stop",) in order, "a half-started server frees its port"


def test_turning_homekit_off_unpaired_cleans_up_and_changes_the_code(setup_rig, notes):
    hass, hub, entry, Driver, order = setup_rig

    async def scenario():
        stop = await hk.async_setup_homekit(hass, entry, hub)
        acc = Driver.last.acc
        assert notes.shown and hass.data[hk.HOMEKIT_DATA]["pairing"]["e1"]
        pending = asyncio.get_running_loop().create_future()
        acc._tasks.add(pending)
        shown_pin = Driver.last.state.pincode
        await stop()
        return acc, pending, shown_pin

    acc, pending, shown_pin = asyncio.run(scenario())
    assert acc._closed and pending.cancelled()
    assert ("transcoder stopped", True) in order
    assert notes.shown == {} and notes.dismissed, "the notification goes with it"
    assert hass.data[hk.HOMEKIT_DATA]["pairing"] == {}
    _state, pin_path = hkf.homekit_files(hass, "e1")
    new = Driver.last.state.pincode
    assert new != shown_pin and hk._load_or_create_pin(pin_path) == new.decode()
    assert hub.rings == [] and hub.ends == []


def test_the_tile_reads_the_ring_photo_once_after_a_restart(acc, monkeypatch):
    a, _procs, _gate = acc
    reads = []

    class Hass:
        async def async_add_executor_job(self, fn, *args):
            reads.append(fn)
            return b"photo"

    a._hass = Hass()
    a._photo_read = False
    monkeypatch.setattr(hk.hkm, "last_frame", lambda: None)
    assert asyncio.run(a.async_get_snapshot({})) == b"photo"
    assert asyncio.run(a.async_get_snapshot({})) == b"photo"
    assert reads == [hk.hkm.last_ring_photo], "read once, then kept"


def test_the_ring_snapshot_waits_for_the_first_frame(acc, monkeypatch):
    """iOS asks for the notification's picture as the ring goes out, before
    the first frame is decoded: it used to get the black placeholder."""
    a, _procs, _gate = acc
    _ringing(monkeypatch, a)
    frame = {"jpeg": None}
    monkeypatch.setattr(hk.hkm, "last_frame", lambda: frame["jpeg"])

    async def scenario():
        snap = asyncio.create_task(a.async_get_snapshot({}))
        await asyncio.sleep(0.35)
        assert not snap.done(), "it waits"
        frame["jpeg"] = b"first frame"
        return await asyncio.wait_for(snap, 1)
    assert asyncio.run(scenario()) == b"first frame"


def test_the_ring_snapshot_takes_the_re_encoders_picture(acc, monkeypatch):
    """The relay lost part of the first keyframe: the frame grabber waits for
    the next complete one (3.6 s on a 40517), the re-encoder conceals the loss
    and has a picture at once."""
    a, _procs, _gate = acc
    _ringing(monkeypatch, a)
    monkeypatch.setattr(hk.hkm, "last_frame", lambda: None)
    a._transcoder = SimpleNamespace(running=True, generation=a._call_gen,
                                    last_jpeg=b"concealed")

    async def scenario():
        return await asyncio.wait_for(a.async_get_snapshot({}), 0.05)
    assert asyncio.run(scenario()) == b"concealed"


def test_an_earlier_calls_re_encoder_picture_is_not_used(acc, monkeypatch):
    a, _procs, _gate = acc
    monkeypatch.setattr(hk.sip, "ringing", lambda: False)
    a._hub.in_call = False
    monkeypatch.setattr(hk.hkm, "last_frame", lambda: b"grabber")
    a._transcoder = SimpleNamespace(running=True, generation=a._call_gen - 1,
                                    last_jpeg=b"previous visitor")
    assert asyncio.run(a.async_get_snapshot({})) == b"grabber"


def test_without_a_ring_the_snapshot_does_not_wait(acc, monkeypatch):
    a, _procs, _gate = acc
    monkeypatch.setattr(hk.sip, "ringing", lambda: False)
    a._hub.in_call = False
    monkeypatch.setattr(hk.hkm, "last_frame", lambda: None)
    a._last_frame = b"earlier"

    async def scenario():
        return await asyncio.wait_for(a.async_get_snapshot({}), 0.05)
    assert asyncio.run(scenario()) == b"earlier"


def test_the_tile_keeps_the_calls_last_frame_after_it_ends(acc, monkeypatch):
    """Nobody asked for a snapshot during the view; once the call ended, the
    Home app's refresh got the black placeholder."""
    a, _procs, _gate = acc
    monkeypatch.setattr(hk.sip, "ringing", lambda: False)
    a._hub.in_call = False
    a._photo_read = True
    frame = {"jpeg": b"the courier"}
    monkeypatch.setattr(hk.hkm, "last_frame", lambda: frame["jpeg"])
    monkeypatch.setattr(hk, "FRAME_KEEP_EVERY", 0.01)

    async def scenario():
        keeper = asyncio.create_task(a._keep_last_frame())
        await asyncio.sleep(0.05)
        frame["jpeg"] = None                     # the call ended
        await asyncio.sleep(0.05)
        a._closed = True
        await keeper
        return await a.async_get_snapshot({})
    assert asyncio.run(scenario()) == b"the courier"


def test_unloading_closes_the_open_views(acc):
    """A reload or HomeKit turned off must not leave a live view's ffmpeg
    running (review of #32)."""
    a, procs, gate = acc
    first, second = session(), session()
    second["id"] = "s2"

    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        a.sessions = {"s1": first, "s2": second}
        assert await a.start_stream(first, {})
        assert await a.start_stream(second, {})
        running = [p.returncode for p in procs]
        await a.async_close_views()
        return running

    running = asyncio.run(scenario())
    assert running == [None, None], "both views had a live ffmpeg"
    assert len(procs) == 2 and all(p.returncode is not None for p in procs)
    assert a.sinks == []
    assert a._hub._stream_viewers == 0


@pytest.mark.parametrize("cached, requests", [(False, 1), (True, 0)])
def test_a_view_without_a_cached_keyframe_asks_the_panel_for_one(acc, monkeypatch, cached, requests):
    """The 40515 honours picture_fast_update (IDR in ~0.25 s, measured on #31):
    a view opening with no whole keyframe cached asks for one instead of waiting
    for the panel's own (up to 3 s). With one cached, no request."""
    a, _procs, gate = acc
    sent = []

    async def request():
        sent.append(True)

    monkeypatch.setattr(hk.sip, "send_keyframe_request", request)
    monkeypatch.setattr(hk.hkm, "gop_has_keyframe", lambda: cached)
    monkeypatch.setattr(hk, "KEYFRAME_WAIT", 0.05)
    info = session()

    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        assert await a.start_stream(info, {})
        await asyncio.sleep(0)
        await a.stop_stream(info)

    asyncio.run(scenario())
    assert len(sent) == requests


def test_the_state_file_with_the_long_term_key_is_0600(monkeypatch, tmp_path, assert_private_mode):
    """The state file holds the accessory's key and the paired controllers:
    0600 like the pin file, whatever mode pyhap's own write leaves."""
    path = tmp_path / "vimar_intercom.e1.homekit.state"

    def pyhap_persist(self):
        path.write_text("{}")
        os.chmod(path, 0o644)                       # a pyhap that doesn't care

    monkeypatch.setattr(hk.AccessoryDriver, "persist", pyhap_persist)
    driver = object.__new__(hk._Driver)             # no server, no network
    driver.persist_file = str(path)
    driver.persist()
    assert path.read_text() == "{}"
    assert_private_mode(path)


def test_the_audio_does_not_wait_for_the_picture(acc, monkeypatch):
    """The audio's ffmpeg needs nothing from the video path. It started after it,
    re-encoder and keyframe wait included: on a 40515 first audio waited ~0.9 s
    for a picture it does not depend on (#32). Now it starts while the video
    still waits for its keyframe."""
    a, procs, gate = acc
    keyframe = {"in": False}
    monkeypatch.setattr(hk.hkm, "gop_has_keyframe", lambda: keyframe["in"])
    info = session()

    async def scenario():
        gate["open"].set()
        start = asyncio.create_task(a.start_stream(info, {}))
        for _ in range(100):                      # the audio's ffmpeg starts...
            await asyncio.sleep(0.01)
            if procs:
                break
        early = dict(info["timeline"])
        keyframe["in"] = True                     # ...then the panel's keyframe
        assert await start
        return early

    early = asyncio.run(scenario())
    assert "ffmpeg" in early and "video begin" not in early
    assert "video begin" in info["timeline"]
    _close(a, info)


def test_the_re_encoder_starts_while_the_call_connects(acc, monkeypatch):
    """Its ffmpeg takes about half a second to start. It used to start only once the
    call's video had arrived: the longest stage of a view on a 40515 (#32)."""
    a, procs, gate = acc
    a._smooth = True
    FakeTranscoder.instances.clear()
    FakeTranscoder.gate = None
    monkeypatch.setattr(hk, "Transcoder", FakeTranscoder)
    video = {"in": False}
    monkeypatch.setattr(hk.hkm, "video_ready", lambda _hub: video["in"])
    monkeypatch.setattr(hk.hkm, "parameter_sets", lambda panel_uri=None: (b"sps", b"pps"))
    info = session()

    async def scenario():
        gate["open"].set()                      # the hub places the call...
        a._hub.in_call = a._hub.video_active = False   # ...which is still connecting
        start = asyncio.create_task(a.start_stream(info, {}))
        await asyncio.sleep(0.05)
        before_the_call = len(FakeTranscoder.instances)
        a._hub.in_call = a._hub.video_active = video["in"] = True
        assert await start
        return before_the_call

    assert asyncio.run(scenario()) == 1, "started before the call was up"
    assert len(FakeTranscoder.instances) == 1, "the view uses that one, not a second"
    assert info["timeline"].get("transcoder") is not None
    _close(a, info)


def test_an_early_re_encoder_goes_with_a_call_that_never_connects(acc, monkeypatch):
    a, procs, gate = acc
    a._smooth = True
    FakeTranscoder.instances.clear()
    FakeTranscoder.gate = None
    monkeypatch.setattr(hk, "Transcoder", FakeTranscoder)
    monkeypatch.setattr(hk.hkm, "video_ready", lambda _hub: False)
    monkeypatch.setattr(hk.hkm, "parameter_sets", lambda panel_uri=None: (b"sps", b"pps"))

    async def scenario():
        await a._ensure_transcoder(early=True)
        a.on_video_ended()                      # the placed call failed (busy, no answer)
        while a._tasks:
            await asyncio.gather(*a._tasks)

    asyncio.run(scenario())
    assert FakeTranscoder.instances and all(t.stopped for t in FakeTranscoder.instances)
    assert a._transcoder is None


def test_no_early_re_encoder_without_the_panels_parameters(acc, monkeypatch):
    """Right after a restart no call has set the panel yet: an early encoder had
    no SPS/PPS, missed the first keyframe, and the picture came at 3.6 s. It now
    waits for the video, as before, and the view still gets one."""
    a, procs, gate = acc
    a._smooth = True
    FakeTranscoder.instances.clear()
    FakeTranscoder.gate = None
    monkeypatch.setattr(hk, "Transcoder", FakeTranscoder)
    video = {"in": False}
    monkeypatch.setattr(hk.hkm, "video_ready", lambda _hub: video["in"])
    asked = []

    def params(panel_uri=None):
        asked.append(panel_uri)
        return (None, None) if panel_uri else (b"sps", b"pps")
    monkeypatch.setattr(hk.hkm, "parameter_sets", params)
    monkeypatch.setattr(hk.R, "INTERCOM", "sip:55100@plant.example.test")
    info = session()

    async def scenario():
        gate["open"].set()
        a._hub.in_call = a._hub.video_active = False
        start = asyncio.create_task(a.start_stream(info, {}))
        await asyncio.sleep(0.05)
        early = len(FakeTranscoder.instances)
        a._hub.in_call = a._hub.video_active = video["in"] = True
        assert await start
        return early

    assert asyncio.run(scenario()) == 0
    assert "sip:55100@plant.example.test" in asked, "asked for the called panel's"
    assert len(FakeTranscoder.instances) == 1, "the view still gets one, once the video is in"
    _close(a, info)


def test_no_early_re_encoder_for_a_call_that_already_failed(acc, monkeypatch):
    """Review of #48: a placed call failing at once (486, or #41's silent panel)
    can end before the spawned early start takes its lock. The start must not
    leave an idle ffmpeg that nothing stops."""
    a, procs, gate = acc
    a._smooth = True
    FakeTranscoder.instances.clear()
    FakeTranscoder.gate = None
    monkeypatch.setattr(hk, "Transcoder", FakeTranscoder)
    monkeypatch.setattr(hk.hkm, "video_ready", lambda _hub: False)
    monkeypatch.setattr(hk.hkm, "parameter_sets", lambda panel_uri=None: (b"sps", b"pps"))
    a._hub.call_coming = False                  # the call is already over

    assert asyncio.run(a._ensure_transcoder(early=True)) is None
    assert FakeTranscoder.instances == []


def test_no_early_re_encoder_during_a_ring(acc, monkeypatch):
    """Review of #48: a ring from another panel also makes a view "in view". The
    early start would take R.INTERCOM's SPS/PPS, not the ringing panel's; the
    ring's early media brings the video soon anyway. The view still gets an
    encoder, the usual way."""
    a, procs, gate = acc
    a._smooth = True
    FakeTranscoder.instances.clear()
    FakeTranscoder.gate = None
    monkeypatch.setattr(hk, "Transcoder", FakeTranscoder)
    monkeypatch.setattr(hk.sip, "ringing", lambda: True)
    video = {"in": False}
    monkeypatch.setattr(hk.hkm, "video_ready", lambda _hub: video["in"])
    monkeypatch.setattr(hk.hkm, "parameter_sets", lambda panel_uri=None: (b"sps", b"pps"))
    info = session()

    async def scenario():
        gate["open"].set()
        a._hub.in_call = a._hub.video_active = False
        start = asyncio.create_task(a.start_stream(info, {}))
        await asyncio.sleep(0.05)
        early = len(FakeTranscoder.instances)
        a._hub.in_call = a._hub.video_active = video["in"] = True
        assert await start
        return early

    assert asyncio.run(scenario()) == 0
    assert len(FakeTranscoder.instances) == 1
    _close(a, info)


def test_the_early_re_encoder_starts_from_the_group_in_order(acc, monkeypatch):
    """Field test on 5G (#48): fed live from the first packet, the early encoder
    lost one the relay delivered out of order ("RTP: dropping old packet received
    too late"), its first keyframe was broken and the view fell back to the
    panel's next keyframe (3.6 s). It only warms up early; the view begins it,
    from the group replayed in sequence order, when its video is in."""
    a, procs, gate = acc
    a._smooth = True
    FakeTranscoder.instances.clear()
    FakeTranscoder.gate = None
    monkeypatch.setattr(hk, "Transcoder", FakeTranscoder)
    video = {"in": False}
    monkeypatch.setattr(hk.hkm, "video_ready", lambda _hub: video["in"])
    monkeypatch.setattr(hk.hkm, "parameter_sets", lambda panel_uri=None: (b"sps", b"pps"))
    info = session()

    async def scenario():
        gate["open"].set()
        a._hub.in_call = a._hub.video_active = False
        start = asyncio.create_task(a.start_stream(info, {}))
        await asyncio.sleep(0.05)
        tc = FakeTranscoder.instances[0]
        before = (tc.begun, list(a.sinks))
        a._hub.in_call = a._hub.video_active = video["in"] = True
        assert await start
        return tc, before

    tc, (begun_early, sinks_early) = asyncio.run(scenario())
    assert not begun_early and tc.feed not in sinks_early, "warmed up, not fed"
    assert tc.begun and tc.begins == 1 and tc.feed in a.sinks
    _close(a, info)
