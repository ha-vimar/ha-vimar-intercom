"""The HTTP views of __init__.py (/audio_ws, /av, /rings) on the harness's fake aiohttp,
with a fake hub: every WebSocket action, the access checks and the /av outcomes."""
from __future__ import annotations

import asyncio
import json
import re
import sys
import types

import pytest
from harness.web import Request, fake_ws, load_views

from custom_components.vimar_intercom import av_stream as av_stream_mod
from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import ring_log, runtime
from custom_components.vimar_intercom import sip_client as sip
from custom_components.vimar_intercom.hub import VimarIntercomHub


class ViewHub:
    """The hub as the views see it: state flags and awaitable actions whose
    results (or exceptions) each test sets."""

    state_message = VimarIntercomHub.state_message

    def __init__(self):
        self.registered, self.in_call, self.is_ringing = True, False, False
        self.video_active, self.call_pending = False, False
        self.results: dict = {}
        self.calls: list = []
        self.claims = 0
        self.opened = self.closed = 0
        self.open_result = True

    def claim_call(self):
        self.claims += 1

    def _act(self, name, *args):
        self.calls.append((name, *args))
        r = self.results.get(name, (True, f"{name} ok"))
        if isinstance(r, Exception):
            raise r
        return r

    async def async_call(self, target=None):
        return self._act("call", target)

    async def async_hangup(self):
        return self._act("hangup")

    async def async_door(self, target=None):
        return self._act("door", target)

    async def async_send_command(self, **kw):
        return self._act("command", kw)

    async def async_probe(self, target):
        return self._act("probe", target)

    async def async_scan(self, start, end):
        return self._act("scan", start, end)

    async def async_answer(self):
        return self._act("answer")

    async def async_decline(self):
        return self._act("decline")

    async def stream_opened(self):
        self.opened += 1
        return self.open_result

    async def stream_closed(self):
        self.closed += 1


class _BackgroundTasks(list):
    def __call__(self, coro, name):
        coro.close()  # a keyframe request would go to the SIP socket: not here
        self.append(name)


def _hass(hub, clients=None, wants=None):
    data = {"e1": {"hub": hub, "audio_ws_clients": clients if clients is not None else set(),
                   "ws_wants": wants if wants is not None else {}}}
    return types.SimpleNamespace(data={"vimar_intercom": data} if hub else {},
                                 async_create_background_task=_BackgroundTasks())


@pytest.fixture
def views(monkeypatch):
    mod = load_views(monkeypatch)
    monkeypatch.setattr(media, "video_proto", None)
    mod.new_ws = mod.web.WebSocketResponse  # _talk replaces it with a factory per test call
    return mod


def _msg(kind, data):
    return types.SimpleNamespace(type=kind, data=data)


def _talk(views, monkeypatch, hub, messages, admin=True, query=None, clients=None, wants=None):
    """Opens /audio_ws, sends `messages`, closes; returns the socket and the JSON it got."""
    ws = fake_ws(monkeypatch, views, views.new_ws())
    for m in messages:
        ws.inbox.put_nowait(m if isinstance(m, types.SimpleNamespace) else _msg("text", json.dumps(m)))
    ws.inbox.put_nowait(None)
    hass = _hass(hub, clients, wants)
    asyncio.run(views.VimarAudioWSView(hass).get(Request(admin=admin, query=query)))
    return ws, [json.loads(s) for s in ws.sent if isinstance(s, str)]


# ─── /audio_ws: connection ─────────────────────────────────────────────────

def test_audio_ws_without_the_integration_closes_with_1011(views, monkeypatch):
    ws, got = _talk(views, monkeypatch, None, [])
    assert ws.closed and ws.close_args["code"] == 1011 and got == []


def test_audio_ws_pings_so_a_vanished_phone_is_closed(views, monkeypatch):
    """Without a heartbeat a suspended iPhone or a dropped 5G link kept its socket,
    and its sender queue and task, until TCP gave up minutes later."""
    made = []
    ws = views.new_ws()
    monkeypatch.setattr(views.web, "WebSocketResponse", lambda **kw: made.append(kw) or ws)
    ws.inbox.put_nowait(None)
    asyncio.run(views.VimarAudioWSView(_hass(ViewHub())).get(Request()))
    assert made == [{"heartbeat": 30}]


def test_audio_ws_registers_for_video_timestamps_only_when_asked(views, monkeypatch):
    seen = []
    ts: set = set()

    class _Hub(ViewHub):
        def state_message(self):
            seen.append(set(ts))
            return {"type": "state"}

    for query in ({"only": "video", "ts": "1"}, {"only": "video"}):
        hass = _hass(_Hub())
        hass.data["vimar_intercom"]["e1"]["ws_ts"] = ts
        ws = fake_ws(monkeypatch, views, views.new_ws())
        ws.inbox.put_nowait(None)
        asyncio.run(views.VimarAudioWSView(hass).get(Request(query=query)))
    assert len(seen[0]) == 1 and seen[1] == set() and ts == set()


def test_audio_ws_sends_the_state_and_replays_the_current_video(views, monkeypatch):
    replayed = []
    monkeypatch.setattr(media, "video_proto",
                        types.SimpleNamespace(replay_gop_ws=lambda send: replayed.append(send)))
    monkeypatch.setattr(media, "ws_send_bytes", lambda data, only=None: None)
    clients: set = set()
    ws, got = _talk(views, monkeypatch, ViewHub(), [], clients=clients)
    assert got == [{"type": "state", "registered": True, "in_call": False, "ringing": False}]
    # Through the client's own queue, not straight to its socket.
    assert replayed[0].func is media.ws_send_bytes and replayed[0].keywords == {"only": ws}
    assert clients == set()  # removed when it left


def test_audio_ws_only_picks_the_media_and_the_video_replay(views, monkeypatch):
    """?only=video: video only, GOP replay included; ?only=audio: no replay either.
    Without it (the iOS app): everything, as before. Forgotten when the socket leaves."""
    wants: dict = {}
    seen = []  # a copy of `wants` at each replay: the socket's entry is gone once it leaves
    monkeypatch.setattr(media, "video_proto",
                        types.SimpleNamespace(replay_gop_ws=lambda send: seen.append(dict(wants))))
    monkeypatch.setattr(media, "ws_send_bytes", lambda data, only=None: None)
    ws, _ = _talk(views, monkeypatch, ViewHub(), [], query={"only": "video"}, wants=wants)
    assert seen == [{ws: b"\x03"}] and wants == {}
    _talk(views, monkeypatch, ViewHub(), [], query={"only": "audio"}, wants=wants)
    assert len(seen) == 1 and wants == {}
    _talk(views, monkeypatch, ViewHub(), [], wants=wants)
    assert seen[1:] == [{}]


def test_audio_ws_tells_a_new_client_that_the_panel_is_ringing(views, monkeypatch):
    # A client joining mid-ring (an Echo through Scrypted) watches instead of
    # placing a call over the ring.
    hub = ViewHub()
    hub.is_ringing = True
    _, got = _talk(views, monkeypatch, hub, [])
    assert got[0]["type"] == "state" and got[0]["ringing"] is True


def test_audio_ws_mic_audio_goes_to_the_panel_only_in_call(views, monkeypatch):
    sent = []
    monkeypatch.setattr(media, "send_audio", sent.append)
    hub = ViewHub()
    hub.in_call = True
    _talk(views, monkeypatch, hub, [
        _msg("binary", b"\x02"),               # no payload
        _msg("binary", b"\x01abcd"),           # not mic audio
        _msg("binary", b"\x02pcm1"),
    ])
    assert sent == [b"pcm1"] and hub.claims == 1


def test_audio_ws_mic_audio_at_rest_is_dropped(views, monkeypatch):
    sent = []
    monkeypatch.setattr(media, "send_audio", sent.append)
    _talk(views, monkeypatch, ViewHub(), [_msg("binary", b"\x02pcm1")])
    assert sent == []


def test_audio_ws_stops_on_an_error_message(views, monkeypatch):
    hub = ViewHub()
    _, got = _talk(views, monkeypatch, hub, [_msg("error", None), {"action": "status"}])
    assert len(got) == 1  # the status after the error was never read


def test_audio_ws_error_in_the_loop_is_contained(views, monkeypatch):
    hub = ViewHub()
    hub.in_call = True

    def broken(pcm):
        raise RuntimeError("media down")
    monkeypatch.setattr(media, "send_audio", broken)
    clients: set = set()
    ws, _ = _talk(views, monkeypatch, hub, [_msg("binary", b"\x02pcm")], clients=clients)
    assert clients == set() and ws.sent  # returned normally, client dropped


# ─── /audio_ws: actions ────────────────────────────────────────────────────

def _action(views, monkeypatch, hub, *messages, admin=True):
    _, got = _talk(views, monkeypatch, hub, list(messages), admin=admin)
    return got[1:]  # after the initial state


def test_invalid_json_is_ignored(views, monkeypatch):
    assert _action(views, monkeypatch, ViewHub(), _msg("text", "{not json")) == []


@pytest.mark.parametrize("action", sorted(["command", "probe", "scan", "register", "reconnect"]))
def test_admin_actions_are_refused_to_other_users(views, monkeypatch, action):
    hub = ViewHub()
    assert _action(views, monkeypatch, hub, {"action": action}, admin=False) == [
        {"type": "error", "msg": "Admin only"}]
    assert hub.calls == []


def test_an_action_after_the_integration_went_away_says_so(views):
    ws = views.web.WebSocketResponse()
    view = views.VimarAudioWSView(_hass(None))
    asyncio.run(view._handle_text(ws, json.dumps({"action": "status"}), True))
    assert json.loads(ws.sent[0]) == {"type": "error", "msg": "Integration not loaded"}


def test_status_reports_registration_and_call(views, monkeypatch):
    hub = ViewHub()
    hub.in_call = True
    assert _action(views, monkeypatch, hub, {"action": "status"}) == [
        {"type": "state", "registered": True, "in_call": True, "ringing": False}]


def test_call_success_is_broadcast(views, monkeypatch):
    hub = ViewHub()
    got = _action(views, monkeypatch, hub, {"action": "call", "target": "55002"})
    assert got == [{"type": "call_started", "msg": "call ok", "target": "55002",
                    "registered": True, "in_call": True}]
    assert hub.calls == [("call", "55002")]


def test_call_while_already_in_call_tells_the_client_at_once(views, monkeypatch):
    hub = ViewHub()
    hub.results["call"] = (False, "busy")
    monkeypatch.setattr(sip, "in_call", True)
    got = _action(views, monkeypatch, hub, {"action": "call"})
    assert got[0]["type"] == "call_started" and got[0]["msg"] == "Already in call"


def test_call_while_calling_waits_for_the_connect(views, monkeypatch):
    hub = ViewHub()
    hub.results["call"] = (False, "busy")
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", True)
    assert _action(views, monkeypatch, hub, {"action": "call"}) == []


def test_call_failure_and_exception_are_errors(views, monkeypatch):
    hub = ViewHub()
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    hub.results["call"] = (False, "486 Busy")
    assert _action(views, monkeypatch, hub, {"action": "call"}) == [{"type": "error", "msg": "486 Busy"}]
    hub.results["call"] = RuntimeError("socket closed")
    assert _action(views, monkeypatch, hub, {"action": "call"}) == [
        {"type": "error", "msg": "socket closed"}]


def test_hangup_is_broadcast_and_its_failure_reported(views, monkeypatch):
    hub = ViewHub()
    assert _action(views, monkeypatch, hub, {"action": "hangup"}) == [
        {"type": "call_ended", "msg": "Call ended", "registered": True, "in_call": False}]
    hub.results["hangup"] = RuntimeError("no dialog")
    assert _action(views, monkeypatch, hub, {"action": "hangup"}) == [{"type": "error", "msg": "no dialog"}]


def test_a_broadcast_drops_a_dead_client(views, monkeypatch):
    class Dead:
        async def send_str(self, s):
            raise ConnectionResetError
    dead = Dead()
    clients = {dead}
    _talk(views, monkeypatch, ViewHub(), [{"action": "hangup"}], clients=clients)
    assert dead not in clients


def test_switch_needs_a_target(views, monkeypatch):
    hub = ViewHub()
    assert _action(views, monkeypatch, hub, {"action": "switch"}) == [{"type": "error", "msg": "No target"}]
    assert hub.calls == []


def test_switch_hangs_up_and_calls_the_new_panel(views, monkeypatch):
    hub = ViewHub()
    got = _action(views, monkeypatch, hub, {"action": "switch", "target": "55003"})
    assert hub.calls == [("hangup",), ("call", "55003")]
    assert got[0]["type"] == "call_started" and got[0]["target"] == "55003"


def test_a_failed_switch_ends_the_call_and_an_exception_is_reported(views, monkeypatch):
    hub = ViewHub()
    hub.results["call"] = (False, "404")
    got = _action(views, monkeypatch, hub, {"action": "switch", "target": "55003"})
    assert got == [{"type": "call_ended", "msg": "Switch failed: 404", "registered": True, "in_call": False}]
    hub.results["hangup"] = RuntimeError("gone")
    assert _action(views, monkeypatch, hub, {"action": "switch", "target": "55003"}) == [
        {"type": "error", "msg": "gone"}]


def test_door_result_is_broadcast(views, monkeypatch):
    hub = ViewHub()
    hub.results["door"] = (True, "opened", 200)
    assert _action(views, monkeypatch, hub, {"action": "door", "target": "55001"}) == [
        {"type": "door", "msg": "opened", "code": 200}]
    hub.results["door"] = (False, "error", 403)
    assert _action(views, monkeypatch, hub, {"action": "door"}) == [{"type": "error", "msg": "error", "code": 403}]
    hub.results["door"] = RuntimeError("down")
    assert _action(views, monkeypatch, hub, {"action": "door"}) == [{"type": "error", "msg": "down"}]


def test_command_sends_a_message_with_default_headers(views, monkeypatch):
    hub = ViewHub()
    got = _action(views, monkeypatch, hub, {"action": "command", "body": "PING", "target": "55001"})
    assert got == [{"type": "command_result", "ok": True, "msg": "command ok", "body": "PING"}]
    assert hub.calls == [("command", {"body": "PING", "target": "55001",
                                      "header_name": "Panda", "header_value": "command"})]
    hub.results["command"] = RuntimeError("x")
    assert _action(views, monkeypatch, hub, {"action": "command"}) == [{"type": "error", "msg": "x"}]


def test_register_reports_success_failure_and_errors(views, monkeypatch):
    results = [True, False, RuntimeError("tls")]

    async def do_register():
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(sip, "do_register", do_register)
    hub = ViewHub()
    assert _action(views, monkeypatch, hub, {"action": "register"}) == [
        {"type": "registered", "msg": "SIP registered", "registered": True, "in_call": False}]
    assert _action(views, monkeypatch, hub, {"action": "register"}) == [
        {"type": "error", "msg": "Registration failed"}]
    assert _action(views, monkeypatch, hub, {"action": "register"}) == [{"type": "error", "msg": "tls"}]


def test_probe_and_scan_return_their_results(views, monkeypatch):
    hub = ViewHub()
    hub.results["scan"] = [{"target": "55001", "ok": True}]
    got = _action(views, monkeypatch, hub, {"action": "probe", "target": "55001"},
                  {"action": "scan", "start": 55001, "end": 55002})
    assert got == [{"type": "probe_result", "target": "55001", "ok": True, "msg": "probe ok"},
                   {"type": "scan_result", "results": [{"target": "55001", "ok": True}]}]
    assert ("scan", 55001, 55002) in hub.calls
    hub.results["probe"] = hub.results["scan"] = RuntimeError("timeout")
    assert _action(views, monkeypatch, hub, {"action": "probe"}, {"action": "scan"}) == [
        {"type": "error", "msg": "timeout"}, {"type": "error", "msg": "timeout"}]


def test_answer_stops_other_devices_ringing_first(views, monkeypatch):
    hub = ViewHub()
    got = _action(views, monkeypatch, hub, {"action": "answer"})
    assert [m["type"] for m in got] == ["ring_ended", "call_started"]
    hub.results["answer"] = (False, "No ringing call")
    assert _action(views, monkeypatch, hub, {"action": "answer"}) == [
        {"type": "error", "msg": "No ringing call"}]
    hub.results["answer"] = RuntimeError("x")
    assert _action(views, monkeypatch, hub, {"action": "answer"}) == [{"type": "error", "msg": "x"}]


def test_decline_reports_only_failures(views, monkeypatch):
    hub = ViewHub()
    assert _action(views, monkeypatch, hub, {"action": "decline"}) == []
    hub.results["decline"] = (False, "")
    assert _action(views, monkeypatch, hub, {"action": "decline"}) == [
        {"type": "error", "msg": "No ringing call"}]
    hub.results["decline"] = RuntimeError("x")
    assert _action(views, monkeypatch, hub, {"action": "decline"}) == [{"type": "error", "msg": "x"}]


def test_reconnect_reports_its_outcome(views, monkeypatch):
    results = [True, False, RuntimeError("dns")]

    async def reconnect():
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(sip, "reconnect", reconnect)
    hub = ViewHub()
    assert _action(views, monkeypatch, hub, {"action": "reconnect"}) == [
        {"type": "state", "registered": True, "in_call": False, "ringing": False, "msg": "Reconnected"}]
    assert _action(views, monkeypatch, hub, {"action": "reconnect"})[0]["msg"] == "Reconnect failed"
    assert _action(views, monkeypatch, hub, {"action": "reconnect"}) == [{"type": "error", "msg": "dns"}]


def test_an_unknown_action_gets_no_answer(views, monkeypatch):
    hub = ViewHub()
    assert _action(views, monkeypatch, hub, {"action": "dance"}) == [] and hub.calls == []


# ─── local-only check ─────────────────────────────────────────────────────

@pytest.mark.parametrize("remote, headers, local", [
    ("127.0.0.1", {}, True),
    ("192.0.2.20", {}, True),
    ("fe80::1", {}, True),
    ("233.252.0.1", {}, False),                        # neither private nor loopback
    ("127.0.0.1", {"x-forwarded-for": "192.0.2.9"}, False),  # behind a proxy
    ("127.0.0.1", {"forwarded": "for=192.0.2.9"}, False),
    (None, {}, False),
    ("not-an-ip", {}, False),
])
def test_only_direct_local_requests_are_local(views, remote, headers, local):
    req = Request()
    req.remote, req.headers = remote, headers
    assert views._is_local_request(req) is local


# ─── /rings and /rings/{name} ───────────────────────────────────────────────

def test_rings_without_a_snapshot_folder_is_an_empty_list(views, monkeypatch):
    monkeypatch.setattr(runtime, "SNAPSHOT_DIR", "")
    r = asyncio.run(views.VimarRingsView(_hass(ViewHub())).get(Request()))
    assert r.data == []


def _executor_hass():
    async def executor(f, *a):
        return f(*a)
    hass = _hass(ViewHub())
    hass.async_add_executor_job = executor
    return hass


@pytest.mark.parametrize("limit, expected", [(None, 10), ("3", 3), ("0", 1), ("999", 50), ("x", 10)])
def test_rings_limit_is_clamped(views, monkeypatch, tmp_path, limit, expected):
    rings = [{"at": str(i)} for i in range(60)]
    (tmp_path / ring_log.RING_LOG).write_text(json.dumps(rings))
    monkeypatch.setattr(runtime, "SNAPSHOT_DIR", str(tmp_path))
    query = {"limit": limit} if limit is not None else {}
    r = asyncio.run(views.VimarRingsView(_executor_hass()).get(Request(query=query)))
    assert len(r.data) == expected and r.data[0] == {"at": "59"}


def test_ring_photo_serves_only_ring_files(views, monkeypatch, tmp_path):
    (tmp_path / "squillo_20260101_120000.jpg").write_bytes(b"jpg")
    (tmp_path / "notes.txt").write_text("x")
    monkeypatch.setattr(runtime, "SNAPSHOT_DIR", str(tmp_path))
    view = views.VimarRingPhotoView(_executor_hass())
    ok = asyncio.run(view.get(Request(), "squillo_20260101_120000.jpg"))
    assert ok.path == str(tmp_path / "squillo_20260101_120000.jpg")
    assert asyncio.run(view.get(Request(), "notes.txt")).status == 404


def test_ring_photo_is_refused_to_users_not_allowed(views, monkeypatch):
    monkeypatch.setattr(runtime, "ALLOWED_USERS", ["u1"])
    req = Request(admin=False)
    req._user = types.SimpleNamespace(is_admin=False, id="u2")
    with pytest.raises(sys.modules["homeassistant.exceptions"].Unauthorized):
        asyncio.run(views.VimarRingPhotoView(_executor_hass()).get(req, "squillo_20260101_120000.jpg"))


# ─── /av ────────────────────────────────────────────────────────────────────

def _queue(*chunks):
    q: asyncio.Queue = asyncio.Queue()
    for c in (*chunks, None):
        q.put_nowait(c)
    return q


def _av(views, hub, query=None, remote="127.0.0.1"):
    hass = _hass(hub)
    req = Request(query=query)
    req.remote = remote

    async def run():
        return await views.VimarAVStreamView(hass).get(req)
    return asyncio.run(run()), hass


@pytest.fixture
def av_stream(views, monkeypatch):
    state = types.SimpleNamespace(queue=None, unsubscribed=[])

    async def subscribe():
        return state.queue

    async def unsubscribe(q):
        state.unsubscribed.append(q)
    monkeypatch.setattr(av_stream_mod, "av_subscribe", subscribe)
    monkeypatch.setattr(av_stream_mod, "av_unsubscribe", unsubscribe)
    return state


def test_av_is_refused_from_outside_the_lan(views):
    r, _ = _av(views, ViewHub(), remote="233.252.0.1")
    assert r.status == 403


def test_av_without_the_integration_is_503(views):
    r, _ = _av(views, None)
    assert (r.status, r.text) == (503, "Integration not loaded")


def test_passive_av_without_video_is_503_and_never_calls(views):
    hub = ViewHub()
    r, _ = _av(views, hub, {"autocall": "0"})
    assert (r.status, r.text) == (503, "No call (passive)") and hub.opened == 0


def test_passive_av_with_video_streams_without_counting_a_viewer(views, av_stream):
    hub = ViewHub()
    hub.video_active = True
    av_stream.queue = _queue(b"ts1", b"ts2")
    r, hass = _av(views, hub, {"mode": "passive"})
    assert r.chunks == [b"ts1", b"ts2"] and r.content_type == "video/mp2t"
    assert av_stream.unsubscribed == [av_stream.queue]
    assert hub.opened == hub.closed == 0
    assert hass.async_create_background_task == ["vimar_intercom keyframe"]


def test_av_that_starts_no_call_is_503(views):
    hub = ViewHub()
    hub.open_result = False
    r, _ = _av(views, hub)
    assert (r.status, r.text) == (503, "No call") and hub.opened == hub.closed == 1


def test_av_whose_call_ends_without_video_is_503_at_once(views, caplog):
    hub = ViewHub()
    with caplog.at_level("WARNING"):
        r, _ = _av(views, hub)
    assert (r.status, r.text) == (503, "Call not established") and hub.closed == 1
    # It said "after 25s" whatever the wait was (#44): the real time now.
    assert any(re.search(r"call not established \(\d+\.\d s\)", rec.getMessage())
               for rec in caplog.records)


def test_av_waits_for_the_video_of_a_pending_call(views, av_stream):
    class Pending(ViewHub):
        checks = 0

        @property
        def video_active(self):
            self.checks += 1
            return self.checks > 1  # video arrives after the first wait

        @video_active.setter
        def video_active(self, v):
            pass
    hub = Pending()
    hub.call_pending = True
    av_stream.queue = _queue(b"ts")
    r, _ = _av(views, hub)
    assert r.chunks == [b"ts"] and hub.closed == 1


def test_av_reports_an_encoder_that_does_not_start(views, av_stream):
    hub = ViewHub()
    hub.video_active = True
    r, _ = _av(views, hub)
    assert (r.status, r.text) == (503, "ffmpeg failed to start") and hub.closed == 1


def test_a_client_that_resets_still_unsubscribes(views, av_stream, monkeypatch):
    hub = ViewHub()
    hub.video_active = True
    av_stream.queue = _queue(b"ts")

    async def reset(self, chunk):
        raise ConnectionResetError
    monkeypatch.setattr(views.web.StreamResponse, "write", reset)
    _av(views, hub)
    assert av_stream.unsubscribed == [av_stream.queue] and hub.closed == 1


@pytest.fixture
def passive(views, monkeypatch):
    state = types.SimpleNamespace(queue=None, unsubscribed=[], hooks=None)

    async def subscribe(is_live, on_live):
        state.hooks = (is_live, on_live)
        return state.queue

    async def unsubscribe(q):
        state.unsubscribed.append(q)
    monkeypatch.setattr(views.av_passive, "subscribe", subscribe)
    monkeypatch.setattr(views.av_passive, "unsubscribe", unsubscribe)
    return state


def test_idle_image_streams_the_continuous_feed(views, passive):
    hub = ViewHub()
    passive.queue = _queue(b"standby")
    r, hass = _av(views, hub, {"autocall": "0", "idle": "image"})
    assert r.chunks == [b"standby"] and passive.unsubscribed == [passive.queue]
    is_live, on_live = passive.hooks
    assert is_live() is False
    hub.video_active = True
    assert is_live() is True
    on_live()
    assert hass.async_create_background_task == ["vimar_intercom keyframe"]
    assert hub.opened == 0


def test_idle_image_reports_an_encoder_that_does_not_start(views, passive):
    r, _ = _av(views, ViewHub(), {"autocall": "0", "idle": "image"})
    assert (r.status, r.text) == (503, "ffmpeg failed to start")


def test_audio_ws_skips_other_message_types(views, monkeypatch):
    got = _action(views, monkeypatch, ViewHub(), _msg("ping", b""), {"action": "status"})
    assert [m["type"] for m in got] == ["state"]


# ─── /debug ────────────────────────────────────────────────────────────────

def test_debug_log_is_for_admins_only(views):
    req = Request(admin=False)
    with pytest.raises(sys.modules["homeassistant.exceptions"].Unauthorized):
        asyncio.run(views.VimarDebugView().get(req))
    req._user = None
    with pytest.raises(sys.modules["homeassistant.exceptions"].Unauthorized):
        asyncio.run(views.VimarDebugView().get(req))


def test_debug_log_with_a_bad_line_count_serves_the_default(views, monkeypatch):
    asked = []
    monkeypatch.setattr(views._log_buffer, "tail", lambda n: asked.append(n) or ["a", "b"])
    r = asyncio.run(views.VimarDebugView().get(Request(query={"lines": "many"})))
    assert asked == [100] and r.text == "a\nb" and r.content_type == "text/plain"


# ─── /av key (#63) ───────────────────────────────────────────────────────────

AV_KEY = "placeholder-av-key-0123456789abc"


def _anon(query=None):
    req = Request(query=query)
    req._user = None  # the stream worker, go2rtc: no HA user
    return req


def _av_req(views, hub, req):
    hass = _hass(hub)

    async def run():
        return await views.VimarAVStreamView(hass).get(req)
    return asyncio.run(run())


@pytest.fixture
def av_key(monkeypatch):
    monkeypatch.setattr(runtime, "AV_KEY", AV_KEY)
    return AV_KEY


def test_plain_av_without_key_or_user_is_refused_and_never_calls(views, av_key):
    hub = ViewHub()
    r = _av_req(views, hub, _anon())
    assert (r.status, r.text) == (403, "Forbidden (key required)") and hub.opened == 0


def test_plain_av_with_the_key_places_the_call(views, av_key):
    hub = ViewHub()
    hub.open_result = False
    r = _av_req(views, hub, _anon({runtime.AV_KEY_PARAM: av_key}))
    assert (r.status, r.text) == (503, "No call") and hub.opened == 1


def test_plain_av_for_an_authenticated_user_needs_no_key(views, av_key):
    hub = ViewHub()
    hub.open_result = False
    r = _av_req(views, hub, Request())
    assert r.status == 503 and hub.opened == 1


def test_the_user_check_reads_the_key_ha_sets(views, av_key):
    """HA's auth middleware stores the result under "ha_authenticated"
    (homeassistant.helpers.http.KEY_AUTHENTICATED). Reading "hass_authenticated",
    as 1.0.18's #63 code did, never matches: an HA user without the key got 403."""
    from custom_components.vimar_intercom import views as views_mod
    assert views_mod.KEY_AUTHENTICATED == "ha_authenticated"

    class OnlyHaKey(Request):
        def get(self, k, d=None):
            return {"ha_authenticated": True}.get(k, d)

    hub = ViewHub()
    hub.open_result = False
    r = _av_req(views, hub, OnlyHaKey())
    assert r.status == 503 and hub.opened == 1


@pytest.mark.parametrize("query", [{}, {"autocall": "0"}, {"mode": "passive"}])
def test_a_wrong_key_is_refused_in_every_mode_even_for_a_user(views, av_key, query, caplog):
    hub = ViewHub()
    wrong = "x" + av_key[1:]
    with caplog.at_level("DEBUG"):
        r = _av_req(views, hub, Request(query={**query, runtime.AV_KEY_PARAM: wrong}))
    assert (r.status, r.text) == (403, "Forbidden (key)") and hub.opened == 0
    assert all(k not in caplog.text for k in (av_key, wrong))


def test_passive_av_keeps_working_without_the_key(views, av_key):
    hub = ViewHub()
    r = _av_req(views, hub, _anon({"autocall": "0"}))
    assert (r.status, r.text) == (503, "No call (passive)") and hub.opened == 0


def test_an_empty_key_is_a_wrong_key(views, av_key):
    r = _av_req(views, ViewHub(), _anon({runtime.AV_KEY_PARAM: ""}))
    assert r.status == 403


def test_av_refusals_are_403_never_401(views, av_key):
    """HA logs "invalid authentication" with the full URL (key included) only when a
    view raises HTTPUnauthorized (ban_middleware -> process_wrong_login). The key
    checks answer 403 and raise nothing."""
    for req in (_anon(), _anon({runtime.AV_KEY_PARAM: "nope"}),
                Request(query={"autocall": "0", runtime.AV_KEY_PARAM: "nope"})):
        assert _av_req(views, ViewHub(), req).status == 403
