"""The RTCP probe (rtcp.py): bound only with the integration's logger at DEBUG.

At the default level nothing is bound and nothing is sent; at DEBUG the RTCP
ports (RTP + 1) listen for the call, the path to the panel's RTCP port is
opened, and the probes are closed with the call and on unload.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct

import pytest

from custom_components.vimar_intercom import log_buffer, rtcp  # noqa: E402
from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import sip_client as sip

OFFER = (
    "v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\nc=IN IP4 10.0.0.1\r\nt=0 0\r\n"
    "m=audio 55730 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
    "m=video 57100 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
)


@pytest.fixture
def level(monkeypatch):
    log = logging.getLogger(log_buffer.LOGGER_NAME)
    monkeypatch.setattr(log, "level", log.level)

    def _set(value):
        log.setLevel(value)
    return _set


@pytest.fixture
def opened(monkeypatch):
    """setup_media with fake protocols; records what the probe would bind."""
    calls = []

    class _Probe:
        def __init__(self, label):
            self.label = label
            self.closed = False

        def close(self):
            self.closed = True

    async def _open(label, port, remote, encrypted=False, key=None):
        calls.append((label, port, remote))
        return _Probe(label)

    async def _noop():
        pass

    monkeypatch.setattr(rtcp, "open_probe", _open)
    monkeypatch.setattr(media, "audio_proto", media.RTPAudioProtocol())
    monkeypatch.setattr(media, "video_proto", media.RTPVideoProtocol())
    monkeypatch.setattr(media.frame_grabber, "start", lambda p: None)
    monkeypatch.setattr(media.frame_grabber, "stop", lambda p: None)
    monkeypatch.setattr(media, "_stun_keepalive", _noop)
    monkeypatch.setattr(media, "_audio_broadcast", _noop)
    monkeypatch.setattr(media, "_tx_loop", _noop)
    monkeypatch.setattr(media, "_rtcp_probes", [])
    return calls


def _setup():
    async def _run():
        await media.setup_media(sip.parse_sdp(OFFER))
        await asyncio.sleep(0)

    asyncio.run(_run())


@pytest.mark.parametrize("value, enabled", [
    (log_buffer.LEVEL_PIN, False),  # log_buffer's pin: nobody chose a level
    (logging.INFO, False),
    (logging.WARNING, False),
    (logging.DEBUG, True),
])
def test_only_a_debug_level_chosen_by_the_user_enables_it(level, value, enabled):
    level(value)
    assert rtcp.debug_enabled() is enabled


@pytest.mark.parametrize("value", [log_buffer.LEVEL_PIN, logging.INFO])
def test_nothing_is_bound_below_debug(level, opened, value):
    level(value)
    _setup()
    assert opened == []
    assert media._rtcp_probes == []


def test_at_debug_both_rtcp_ports_are_bound_and_punched(level, opened):
    level(logging.DEBUG)
    _setup()
    assert opened == [
        ("audio", sip.C.RTP_AUDIO_PORT + 1, ("10.0.0.1", 55730)),
        ("video", sip.C.RTP_VIDEO_PORT + 1, ("10.0.0.1", 57100)),
    ]
    probes = list(media._rtcp_probes)
    assert len(probes) == 2

    asyncio.run(media.stop_media())
    assert all(p.closed for p in probes) and media._rtcp_probes == []


def test_the_unload_closes_the_probes(level, opened, monkeypatch):
    level(logging.DEBUG)
    _setup()
    probes = list(media._rtcp_probes)
    monkeypatch.setattr(media, "audio_proto", None)
    monkeypatch.setattr(media, "video_proto", None)
    media.close_transports()
    assert all(p.closed for p in probes) and media._rtcp_probes == []


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_the_probe_punches_listens_and_closes(caplog, level):
    """For real, on loopback: the STUN punch reaches the remote RTCP port (RTP + 1),
    and an SR sent back is logged with its type and SSRC."""
    level(logging.DEBUG)
    panel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    panel.bind(("127.0.0.1", 0))
    panel.settimeout(2)
    panel_rtcp = panel.getsockname()[1]
    port = _free_port()

    async def _run():
        probe = await rtcp.open_probe("video", port, ("127.0.0.1", panel_rtcp - 1))
        punch, addr = await asyncio.to_thread(panel.recvfrom, 64)
        sr = struct.pack("!BBHI", 0x80, 200, 6, 0x1234ABCD) + bytes(20)
        panel.sendto(sr, ("127.0.0.1", port))
        for _ in range(100):
            if probe.count:
                break
            await asyncio.sleep(0.01)
        probe.close()
        return probe, punch, addr

    with caplog.at_level(logging.DEBUG, logger="custom_components.vimar_intercom.rtcp"):
        try:
            probe, punch, addr = asyncio.run(_run())
        finally:
            panel.close()
    assert punch[:2] == b"\x00\x01" and punch[4:8] == b"\x21\x12\xa4\x42", "a STUN binding request"
    assert addr[1] == port, "sent from the RTCP port itself"
    assert probe.count == 1 and probe.remote_ssrc == 0x1234ABCD
    assert "first record SR" in caplog.text and "ssrc=305441741" in caplog.text
    assert probe.transport is None
    # Closed: the port is free again.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", port))
    s.close()


def test_srtcp_is_logged_without_decrypting_when_no_decoder(monkeypatch, caplog):
    monkeypatch.delattr(rtcp.srtp, "SRTCPContext", raising=False)
    probe = rtcp.RTCPProbe("audio", encrypted=True, key="k")
    sr = struct.pack("!BBHI", 0x80, 200, 6, 42) + bytes(40)
    with caplog.at_level(logging.DEBUG, logger="custom_components.vimar_intercom.rtcp"):
        probe.datagram_received(sr, ("10.0.0.1", 55731))
    assert probe.srtcp_rx is None
    assert "SRTCP, first record SR, ssrc=42" in caplog.text
    assert ": SR" not in caplog.text, "the encrypted records are not guessed at"


def test_plain_rtcp_lists_every_record():
    sr = struct.pack("!BBHI", 0x80, 200, 6, 42) + bytes(20)
    sdes = struct.pack("!BBHI", 0x81, 202, 1, 42)
    assert rtcp.records(sr + sdes) == ["SR", "SDES"]
