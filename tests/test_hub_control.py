"""The hub's control side on a fake SIP stack: start and stop, startup
registration, model probe, door retry, commands, scans, the automatic
hang-up's edges and the away message's failure paths.

Every SIP call is a fake: nothing leaves the process.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")
sip = hub_mod.sip
media = hub_mod.media
R = hub_mod.R
S = hub_mod.S


@pytest.fixture
def no_sleep(monkeypatch):
    """asyncio.sleep returns at once: the hub's pauses (startup, probe, scan)
    are not what these tests are about."""
    async def _sleep(delay=0, result=None):
        return result

    monkeypatch.setattr(hub_mod.asyncio, "sleep", _sleep)


# ─── start and stop ──────────────────────────────────────────────────────────

def _fake_start(monkeypatch, h):
    calls = []
    monkeypatch.setattr(sip, "init", lambda fn: calls.append("sip.init"))
    monkeypatch.setattr(sip, "set_state_callback", lambda cb: calls.append("state cb"))
    monkeypatch.setattr(sip, "set_model_callback", lambda cb: calls.append("model cb"))
    monkeypatch.setattr(media, "init", lambda fn: calls.append("media.init"))
    monkeypatch.setattr(sip, "get_local_ip", lambda: "192.0.2.5")
    monkeypatch.setattr(sip, "MY_IP", None)
    monkeypatch.setattr(sip, "incoming_requests", None)

    async def setup_transports():
        calls.append("transports")

    async def restore(store):
        calls.append(("restore", store))

    async def connect():
        calls.append("connect")

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(media, "setup_transports", setup_transports)
    monkeypatch.setattr(media, "restore_sps_pps", restore)
    monkeypatch.setattr(sip, "connect", connect)
    monkeypatch.setattr(sip, "reader_task", idle)
    monkeypatch.setattr(sip, "request_processor", idle)
    monkeypatch.setattr(h, "_auto_startup", idle)
    monkeypatch.setattr(h, "_keepalive_loop", idle)
    return calls


def _fake_stop(monkeypatch):
    async def stop_media():
        pass

    monkeypatch.setattr(media, "stop_media", stop_media)
    monkeypatch.setattr(media, "close_transports", lambda: None)


def test_start_wires_sip_and_media_once(hub, monkeypatch):
    calls = _fake_start(monkeypatch, hub)
    _fake_stop(monkeypatch)

    async def run():
        await hub.async_start()
        await hub.async_start()  # already running: nothing again
        tasks = list(hub._tasks)
        running = hub._running
        await hub.async_stop()
        return tasks, running

    tasks, running = asyncio.run(run())
    assert running and len(tasks) == 4
    assert all(t.cancelled() for t in tasks)
    assert calls == ["sip.init", "state cb", "model cb", "media.init", "transports", "connect"]
    assert sip.MY_IP == "192.0.2.5"


def test_start_restores_the_saved_sps_pps(hub, monkeypatch):
    calls = _fake_start(monkeypatch, hub)
    _fake_stop(monkeypatch)
    store = object()

    async def run():
        await hub.async_start(store)
        await hub.async_stop()

    asyncio.run(run())
    assert ("restore", store) in calls


def test_stop_survives_a_failed_hang_up_and_broken_sockets(hub, monkeypatch):
    _fake_stop(monkeypatch)
    monkeypatch.setattr(sip, "in_call", True)

    async def failing_hangup(on_local_end=None):
        raise OSError("connection gone")

    class Broken:
        def close(self):
            raise OSError("already closed")

    monkeypatch.setattr(sip, "do_hangup", failing_hangup)
    monkeypatch.setattr(sip, "writer", Broken())
    monkeypatch.setattr(sip, "_udp_sock", Broken())
    asyncio.run(hub.async_stop())
    assert hub._running is False
    assert sip.writer is None and sip._udp_sock is None  # reset_state ran


# ─── startup registration and model probe ────────────────────────────────────

def _startup(monkeypatch, h, register, local_udp=False, profiles=None):
    sent = []

    async def do_register():
        if isinstance(register, Exception):
            raise register
        return register

    async def do_system_message(uri, body, extra_headers=None, timeout=15):
        sent.append(body)
        return True, "200 OK"

    async def connect_profiles():
        if isinstance(profiles, Exception):
            raise profiles
        sent.append("connectProfiles")
        return True, "200 OK"

    async def probe():
        sent.append("probe")

    monkeypatch.setattr(sip, "do_register", do_register)
    monkeypatch.setattr(sip, "do_system_message", do_system_message)
    monkeypatch.setattr(sip, "do_connect_profiles", connect_profiles)
    monkeypatch.setattr(h, "_probe_model", probe)
    monkeypatch.setattr(R, "USE_LOCAL_UDP", local_udp)
    monkeypatch.setattr(R, "PICG_TARGET", "55002")

    async def run():
        await h._auto_startup()
        await asyncio.gather(*h._tasks)

    asyncio.run(run())
    return sent


def test_startup_on_the_cloud_asks_the_status_and_connects_profiles(hub, monkeypatch, no_sleep):
    sent = _startup(monkeypatch, hub, True)
    assert sent == [hub_mod.C.GET_INIT_STATUS, "connectProfiles", "probe"]
    assert hub.stats["last_register_time"] is not None and hub._init_status_sent


def test_startup_on_local_udp_skips_connect_profiles(hub, monkeypatch, no_sleep):
    sent = _startup(monkeypatch, hub, True, local_udp=True)
    assert "connectProfiles" not in sent and "probe" in sent


def test_a_failed_startup_registration_is_counted(hub, monkeypatch, no_sleep):
    sent = _startup(monkeypatch, hub, False)
    assert sent == [] and hub.stats["register_failures"] == 1


def test_a_connect_profiles_error_does_not_stop_the_startup(hub, monkeypatch, no_sleep, caplog):
    sent = _startup(monkeypatch, hub, True, profiles=OSError("tls"))
    assert "connectProfiles error" in caplog.text and "probe" in sent


def test_a_startup_exception_is_logged(hub, monkeypatch, no_sleep, caplog):
    _startup(monkeypatch, hub, OSError("socket closed"))
    assert "Auto startup error" in caplog.text


def test_the_model_probe_stops_at_the_first_panel_that_identifies(hub, monkeypatch, no_sleep):
    monkeypatch.setattr(S, "DETECTED_MODEL", "")
    monkeypatch.setattr(R, "SIP_DOMAIN", "plant.example")
    asked = []

    async def do_options(target=None):
        asked.append(target)
        if len(asked) == 1:
            raise OSError("no route")
        S.DETECTED_MODEL = "Elvox Tab 7S"
        return True, "200 OK"

    monkeypatch.setattr(sip, "do_options", do_options)
    asyncio.run(hub._probe_model())
    assert asked == ["sip:55001@plant.example", "sip:55002@plant.example"]


def test_the_model_probe_asks_every_panel_when_none_identifies(hub, monkeypatch, no_sleep, caplog):
    caplog.set_level(logging.INFO, logger=hub_mod.__name__)
    monkeypatch.setattr(S, "DETECTED_MODEL", "")
    monkeypatch.setattr(sip, "_seen_uas", set())
    asked = []

    async def do_options(target=None):
        asked.append(target)
        return True, "200 OK"

    monkeypatch.setattr(sip, "do_options", do_options)
    asyncio.run(hub._probe_model())
    assert len(asked) == len(hub_mod.MODEL_PROBE_TARGETS)
    assert "Modello non rilevato" in caplog.text


def test_register_now_registers_at_once(hub, monkeypatch):
    async def do_register():
        return True

    monkeypatch.setattr(sip, "do_register", do_register)
    assert asyncio.run(hub.async_register_now()) is True


# ─── init status ─────────────────────────────────────────────────────────────

def test_an_invalid_picg_target_is_not_sent(hub, monkeypatch):
    sent = []

    async def do_system_message(uri, body, extra_headers=None, timeout=15):
        sent.append(uri)
        return True, "200 OK"

    monkeypatch.setattr(sip, "do_system_message", do_system_message)
    monkeypatch.setattr(R, "PICG_TARGET", "55002\r\nX: y")
    asyncio.run(hub.async_request_status())
    assert sent == [] and hub._init_status_sent is False


def test_a_failed_init_status_send_is_logged_and_retried_later(hub, monkeypatch, caplog):
    async def do_system_message(uri, body, extra_headers=None, timeout=15):
        raise OSError("socket closed")

    monkeypatch.setattr(sip, "do_system_message", do_system_message)
    monkeypatch.setattr(R, "PICG_TARGET", "55002")
    asyncio.run(hub._request_init_status())
    assert "GET_INIT_STATUS invio fallito" in caplog.text
    assert hub._init_status_sent is False


# ─── door, commands, probes ──────────────────────────────────────────────────

def test_a_door_retry_that_fails_again_reports_the_second_failure(hub, monkeypatch):
    answers = iter([(False, "500 Server Error", 500), (False, "503 Unavailable", 503)])
    sent = []

    async def send_message(uri, body, extra_headers=None, timeout=15):
        sent.append(uri)
        return next(answers)

    async def do_register():
        return True

    monkeypatch.setattr(sip, "send_message", send_message)
    monkeypatch.setattr(sip, "do_register", do_register)
    monkeypatch.setattr(R, "SIP_DOMAIN", "plant.example")
    assert asyncio.run(hub.async_door(target="55001")) == (False, hub_mod.DOOR_ERROR, 503)
    assert sent == ["sip:55001@plant.example"] * 2
    assert hub.stats["door_count"] == 0
    assert (hub.stats["last_door_result"], hub.stats["last_door_code"]) == (hub_mod.DOOR_ERROR, 503)


@pytest.mark.parametrize("first", [("Non registrato", 0), ("Errore: 503", 503)])
def test_a_door_command_that_never_got_through_is_retried_over_the_cloud(hub, monkeypatch, first):
    answers = iter([(False, *first), (True, "OK (200)", 200)])
    sent = []

    async def send_message(uri, body, extra_headers=None, timeout=15):
        sent.append(timeout)
        return next(answers)

    async def do_register():
        return True

    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(sip, "send_message", send_message)
    monkeypatch.setattr(sip, "do_register", do_register)
    assert asyncio.run(hub.async_door(target="55001")) == (True, hub_mod.DOOR_OPENED, 200)
    assert sent == [hub_mod.DOOR_TLS_TIMEOUT] * 2
    assert hub.stats["door_count"] == 1


@pytest.mark.parametrize(
    "first, result, sent_count",
    [
        ((False, "Errore: 408", 408), (True, hub_mod.DOOR_OPENED, 200), 2),  # no relay on UDP: the door said no
        ((False, "Errore: 504", 504), (True, hub_mod.DOOR_OPENED, 200), 2),
        ((True, "OK (202)", 202), (False, hub_mod.QUEUED, 202), 1),
    ],
)
def test_on_local_udp_a_408_or_504_is_retried_and_a_202_is_not_an_open(hub, monkeypatch, first, result, sent_count):
    answers = iter([first, (True, "OK (200)", 200)])
    sent = []

    async def send_message(uri, body, extra_headers=None, timeout=15):
        sent.append(uri)
        return next(answers)

    async def do_register():
        return True

    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(sip, "send_message", send_message)
    monkeypatch.setattr(sip, "do_register", do_register)
    assert asyncio.run(hub.async_door(target="55001")) == result
    assert len(sent) == sent_count


def test_a_second_door_command_while_one_is_in_flight_is_refused(hub, monkeypatch):
    """A tap while the first command waits for the relay must not send a second copy."""
    sent = []

    async def send_message(uri, body, extra_headers=None, timeout=15):
        sent.append(uri)
        await asyncio.sleep(0.05)
        return True, "OK (200)", 200

    monkeypatch.setattr(sip, "send_message", send_message)

    async def both():
        return await asyncio.gather(hub.async_door(target="55001"), hub.async_door(target="55001"))

    first, second = asyncio.run(both())
    assert first[0] and second == (False, hub_mod.DOOR_BUSY, None)
    assert len(sent) == 1 and hub.stats["door_count"] == 1
    assert asyncio.run(hub.async_door(target="55001"))[0], "the guard is released afterwards"


def test_a_cancelled_door_caller_keeps_the_guard_until_the_message_ends(hub, monkeypatch):
    """An automation in restart mode cancels the first run mid-wait: the rerun must not send a copy."""
    sent = []

    async def send_message(uri, body, extra_headers=None, timeout=15):
        sent.append(uri)
        await asyncio.sleep(0.05)
        return True, "OK (200)", 200

    monkeypatch.setattr(sip, "send_message", send_message)

    async def restart():
        first = asyncio.ensure_future(hub.async_door(target="55001"))
        await asyncio.sleep(0.01)
        first.cancel()
        second = await hub.async_door(target="55001")
        await asyncio.sleep(0.1)
        return second

    assert asyncio.run(restart()) == (False, hub_mod.DOOR_BUSY, None)
    assert len(sent) == 1 and hub.stats["door_count"] == 1
    assert asyncio.run(hub.async_door(target="55001"))[0], "the guard is released afterwards"


def test_a_command_on_local_udp_still_waits_15_s(hub, monkeypatch):
    sent = []

    async def send_message(uri, body, extra_headers=None, timeout=None):
        sent.append(timeout)
        return True, "OK (200)", 200

    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(sip, "send_message", send_message)
    assert asyncio.run(hub.async_send_command("PING", target="55001")) == (True, "OK (200)")
    assert sent == [15]


def test_a_command_that_raises_is_reported_as_a_failure(hub, monkeypatch):
    async def send_message(uri, body, extra_headers=None, timeout=15):
        raise OSError("socket closed")

    monkeypatch.setattr(sip, "send_message", send_message)
    ok, msg = asyncio.run(hub.async_send_command("PING", target="55001"))
    assert (ok, msg) == (False, "socket closed")
    assert hub.stats["last_command_result"] == "socket closed"


def test_probe_and_scan_send_options_to_each_address(hub, monkeypatch, no_sleep):
    monkeypatch.setattr(R, "SIP_DOMAIN", "plant.example")

    async def do_options(target=None):
        if target.startswith("sip:55002"):
            raise OSError("no route")
        return True, f"200 {target}"

    monkeypatch.setattr(sip, "do_options", do_options)
    assert asyncio.run(hub.async_probe("55001")) == (True, "200 sip:55001@plant.example")
    assert asyncio.run(hub.async_scan(55001, 55002)) == [
        {"addr": 55001, "ok": True, "msg": "200 sip:55001@plant.example"},
        {"addr": 55002, "ok": False, "msg": "no route"},
    ]


def test_find_picg_records_a_probe_that_raises(hub, monkeypatch):
    monkeypatch.setattr(R, "SIP_DOMAIN", "plant.example")

    async def do_system_message(uri, body, extra_headers=None, timeout=15):
        raise OSError("socket closed")

    monkeypatch.setattr(sip, "do_system_message", do_system_message)
    result = asyncio.run(hub.async_find_picg(["55001"], reply_wait=0.01, delay=0))
    assert result["ok"] and result["picg"] is None
    assert result["probes"] == [{"target": "55001", "outcome": "error", "sip": "socket closed"}]


def test_find_picg_keeps_going_after_a_late_reply_without_picg(hub, monkeypatch):
    """A GET_NICKS_REPLY outside the wait window that declares no PICG does
    not stop the search: the next address is still asked."""
    monkeypatch.setattr(R, "SIP_DOMAIN", "plant.example")
    asked = []

    async def do_system_message(uri, body, extra_headers=None, timeout=15):
        asked.append(uri)
        if len(asked) == 1:
            # The reply lands after the probe's answer but carries no PICG.
            hub._nicks_seq += 1
            hub.stats["nicknames"] = [{"role": "APT", "ext": "60001", "name": "Casa"}]
        return False, "408 Timeout"

    monkeypatch.setattr(sip, "do_system_message", do_system_message)
    result = asyncio.run(hub.async_find_picg(["55001", "55002"], reply_wait=0.01, delay=0))
    assert len(asked) == 2 and result["picg"] is None
    assert result["probes"][0].get("late_reply") is True


# ─── the automatic hang-up's edges ───────────────────────────────────────────

def test_a_state_change_without_a_loop_drops_the_hang_up_guard_at_once(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)  # the local-end settle is the cloud's
    hub._hanging_up = True
    hub._on_sip_state_change()  # sip idle, no running loop
    assert hub._hanging_up is False


def test_a_view_that_waits_too_long_for_a_hang_up_does_not_call_when_offline(hub, monkeypatch):
    monkeypatch.setattr(hub_mod, "HANGUP_SETTLE", 0.01)
    monkeypatch.setattr(sip, "registered", False)
    monkeypatch.setattr(sip, "ringing", lambda cid=None: False)

    async def run():
        hub._begin_hanging_up()  # never ends: the wait times out
        return await hub.stream_opened()

    assert asyncio.run(run()) is False
    assert hub._auto_called is False


def test_a_fallback_panel_that_fails_too_is_not_learned(hub, monkeypatch):
    monkeypatch.setattr(R, "CAMERA_TARGET_CONFIGURED", False)
    monkeypatch.setattr(R, "CAMERA_TARGET", "55001")
    monkeypatch.setattr(R, "SIP_DOMAIN", "plant.example")
    hub._last_ring_panel = "55009"
    answers = iter([(False, "404 Not Found"), (False, "486 Busy Here")])

    async def do_call(target=None, silence_limit=None, **_kw):
        return next(answers)

    monkeypatch.setattr(sip, "do_call", do_call)
    hub._auto_called = True
    asyncio.run(hub._do_auto_call())
    assert R.CAMERA_TARGET == "55001" and hub._auto_called is False


def test_a_learned_panel_without_a_persist_callback_is_kept_in_memory(hub, monkeypatch):
    monkeypatch.setattr(R, "CAMERA_TARGET_CONFIGURED", False)
    monkeypatch.setattr(R, "CAMERA_TARGET", "55001")
    monkeypatch.setattr(R, "INTERCOM", "sip:55001@plant.example")
    monkeypatch.setattr(R, "SIP_DOMAIN", "plant.example")
    hub._last_ring_panel = "55009"
    answers = iter([(False, "404 Not Found"), (True, "200 OK")])

    async def do_call(target=None, silence_limit=None, **_kw):
        return next(answers)

    monkeypatch.setattr(sip, "do_call", do_call)
    asyncio.run(hub._do_auto_call())
    assert (R.CAMERA_TARGET, R.INTERCOM) == ("55009", "sip:55009@plant.example")


def test_the_delayed_hang_up_leaves_a_call_someone_is_watching(hub, monkeypatch):
    monkeypatch.setattr(hub_mod, "STREAM_HANGUP_DELAY", 0)
    monkeypatch.setattr(sip, "in_call", True)
    hub._auto_called = True
    hub._stream_viewers = 1
    hung_up = []

    async def do_hangup(on_local_end=None):
        hung_up.append(1)

    monkeypatch.setattr(sip, "do_hangup", do_hangup)
    asyncio.run(hub._delayed_hangup())
    assert hung_up == [] and hub._auto_called is True


def test_ending_the_guard_without_an_event_just_drops_it(hub):
    hub._hanging_up = True
    hub._end_hanging_up()
    assert hub._hanging_up is False


def test_a_hang_up_that_times_out_is_logged_and_drops_its_guard(hub, caplog):
    async def run():
        done = hub._begin_hanging_up()
        fut = asyncio.get_running_loop().create_future()
        fut.set_exception(TimeoutError())
        hub._hangup_finished(done, fut)
        return done.is_set()

    assert asyncio.run(run()) is True
    assert hub._hanging_up is False
    assert "no end after" in caplog.text


def test_a_bye_without_answer_gives_up_after_the_timeout(hub, monkeypatch, caplog):
    monkeypatch.setattr(hub_mod, "HANGUP_BYE_TIMEOUT", 0.01)

    async def do_hangup(on_local_end=None):
        await asyncio.Event().wait()

    monkeypatch.setattr(sip, "do_hangup", do_hangup)
    asyncio.run(hub._bye(lambda: None))
    assert "Hang-up: no end after" in caplog.text


def test_the_call_cap_hangs_up_a_call_still_up(hub, monkeypatch):
    monkeypatch.setattr(hub_mod, "MAX_CALL_DURATION", 0)
    monkeypatch.setattr(sip, "in_call", True)
    hub._auto_called = True
    hung_up = []

    async def do_hangup(on_local_end=None):
        hung_up.append(1)

    monkeypatch.setattr(sip, "do_hangup", do_hangup)
    asyncio.run(hub._call_timeout())
    assert hung_up == [1] and hub._auto_called is False


# ─── away message ────────────────────────────────────────────────────────────

def test_the_away_message_gives_up_when_the_answer_fails(hub, monkeypatch, no_sleep):
    monkeypatch.setattr(sip, "ringing", lambda cid=None: True)
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "/nonexistent/msg.wav")
    played = []

    async def load_pcm(path):
        return b"\x00\x00" * 80

    async def answer():
        return False, "481 Call Does Not Exist"

    async def send_pcm(pcm, alive):
        played.append(pcm)

    monkeypatch.setattr(media, "load_pcm", load_pcm)
    monkeypatch.setattr(media, "send_pcm", send_pcm)
    monkeypatch.setattr(sip, "do_answer_incoming", answer)
    asyncio.run(hub._away_message("cid-1"))
    assert played == [] and hub._ring_answered is False


def test_a_keyframe_request_is_sent_for_a_lost_packet(hub, monkeypatch):
    asked = []

    async def send_keyframe_request():
        asked.append(1)

    monkeypatch.setattr(sip, "send_keyframe_request", send_keyframe_request)

    async def run():
        hub._request_keyframe()
        await asyncio.gather(*hub._background)

    asyncio.run(run())
    assert asked == [1]


def test_keyframe_requests_are_held_until_done(hub, monkeypatch):
    # Each lost packet starts one: a task kept only in an attribute that the
    # next request overwrites could be garbage-collected before the INFO left.
    release = None

    async def send_keyframe_request():
        await release.wait()

    monkeypatch.setattr(sip, "send_keyframe_request", send_keyframe_request)

    async def run():
        nonlocal release
        release = asyncio.Event()
        hub._request_keyframe()
        hub._request_keyframe()
        held = [t for t in hub._background if not t.done()]
        release.set()
        await asyncio.gather(*held)
        return held

    assert len(asyncio.run(run())) == 2



def test_call_coming_is_a_call_not_an_open_websocket(hub, monkeypatch):
    """HomeKit's early re-encoder asks for this (#48): a call up or being placed,
    or a ring. An open card or app WebSocket counts for call_pending, not here."""
    monkeypatch.setattr(sip, "ringing", lambda cid=None: False)
    hub._has_ws_clients = lambda: True
    hub._auto_called = False
    assert hub.call_pending and not hub.call_coming
    hub._auto_called = True                     # a view's auto-call being placed
    assert hub.call_coming
