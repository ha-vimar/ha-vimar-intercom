"""Keyframe requests (SIP INFO picture_fast_update) during a call.

A burst at call start, one request on video packet loss, and no periodic
refresh: on the plants tested the panel ignores the request and sends its
keyframes every 3 s on its own, and each INFO crosses the cloud relay.
"""
from __future__ import annotations

import asyncio
import types

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")

sip = hub_mod.sip
media = hub_mod.media


@pytest.fixture
def infos(monkeypatch):
    sent = []

    async def _info():
        sent.append(True)

    monkeypatch.setattr(sip, "send_keyframe_request", _info)
    return sent


@pytest.mark.parametrize("flowing", [False, True])
def test_no_info_after_the_burst_while_the_call_stays_up(monkeypatch, infos, flowing):
    hub = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(sip, "in_call", True, raising=False)
    monkeypatch.setattr(media, "video_proto", types.SimpleNamespace(pkt_count=0))
    sleeps = []
    real_sleep = asyncio.sleep

    async def _sleep(seconds):
        sleeps.append(seconds)
        if flowing and len(sleeps) == 3:
            media.video_proto.pkt_count = 10  # the first IDR arrived
        if len(sleeps) > 50:  # the old periodic loop: stop it, the asserts below fail
            monkeypatch.setattr(sip, "in_call", False, raising=False)
        await real_sleep(0)

    monkeypatch.setattr(hub_mod.asyncio, "sleep", _sleep)

    async def _run():
        await asyncio.wait_for(hub._keyframe_loop(), timeout=5)

    asyncio.run(_run())
    assert sip.in_call, "the loop must end on its own while the call is still up"
    assert all(s == 0.15 for s in sleeps), sleeps
    assert len(infos) == (3 if flowing else 9)


def test_a_lost_video_packet_still_asks_for_a_keyframe(monkeypatch, infos):
    hub = hub_mod.VimarIntercomHub()
    assert media.request_keyframe == hub._request_keyframe

    async def _run():
        vp = media.RTPVideoProtocol()
        vp._lost("seq 42 persa")
        vp._lost("seq 43 persa")  # at most one a second
        await asyncio.gather(*hub._background)

    asyncio.run(_run())
    assert infos == [True]
