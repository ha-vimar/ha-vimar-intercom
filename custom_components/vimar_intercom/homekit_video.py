"""Il video dalla targa al telefono, senza ffmpeg in mezzo.

La targa manda già H.264 in RTP. Passarlo da ffmpeg (``-c:v copy``) non lo
trasformava: lo ri-impacchettava e basta, al prezzo di circa sette decimi fra
avvio e analisi — e soprattutto consegnava al telefono il keyframe di apertura
dentro una raffica, mezzo secondo di video in un millisecondo, con il tempo di
presentazione già nel passato. Il telefono lo scartava e aspettava il keyframe
naturale successivo della targa: i due secondi e mezzo misurati il 20
settembre, che nessun'altra correzione aveva scalfito.

Qui i pacchetti della targa vanno al telefono così come sono, ritimbrati per
lui (SSRC e payload type negoziati, numeri di sequenza continui) e cifrati con
la sua chiave:

- per primo il gruppo già in memoria, dal keyframe in poi, con i tempi
  schiacciati in pochi tick: il telefono lo decodifica tutto, riferimenti
  compresi, e lo presenta subito invece di trattarlo come arretrato;
- poi il flusso dal vivo, alla sua cadenza, con l'orologio che prosegue da lì;
- un Sender Report RTCP subito e poi ogni due secondi, come faceva ffmpeg.

È la stessa forma della app VIEW: il flusso della targa, decodificato
direttamente, senza un secondo flusso costruito sopra.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import time
from collections import OrderedDict

from .homekit_audio import _Datagrams, is_rtcp
from .srtp import SRTCPContext, SRTPContext

_LOGGER = logging.getLogger(__name__)

_MASK32 = 0xFFFFFFFF
_NTP_EPOCH = 2208988800           # 1900 → 1970
_VIDEO_CLOCK = 90000
SR_INTERVAL = 2.0
# Quanti pacchetti già mandati si tengono per rispondere ai NACK: a 20-30
# pacchetti al secondo sono una quindicina di secondi, ben oltre il buffer
# di qualunque telefono.
SENT_KEEP = 512
_RTPFB_NAMES = {1: "NACK", 3: "TMMBR", 4: "TMMBN", 15: "TWCC"}

_RTCP_NAMES = {200: "SR", 201: "RR", 202: "SDES", 203: "BYE", 204: "APP",
               205: "RTPFB", 206: "PSFB"}


def rtp_payload(packet: bytes) -> bytes:
    """Il payload di un pacchetto RTP, saltando CSRC ed estensione."""
    hlen = 12 + (packet[0] & 0x0F) * 4
    if packet[0] & 0x10 and len(packet) >= hlen + 4:
        hlen += 4 + struct.unpack_from("!H", packet, hlen + 2)[0] * 4
    return packet[hlen:]


def _seq(packet: bytes) -> int:
    return struct.unpack_from("!H", packet, 2)[0]


def _ts(packet: bytes) -> int:
    return struct.unpack_from("!I", packet, 4)[0]


class DirectVideo:
    """Una sessione video verso un telefono, dalla porta che gli abbiamo dato."""

    def __init__(self, sock: socket.socket, phone_addr: tuple[str, int],
                 srtp_key_b64: str, ssrc: int, payload_type: int) -> None:
        self._sock = sock
        self._phone = phone_addr
        self._srtp = SRTPContext(srtp_key_b64)
        self._srtcp = SRTCPContext(srtp_key_b64)
        self._srtcp_rx = SRTCPContext(srtp_key_b64)
        self._ssrc = ssrc & _MASK32
        self._pt = payload_type & 0x7F
        # Sequenza e orologio partono da valori a caso, come vuole RFC 3550.
        self._seq_off = int.from_bytes(os.urandom(2), "big")
        self._ts_base = int.from_bytes(os.urandom(4), "big")
        # Tempi della targa → nostri, per i fotogrammi del gruppo iniziale.
        self._ts_map: dict[int, int] = {}
        # Il punto d'aggancio del dal vivo: (tempo della targa, tempo nostro).
        self._anchor: tuple[int, int] | None = None
        self._last_ts: int | None = None
        self._last_wall = 0.0
        self._tr: asyncio.DatagramTransport | None = None
        self._sr_task: asyncio.Task | None = None
        # I pacchetti già mandati, cifrati, per rimandarli quando il telefono
        # dice di averli persi (NACK).
        self._sent: OrderedDict[int, bytes] = OrderedDict()
        self._panel_max_seq: int | None = None
        self.stats = {"packets": 0, "octets": 0, "backlog": 0, "late_dropped": 0,
                      "rtcp_in": {}, "keyframe_requests": 0,
                      # Dove si perdono i pacchetti: prima di noi (la targa,
                      # il relay) o dopo (il Wi-Fi verso il telefono).
                      "upstream_missing": 0, "upstream_late": 0,
                      "nack_lost": 0, "nack_we_had": 0, "nack_never_had": 0,
                      "retransmitted": 0,
                      "rr_fraction_lost": None, "rr_cumulative_lost": None,
                      "rr_jitter_max": 0}

    async def open(self) -> None:
        """Apre il socket nell'event loop. Non manda ancora niente."""
        loop = asyncio.get_running_loop()
        self._tr, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self._from_phone), sock=self._sock)

    def begin(self, backlog: list[bytes], prefix: list[bytes] | None = None) -> None:
        """Manda il gruppo iniziale. Da chiamare SENZA await in mezzo fra la
        lettura del gruppo e l'aggancio al dal vivo, o un pacchetto si perde."""
        prefix = prefix or []
        packets = list(prefix) + list(backlog)
        if backlog:
            first_seq = _seq(backlog[0])
            k = 0
            for pkt in packets:
                panel_ts = _ts(pkt)
                if panel_ts not in self._ts_map:
                    self._ts_map[panel_ts] = (self._ts_base + k) & _MASK32
                    k += 1
            for i, pkt in enumerate(prefix):
                # Il prefisso (SPS+PPS aggiunti da noi) sta subito prima.
                self._send(pkt, self._ts_map[_ts(pkt)],
                           (first_seq + self._seq_off - len(prefix) + i) & 0xFFFF)
            for pkt in backlog:
                self._send(pkt, self._ts_map[_ts(pkt)])
            last = backlog[-1]
            self._anchor = (_ts(last), self._ts_map[_ts(last)])
            self._panel_max_seq = _seq(last)
            self.stats["backlog"] = len(packets)
        self._send_sr()
        self._sr_task = asyncio.get_running_loop().create_task(self._sr_loop())

    def on_live(self, packet: bytes) -> None:
        """Un pacchetto dal vivo della targa, così come arriva."""
        if len(packet) < 12:
            return
        self._count_upstream(_seq(packet))
        panel_ts = _ts(packet)
        if panel_ts in self._ts_map:
            # Un frammento in ritardo di un fotogramma già mandato.
            self._send(packet, self._ts_map[panel_ts])
            return
        if self._anchor is None:
            self._anchor = (panel_ts, self._ts_base)
        anchor_panel, anchor_ours = self._anchor
        delta = (panel_ts - anchor_panel) & _MASK32
        if delta > 0x7FFFFFFF:
            # Più vecchio dell'aggancio e non fra quelli mandati: arretrato.
            self.stats["late_dropped"] += 1
            return
        self._send(packet, (anchor_ours + delta) & _MASK32)

    def _count_upstream(self, seq: int) -> None:
        """Buchi nella sequenza della targa: pacchetti che non ci arrivano."""
        if self._panel_max_seq is None:
            self._panel_max_seq = seq
            return
        d = (seq - self._panel_max_seq) & 0xFFFF
        if 0 < d < 0x8000:
            self.stats["upstream_missing"] += d - 1
            self._panel_max_seq = seq
        elif d:
            # Arriva dopo uno più nuovo: un buco che si chiude in ritardo.
            self.stats["upstream_late"] += 1
            if self.stats["upstream_missing"]:
                self.stats["upstream_missing"] -= 1

    def _send(self, packet: bytes, our_ts: int, our_seq: int | None = None) -> None:
        if self._tr is None:
            return
        if our_seq is None:
            our_seq = (_seq(packet) + self._seq_off) & 0xFFFF
        payload = rtp_payload(packet)
        rtp = (bytes([0x80, (packet[1] & 0x80) | self._pt])
               + struct.pack("!HII", our_seq, our_ts, self._ssrc) + payload)
        srtp = self._srtp.protect(rtp)
        self._tr.sendto(srtp, self._phone)
        self._sent[our_seq] = srtp
        if len(self._sent) > SENT_KEEP:
            self._sent.popitem(last=False)
        self.stats["packets"] += 1
        self.stats["octets"] += len(payload)
        self._last_ts = our_ts
        self._last_wall = time.time()

    def _sender_report(self) -> bytes:
        now = time.time()
        if self._last_ts is None:
            rtp_now = self._ts_base
        else:
            rtp_now = (self._last_ts + int((now - self._last_wall) * _VIDEO_CLOCK)) & _MASK32
        frac = int((now % 1) * (1 << 32)) & _MASK32
        return struct.pack("!BBHIIIIII", 0x80, 200, 6, self._ssrc,
                           (int(now) + _NTP_EPOCH) & _MASK32, frac, rtp_now,
                           self.stats["packets"] & _MASK32,
                           self.stats["octets"] & _MASK32)

    def _send_sr(self) -> None:
        if self._tr is not None:
            self._tr.sendto(self._srtcp.protect(self._sender_report()), self._phone)

    async def _sr_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(SR_INTERVAL)
                self._send_sr()
        except asyncio.CancelledError:
            pass

    def _from_phone(self, data: bytes, addr) -> None:
        """L'RTCP del telefono: per ora lo si guarda e basta."""
        if addr[0] != self._phone[0] or not is_rtcp(data):
            return
        plain = self._srtcp_rx.unprotect(data)
        if plain is None:
            return
        off = 0
        while off + 4 <= len(plain):
            pt = plain[off + 1]
            fmt = plain[off] & 0x1F
            name = _RTCP_NAMES.get(pt, str(pt))
            length = (struct.unpack_from("!H", plain, off + 2)[0] + 1) * 4
            if pt == 206 and fmt in (1, 4):   # PLI, FIR: «mandami un keyframe»
                name = "PLI" if fmt == 1 else "FIR"
                self.stats["keyframe_requests"] += 1
                if self.stats["keyframe_requests"] <= 3:
                    _LOGGER.info("HomeKit: il telefono chiede un keyframe (%s)", name)
            elif pt == 205:
                name = _RTPFB_NAMES.get(fmt, f"RTPFB{fmt}")
                if fmt == 1:
                    self._on_nack(plain[off + 12:off + length])
            elif pt in (200, 201):
                self._on_report(plain[off:off + length], rc=fmt)
            counts = self.stats["rtcp_in"]
            counts[name] = counts.get(name, 0) + 1
            off += length

    def _on_nack(self, fci: bytes) -> None:
        """RFC 4585 §6.2.1: «ho perso questi». Se li abbiamo, si rimandano."""
        for i in range(0, len(fci) - 3, 4):
            pid, blp = struct.unpack_from("!HH", fci, i)
            lost = [pid] + [(pid + b + 1) & 0xFFFF for b in range(16) if blp >> b & 1]
            for seq in lost:
                self.stats["nack_lost"] += 1
                packet = self._sent.get(seq)
                if packet is None:
                    self.stats["nack_never_had"] += 1
                    continue
                self.stats["nack_we_had"] += 1
                if self._tr is not None:
                    self._tr.sendto(packet, self._phone)
                    self.stats["retransmitted"] += 1
            if self.stats["nack_lost"] <= len(lost):
                _LOGGER.info("HomeKit: il telefono ha perso %s", lost)

    def _on_report(self, packet: bytes, rc: int) -> None:
        """Il blocco di ricezione che riguarda il nostro flusso (RFC 3550 §6.4)."""
        first = 8 if packet[1] == 201 else 28
        for i in range(rc):
            off = first + i * 24
            if off + 24 > len(packet):
                return
            if struct.unpack_from("!I", packet, off)[0] != self._ssrc:
                continue
            self.stats["rr_fraction_lost"] = packet[off + 4]
            self.stats["rr_cumulative_lost"] = int.from_bytes(packet[off + 5:off + 8], "big")
            jitter = struct.unpack_from("!I", packet, off + 12)[0]
            self.stats["rr_jitter_max"] = max(self.stats["rr_jitter_max"], jitter)

    def send_bye(self) -> None:
        """RTCP BYE (RFC 3550 §6.6): «questo flusso è finito»."""
        if self._tr is not None:
            bye = struct.pack("!BBHI", 0x81, 203, 1, self._ssrc)
            self._tr.sendto(self._srtcp.protect(bye), self._phone)

    async def stop(self) -> None:
        if self._sr_task:
            self._sr_task.cancel()
        if self._tr:
            self._tr.close()
            self._tr = None
        st = self.stats
        _LOGGER.info(
            "HomeKit: video diretto chiuso — %d pacchetti al telefono (%d del gruppo "
            "iniziale), %d arretrati scartati | persi PRIMA di noi (targa/relay): %d, "
            "arrivati in ritardo: %d | il telefono ne ha chiesti %d: %d li avevamo e "
            "sono stati rimandati, %d mai avuti | RR: perdita cumulativa %s, jitter "
            "max %.0f ms | RTCP dal telefono %s",
            st["packets"], st["backlog"], st["late_dropped"],
            st["upstream_missing"], st["upstream_late"],
            st["nack_lost"], st["nack_we_had"], st["nack_never_had"],
            st["rr_cumulative_lost"], st["rr_jitter_max"] / 90, st["rtcp_in"])
