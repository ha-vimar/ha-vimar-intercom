"""A HomeKit view that opens and closes in awkward order leaves nothing behind.

Backing out of the live view within a couple of seconds sends the phone's
"stop" while start_stream is still waiting for the call. The old code closed
the sockets under it, then start_stream carried on and attached a dead video
sink for good, or left ffmpeg running. And three parties can close a session
(the phone, the panel hanging up, ffmpeg exiting), sometimes at once.
"""
import asyncio
import socket

import pytest

hk = pytest.importorskip("custom_components.vimar_intercom.homekit_accessory")
media = hk.media


class FakeVideo:
    instances: list = []

    def __init__(self, *_a):
        self.stopped = 0
        FakeVideo.instances.append(self)

    async def open(self):
        pass

    def begin(self, *_a):
        pass

    def on_live(self, *_a):
        pass

    def send_bye(self):
        pass

    async def stop(self):
        self.stopped += 1


class FakeBridge:
    instances: list = []
    encoder_port = 9

    def __init__(self, *_a):
        self.stopped = 0
        FakeBridge.instances.append(self)

    async def start(self):
        pass

    def send_bye(self):
        pass

    async def stop(self):
        self.stopped += 1


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.pid = 4242
        self._done = asyncio.Event()
        self.stderr = None

    def terminate(self):
        self.returncode = -15
        self._done.set()

    def kill(self):
        self.terminate()

    async def wait(self):
        await self._done.wait()
        return self.returncode


class Hub:
    in_call = True
    calling = False

    def spawn(self, coro):
        return asyncio.ensure_future(coro)


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
    procs = []
    gate = {}

    async def slow_source(_hub, _hass):
        await gate["open"].wait()
        return "-i x.sdp", {"id": "m"}

    async def spawn_ffmpeg(*_a, **_k):
        procs.append(FakeProc())
        return procs[-1]

    async def no_stderr(*_a):
        return None

    monkeypatch.setattr(hk, "prepare_homekit_source", slow_source)
    monkeypatch.setattr(hk, "DirectVideo", FakeVideo)
    monkeypatch.setattr(hk, "AudioBridge", FakeBridge)
    monkeypatch.setattr(hk, "log_stderr", no_stderr)
    monkeypatch.setattr(hk, "ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(hk.asyncio, "create_subprocess_exec", spawn_ffmpeg)
    monkeypatch.setattr(media, "video_ready", lambda: True)
    monkeypatch.setattr(media, "gop_has_keyframe", lambda: True)
    monkeypatch.setattr(media, "gop_for_direct_video", lambda: ([], []))
    monkeypatch.setattr(media, "_video_sinks", [], raising=False)

    a = hk.VimarDoorbell.__new__(hk.VimarDoorbell)
    a._hub, a._hass, a._smooth, a._transcoder = Hub(), None, False, None
    a.sessions = {}
    a.set_streaming_available = lambda _idx: None
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
    assert media._video_sinks == [], "no dead video sink left behind"
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
    assert media._video_sinks == []


def test_a_normal_view_opens_and_closes(acc):
    a, procs, gate = acc
    info = session()

    async def scenario():
        gate["open"] = asyncio.Event()
        gate["open"].set()
        ok = await a.start_stream(info, {})
        sinks_while_open = list(media._video_sinks)
        await a.stop_stream(info)
        return ok, sinks_while_open

    ok, sinks = asyncio.run(scenario())
    assert ok is True and len(sinks) == 1
    assert media._video_sinks == []
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
    from pyhap.camera import (
        SELECTED_STREAM_CONFIGURATION_TYPES, SETUP_TYPES, STREAMING_STATUS)
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
            return True, "OK (200)"

    a._hub, a._char_lock_current, a._char_lock_target = Hub(), Char(), Char()
    monkeypatch.setattr(hk, "GATE_RELOCK_SECONDS", 0)
    asyncio.run(a._open_gate())
    assert a._char_lock_current.values == [hk.LOCK_UNSECURED, hk.LOCK_UNKNOWN]
    assert hk.LOCK_SECURED not in a._char_lock_current.values
    assert a._char_lock_target.values == [hk.LOCK_SECURED], "the next tap opens again"
