"""Vimar Intercom: an RTCP probe for debugging, listening on the RTCP ports during a call.

Off unless the integration's logger is set to DEBUG (logger: in configuration.yaml,
or the logger.set_level service) when the call's media is set up. Then the RTCP
ports (RTP port + 1, RFC 3550) are bound for the call, the path to the panel's
RTCP port is opened with one STUN binding request, and what arrives is logged.
Otherwise nothing is bound and nothing is sent: RTCP is not used by the call.

Nothing is ever answered. The probe only tells whether the panel sends RTCP
through the relay, from where and with which SSRC, and what kind of reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import struct

from . import srtp
from .log_buffer import LEVEL_PIN, LOGGER_NAME

_LOGGER = logging.getLogger(__name__)

# RTCP packet types, so the log reads without a table at hand.
TYPES = {
    192: "FIR (RFC 2032)", 193: "NACK (RFC 2032)",
    200: "SR", 201: "RR", 202: "SDES", 203: "BYE", 204: "APP",
    205: "RTPFB", 206: "PSFB", 207: "XR",
}
# Log the first packets of each port, then one in LOG_EVERY.
LOG_FIRST = 10
LOG_EVERY = 50


def debug_enabled() -> bool:
    """Whether the user set the integration's logger to DEBUG.

    log_buffer pins the package logger at LEVEL_PIN (below DEBUG) so that its
    buffer receives everything: that level means nobody chose one, and does
    not turn the probe on.
    """
    log = logging.getLogger(LOGGER_NAME)
    if log.level == LEVEL_PIN:
        return False
    return log.isEnabledFor(logging.DEBUG)


def records(rtcp: bytes) -> list[str]:
    """The record types of a plain (not encrypted) compound RTCP packet."""
    out, i = [], 0
    while i + 4 <= len(rtcp):
        length = struct.unpack_from("!H", rtcp, i + 2)[0]
        out.append(TYPES.get(rtcp[i + 1], str(rtcp[i + 1])))
        i += (length + 1) * 4
    return out


class RTCPProbe(asyncio.DatagramProtocol):
    """Logs the RTCP that reaches one of our RTCP ports.

    In SRTCP the first record's header stays in clear (RFC 3711 section 3.4),
    so its type and the sender's SSRC are readable without the key. The
    records after it are shown only for plain RTCP, or when an SRTCP decoder
    (srtp.SRTCPContext) is available and the call negotiated a key.
    """

    def __init__(self, label: str, encrypted: bool = False, key: str | None = None):
        self.label = label
        self.encrypted = encrypted
        self.transport = None
        self.count = 0
        self.remote_ssrc = 0
        self.remote_addr = None
        self.srtcp_rx = None
        if encrypted and key:
            with contextlib.suppress(Exception):
                self.srtcp_rx = srtp.SRTCPContext(key)
        self._auth_ok = 0
        self._auth_fail = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.count += 1
        kind = TYPES.get(data[1], str(data[1])) if len(data) > 1 else "?"
        ssrc = struct.unpack_from("!I", data, 4)[0] if len(data) >= 8 else 0
        if ssrc:
            self.remote_ssrc = ssrc
        # The relay rewrites ports: where it comes from is not always what the
        # SDP said.
        self.remote_addr = addr
        detail = ""
        if not self.encrypted:
            detail = " | ".join(records(data))
        elif self.srtcp_rx:
            plain = self.srtcp_rx.unprotect(data)
            if plain is None:
                self._auth_fail += 1
            else:
                self._auth_ok += 1
                detail = " | ".join(records(plain))
        if self.count <= LOG_FIRST or self.count % LOG_EVERY == 0:
            _LOGGER.debug(
                "RTCP %s #%d from %s: %d B, %s, first record %s, ssrc=%d%s%s",
                self.label, self.count, addr, len(data),
                "SRTCP" if self.encrypted else "RTCP", kind, ssrc,
                f" [srtcp ok={self._auth_ok} failed={self._auth_fail}]" if self.srtcp_rx else "",
                f": {detail}" if detail else "")

    def close(self) -> None:
        if self.transport:
            self.transport.close()
            self.transport = None


def stun_binding_request() -> bytes:
    """A STUN binding request (RFC 5389): no attributes, a random transaction id."""
    return struct.pack("!HHI", 0x0001, 0, 0x2112A442) + os.urandom(12)


def punch(probe: RTCPProbe | None, remote_ip: str, rtp_port: int) -> None:
    """Open the path to the remote RTCP port (RTP + 1) with a STUN binding request.

    The relay forwards only towards addresses it has already seen traffic
    from: without this, RTCP from the panel would never reach us.
    """
    if not probe or not probe.transport:
        return
    try:
        probe.transport.sendto(stun_binding_request(), (remote_ip, rtp_port + 1))
        _LOGGER.debug("RTCP %s: path opened towards %s:%d", probe.label, remote_ip, rtp_port + 1)
    except OSError as e:
        _LOGGER.debug("RTCP %s: punch failed: %s", probe.label, e)


async def open_probe(label: str, port: int, remote: tuple[str, int],
                     encrypted: bool = False, key: str | None = None) -> RTCPProbe | None:
    """Bind `port`, listen for RTCP and open the path to `remote` (ip, RTP port)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as e:
        _LOGGER.debug("RTCP %s: cannot bind %d: %s", label, port, e)
        sock.close()
        return None
    _, probe = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: RTCPProbe(label, encrypted, key), sock=sock)
    _LOGGER.debug("RTCP %s listening on :%d", label, port)
    punch(probe, *remote)
    return probe
