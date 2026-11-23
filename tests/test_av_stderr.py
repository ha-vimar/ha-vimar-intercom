"""What the /av ffmpeg says on stderr: real faults at WARNING, the rest at DEBUG.

Every line went to DEBUG, which the Home Assistant log does not receive by
default: a bind failure or a broken input stayed invisible. Known harmless
lines (the decoder concealing a lost packet, the jitter buffer giving up on a
late one) and everything said after we asked ffmpeg to stop stay at DEBUG.
"""
from __future__ import annotations

import asyncio
import collections
import logging

import pytest

from custom_components.vimar_intercom import av_stream
from custom_components.vimar_intercom import media_handler as mh


class _Proc:
    def __init__(self, lines):
        self._lines = [line.encode() + b"\n" for line in lines]
        self.stderr = self

    def readline(self):
        return self._lines.pop(0) if self._lines else b""


def _levels(monkeypatch, caplog, lines, *, current=True, stopping=False):
    proc = _Proc(lines)
    monkeypatch.setattr(av_stream, "av_ffmpeg_proc", proc if current else None)
    monkeypatch.setattr(av_stream, "_av_stopping", stopping)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=av_stream.__name__):
        asyncio.run(av_stream._read_av_ffmpeg_stderr(proc, collections.deque(maxlen=20)))
    return {r.getMessage().removeprefix("AV ffmpeg: "): r.levelno for r in caplog.records
            if r.getMessage().startswith("AV ffmpeg: ")}


def test_a_real_fault_is_a_warning(monkeypatch, caplog):
    levels = _levels(monkeypatch, caplog, [
        "[udp @ 0x1] bind failed: Address in use",
        "Error opening input file /tmp/av.sdp.",
    ])
    assert set(levels.values()) == {logging.WARNING}


@pytest.mark.parametrize("line", [
    "[sdp @ 0x1] max delay reached. need to consume packet",
    "[sdp @ 0x1] RTP: missed 3 packets",
    "[in#0/sdp] RTP: dropping old packet received too late",
    "[h264 @ 0x1] non-existing PPS 0 referenced",
    "[h264 @ 0x1] no frame!",
    "[h264 @ 0x1] error while decoding MB 3 4",
    "Error during demuxing: Operation timed out",
])
def test_known_harmless_lines_stay_at_debug(monkeypatch, caplog, line):
    assert _levels(monkeypatch, caplog, [line]) == {line: logging.DEBUG}


def test_everything_is_debug_once_a_stop_was_requested(monkeypatch, caplog):
    lines = ["[out#0/mpegts] Error muxing a packet", "Error writing trailer: Immediate exit requested",
             "Task finished with error code: -22 (Invalid argument)"]
    assert set(_levels(monkeypatch, caplog, lines, stopping=True).values()) == {logging.DEBUG}


def test_a_replaced_process_is_silenced(monkeypatch, caplog):
    levels = _levels(monkeypatch, caplog, ["Error muxing a packet"], current=False)
    assert levels == {"Error muxing a packet": logging.DEBUG}


def test_the_stop_sets_the_stopping_flag(monkeypatch):
    monkeypatch.setattr(av_stream, "_av_stopping", False)
    monkeypatch.setattr(av_stream, "av_ffmpeg_proc", None)
    monkeypatch.setattr(mh, "video_proto", None)
    monkeypatch.setattr(mh, "audio_proto", None)
    asyncio.run(av_stream._stop_av_ffmpeg_locked())
    assert av_stream._av_stopping is True


def test_a_broken_fu_a_counts_as_a_loss():
    p = mh.RTPVideoProtocol.__new__(mh.RTPVideoProtocol)
    p._fua_buf = bytearray()
    p._fua_started = False
    p._fua_expected_seq = None
    p.pkt_count = 1
    losses = []
    p._lost = losses.append
    p._depacketize(bytes([0x7C, 0x05]) + b"\x00" * 10, 7)  # middle fragment, start lost
    assert len(losses) == 1
    p._depacketize(bytes([0x7C, 0x85]) + b"\x00" * 10, 8)  # IDR start ...
    p._depacketize(bytes([0x7C, 0x85]) + b"\x00" * 10, 9)  # ... then a new start: end lost
    assert len(losses) == 2
