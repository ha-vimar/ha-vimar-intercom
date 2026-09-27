"""RTCP dalla targa: ascolto, e la strada aperta verso la sua porta."""

import asyncio
import logging
import os
import struct

_LOGGER = logging.getLogger(__name__)


class RTCPProbe(asyncio.DatagramProtocol):
    """Sta in ascolto sulla porta RTCP e racconta cosa ci arriva.

    Finora quelle porte non erano nemmeno aperte: ogni pacchetto RTCP della
    targa si prendeva un "porta non raggiungibile" dal kernel. Prima di
    scrivere un solo byte di SRTCP serve sapere se il relay ce li inoltra, e
    con quale SSRC: se non arriva niente, non c'è nessun canale di ritorno da
    usare e tutto il resto del lavoro non ha senso.

    Non risponde e non decifra: i pacchetti sono protetti (SRTCP) e qui
    servono solo come prova di esistenza e come campioni veri su cui
    verificare una futura implementazione.
    """

    # Nomi dei tipi RTCP, per leggere il registro senza tabelle alla mano.
    TYPES = {
        192: "FIR (RFC 2032)", 193: "NACK (RFC 2032)",
        200: "SR", 201: "RR", 202: "SDES", 203: "BYE", 204: "APP",
        205: "RTPFB", 206: "PSFB", 207: "XR",
    }

    def __init__(self, label: str):
        self.label = label
        self.transport = None
        self.count = 0
        self.srtcp_rx = None
        self.remote_rtcp_addr = None
        self.remote_ssrc = 0
        self._auth_ok = 0
        self._auth_fail = 0

    def connection_made(self, transport):
        self.transport = transport

    def _records(self, rtcp: bytes) -> str:
        """Elenca i record di un pacchetto composto, per leggerlo nel log."""
        out, i = [], 0
        while i + 4 <= len(rtcp):
            length = struct.unpack_from("!H", rtcp, i + 2)[0]
            out.append(self.TYPES.get(rtcp[i + 1], str(rtcp[i + 1])))
            i += (length + 1) * 4
            if length == 0 and rtcp[i - 4:i - 3] == b"":
                break
        return " ‖ ".join(out)

    def datagram_received(self, data, addr):
        self.count += 1
        # In SRTCP l'intestazione del primo record resta in chiaro: tipo e
        # SSRC si leggono anche senza chiave.
        kind = self.TYPES.get(data[1], str(data[1])) if len(data) > 1 else "?"
        ssrc = struct.unpack("!I", data[4:8])[0] if len(data) >= 8 else 0
        if ssrc:
            self.remote_ssrc = ssrc
        # Si risponde da dove arriva: il relay riscrive le porte, e quella
        # annunciata nell'SDP non è quella da cui poi parla davvero.
        self.remote_rtcp_addr = addr
        # Decifrare i pacchetti veri della targa è l'unico modo onesto di
        # verificare la nostra SRTCP: un giro protect/unprotect fatto in casa
        # dimostra solo che siamo coerenti con noi stessi.
        decoded = ""
        if self.srtcp_rx:
            plain = self.srtcp_rx.unprotect(data)
            if plain is None:
                self._auth_fail += 1
            else:
                self._auth_ok += 1
                decoded = f" → {self._records(plain)}"
        if self.count <= 10 or self.count % 50 == 0:
            _LOGGER.info(
                "RTCP %s #%d da %s: %dB, primo record %s, ssrc=%d "
                "[srtcp ok=%d ko=%d]%s",
                self.label, self.count, addr, len(data), kind, ssrc,
                self._auth_ok, self._auth_fail, decoded)


def _punch_rtcp(probe, remote_ip: str, rtp_port: int, label: str) -> None:
    """Apre la strada verso la porta RTCP del remoto (RTP+1, RFC 3550).

    Il relay inoltra solo verso indirizzi da cui ha già visto traffico: senza
    questo, anche se la targa mandasse RTCP, non arriverebbe mai fin qui.
    """
    if not probe or not probe.transport:
        return
    stun = struct.pack("!HHI", 0x0001, 0, 0x2112A442) + os.urandom(12)
    try:
        probe.transport.sendto(stun, (remote_ip, rtp_port + 1))
        _LOGGER.info("RTCP %s: aperta la strada verso %s:%d",
                     label, remote_ip, rtp_port + 1)
    except OSError as e:
        _LOGGER.debug("RTCP %s: punch fallito: %s", label, e)
