"""Hub lifecycle races seen in the field.

* A call_ended from the previous call arriving after a new call started used
  to cancel the new call's timers and clear its auto-call flag.
* A view opening while the automatic hang-up was sending its BYE cancelled the
  BYE half way: the panel stayed busy and answered 486 to the next call.
* An audio WebSocket open anywhere stopped every HomeKit view from calling.
* Unloading mid-call left the panel on and the next hub started "in a call".
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.vimar_intercom import hub as hub_mod

sip = hub_mod.sip


@pytest.fixture(autouse=True)
def isolated_sip(monkeypatch):
    """Module globals of sip_client these tests write to: a fresh copy each."""
    from custom_components.vimar_intercom.inventory import DeviceInventory
    monkeypatch.setattr(sip, "DEVICES", DeviceInventory())
    monkeypatch.setattr(sip, "_seen_requests", {})
    monkeypatch.setattr(sip, "_last_sweep", 0.0)


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    monkeypatch.setattr(sip, "in_call", False, raising=False)
    monkeypatch.setattr(sip, "calling", False, raising=False)
    monkeypatch.setattr(sip, "registered", True, raising=False)
    return h


def test_a_late_call_ended_leaves_the_new_call_alone(hub, monkeypatch):
    monkeypatch.setattr(sip, "in_call", True)
    hub._auto_called = True
    cancelled = []
    monkeypatch.setattr(hub, "_cancel_call_timeout", lambda: cancelled.append("timeout"))
    monkeypatch.setattr(hub, "_cancel_keyframe_loop", lambda: cancelled.append("keyframes"))
    asyncio.run(hub._handle_broadcast("call_ended", ""))
    assert hub._auto_called is True and cancelled == []


def test_a_late_call_ended_is_not_sent_to_the_cards(hub, monkeypatch):
    """The card would close the view of the call that is up."""
    monkeypatch.setattr(sip, "in_call", True)
    sent = []

    async def ws(payload):
        sent.append(payload["type"])

    hub.set_ws_broadcast(ws)
    asyncio.run(hub._handle_broadcast("call_ended", ""))
    assert sent == []
    monkeypatch.setattr(sip, "in_call", False)
    asyncio.run(hub._handle_broadcast("call_ended", ""))
    assert sent == ["call_ended"], "a real end still reaches the cards"


def test_a_late_call_ended_does_not_close_the_new_calls_stats(hub, monkeypatch):
    monkeypatch.setattr(sip, "in_call", True)
    hub.stats["last_call_end"] = None
    hub._update_stats("call_ended", "")
    assert hub.stats["last_call_end"] is None


def test_a_view_opening_during_the_hang_up_does_not_cut_the_bye(hub, monkeypatch):
    monkeypatch.setattr(hub_mod, "STREAM_HANGUP_DELAY", 0)
    monkeypatch.setattr(sip, "in_call", True)
    hub._auto_called = True
    events = []

    async def slow_hangup():
        events.append("bye sent")
        await asyncio.sleep(0.3)
        monkeypatch.setattr(sip, "in_call", False)
        events.append("bye done")

    async def do_call(target=None, **_kw):
        events.append("new call")
        return True, "200"

    monkeypatch.setattr(sip, "do_hangup", slow_hangup)
    monkeypatch.setattr(sip, "do_call", do_call)

    async def main():
        hub._hangup_task = asyncio.create_task(hub._delayed_hangup())
        await asyncio.sleep(0.05)          # the BYE is on its way
        await hub.stream_opened()          # a view opens and cancels the task
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert events == ["bye sent", "bye done", "new call"]
    assert hub._hanging_up is False


def _auto_hangup_scenario(hub, monkeypatch, hangup):
    monkeypatch.setattr(hub_mod, "STREAM_HANGUP_DELAY", 0)
    monkeypatch.setattr(sip, "in_call", True)
    hub._auto_called = True
    calls = []

    async def do_call(target=None, **_kw):
        calls.append(target)
        return True, "200"

    monkeypatch.setattr(sip, "do_hangup", hangup)
    monkeypatch.setattr(sip, "do_call", do_call)
    return calls


def test_a_viewer_leaving_during_the_hang_up_wait_gets_no_call(hub, monkeypatch):
    async def slow_hangup():
        await asyncio.sleep(0.3)
        monkeypatch.setattr(sip, "in_call", False)

    calls = _auto_hangup_scenario(hub, monkeypatch, slow_hangup)

    async def main():
        hub._hangup_task = asyncio.create_task(hub._delayed_hangup())
        await asyncio.sleep(0.05)
        opened = asyncio.create_task(hub.stream_opened())
        await asyncio.sleep(0.05)
        await hub.stream_closed()          # the viewer goes away while waiting
        result = await opened
        await asyncio.sleep(0.05)
        return result

    assert asyncio.run(main()) is False
    assert calls == []
    assert hub._auto_called is False


def test_a_view_after_a_local_hang_up_does_not_wait_for_the_bye_answer(hub, monkeypatch):
    """The cloud never answers our BYE: do_hangup ends the call locally, then
    waits 5 s for nothing. The view must not wait for that answer."""
    monkeypatch.setattr(hub_mod.R, "USE_LOCAL_UDP", False)  # the cloud: see test_view_after_hangup
    monkeypatch.setattr(hub_mod, "HANGUP_LOCAL_SETTLE", 0.1)

    async def hangup_without_answer():
        monkeypatch.setattr(sip, "in_call", False)
        hub._on_sip_state_change()         # what _set_in_call does for real
        await asyncio.sleep(5)             # the BYE answer that never comes

    calls = _auto_hangup_scenario(hub, monkeypatch, hangup_without_answer)

    async def main():
        hub._hangup_task = asyncio.create_task(hub._delayed_hangup())
        await asyncio.sleep(0.02)
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await hub.stream_opened() is True
        waited = loop.time() - started
        await asyncio.sleep(0)
        for task in asyncio.all_tasks() - {asyncio.current_task()}:
            task.cancel()
        return waited

    waited = asyncio.run(main())
    assert waited < 1.0
    assert calls == [None]


def test_a_failing_hang_up_clears_the_flag_and_is_logged(hub, monkeypatch, caplog):
    async def broken_hangup():
        raise OSError("socket gone")

    _auto_hangup_scenario(hub, monkeypatch, broken_hangup)

    async def main():
        await hub._delayed_hangup()
        await asyncio.sleep(0)

    with caplog.at_level("WARNING"):
        asyncio.run(main())
    assert hub._hanging_up is False
    assert hub._hangup_done is not None and hub._hangup_done.is_set()
    assert "socket gone" in caplog.text


def test_an_open_audio_websocket_does_not_stop_a_view_from_calling(hub, monkeypatch):
    calls = []

    async def do_call(target=None, **_kw):
        calls.append(target)
        return True, "200"

    monkeypatch.setattr(sip, "do_call", do_call)
    hub._has_ws_clients = lambda: True

    async def main():
        assert await hub.stream_opened() is True
        await asyncio.sleep(0)

    asyncio.run(main())
    assert calls == [None]


def test_unloading_mid_call_hangs_up_and_resets_the_sip_state(hub, monkeypatch):
    monkeypatch.setattr(sip, "in_call", True)
    hung_up = []

    async def do_hangup():
        hung_up.append(True)

    async def nothing():
        return None

    monkeypatch.setattr(sip, "do_hangup", do_hangup)
    monkeypatch.setattr(hub_mod.media, "stop_media", nothing)
    monkeypatch.setattr(hub_mod.media, "close_transports", lambda: None)
    monkeypatch.setattr(sip, "writer", None, raising=False)
    monkeypatch.setattr(sip, "_udp_sock", None, raising=False)
    monkeypatch.setattr(sip, "_state_change_callback", None, raising=False)
    monkeypatch.setitem(sip.pending_incoming, "active", True)
    asyncio.run(hub.async_stop())
    assert hung_up == [True]
    assert sip.in_call is False and sip.registered is False
    assert sip.pending_incoming["active"] is False


def test_the_echo_of_our_own_call_is_declined_as_busy(hub, monkeypatch):
    """603 would tell the PBX to stop the ring everywhere, the Tab included."""
    monkeypatch.setattr(sip, "in_call", True)
    reasons = []

    async def decline(reason="603 Decline"):
        reasons.append(reason)

    monkeypatch.setattr(sip, "do_decline_incoming", decline)

    async def main():
        await hub._handle_broadcast("ring", "")
        await asyncio.sleep(0)

    asyncio.run(main())
    assert reasons == ["486 Busy Here"]


def _requests(monkeypatch):
    sent, broadcasts = [], []

    async def send(msg):
        sent.append(msg)

    async def broadcast(kind, body):
        broadcasts.append(body)

    monkeypatch.setattr(sip, "send", send)
    monkeypatch.setattr(sip, "broadcast", broadcast)

    def deliver(raw):
        asyncio.run(sip._process_request(raw, lambda coro, name: coro.close()))
    return sent, broadcasts, deliver


def _message(cseq=7, cid="m1", body="DOOR;OPEN;1", panda="Panda: blue\r\n"):
    return ("MESSAGE sip:60999@d SIP/2.0\r\nVia: SIP/2.0/TLS 1.2.3.4;branch=z9hG4bK1\r\n"
            f"From: <sip:55001@d>;tag=a\r\nTo: <sip:60999@d>\r\nCall-ID: {cid}\r\n"
            f"CSeq: {cseq} MESSAGE\r\n{panda}Content-Length: {len(body)}\r\n\r\n{body}")


@pytest.mark.parametrize("panda", ["Panda: white\r\n", "Panda: black\r\n", "Panda: grey\r\n", ""])
def test_only_a_panda_blue_message_reaches_the_plant_parser(monkeypatch, panda):
    """The apps drop every MESSAGE whose Panda is not blue (PROTOCOL §4): a text
    message that contains DND;ON must not switch DND."""
    sent, broadcasts, deliver = _requests(monkeypatch)
    deliver(_message(body="DND;ON", panda=panda))
    assert broadcasts == []
    assert len(sent) == 1, "it is still answered"
    deliver(_message(cseq=8, body="DND;ON", panda="Panda: Blue\r\n"))
    assert broadcasts == ["DND;ON"]


def test_a_duplicate_message_is_broadcast_once(monkeypatch):
    sent, broadcasts, deliver = _requests(monkeypatch)
    for _ in range(2):
        deliver(_message())
    assert broadcasts == ["DOOR;OPEN;1"]
    assert len(sent) == 2, "every copy is still answered"


def test_the_same_call_id_with_a_new_cseq_is_a_new_message(monkeypatch):
    _, broadcasts, deliver = _requests(monkeypatch)
    deliver(_message(cseq=7, body="VOICEMAIL;ON"))
    deliver(_message(cseq=8, body="VOICEMAIL;OFF"))
    assert broadcasts == ["VOICEMAIL;ON", "VOICEMAIL;OFF"]


def test_a_duplicate_notify_is_broadcast_once(monkeypatch):
    sent, broadcasts, deliver = _requests(monkeypatch)
    raw = ("NOTIFY sip:60999@d SIP/2.0\r\nVia: SIP/2.0/TLS 1.2.3.4;branch=z9hG4bK2\r\n"
           "From: <sip:55001@d>;tag=a\r\nTo: <sip:60999@d>\r\nCall-ID: n1\r\n"
           "CSeq: 3 NOTIFY\r\nEvent: dnd\r\nContent-Length: 6\r\n\r\nDND;ON")
    deliver(raw)
    deliver(raw)
    assert broadcasts == ["NOTIFY dnd: DND;ON"]
    assert len(sent) == 2


def test_an_old_copy_outside_the_window_is_a_new_request(monkeypatch):
    _, broadcasts, deliver = _requests(monkeypatch)
    deliver(_message())
    for key in sip._seen_requests:
        sip._seen_requests[key] -= sip.DUPLICATE_WINDOW + 1
    deliver(_message())
    assert len(broadcasts) == 2


def test_unloading_forgets_the_seen_requests_keys_and_ring(monkeypatch):
    monkeypatch.setitem(sip._seen_requests, ("MESSAGE", "m1", "7 MESSAGE"), 1.0)
    monkeypatch.setattr(sip.SDP, "_local_crypto_key", "k1")
    monkeypatch.setattr(sip.SDP, "_local_video_crypto_key", "k2")
    monkeypatch.setattr(sip, "_state_change_callback", None, raising=False)
    for key, value in dict(cid="c1", caller_uri="sip:55001@d", body="v=0").items():
        monkeypatch.setitem(sip.pending_incoming, key, value)
    sip.DEVICES.note_binding('<sip:60999@1.1.1.1>;+sip.instance="<urn:uuid:aaa>";expires=900')
    sip.reset_state()
    assert sip._seen_requests == {}
    assert sip.SDP._local_crypto_key is None and sip.SDP._local_video_crypto_key is None
    assert sip.pending_incoming == sip._PENDING_INITIAL
    assert sip.DEVICES.snapshot()[0]["registered"] is False


def test_a_sip_uri_that_would_split_the_request_line_is_refused(hub):
    ok, msg = asyncio.run(hub.async_send_command("X", target="sip:55001@d\r\nVia: evil"))
    assert not ok


@pytest.mark.parametrize("target", [
    "sip:55001@elsewhere.example", "sip:55001@d;transport=udp", "sip:admin@d",
    "sip:55001@d>", "sip:@d", "sip:55001@d\r\nVia: evil"])
def test_only_a_plant_id_on_the_plant_domain_is_accepted(hub, monkeypatch, target):
    monkeypatch.setattr(hub_mod.R, "SIP_DOMAIN", "d")
    sent = []

    async def message(uri, body, extra_headers=None, timeout=15):
        sent.append(uri)
        return True, "OK", 200

    monkeypatch.setattr(sip, "send_message", message)
    ok, _ = asyncio.run(hub.async_send_command("X", target=target))
    assert not ok and sent == []


@pytest.mark.parametrize("domain_attr", ["SIP_DOMAIN", "LOCAL_DOMAIN", "CLOUD_DOMAIN"])
def test_a_plant_id_on_any_plant_domain_is_sent(hub, monkeypatch, domain_attr):
    for attr in ("SIP_DOMAIN", "LOCAL_DOMAIN", "CLOUD_DOMAIN"):
        monkeypatch.setattr(hub_mod.R, attr, "")
    monkeypatch.setattr(hub_mod.R, domain_attr, "plant.example")
    sent = []

    async def message(uri, body, extra_headers=None, timeout=15):
        sent.append(uri)
        return True, "OK", 200

    monkeypatch.setattr(sip, "send_message", message)
    ok, _ = asyncio.run(hub.async_send_command("X", target="sip:55001@plant.example"))
    assert ok and sent == ["sip:55001@plant.example"]


def test_a_status_refresh_is_not_recorded_as_the_users_command(hub, monkeypatch):
    monkeypatch.setattr(hub_mod.R, "SIP_DOMAIN", "d")
    monkeypatch.setattr(hub_mod.R, "PICG_TARGET", "55001")
    sent = []

    async def message(uri, body, extra_headers=None, timeout=15):
        sent.append((uri, body, extra_headers))
        return True, "OK"

    monkeypatch.setattr(sip, "do_system_message", message)
    hub.stats["last_command_body"] = "VOICEMAIL;ON"
    asyncio.run(hub.async_request_status())
    assert sent == [("sip:55001@d", hub_mod.C.GET_INIT_STATUS, {"Panda": "blue"})]
    assert hub.stats["last_command_body"] == "VOICEMAIL;ON"


def test_a_spawned_task_is_held_until_it_is_done(hub):
    """asyncio keeps only weak references to tasks: a bare create_task() for the
    ring webhook or the auto-call could be collected before it finished."""
    async def _run():
        gate = asyncio.Event()

        async def _work():
            await gate.wait()
            return "done"

        task = hub._spawn(_work(), "test")
        assert task in hub._background
        await asyncio.sleep(0)
        assert task in hub._background
        gate.set()
        await task
        await asyncio.sleep(0)
        assert task not in hub._background

    asyncio.run(_run())


def test_a_spawned_task_that_fails_is_logged(hub, caplog):
    async def _run():
        async def _boom():
            raise RuntimeError("boom")

        task = hub._spawn(_boom(), "ring webhook")
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)
        assert not hub._background

    with caplog.at_level("ERROR"):
        asyncio.run(_run())
    assert "ring webhook failed: boom" in caplog.text


def test_the_ring_webhook_task_is_held(hub, monkeypatch):
    monkeypatch.setattr(hub_mod.R, "RING_WEBHOOK_URL", "http://192.0.2.1/hook")

    async def _run():
        gate = asyncio.Event()

        async def _fire(url):
            await gate.wait()

        monkeypatch.setattr(hub_mod.webhook, "fire", _fire)
        hub.fire_ring_callbacks()
        assert len(hub._background) == 1
        gate.set()
        await asyncio.gather(*hub._background)
        await asyncio.sleep(0)
        assert not hub._background

    asyncio.run(_run())


# ─── the public hang-up (button, card, HomeKit) ──────────────────────────────

def _hangup_without_answer(hub, monkeypatch, events, before_end=0.0):
    async def do_hangup(on_local_end=None):
        await asyncio.sleep(before_end)
        monkeypatch.setattr(sip, "in_call", False)
        hub._on_sip_state_change()         # what _set_in_call does for real
        events.append("local end")
        if on_local_end:
            on_local_end()
        await asyncio.sleep(5)             # the BYE answer the cloud never sends
        events.append("bye answered")
    return do_hangup


def test_async_hangup_returns_once_the_call_ended_locally(hub, monkeypatch):
    monkeypatch.setattr(sip, "in_call", True)
    events = []
    monkeypatch.setattr(sip, "do_hangup", _hangup_without_answer(hub, monkeypatch, events))

    async def main():
        loop = asyncio.get_running_loop()
        started = loop.time()
        await hub.async_hangup()
        waited = loop.time() - started
        held = [t for t in hub._background if not t.done()]
        for task in asyncio.all_tasks() - {asyncio.current_task()}:
            task.cancel()
        return waited, held

    waited, held = asyncio.run(main())
    assert waited < 1.0, "it waited for the BYE answer"
    assert events == ["local end"] and held, "the BYE goes on in the background"


def test_a_view_opening_right_after_async_hangup_waits_for_the_local_end(hub, monkeypatch):
    monkeypatch.setattr(hub_mod.R, "USE_LOCAL_UDP", False)  # the cloud: see test_view_after_hangup
    monkeypatch.setattr(hub_mod, "HANGUP_LOCAL_SETTLE", 0.05)
    monkeypatch.setattr(sip, "in_call", True)
    events = []
    monkeypatch.setattr(sip, "do_hangup", _hangup_without_answer(hub, monkeypatch, events, before_end=0.2))

    async def do_call(target=None, **_kw):
        events.append("new call")
        return True, "200"

    monkeypatch.setattr(sip, "do_call", do_call)

    async def main():
        hang = asyncio.create_task(hub.async_hangup())
        await asyncio.sleep(0.01)
        assert hub._hanging_up, "the guard is up while the BYE leaves"
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await hub.stream_opened() is True
        waited = loop.time() - started
        await hang
        await asyncio.sleep(0)
        for task in asyncio.all_tasks() - {asyncio.current_task()}:
            task.cancel()
        return waited

    waited = asyncio.run(main())
    assert events == ["local end", "new call"]
    assert waited < 1.0


def test_async_hangup_that_fails_before_the_local_end_raises(hub, monkeypatch):
    async def broken(on_local_end=None):
        raise OSError("socket gone")

    monkeypatch.setattr(sip, "do_hangup", broken)
    with pytest.raises(OSError):
        asyncio.run(hub.async_hangup())
    assert hub._hanging_up is False


def test_an_old_hang_up_finishing_leaves_the_newer_guard_up(hub):
    async def main():
        first = hub._begin_hanging_up()
        second = hub._begin_hanging_up()
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        hub._hangup_finished(first, done)
        assert first.is_set(), "its own waiters wake up"
        assert hub._hanging_up and not second.is_set()
        hub._hangup_finished(second, done)
        assert hub._hanging_up is False and second.is_set()

    asyncio.run(main())


def test_a_stale_auto_call_failure_keeps_the_current_flag(hub, monkeypatch):
    async def fail(target=None):
        return False, "486 Busy Here"

    monkeypatch.setattr(sip, "do_call", fail)
    hub._auto_gen = 2
    hub._auto_called = True                # the auto-call of generation 2
    asyncio.run(hub._do_auto_call(1))      # generation 1 fails late
    assert hub._auto_called is True
    asyncio.run(hub._do_auto_call(2))
    assert hub._auto_called is False


def test_unloading_cancels_the_background_tasks(hub, monkeypatch):
    async def nothing():
        return None

    monkeypatch.setattr(hub_mod.media, "stop_media", nothing)
    monkeypatch.setattr(hub_mod.media, "close_transports", lambda: None)
    monkeypatch.setattr(sip, "writer", None, raising=False)
    monkeypatch.setattr(sip, "_udp_sock", None, raising=False)

    async def main():
        task = hub._spawn(asyncio.sleep(30), "webhook")
        hub._begin_hanging_up()
        hub._hangup_settle = asyncio.get_running_loop().call_later(30, hub._end_hanging_up)
        settle = hub._hangup_settle
        await hub.async_stop()
        await asyncio.sleep(0)
        return task.cancelled(), settle.cancelled(), hub._hangup_settle

    assert asyncio.run(main()) == (True, True, None)


def test_reset_state_wakes_waiters_closes_the_transport_and_notifies_nobody(monkeypatch):
    closed, notified = [], []

    class _Sock:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(sip, "writer", _Sock())
    monkeypatch.setattr(sip, "_udp_sock", _Sock())
    monkeypatch.setattr(sip, "in_call", True)
    sip.set_state_callback(lambda: notified.append(True))

    async def main():
        waiter = asyncio.create_task(sip._send_request(
            "OPTIONS sip:x SIP/2.0\r\nCSeq: 1 OPTIONS\r\n\r\n", "cid-wait", timeout=30))
        await asyncio.sleep(0.01)
        sip.reset_state()
        return await asyncio.wait_for(waiter, 1)

    async def send(_msg):
        return None

    monkeypatch.setattr(sip, "send", send)
    assert asyncio.run(main()) == []
    assert closed == [True, True] and sip.writer is None and sip._udp_sock is None
    assert notified == [], "the hub being unloaded is not called back"
    assert sip.in_call is False and sip._state_change_callback is None


def test_an_auto_call_that_connects_after_its_viewer_left_is_hung_up(hub, monkeypatch):
    """The viewer leaves before do_call raises `calling`: stream_closed sees no
    call and schedules nothing, so the auto-call itself must hang up."""
    monkeypatch.setattr(hub_mod, "STREAM_HANGUP_DELAY", 0)
    hung_up = []

    async def do_call(target=None, **_kw):
        await asyncio.sleep(0.05)
        monkeypatch.setattr(sip, "in_call", True)
        return True, "200"

    async def do_hangup():
        hung_up.append(True)
        monkeypatch.setattr(sip, "in_call", False)

    monkeypatch.setattr(sip, "do_call", do_call)
    monkeypatch.setattr(sip, "do_hangup", do_hangup)

    async def main():
        assert await hub.stream_opened() is True
        await hub.stream_closed()          # gone before the call is placed
        assert hub._hangup_task is None
        await asyncio.sleep(0.2)

    asyncio.run(main())
    assert hung_up == [True]
    assert hub._auto_called is False


def test_an_auto_call_with_a_viewer_still_there_is_not_hung_up(hub, monkeypatch):
    monkeypatch.setattr(hub_mod, "STREAM_HANGUP_DELAY", 0)
    hung_up = []

    async def do_call(target=None, **_kw):
        monkeypatch.setattr(sip, "in_call", True)
        return True, "200"

    async def do_hangup():
        hung_up.append(True)

    monkeypatch.setattr(sip, "do_call", do_call)
    monkeypatch.setattr(sip, "do_hangup", do_hangup)

    async def main():
        await hub.stream_opened()
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert hung_up == [] and hub._hangup_task is None
