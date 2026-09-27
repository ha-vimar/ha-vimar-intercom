"""Vimar Intercom — Media: RTP transport, STUN, video capture, audio.

Il codec G.711 sta in g711.py, il riordino RTP e gli aiuti H.264 in
rtp_h264.py, l'ascolto RTCP in rtcp.py.
"""

from collections import deque
import asyncio
import base64
import json
import logging
import os
import random
import signal
import socket
import struct
import subprocess
import time
import tempfile

from .const import (
    RTP_AUDIO_PORT, RTP_VIDEO_PORT,
    FFMPEG_AV_VIDEO_PORT, FFMPEG_AV_AUDIO_PORT,
)
from .g711 import SILENCE_ULAW, ulaw_decode, ulaw_encode  # noqa: F401
from .homekit_audio import DECODER_CONCEALMENT
from .rtcp import RTCPProbe, _punch_rtcp
from .rtp_h264 import ReorderBuffer, _packet_has_sps, _stap_a  # noqa: F401
from .srtp import SRTPContext, SRTCPContext

_LOGGER = logging.getLogger(__name__)

# ─── Broadcast callback (set by main.py) ────────────────────────────
_broadcast = None


def init(broadcast_fn):
    global _broadcast
    _broadcast = broadcast_fn


async def broadcast(msg_type, msg):
    if _broadcast:
        await _broadcast(msg_type, msg)


# ─── RTP Protocols ──────────────────────────────────────────────────

class RTPAudioProtocol(asyncio.DatagramProtocol):
    """Audio: receive (S)RTP PCMU → [decrypt] → decode → buffer. Send as (S)RTP.

    SRTP è opzionale: i contesti srtp_rx/srtp_tx vengono creati in setup_media
    solo se il remoto negozia a=crypto (media_enc). Quando sono None il traffico
    è RTP in chiaro (caso di default su questo impianto).
    Also forwards RTP to a secondary port for AV ffmpeg."""

    def __init__(self):
        self.transport = None
        self.remote_addr = None
        self.audio_buffer = asyncio.Queue(maxsize=200)
        self.rtp_seq = random.randint(0, 65535)
        self.rtp_ts = random.randint(0, 2**32 - 1)
        self.rtp_ssrc = random.randint(0, 2**32 - 1)
        self.pkt_count = 0
        self.last_tx = 0.0
        self.srtp_rx: SRTPContext | None = None
        self.srtp_tx: SRTPContext | None = None
        # Forward decrypted RTP to the AV ffmpeg, only while it's running.
        self.ffmpeg_av_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.forward_av = False

    def connection_made(self, transport):
        self.transport = transport
        _LOGGER.info("RTP Audio ready on :%d", RTP_AUDIO_PORT)

    def datagram_received(self, data, addr):
        if len(data) < 4:
            return
        if (data[0] & 0xC0) == 0x00:  # STUN
            return
        if (data[0] & 0xC0) != 0x80:  # not RTP/SRTP
            return

        # Decrypt SRTP → RTP
        if self.srtp_rx:
            rtp = self.srtp_rx.unprotect(data)
            if rtp is None:
                if self.pkt_count == 0:
                    _LOGGER.warning("SRTP audio auth failed from %s (%dB)", addr, len(data))
                return
        elif _from_the_call(self, addr):
            rtp = data
        else:
            return

        if (rtp[1] & 0x7F) != 0:  # not PCMU
            return
        cc = rtp[0] & 0x0F
        hlen = 12 + cc * 4
        if len(rtp) <= hlen:
            return
        # Forward decrypted RTP to AV ffmpeg port (only while ffmpeg is up).
        # Le sessioni HomeKit hanno il loro ffmpeg: non dipendono da quello di
        # /av, e fermarlo toglieva l'audio anche a una vista HomeKit aperta.
        try:
            if self.forward_av:
                self.ffmpeg_av_sock.sendto(rtp, ('127.0.0.1', FFMPEG_AV_AUDIO_PORT))
            for port in homekit_session_ports("audio"):
                self.ffmpeg_av_sock.sendto(rtp, ('127.0.0.1', port))
        except OSError:
            pass
        payload = rtp[hlen:]
        self.pkt_count += 1
        if self.pkt_count == 1:
            _LOGGER.info("First %s audio from %s (%dB)",
                         "SRTP" if self.srtp_rx else "RTP", addr, len(payload))
        if not has_ws_clients():
            return
        pcm = ulaw_decode(payload)
        try:
            self.audio_buffer.put_nowait(pcm)
        except asyncio.QueueFull:
            try:
                self.audio_buffer.get_nowait()
                self.audio_buffer.put_nowait(pcm)
            except Exception:
                pass

    def send_rtp(self, ulaw_payload: bytes):
        if not self.transport or not self.remote_addr:
            return
        self.last_tx = time.monotonic()
        self.rtp_seq = (self.rtp_seq + 1) & 0xFFFF
        self.rtp_ts = (self.rtp_ts + len(ulaw_payload)) & 0xFFFFFFFF
        header = struct.pack('!BBHII',
            0x80, 0, self.rtp_seq, self.rtp_ts, self.rtp_ssrc)
        rtp = header + ulaw_payload
        if self.srtp_tx:
            rtp = self.srtp_tx.protect(rtp)
        self.transport.sendto(rtp, self.remote_addr)

    def send_stun(self):
        if not self.transport or not self.remote_addr:
            return
        stun = struct.pack('!HHI', 0x0001, 0, 0x2112A442) + os.urandom(12)
        self.transport.sendto(stun, self.remote_addr)
        _LOGGER.debug("STUN Audio → %s", self.remote_addr)


def _from_the_call(proto, addr) -> bool:
    """RTP in chiaro solo dalla controparte della chiamata in corso.

    Con SRTP ogni pacchetto è autenticato dalla chiave, e basta quella. In
    chiaro no: i socket restano aperti anche fra una chiamata e l'altra, e
    chiunque in rete poteva mandarci video — un SPS finto finiva salvato su
    disco e rigiocato a ogni apertura. Si guarda solo l'indirizzo IP: il
    relay cambia le porte rispetto all'SDP.
    """
    remote = proto.remote_addr
    return bool(remote) and addr[0] == remote[0]


class RTPVideoProtocol(asyncio.DatagramProtocol):
    """Video: [decrypt] → depacketize RTP H.264 → send NALs via WebSocket.

    SRTP (srtp_rx) è opzionale: creato in setup_media solo se il remoto
    negozia a=crypto. Quando è None si depacketizza RTP in chiaro (default).
    No ffmpeg — direct pipeline like the official Vimar app."""

    REORDER_BUF_SIZE = 5  # Hold up to 5 packets for reordering (~30ms at 15fps)
    # Un keyframe di questa targa sta in poche decine di pacchetti.
    GOP_BUFFER_MAX = 200

    def __init__(self):
        self.transport = None
        self.remote_addr = None
        self.pkt_count = 0
        self.srtp_rx: SRTPContext | None = None
        # Forward plain RTP (H.264) to the AV ffmpeg only while it's running.
        # Enabled by start_av_ffmpeg(), disabled by stop_av_ffmpeg() so we
        # never blast packets at a closed/absent socket.
        self.ffmpeg_av_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.forward_av = False
        # FU-A reassembly buffer
        self._fua_buf = bytearray()
        self._fua_started = False
        self._fua_expected_seq = None  # Track RTP seq for FU-A continuity
        # L'ultimo keyframe in pacchetti RTP grezzi, da rigiocare a chi
        # si collega dopo che è passato.
        self._gop: deque[bytes] = deque(maxlen=self.GOP_BUFFER_MAX)
        # Ordered NAL send queue — preserves SPS→PPS→IDR order
        self._nal_queue: asyncio.Queue | None = None
        self._nal_sender_task: asyncio.Task | None = None
        # SPS/PPS reorder buffer — hold IDR until SPS+PPS received
        self._last_sps = None
        self._last_pps = None
        self._sps_pps_sent = False  # True after first SPS+PPS pair sent
        self._pending_idr = None   # IDR waiting for SPS+PPS
        # RTP reorder buffer — fixes out-of-order UDP packets
        self._reorder = ReorderBuffer(self.REORDER_BUF_SIZE)
        # Diagnostics
        self._srtp_fail = 0
        self._srtp_ok = 0
        self._nal_count = 0
        self._nal_types = {}  # type -> count

    def connection_made(self, transport):
        self.transport = transport
        _LOGGER.info("RTP Video ready on :%d", RTP_VIDEO_PORT)
        # Start ordered NAL sender
        loop = asyncio.get_event_loop()
        self._nal_queue = asyncio.Queue(maxsize=500)
        self._nal_sender_task = loop.create_task(self._nal_sender())

    def datagram_received(self, data, addr):
        if len(data) < 4:
            return
        if (data[0] & 0xC0) != 0x80:  # not RTP/SRTP
            return

        # Decrypt SRTP → plain RTP
        if self.srtp_rx:
            rtp = self.srtp_rx.unprotect(data)
            if rtp is None:
                self._srtp_fail += 1
                if self._srtp_fail <= 5 or self._srtp_fail % 100 == 0:
                    _LOGGER.warning("SRTP video auth FAIL #%d (pkt %dB)", self._srtp_fail, len(data))
                return
            self._srtp_ok += 1
        elif _from_the_call(self, addr):
            rtp = data
        else:
            return

        # Il video diretto verso i telefoni HomeKit (homekit_video): ogni
        # pacchetto, appena decifrato, così com'è. Non dipende da forward_av,
        # che segue l'ffmpeg interno: il video al telefono non passa più di lì.
        for sink in tuple(_video_sinks):
            try:
                sink(rtp)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Inoltro video diretto a HomeKit")

        # Forward decrypted RTP to the AV ffmpeg (MPEG-TS) only while it is up;
        # alle sessioni HomeKit sempre (vedi l'audio).
        try:
            if self.forward_av:
                self.ffmpeg_av_sock.sendto(rtp, ('127.0.0.1', FFMPEG_AV_VIDEO_PORT))
            for port in homekit_session_ports("video"):
                self.ffmpeg_av_sock.sendto(rtp, ('127.0.0.1', port))
        except OSError:
            pass

        self.pkt_count += 1
        if self.pkt_count == 1:
            _LOGGER.info("First video RTP from %s (%dB)", addr, len(rtp))
            mark("primo pacchetto video")
        if self.pkt_count <= 3 or self.pkt_count % 200 == 0:
            _LOGGER.info("Video pkt #%d: %dB, srtp_ok=%d fail=%d nals=%d types=%s",
                         self.pkt_count, len(rtp), self._srtp_ok, self._srtp_fail,
                         self._nal_count, self._nal_types)

        # Parse RTP header
        cc = rtp[0] & 0x0F
        hlen = 12 + cc * 4
        seq = struct.unpack_from('!H', rtp, 2)[0]
        # Check for extension header
        if rtp[0] & 0x10:
            if len(rtp) < hlen + 4:
                return
            ext_len = struct.unpack_from('!H', rtp, hlen + 2)[0]
            hlen += 4 + ext_len * 4
        if len(rtp) <= hlen:
            return
        payload = rtp[hlen:]

        # La targa manda SPS, PPS e un IDR completo entro mezzo secondo dalla
        # risposta alla chiamata, e noi accendiamo l'inoltro una settantina di
        # millisecondi DOPO: misurato, ogni volta. Chi si collega si trova
        # quindi senza niente di decodificabile fino al keyframe successivo,
        # tre secondi più tardi — e se nel frattempo chiude, non vede nulla.
        # Lo si tiene da parte per rigiocarglielo.
        if payload:
            self._remember_for_replay(rtp)

        # RTP reorder buffer — hold packets briefly to fix out-of-order UDP
        for s_, p in self._reorder.push(seq, payload):
            self._depacketize(p, s_)

    def _depacketize(self, payload, seq):
        """Depacketize RTP H.264 payload → send NAL units via WebSocket."""
        if len(payload) < 1:
            return
        nal_type = payload[0] & 0x1F

        if 1 <= nal_type <= 23:
            # Single NAL unit — send directly with Annex B start code
            self._emit_nal(payload)

        elif nal_type == 24:  # STAP-A
            # Aggregation: multiple NALs packed together
            off = 1
            while off + 2 <= len(payload):
                nalu_size = struct.unpack_from('!H', payload, off)[0]
                off += 2
                if off + nalu_size > len(payload):
                    break
                self._emit_nal(payload[off:off + nalu_size])
                off += nalu_size

        elif nal_type == 28:  # FU-A
            # Fragmentation: one NAL split across packets
            if len(payload) < 2:
                return
            fu_header = payload[1]
            start = bool(fu_header & 0x80)
            end = bool(fu_header & 0x40)
            nal_unit_type = fu_header & 0x1F
            fragment = payload[2:]

            if start:
                # Reconstruct NAL header: F|NRI from original + type from FU
                nal_header = (payload[0] & 0xE0) | nal_unit_type
                if self._fua_started:
                    _LOGGER.debug("FU-A new start while prev incomplete (type=%d buf=%d)",
                                  nal_unit_type, len(self._fua_buf))
                self._fua_buf = bytearray([nal_header])
                self._fua_buf.extend(fragment)
                self._fua_started = True
                self._fua_expected_seq = (seq + 1) & 0xFFFF
                if nal_unit_type in (5, 7, 8) or self.pkt_count <= 20:
                    _LOGGER.debug("FU-A START seq=%d nalType=%d fragSize=%d",
                                 seq, nal_unit_type, len(fragment))
            elif not self._fua_started:
                # FU-A continuation without start — dropped start packet.
                # Normale all'apertura (il flusso parte a metà frammento) e con
                # le perdite del relay: diagnostica, non un guasto.
                if self.pkt_count <= 20:
                    _LOGGER.debug("FU-A middle/end without start: seq=%d nalType=%d end=%s",
                                    seq, nal_unit_type, end)
                return
            else:
                # Continuità della sequenza. La differenza va interpretata con
                # segno: i numeri RTP sono a 16 bit e si riavvolgono, quindi un
                # pacchetto arrivato in ritardo dà una differenza enorme
                # (65475) se la si legge come positiva, quando in realtà vale
                # -61. Contandolo come "65475 pacchetti persi" si buttava via
                # un fotogramma valido a ogni pacchetto fuori ordine.
                if self._fua_expected_seq is not None and seq != self._fua_expected_seq:
                    delta = (seq - self._fua_expected_seq) & 0xFFFF
                    if delta > 0x8000:
                        delta -= 0x10000
                    if delta < 0:
                        # Doppione o ritardatario: si ignora il pacchetto, non
                        # il fotogramma che stiamo ancora componendo.
                        _LOGGER.debug(
                            "Pacchetto video in ritardo di %d posizioni (seq %d): ignorato",
                            -delta, seq)
                        return
                    if delta > MAX_FUA_GAP:
                        # Il relay perde pacchetti, due-quattro su cento: è il
                        # suo modo normale di funzionare, non un guasto nostro.
                        _LOGGER.debug(
                            "Frammenti video persi: attesa la sequenza %d, arrivata %d "
                            "(%d pacchetti mancanti) — fotogramma scartato",
                            self._fua_expected_seq, seq, delta)
                        self._fua_buf = bytearray()
                        self._fua_started = False
                        self._fua_expected_seq = None
                        _recover_from_loss()
                        return
                    # Buco piccolo: il NAL può ancora decodificare.
                    _LOGGER.debug("Buco di %d pacchetti nella sequenza video (seq %d): continuo",
                                  delta, seq)
                self._fua_buf.extend(fragment)
                self._fua_expected_seq = (seq + 1) & 0xFFFF

            if end and self._fua_started:
                completed_type = self._fua_buf[0] & 0x1F if self._fua_buf else 0
                if completed_type in (5, 7, 8) or self._nal_count <= 20:
                    _LOGGER.debug("FU-A END seq=%d nalType=%d totalSize=%d",
                                 seq, completed_type, len(self._fua_buf))
                self._emit_nal(bytes(self._fua_buf))
                self._fua_buf = bytearray()
                self._fua_started = False
                self._fua_expected_seq = None

    def _emit_nal(self, nal_data):
        """Queue a complete NAL unit for ordered sending via WebSocket.

        Ensures SPS→PPS→IDR ordering: if IDR arrives before SPS+PPS,
        buffer it and emit after both parameter sets are received.
        """
        if not nal_data or not ws_send_bytes or not self._nal_queue:
            return
        nal_type = nal_data[0] & 0x1F if nal_data else 0
        self._nal_count += 1
        self._nal_types[nal_type] = self._nal_types.get(nal_type, 0) + 1
        if self._nal_count <= 10 or nal_type in (7, 8, 5):
            _LOGGER.debug("NAL #%d type=%d size=%d (first4: %s)",
                         self._nal_count, nal_type, len(nal_data),
                         nal_data[:4].hex() if len(nal_data) >= 4 else nal_data.hex())

        # Reorder: ensure SPS+PPS always precede IDR
        if nal_type == 7:  # SPS
            self._last_sps = nal_data
            # If we have both SPS+PPS now, emit them + any pending IDR
            if self._last_pps is not None:
                self._flush_params_and_idr()
            return
        elif nal_type == 8:  # PPS
            self._last_pps = nal_data
            if self._last_sps is not None:
                self._flush_params_and_idr()
            return
        elif nal_type == 5:  # IDR
            if not self._sps_pps_sent:
                # No SPS+PPS sent yet — buffer IDR
                _LOGGER.info("IDR buffered — waiting for SPS+PPS")
                self._pending_idr = nal_data
                return
            else:
                # Re-emit latest SPS+PPS before each IDR for robustness
                if self._last_sps:
                    self._queue_nal(self._last_sps)
                if self._last_pps:
                    self._queue_nal(self._last_pps)
        elif nal_type == 1:  # P-frame
            if not self._sps_pps_sent:
                return  # Drop P-frames before first IDR

        self._queue_nal(nal_data)

    def _flush_params_and_idr(self):
        """Emit SPS→PPS→(pending IDR) in correct order."""
        _LOGGER.debug("Flushing SPS→PPS→IDR (pending_idr=%s)",
                     "yes" if self._pending_idr else "no")
        if self._last_sps:
            self._queue_nal(self._last_sps)
        if self._last_pps:
            self._queue_nal(self._last_pps)
        self._sps_pps_sent = True
        if self._pending_idr:
            self._queue_nal(self._pending_idr)
            self._pending_idr = None

    def _queue_nal(self, nal_data):
        """Low-level: queue NAL bytes to WebSocket send queue."""
        msg = b'\x03\x00\x00\x00\x01' + nal_data
        try:
            self._nal_queue.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                self._nal_queue.get_nowait()
                self._nal_queue.put_nowait(msg)
            except Exception:
                pass

    async def _nal_sender(self):
        """Drain NAL queue and send via WebSocket in order."""
        try:
            while True:
                msg = await self._nal_queue.get()
                if ws_send_bytes:
                    try:
                        await ws_send_bytes(msg)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    def _remember_for_replay(self, rtp: bytes) -> None:
        """Tiene il pacchetto nella finestra scorrevole, e nient'altro.

        Qui si raccoglie soltanto. Tagliare il gruppo QUI, al volo sul primo
        SPS che passa, voleva dire tagliarlo in ordine di ARRIVO: quando l'SPS
        arriva in ritardo rispetto al PPS e all'IDR che gli vanno dietro — e
        succede, il riordino poco più sopra esiste apposta — la sforbiciata
        buttava via proprio il keyframe appena raccolto. Misurato in casa:

            Video pkt #2: 1252B, nals=1 types={8: 1}      <- il PPS c'è
            IDR buffered — waiting for SPS+PPS            <- l'IDR pure
            Keyframe rigiocato: buchi=6, nal=[7:9,1:2162,1:1937,1:2279,1:2138]

        Il depacchettizzatore, che sta DIETRO il riordino, l'IDR lo vedeva; al
        telefono rigiocavamo un SPS spaiato con quattro P-frame, e lui restava
        nero fino al keyframe naturale successivo: tre secondi tondi.

        Dove comincia il gruppo lo si decide al momento di rigiocarlo, sui
        numeri di sequenza — vedi _gop_in_sequence_order.
        """
        self._gop.append(rtp)

    def replay_gop_to(self, port: int) -> int:
        """Rigioca l'ultimo keyframe verso una porta sola.

        Va solo alla sessione HomeKit, che legge RTP puro: lì le due tracce
        restano flussi separati con il loro orologio, che è la forma in cui la
        targa le manda. Rigiocarlo nel percorso MPEG-TS invece no — lì un
        keyframe di mezzo secondo prima sfasava audio e video di 95443
        secondi, e a valle l'audio veniva buttato via.
        """
        gop = self._gop_in_sequence_order()
        if not gop:
            return 0
        # Se in questo gruppo l'SPS non c'è, lo si antepone insieme al PPS in
        # un solo pacchetto STAP-A: senza, il telefono non sa decodificare
        # l'IDR che segue e resta nero fino al prossimo gruppo che ne porti
        # uno. La targa lo omette davvero: due aperture su sette.
        if not self._gop_has_sps() and _known_sps and _known_pps:
            gop.insert(0, _stap_a(_known_sps, _known_pps, gop[0]))
        sent = 0
        for rtp in gop:
            try:
                self.ffmpeg_av_sock.sendto(rtp, ('127.0.0.1', port))
                sent += 1
            except OSError:
                break
        return sent

    def _gop_in_sequence_order(self) -> list[bytes]:
        """Il keyframe messo da parte, rimesso in ordine di sequenza.

        I pacchetti finiscono nel buffer nell'ordine in cui ARRIVANO, che non
        è sempre quello giusto: il riordino avviene due righe più sotto, e il
        buffer si riempie prima. Rigiocandoli così come sono, ffmpeg prende il
        primo come riferimento e scarta tutti quelli che lo precedono —

            [in#0/sdp] RTP: dropping old packet received too late

        tre pacchetti persi su quindici, l'IDR incompleto, e niente di
        decodificabile fino al keyframe successivo: i 2,8 secondi misurati fra
        il nostro rigioco e il primo fotogramma che esce davvero.

        L'ordine si ricostruisce attorno al primo pacchetto, trattando la
        differenza come un intero con segno a 16 bit, così un giro del
        contatore dentro il gruppo non lo scombina.
        """
        packets = [p for p in self._gop if len(p) >= 12]
        if len(packets) < 2:
            return list(packets)
        base = struct.unpack_from("!H", packets[0], 2)[0]

        def distance(pkt: bytes) -> int:
            delta = (struct.unpack_from("!H", pkt, 2)[0] - base) & 0xFFFF
            return delta - 0x10000 if delta > 0x8000 else delta

        ordinati = sorted(packets, key=distance)
        # Il gruppo comincia all'ultimo SPS, cercato QUI e non all'arrivo:
        # adesso l'ordine è quello giusto, quindi quello che sta dopo l'SPS ci
        # sta davvero dietro, e il PPS e l'IDR che lo avevano preceduto per
        # strada tornano al loro posto invece di finire nel cestino.
        inizio = 0
        for i, pkt in enumerate(ordinati):
            if _packet_has_sps(pkt):
                inizio = i
        return ordinati[inizio:]

    def _gop_has_idr(self) -> bool:
        """Se nel gruppo c'è un IDR INTERO, non solo i suoi pezzi.

        Un IDR spezzato in FU-A vale solo se ci sono sia il frammento che apre
        sia quello che chiude: al telefono un IDR monco non serve a niente, lo
        scarta in silenzio e resta nero fino al keyframe naturale successivo —
        tre secondi tondi più tardi. Che è esattamente il ritardo misurato.
        """
        aperto = False
        for rtp in self._gop_in_sequence_order():
            hlen = 12 + (rtp[0] & 0x0F) * 4
            if len(rtp) <= hlen:
                continue
            payload = rtp[hlen:]
            t = payload[0] & 0x1F
            if t == 5:                                   # NAL singolo: basta
                return True
            if t == 24:                                  # STAP-A
                i = 1
                while i + 2 <= len(payload):
                    size = int.from_bytes(payload[i:i + 2], "big")
                    if i + 2 < len(payload) and (payload[i + 2] & 0x1F) == 5:
                        return True
                    i += 2 + size
            elif t == 28 and len(payload) >= 2:          # FU-A
                fu = payload[1]
                if (fu & 0x1F) != 5:
                    continue
                if fu & 0x80:
                    aperto = True
                elif fu & 0x40 and aperto:
                    return True
        return False

    def _gop_report(self) -> str:
        """Cosa stiamo per rigiocare davvero, riga per riga nei log.

        Il telefono mostra l'immagine al primo IDR che riesce a decodificare.
        Se quello che gli rigiochiamo è monco — un frammento perso, la
        sequenza bucata, l'unità che non si chiude — lo butta via senza dire
        niente e aspetta il keyframe naturale della targa: tre secondi tondi
        più tardi, che è esattamente il ritardo che l'utente vede. Senza
        questa riga nei log la differenza fra le due cose non si distingue.
        """
        gop = self._gop_in_sequence_order()
        if not gop:
            return "vuoto"
        seqs, payloads = [], []
        for rtp in gop:
            hlen = 12 + (rtp[0] & 0x0F) * 4
            if rtp[0] & 0x10:                       # estensione
                if len(rtp) < hlen + 4:
                    continue
                hlen += 4 + struct.unpack_from("!H", rtp, hlen + 2)[0] * 4
            if len(rtp) <= hlen:
                continue
            seqs.append(struct.unpack_from("!H", rtp, 2)[0])
            payloads.append((rtp[hlen:], bool(rtp[1] & 0x80)))

        buchi = sum(((b - a) & 0xFFFF) - 1 for a, b in zip(seqs, seqs[1:]))

        nals: list[str] = []
        byte_nal = 0
        fua_aperto, fua_byte = None, 0
        for payload, _marker in payloads:
            t = payload[0] & 0x1F
            if 1 <= t <= 23:
                nals.append(f"{t}:{len(payload)}")
                byte_nal += len(payload)
            elif t == 24:                           # STAP-A
                off = 1
                while off + 2 <= len(payload):
                    size = struct.unpack_from("!H", payload, off)[0]
                    off += 2
                    if off + size > len(payload):
                        break
                    nals.append(f"{payload[off] & 0x1F}:{size}")
                    byte_nal += size
                    off += size
            elif t == 28 and len(payload) >= 2:     # FU-A
                fu, tipo = payload[1], payload[1] & 0x1F
                byte_nal += len(payload) - 2
                fua_byte += len(payload) - 2
                if fu & 0x80:
                    fua_aperto, fua_byte = tipo, len(payload) - 2
                if fu & 0x40:
                    nals.append(f"{tipo}:{fua_byte}"
                                if fua_aperto == tipo else f"{tipo}!monco")
                    fua_aperto = None
        if fua_aperto is not None:
            nals.append(f"{fua_aperto}!aperto:{fua_byte}")

        chiuso = payloads[-1][1]
        return (f"{len(gop)} pacchetti, {byte_nal}B di NAL, buchi={buchi}, "
                f"unità chiusa={'sì' if chiuso else 'NO'}, nal=[{','.join(nals)}]")

    def _gop_has_sps(self) -> bool:
        """Se il gruppo che rigiocheremo contiene già un SPS."""
        return any(_packet_has_sps(rtp) for rtp in self._gop_in_sequence_order())

    def send_stun(self):
        if not self.transport or not self.remote_addr:
            return
        stun = struct.pack('!HHI', 0x0001, 0, 0x2112A442) + os.urandom(12)
        self.transport.sendto(stun, self.remote_addr)
        _LOGGER.debug("STUN Video → %s", self.remote_addr)


# ─── State ──────────────────────────────────────────────────────────

audio_proto: RTPAudioProtocol | None = None

# Chi vuole i pacchetti video decifrati appena arrivano: le sessioni HomeKit
# in video diretto. Si registrano e si tolgono da sole.
_video_sinks: list = []


def add_video_sink(sink) -> None:
    _video_sinks.append(sink)


def remove_video_sink(sink) -> None:
    if sink in _video_sinks:
        _video_sinks.remove(sink)


def gop_for_direct_video() -> tuple[list[bytes], list[bytes]]:
    """Il gruppo da mandare per primo a un telefono: (prefisso, gruppo).

    Il gruppo è quello in memoria, dall'ultimo SPS, in ordine di sequenza. Il
    prefisso è l'SPS+PPS in STAP-A quando la targa non l'ha messo nel gruppo
    (succede: due aperture su sette), preso da quelli ricordati.
    """
    proto = video_proto
    if not proto:
        return [], []
    gop = proto._gop_in_sequence_order()
    prefix: list[bytes] = []
    if gop and not proto._gop_has_sps() and _known_sps and _known_pps:
        prefix = [_stap_a(_known_sps, _known_pps, gop[0])]
    return prefix, gop


def gop_has_keyframe() -> bool:
    return bool(video_proto and video_proto._gop_has_idr())
video_proto: RTPVideoProtocol | None = None
av_ffmpeg_proc = None
# Vera mentre siamo NOI a fermare l'ffmpeg interno: quello che
# dice uscendo non è un guasto, è obbedienza.
_av_stopping = False
_stun_task = None
_audio_task = None
_audio_tx_task = None

# 20 ms di silenzio G.711 µ-law (0xFF = silenzio in µ-law), 160 campioni a 8 kHz.
AUDIO_FRAME_SECONDS = 0.02

# Un video H.264 arriva a raffiche di frammenti: con il buffer di ricezione di
# default il kernel ne scarta e il NAL si spezza ("FU-A seq gap ... discarding").
RTP_RECV_BUFFER = 1024 * 1024

# Ultima istantanea utile, riscritta una volta al secondo durante le chiamate.
#
# ATTENZIONE, è una fotografia di chi c'era alla porta e resta su disco in
# chiaro fino alla chiamata successiva. Sta sotto /tmp e non dentro la
# cartella di configurazione, così non finisce nei backup di Home Assistant;
# in container è nello strato scrivibile, che sopravvive a un riavvio del
# servizio e sparisce quando il container viene ricreato. Non è RAM: chi
# tiene alla riservatezza monti /tmp come tmpfs.
SNAPSHOT_PATH = "/tmp/vimar_intercom_snapshot.jpg"  # noqa: S108
_last_good_snapshot: bytes | None = None


def read_snapshot() -> bytes | None:
    """L'ultima anteprima BUONA. Bloccante: da chiamare in un executor.

    ffmpeg apre (e quindi azzera) il file prima di aver scritto il primo
    fotogramma: se poi il codificatore non parte, il file resta vuoto. Qui si
    accetta solo un JPEG intero, e altrimenti si ridà l'ultimo che lo era — così
    un avvio andato storto non cancella l'immagine che l'app Casa mostra.
    """
    global _last_good_snapshot
    try:
        with open(SNAPSHOT_PATH, "rb") as f:
            data = f.read()
    except OSError:
        data = b""
    if len(data) > 100 and data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
        _last_good_snapshot = data
    return _last_good_snapshot

# Traccia video di cortesia per le chiamate senza immagini.
PLACEHOLDER_SIZE = "320x240"
PLACEHOLDER_FPS = 5

# Quanto si concede al video di farsi vivo prima di partire in solo audio.
# Vale quando non arriva proprio nulla: se in questo tempo non si è visto un
# solo pacchetto video, la chiamata è davvero senza immagini.
VIDEO_WAIT_BEFORE_START = 0.8

# Se invece i pacchetti video ARRIVANO ma SPS e PPS non si sono ancora visti,
# aspettare conviene: la targa manda i parametri insieme al prossimo keyframe,
# che è a un massimo di circa tre secondi. Partire prima significa partire in
# solo audio con l'immagine nera e doversi riavviare a metà flusso, cambiando
# codec e base dei tempi sotto un lettore che ha già analizzato lo stream.
VIDEO_WAIT_FOR_PARAMS = 4.0

# Oltre questo numero di pacchetti mancanti il NAL non vale più la pena.
MAX_FUA_GAP = 5



# ─── La sessione di HomeKit: saperla finita, e finirla ───────────────

def homekit_ffmpeg_pids(sdp_path: str | None = None) -> list[int]:
    """I processi ffmpeg che stanno leggendo il NOSTRO SDP.

    Non c'è una richiesta HTTP la cui fine segnali che HomeKit ha chiuso la
    vista, e l'SDP non produce nessun evento. Quel processo però esiste solo
    finché la sessione è aperta: vederlo comparire e sparire è il segnale più
    onesto che abbiamo.
    """
    pids = []
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                with open(f"/proc/{entry.name}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\x00", b" ").decode(errors="replace")
            except OSError:
                continue
            if "ffmpeg" not in cmd:
                continue
            # Due strade possibili: l'SDP diretto, e il ripiego via /av per
            # le chiamate senza video. Vanno riconosciute entrambe, o della
            # seconda non ci si accorge mai che è finita.
            # Senza un percorso preciso si cerca il PREFISSO: i file di
            # sessione sono vimar_intercom_homekit_<porta>.sdp, e il vecchio
            # nome fisso non li intercettava più — un ffmpeg poteva restare
            # acceso a tenersi le porte.
            marker = sdp_path or "vimar_intercom_homekit"
            if marker in cmd or (sdp_path is None and "/api/vimar_intercom/av" in cmd):
                pids.append(int(entry.name))
    except OSError:
        pass
    return pids


def stop_homekit_ffmpeg(pids=None) -> int:
    """Chiude il processo di HomeKit, se è rimasto acceso.

    Serve quando è il citofono a chiudere la chiamata. Un ingresso RTP non
    finisce da solo: senza pacchetti quel processo resta lì, si tiene le
    porte, e chi guarda continua a vedere l'ultimo fotogramma come se il
    collegamento fosse ancora vivo — mentre l'audio è già morto. Chiuderlo fa
    accorgere Home Assistant che la sessione è finita, e la vista si chiude.
    """
    killed = 0
    # Solo quelli indicati, quando li conosciamo: a chiamata finita può già
    # essere partita una sessione nuova, e il suo processo non va toccato.
    for pid in (pids if pids is not None else homekit_ffmpeg_pids()):
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except OSError:
            pass
    if killed:
        _LOGGER.info("Sessione HomeKit chiusa: terminati %d ffmpeg", killed)
    return killed




def _gop_sps_state(proto) -> str:
    """Se il gruppo rigiocato aveva l'SPS suo o glielo abbiamo aggiunto noi."""
    try:
        return "proprio" if proto._gop_has_sps() else "aggiunto"
    except Exception:  # noqa: BLE001
        return "?"


# ─── Una sola linea del tempo, per smettere di tirare a indovinare ───

# Tre giorni di correzioni sono nati dal leggere registri con orologi diversi.
# Qui ogni apertura ha un'origine sola e ogni tappa si misura da quella, così
# il costo si legge invece di dedurlo.
_open_t0: float | None = None


def open_timeline_start() -> float:
    """Segna l'istante in cui HomeKit ha chiesto il flusso."""
    global _open_t0
    _open_t0 = time.monotonic()
    _LOGGER.info("[T] 0.000 richiesta di flusso")
    return _open_t0


def mark(event: str, **extra) -> None:
    """Registra una tappa, in secondi dall'inizio dell'apertura."""
    if _open_t0 is None:
        return
    detail = " ".join(f"{k}={v}" for k, v in extra.items())
    _LOGGER.info("[T] %.3f %s%s", time.monotonic() - _open_t0, event,
                 f"  {detail}" if detail else "")


# ─── Una sessione HomeKit alla volta, ma con roba tutta sua ──────────

# Ogni sessione prende una coppia di porte e un SDP proprio. Serve perché la
# vecchia sessione può metterci qualche secondo a spegnersi: finché condivide
# porte e file con la nuova, quella che muore si porta dietro anche l'altra, e
# nessun guardiano abbastanza furbo può distinguerle. Con roba separata il
# problema non esiste, invece di essere evitato.

_HOMEKIT_PORT_POOL = [(19206, 19208), (19210, 19212), (19214, 19216)]
_homekit_sessions: dict[str, dict] = {}


def acquire_homekit_session() -> dict | None:
    """Prende una coppia di porte libere e scrive l'SDP di questa sessione."""
    session = reserve_homekit_session()
    if session:
        _write_homekit_sdp_for(session)
    return session


def reserve_homekit_session() -> dict | None:
    """Prende una coppia di porte libere. Va chiamata sull'event loop.

    Girava in un thread: due viste aperte insieme potevano prendere la stessa
    coppia, e intanto l'event loop scorreva _homekit_sessions mentre il thread
    la modificava. Qui non c'è niente di bloccante — solo bind di prova — e
    il file SDP lo scrive poi il chiamante, fuori dall'event loop.
    """
    used = {(s["video_port"], s["audio_port"]) for s in _homekit_sessions.values()}
    for video_port, audio_port in _HOMEKIT_PORT_POOL:
        if (video_port, audio_port) in used:
            continue
        if not _udp_port_free(video_port) or not _udp_port_free(audio_port):
            # Qualcuno le tiene ancora: è la sessione precedente che non si è
            # ancora spenta del tutto.
            continue
        sid = f"{video_port}"
        path = os.path.join(
            tempfile.gettempdir(), f"vimar_intercom_homekit_{sid}.sdp")
        session = {"id": sid, "video_port": video_port,
                   "audio_port": audio_port, "sdp": path,
                   # Finché è False il flusso dal vivo NON le va: prima deve
                   # ricevere il keyframe, altrimenti quello arriva dopo
                   # pacchetti più recenti e ffmpeg lo butta via come
                   # "old packet received too late" — visto succedere.
                   "ready": False}
        _homekit_sessions[sid] = session
        _LOGGER.info("Sessione HomeKit %s: porte %d/%d", sid, video_port, audio_port)
        mark("sessione allocata", id=sid, video=video_port)
        return session
    _LOGGER.warning("Nessuna coppia di porte libera per una nuova sessione HomeKit")
    return None


async def feed_keyframe_when_ready(session: dict, timeout: float = 4.0) -> None:
    """Aspetta che l'ffmpeg della sessione apra la sua porta, poi gli dà il via.

    Non si può rigiocare subito: quel processo ci mette qualche decimo ad
    alzarsi, e quello che arriva prima che abbia aperto la porta è perso.
    """
    port = session["video_port"]
    deadline = time.monotonic() + timeout
    opened = False
    atteso = False
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        if session["id"] not in _homekit_sessions:
            return
        if _udp_port_free(port):
            continue
        proto = video_proto
        if not proto:
            return
        if not opened:
            opened = True
            mark("porta di ffmpeg aperta", port=port)
        # La porta è aperta, ma se quello che abbiamo da parte non contiene un
        # IDR intero non c'è niente da rigiocare: mandarglielo lo stesso vuol
        # dire far partire il flusso dal vivo su un telefono che non ha di che
        # decodificarlo. Si aspetta, entro il tempo concesso, che dal citofono
        # ne arrivi uno buono — ne manda uno ogni tre secondi.
        if not proto._gop_has_idr():
            if not atteso:
                atteso = True
                _LOGGER.info("Sessione %s: nessun IDR intero da rigiocare, si "
                             "aspetta quello della targa (%s)",
                             session["id"], proto._gop_report())
            continue
        sent = proto.replay_gop_to(port)
        # Solo adesso le si apre il flusso dal vivo: i pacchetti che seguono
        # hanno numeri di sequenza più alti di quelli appena rigiocati, quindi
        # la sequenza resta crescente e ffmpeg non scarta niente.
        session["ready"] = True
        _LOGGER.info("Sessione %s: keyframe rigiocato (%d pacchetti), poi via "
                     "al flusso dal vivo", session["id"], sent)
        mark("keyframe rigiocato", pacchetti=sent, sps=_gop_sps_state(proto))
        _LOGGER.info("Keyframe rigiocato: %s", proto._gop_report())
        return
    # Scaduto il tempo senza che la porta si aprisse, o senza che arrivasse
    # un IDR intero: meglio il flusso dal vivo senza keyframe — che almeno
    # parte al prossimo IDR — che una sessione che non riceve mai niente.
    if session["id"] in _homekit_sessions and not session["ready"]:
        session["ready"] = True
        _LOGGER.warning("Sessione %s: porta mai aperta in %.0fs, flusso dal "
                        "vivo senza keyframe", session["id"], timeout)


def release_homekit_session(sid: str) -> None:
    session = _homekit_sessions.pop(sid, None)
    if not session:
        return
    try:
        os.unlink(session["sdp"])
    except OSError:
        pass
    _LOGGER.info("Sessione HomeKit %s rilasciata", sid)


def _write_homekit_sdp_for(session: dict) -> str:
    sdp = (
        "v=0\r\n"
        "o=- 0 0 IN IP4 127.0.0.1\r\n"
        "s=Vimar\r\n"
        "c=IN IP4 127.0.0.1\r\n"
        "t=0 0\r\n"
        f"m=audio {session['audio_port']} RTP/AVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    )
    if video_ready():
        sdp += (
            f"m=video {session['video_port']} RTP/AVP 96\r\n"
            "a=rtpmap:96 H264/90000\r\n"
            f"a=fmtp:96 profile-level-id=42801F;packetization-mode=1"
            f"{_sprop_parameter_sets()}\r\n"
        )
    with open(session["sdp"], "w") as f:
        f.write(sdp)
    return session["sdp"]


def homekit_session_count() -> int:
    """Quante viste HomeKit sono aperte adesso."""
    return len(_homekit_sessions)


def homekit_session_ports(kind: str) -> list[int]:
    """Le porte verso cui inoltrare l'RTP dal vivo.

    L'audio parte subito: non ha keyframe da aspettare. Il video no, finché
    la sessione non ha avuto il suo keyframe: mandarle prima i pacchetti
    correnti significa far scartare il keyframe che arriva dopo, perché ha
    numeri di sequenza più vecchi.
    """
    if kind == "audio":
        return [s["audio_port"] for s in _homekit_sessions.values()]
    return [s["video_port"] for s in _homekit_sessions.values() if s["ready"]]


rtcp_audio_probe: RTCPProbe | None = None
rtcp_video_probe: RTCPProbe | None = None


# ─── Transport setup ────────────────────────────────────────────────

async def setup_transports():
    global audio_proto, video_proto, rtcp_audio_probe, rtcp_video_probe
    loop = asyncio.get_event_loop()

    # Use SO_REUSEADDR to avoid "Address in use" on HA restart/reload
    audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    audio_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RTP_RECV_BUFFER)
    audio_sock.bind(('0.0.0.0', RTP_AUDIO_PORT))

    video_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    video_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    video_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RTP_RECV_BUFFER)
    video_sock.bind(('0.0.0.0', RTP_VIDEO_PORT))

    _, audio_proto = await loop.create_datagram_endpoint(
        RTPAudioProtocol, sock=audio_sock)
    _, video_proto = await loop.create_datagram_endpoint(
        RTPVideoProtocol, sock=video_sock)

    # Le porte RTCP (RTP+1) finora restavano chiuse, così il kernel rispondeva
    # "porta non raggiungibile" a ogni pacchetto di ritorno della targa.
    for port, label in ((RTP_AUDIO_PORT + 1, "audio"), (RTP_VIDEO_PORT + 1, "video")):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as e:
            _LOGGER.warning("RTCP %s: impossibile aprire la %d: %s", label, port, e)
            sock.close()
            continue
        _, probe = await loop.create_datagram_endpoint(
            lambda label=label: RTCPProbe(label), sock=sock)
        if label == "audio":
            rtcp_audio_probe = probe
        else:
            rtcp_video_probe = probe
        _LOGGER.info("RTCP %s in ascolto sulla :%d", label, port)


async def setup_media(remote_sdp, local_crypto_key=None, local_video_crypto_key=None):
    """Start media after SIP call established. Called by sip.py."""
    global _stun_task, _audio_task, _audio_tx_task, has_video
    audio = remote_sdp.get("audio", {})
    video = remote_sdp.get("video", {})
    has_video = bool(video.get("port"))
    if not has_video:
        _LOGGER.info("Media solo audio: nessun flusso video in questa chiamata")
    remote_ip = remote_sdp.get("conn", "")

    remote_audio_key = audio.get("crypto_key")
    remote_video_key = video.get("crypto_key")

    if audio.get("port") and audio_proto:
        aip = audio.get("ip", remote_ip)
        audio_proto.remote_addr = (aip, audio["port"])
        audio_proto.pkt_count = 0
        # SRTP solo se il remoto negozia crypto E abbiamo una chiave locale.
        # Se il remoto risponde in chiaro (nessun a=crypto) restano None → RTP puro.
        audio_proto.srtp_rx = None
        audio_proto.srtp_tx = None
        if remote_audio_key:
            audio_proto.srtp_rx = SRTPContext(remote_audio_key)
            _LOGGER.info("SRTP Audio RX context created")
        if local_crypto_key and remote_audio_key:
            audio_proto.srtp_tx = SRTPContext(local_crypto_key)
            _LOGGER.info("SRTP Audio TX context created")
        audio_proto.send_stun()
        if rtcp_audio_probe and remote_audio_key:
            rtcp_audio_probe.srtcp_rx = SRTCPContext(remote_audio_key)
        _punch_rtcp(rtcp_audio_probe, aip, audio["port"], "audio")
        _mode = "SRTP" if audio_proto.srtp_rx else "RTP"
        await broadcast("log", f"Audio {_mode} → {aip}:{audio['port']}")

    if video.get("port") and video_proto:
        vip = video.get("ip", remote_ip)
        video_proto.remote_addr = (vip, video["port"])
        # Reset ALL state for new call
        video_proto.pkt_count = 0
        video_proto._fua_buf = bytearray()
        video_proto._fua_started = False
        video_proto._fua_expected_seq = None
        video_proto._gop.clear()
        video_proto._last_sps = None
        video_proto._last_pps = None
        video_proto._sps_pps_sent = False
        video_proto._pending_idr = None
        video_proto._reorder.reset()
        video_proto._srtp_fail = 0
        video_proto._srtp_ok = 0
        video_proto._nal_count = 0
        video_proto._nal_types = {}
        video_proto.srtp_rx = None
        if remote_video_key:
            video_proto.srtp_rx = SRTPContext(remote_video_key)
            _LOGGER.info("SRTP Video RX — direct H.264 depacketization (no ffmpeg)")
        video_proto.send_stun()
        if rtcp_video_probe and remote_video_key:
            rtcp_video_probe.srtcp_rx = SRTCPContext(remote_video_key)
        _punch_rtcp(rtcp_video_probe, vip, video["port"], "video")
        _vmode = "SRTP" if video_proto.srtp_rx else "RTP"
        await broadcast("log", f"Video {_vmode} → {vip}:{video['port']} (direct)")

    if _stun_task:
        _stun_task.cancel()
    _stun_task = asyncio.create_task(_stun_keepalive())

    if _audio_task:
        _audio_task.cancel()
    _audio_task = asyncio.create_task(_audio_broadcast())

    if _audio_tx_task:
        _audio_tx_task.cancel()
    _reset_tx_voice()
    _audio_tx_task = asyncio.create_task(_audio_tx_loop())


async def stop_media():
    """Stop all media. Called on hangup/bye."""
    global _stun_task, _audio_task, _audio_tx_task, has_video
    if _stun_task:
        _stun_task.cancel()
        _stun_task = None
    if _audio_task:
        _audio_task.cancel()
        _audio_task = None
    if _audio_tx_task:
        _audio_tx_task.cancel()
        _audio_tx_task = None
    _reset_tx_voice()
    if audio_proto:
        audio_proto.remote_addr = None
        audio_proto.pkt_count = 0
        audio_proto.srtp_rx = None
        audio_proto.srtp_tx = None
        while not audio_proto.audio_buffer.empty():
            try:
                audio_proto.audio_buffer.get_nowait()
            except Exception:
                break
    has_video = False
    if video_proto:
        video_proto.remote_addr = None
        video_proto.pkt_count = 0
        video_proto.srtp_rx = None
        video_proto._fua_buf = bytearray()
        video_proto._fua_started = False
        video_proto._fua_expected_seq = None
        # I parametri della chiamata appena chiusa non dicono nulla sulla
        # prossima: tenendoli, una chiamata senza video sembrava averlo — si
        # dichiarava a ffmpeg un flusso che non sarebbe mai arrivato, e con
        # esso moriva anche l'audio.
        video_proto._last_sps = None
        video_proto._last_pps = None
        video_proto._sps_pps_sent = False
    await stop_av_ffmpeg()


def close_transports():
    """Close UDP transports — called on integration unload."""
    global audio_proto, video_proto
    # Il mittente dei NAL gira finché non lo si ferma: a ogni ricaricamento
    # dell'integrazione ne restava uno appeso, e Home Assistant lo segnalava
    # come «Task was destroyed but it is pending».
    task = getattr(video_proto, "_nal_sender_task", None) if video_proto else None
    if task and not task.done():
        task.cancel()
    if audio_proto and audio_proto.transport:
        audio_proto.transport.close()
        audio_proto = None
    if video_proto and video_proto.transport:
        video_proto.transport.close()
        video_proto = None


# La voce da mandare al posto esterno, già in pacchetti da 20 ms. La spedisce
# solo _audio_tx_loop, un pacchetto per tick: così il timestamp RTP avanza
# esattamente quanto il tempo vero.
#
# Prima send_audio spediva subito, e il riempitivo di silenzio del tick partiva
# ogni volta che un fotogramma di voce era in ritardo anche di un millisecondo:
# misurato, ogni 10 s di voce ne arrivavano alla targa 12,5-13,7 — da 122 a 186
# pacchetti di silenzio infilati fra le parole. La voce arrivava a pezzi e con
# un orologio più veloce del vero, e il buffer della targa cresceva per starci
# dietro: il secondo di ritardo che si sentiva parlando.
#
# Tetto di 80 ms: dopo un intoppo di rete il ritardo accumulato si butta, non
# si trascina per tutta la conversazione.
TX_QUEUE_FRAMES = 4
_tx_voice: deque = deque(maxlen=TX_QUEUE_FRAMES)
_tx_partial = bytearray()


def send_audio(pcm_data: bytes):
    """Voce in PCM a 8 kHz (browser, HomeKit): in coda, la spedisce il tick."""
    global _tx_partial
    _tx_partial += ulaw_encode(pcm_data)
    while len(_tx_partial) >= len(SILENCE_ULAW):
        _tx_voice.append(bytes(_tx_partial[:len(SILENCE_ULAW)]))
        del _tx_partial[:len(SILENCE_ULAW)]


def _reset_tx_voice() -> None:
    _tx_voice.clear()
    _tx_partial.clear()


async def _audio_tx_loop():
    """Tiene attivo un flusso audio in uscita per tutta la chiamata.

    La targa chiude la chiamata se non riceve RTP da noi: con HomeKit non
    esiste un percorso microfono, quindi senza questo flusso la conversazione
    veniva terminata dalla targa con un BYE dopo una decina di secondi. Quando
    non c'è audio reale da inviare si manda silenzio, così lo stream resta
    continuo e correttamente cadenzato anche per il jitter buffer remoto.
    """
    try:
        next_frame = time.monotonic()
        while True:
            next_frame += AUDIO_FRAME_SECONDS
            delay = next_frame - time.monotonic()
            if delay < -1:
                next_frame = time.monotonic()  # ripartenza dopo una pausa lunga
                delay = 0
            await asyncio.sleep(max(0.0, delay))
            proto = audio_proto
            if not proto or not proto.remote_addr:
                continue
            # Un pacchetto per tick, sempre: la voce se c'è, altrimenti silenzio.
            proto.send_rtp(_tx_voice.popleft() if _tx_voice else SILENCE_ULAW)
    except asyncio.CancelledError:
        pass


# ─── STUN keepalive ─────────────────────────────────────────────────

async def _stun_keepalive():
    try:
        while True:
            await asyncio.sleep(15)
            if audio_proto and audio_proto.remote_addr:
                audio_proto.send_stun()
            if video_proto and video_proto.remote_addr:
                video_proto.send_stun()
    except asyncio.CancelledError:
        pass


# ─── Audio broadcast ────────────────────────────────────────────────

# ws_send_bytes: set by main.py — async fn(data) to send binary to all clients
ws_send_bytes = None
# Predicato impostato da __init__: dice se ci sono davvero client WebSocket.
# ws_send_bytes è sempre valorizzato, quindi da solo non distingue "nessuno in
# ascolto" da "qualcuno in ascolto", e si finiva per decodificare PCM e
# accodare NAL per un insieme vuoto, 50 volte al secondo.
has_ws_clients = lambda: False  # noqa: E731
# Impostato da __init__: chiede alla targa un fotogramma chiave. Serve dopo una
# perdita, altrimenti l'immagine resta congelata fino al keyframe periodico,
# che su questo impianto arriva ogni tre secondi circa.
request_keyframe = None
_last_keyframe_request = 0.0
KEYFRAME_REQUEST_INTERVAL = 2.0


def _recover_from_loss():
    global _last_keyframe_request
    if request_keyframe is None:
        return
    now = time.monotonic()
    if now - _last_keyframe_request < KEYFRAME_REQUEST_INTERVAL:
        return
    _last_keyframe_request = now
    _track(asyncio.create_task(request_keyframe()))


async def _audio_broadcast():
    """Forward decoded PCM to browser via WebSocket."""
    try:
        while True:
            if not audio_proto:
                await asyncio.sleep(0.5)
                continue
            try:
                pcm = await asyncio.wait_for(
                    audio_proto.audio_buffer.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if ws_send_bytes:
                await ws_send_bytes(b'\x01' + pcm)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _LOGGER.error("Audio broadcast error: %s", e)



# (ffmpeg video pipeline removed — H.264 NALs sent directly from RTPVideoProtocol)


# ─── AV stream (H264 video + PCMU audio → MPEG-TS for HomeKit) ────

_AV_SDP_PATH = os.path.join(tempfile.gettempdir(), "vimar_intercom_av.sdp")

# Lo stdout di ffmpeg è UNO solo: se ogni client HTTP lo legge per conto suo, i
# byte vengono spartiti fra i client e nessuno riceve un MPEG-TS valido (con
# HomeKit succede di continuo, perché apre più connessioni allo stesso stream).
# Un solo lettore pompa lo stdout e lo distribuisce a tutti gli iscritti.
# Vero quando la chiamata in corso ha davvero un flusso video: un posto
# esterno solo audio non ne manda, e l'SDP dato a ffmpeg non deve dichiararlo.
has_video = False
# Se l'ultimo SDP passato a ffmpeg dichiarava il video: serve a sapere quando
# vale la pena riavviarlo perché il video è arrivato dopo.
_av_sdp_has_video = False
_av_restarting = False
_av_subscribers: set = set()
# asyncio tiene solo riferimenti deboli ai task: senza questo insieme un task
# può essere raccolto dal garbage collector mentre sta ancora lavorando.
_background_tasks: set = set()


def _track(task):
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
_av_pump_task = None
# Spegnimento ritardato di ffmpeg quando l'ultimo spettatore se ne va.
_av_idle_stop_task = None
# Quanto si tiene acceso ffmpeg dopo l'ultimo spettatore. Sotto la durata della
# chiamata concessa dall'impianto, così la grazia non sopravvive alla chiamata.
AV_IDLE_GRACE = 10.0
# ~31 letture al secondo: 32 pezzi sono circa un secondo di margine. Con 256
# un client rimasto indietro una volta restava indietro di otto secondi per
# tutta la chiamata, perché la coda si svuota solo scartando.
_AV_QUEUE_CHUNKS = 32


def subscribe_av() -> asyncio.Queue:
    """Iscrive un client allo stream MPEG-TS; restituisce la sua coda."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=_AV_QUEUE_CHUNKS)
    _av_subscribers.add(queue)
    _LOGGER.info("AV subscriber added (%d total)", len(_av_subscribers))
    return queue


def unsubscribe_av(queue) -> int:
    """Rimuove un client e restituisce quanti ne restano."""
    _av_subscribers.discard(queue)
    _LOGGER.info("AV subscriber removed (%d left)", len(_av_subscribers))
    return len(_av_subscribers)


async def release_av(queue) -> None:
    """Sgancia un client e spegne ffmpeg solo se era davvero l'ultimo.

    La decisione deve stare sotto lo stesso lock di start/stop: altrimenti chi
    esce può spegnere il processo proprio mentre un altro client sta entrando,
    e quest'ultimo resta in attesa di byte che non arriveranno mai (HomeKit
    apre e chiude connessioni in rapida successione, quindi capita davvero).
    """
    global _av_idle_stop_task
    async with _av_lock:
        unsubscribe_av(queue)
        if _av_subscribers:
            return
        # Spegnere subito costa caro: l'app Casa chiude e riapre in rapida
        # successione, e ogni riapertura ripaga l'avvio da freddo (chiamata SIP
        # compresa, se nel frattempo è caduta). La chiamata resta comunque viva
        # ancora per un po', quindi tenere ffmpeg acceso qualche secondo rende
        # il secondo tocco immediato invece che lento come il primo.
        if _av_idle_stop_task and not _av_idle_stop_task.done():
            _av_idle_stop_task.cancel()
        _av_idle_stop_task = _track(asyncio.create_task(_stop_av_when_idle()))


async def _stop_av_when_idle() -> None:
    """Spegne ffmpeg se dopo la grazia non è tornato nessuno a guardare."""
    try:
        await asyncio.sleep(AV_IDLE_GRACE)
    except asyncio.CancelledError:
        return
    async with _av_lock:
        if _av_subscribers:
            return
        _LOGGER.info("Nessuno guarda da %.0fs — spengo ffmpeg", AV_IDLE_GRACE)
        await _stop_av_ffmpeg_locked()


def _broadcast_av(chunk: bytes) -> None:
    for queue in list(_av_subscribers):
        if queue.full():
            # Un client lento non deve bloccare gli altri: si scarta il più
            # vecchio, che per un flusso live è comunque inutile.
            try:
                queue.get_nowait()
            except Exception:  # noqa: BLE001
                pass
        try:
            queue.put_nowait(chunk)
        except Exception:  # noqa: BLE001
            pass


async def _av_pump():
    """Unico lettore dello stdout di ffmpeg, con fan-out verso i client."""
    loop = asyncio.get_event_loop()
    proc = av_ffmpeg_proc
    reader = proc.stdout if proc else None
    # read1() restituisce ciò che è già disponibile invece di attendere di
    # riempire il buffer: meno latenza a parità di byte trasferiti.
    read = getattr(reader, "read1", None) or getattr(reader, "read", None)
    try:
        while proc and proc.poll() is None and read:
            chunk = await loop.run_in_executor(None, read, 4096)
            if not chunk:
                break
            _broadcast_av(chunk)
    except asyncio.CancelledError:
        pass
    except Exception as e:  # noqa: BLE001
        _LOGGER.error("AV pump error: %s", e)
    finally:
        if not _av_restarting:
            _broadcast_av(b"")  # sentinella di fine stream

# Serializza start/stop dell'ffmpeg AV: due /av concorrenti non devono
# lanciare due processi che si contendono le stesse porte UDP.
_av_lock = asyncio.Lock()


def _video_is_flowing() -> bool:
    """Se il video sta davvero arrivando, non solo se è stato negoziato.

    Un INVITE può dichiarare una sezione video e poi non mandare un solo
    pacchetto: succede quando si chiama Home Assistant dal display del Tab.
    ffmpeg però, se gli si dichiara un video che non arriva, non riesce a
    determinarne la geometria e allora non produce nulla del tutto — audio
    compreso. Meglio partire in solo audio e aggiungere il video quando c'è.
    """
    proto = video_proto
    if not proto:
        return False
    # Servono i parametri, non solo i pacchetti: dichiarare il video senza
    # SPS/PPS obbliga ffmpeg a cercarne la geometria nel flusso, ed è così che
    # la traccia video sparisce quando il tempo di analisi è breve.
    return bool(getattr(proto, "_last_sps", None) and getattr(proto, "_last_pps", None))


def _video_packets_arriving() -> bool:
    """Se la targa sta mandando pacchetti video, a prescindere dai parametri.

    Distingue i due casi che ``_video_is_flowing`` confonde: "non arriva
    niente" (chiamata senza immagini) e "arrivano pacchetti ma SPS/PPS non si
    sono ancora visti" (arriveranno col prossimo keyframe). Nel secondo caso
    aspettare è la scelta giusta; nel primo è tempo perso.
    """
    proto = video_proto
    return bool(proto and getattr(proto, "pkt_count", 0) > 0)


def video_ready() -> bool:
    """Se questa chiamata sta davvero portando video, e sappiamo decodificarlo.

    Servono tre cose, e ognuna copre un incidente già capitato:

    * ``has_video`` — il video è stato negoziato nell'SDP;
    * i PACCHETTI stanno arrivando. Un INVITE può dichiarare una sezione
      video e poi non mandare niente (succede quando è il Tab a chiamare
      Home Assistant): dichiararlo lo stesso lascia ffmpeg ad aspettare un
      video che non arriva, e ad aspettare insieme a lui anche l'audio;
    * i parametri, SPS e PPS, che però possono venire dalla MEMORIA. Sono
      sempre gli stessi — descrivono l'encoder della targa, che non cambia —
      e ricordarli è ciò che toglie l'attesa: misurata una chiamata ferma
      3,1 s dopo aver già ricevuto PPS e un keyframe completo, solo perché
      in quel gruppo l'SPS non c'era.
    """
    proto = video_proto
    arriving = bool(proto and getattr(proto, "pkt_count", 0) > 0)
    sps, pps = _parameter_sets()
    return bool(has_video and arriving and sps and pps)


# Ultimi SPS/PPS visti, che sopravvivono alla fine della chiamata.
#
# La targa li manda sempre uguali: descrivono il suo encoder — 320x240,
# Baseline — che non cambia da una chiamata all'altra. Ricordarli evita di
# stare fermi ad aspettarli a ogni apertura. Misurato: una chiamata in cui la
# targa aveva già mandato PPS e un keyframe completo dopo 1,5 s è rimasta
# ferma altri 3,1 s perché in quel gruppo l'SPS non c'era.
_known_sps: bytes | None = None
_known_pps: bytes | None = None
# E sopravvivono anche al riavvio. In memoria soltanto, ogni riavvio di Home
# Assistant li cancellava: la prima apertura dopo, se la targa aveva lasciato
# fuori l'SPS dal gruppo d'apertura, restava ferma tre secondi ad aspettare il
# gruppo successivo. Misurato il 26 settembre: 3,09 s contro i soliti 0,07.
_params_path: str | None = None


def load_parameter_sets(path: str) -> bool:
    """Carica SPS/PPS salvati. Bloccante: da chiamare in un executor."""
    global _known_sps, _known_pps, _params_path
    _params_path = path
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        sps, pps = base64.b64decode(data["sps"]), base64.b64decode(data["pps"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if sps and pps and (sps[0] & 0x1F) == 7 and (pps[0] & 0x1F) == 8:
        _known_sps, _known_pps = sps, pps
        _LOGGER.info("SPS/PPS della targa ricordati dal disco (%dB/%dB)", len(sps), len(pps))
        return True
    return False


def _save_parameter_sets(path: str, sps: bytes, pps: bytes) -> None:
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sps": base64.b64encode(sps).decode(),
                       "pps": base64.b64encode(pps).decode()}, f)
        os.replace(tmp, path)
    except OSError as err:
        _LOGGER.debug("SPS/PPS non salvati: %s", err)


def _remember_parameter_sets() -> None:
    """Aggiorna la coppia nota, se questa chiamata ne ha portata una."""
    global _known_sps, _known_pps
    proto = video_proto
    if not proto:
        return
    sps = getattr(proto, "_last_sps", None)
    pps = getattr(proto, "_last_pps", None)
    if sps and pps:
        sps, pps = bytes(sps), bytes(pps)
        changed = (sps, pps) != (_known_sps, _known_pps)
        _known_sps, _known_pps = sps, pps
        if changed and _params_path:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.run_in_executor(None, _save_parameter_sets, _params_path, sps, pps)


def _parameter_sets() -> tuple[bytes | None, bytes | None]:
    """Quelli di questa chiamata se ci sono, altrimenti gli ultimi noti."""
    _remember_parameter_sets()
    proto = video_proto
    sps = getattr(proto, "_last_sps", None) if proto else None
    pps = getattr(proto, "_last_pps", None) if proto else None
    if sps and pps:
        return bytes(sps), bytes(pps)
    return _known_sps, _known_pps


def _sprop_parameter_sets():
    """SPS/PPS nel formato sprop-parameter-sets dell'SDP.

    Senza questi ffmpeg non conosce la geometria del video e resta in attesa
    finché non intercetta una coppia SPS/PPS nel flusso: la targa le manda
    circa ogni 3 s, quindi l'avvio del video costava parecchi secondi.
    Dichiararle nell'SDP permette a ffmpeg di emettere dal primo pacchetto
    utile — e prenderle dalla memoria, quando questa chiamata non le ha
    ancora portate, permette di non aspettarle affatto.
    """
    sps, pps = _parameter_sets()
    if not sps or not pps:
        return ""
    return (
        f";sprop-parameter-sets={base64.b64encode(sps).decode()},"
        f"{base64.b64encode(pps).decode()}"
    )


def _write_av_sdp():
    """Write the ffmpeg input SDP to disk (blocking — run in executor).

    PT/porte devono combaciare con ciò che RTPVideo/RTPAudioProtocol
    inoltrano su 127.0.0.1 (FFMPEG_AV_VIDEO_PORT / FFMPEG_AV_AUDIO_PORT).
    """
    sdp = (
        "v=0\r\n"
        "o=- 0 0 IN IP4 127.0.0.1\r\n"
        "s=AV\r\n"
        "c=IN IP4 127.0.0.1\r\n"
        "t=0 0\r\n"
        f"m=audio {FFMPEG_AV_AUDIO_PORT} RTP/AVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    )
    global _av_sdp_has_video
    _av_sdp_has_video = video_ready()
    if _av_sdp_has_video:
        sdp += (
            f"m=video {FFMPEG_AV_VIDEO_PORT} RTP/AVP 96\r\n"
            "a=rtpmap:96 H264/90000\r\n"
            f"a=fmtp:96 profile-level-id=42801F;packetization-mode=1"
            f"{_sprop_parameter_sets()}\r\n"
        )
    with open(_AV_SDP_PATH, "w") as f:
        f.write(sdp)
    return _AV_SDP_PATH


def _udp_port_free(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


async def _wait_until_ffmpeg_listens(timeout: float = 0.5, *, with_video: bool = True) -> bool:
    """Attende che ffmpeg abbia davvero preso le porte UDP di ingresso.

    Prima si aspettava un fisso di 300 ms: troppo quasi sempre, e comunque una
    scommessa. Sondare il bind dice esattamente quando è pronto, così l'attesa
    tipica scende a poche decine di millisecondi senza rischiare di perdere i
    primi pacchetti (che possono contenere l'IDR da cui parte il video).
    """
    ports = [FFMPEG_AV_AUDIO_PORT] + ([FFMPEG_AV_VIDEO_PORT] if with_video else [])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(not _udp_port_free(port) for port in ports):
            return True
        await asyncio.sleep(0.01)
    return False


async def start_av_ffmpeg(force: bool = False):
    """Start ffmpeg that reads H264+PCMU RTP and outputs MPEG-TS to pipe.

    Robustezza: idempotente e serializzato. Ferma qualsiasi istanza
    precedente (attendendone la reale terminazione, così le porte UDP
    19201/19202 si liberano prima del nuovo bind → niente "Address in
    use"), scrive l'SDP in executor (no blocking I/O nell'event loop),
    poi abilita il forward RTP verso ffmpeg SOLO dopo lo start.
    """
    global av_ffmpeg_proc, _av_pump_task, _av_stopping
    async with _av_lock:
        pump_alive = _av_pump_task is not None and not _av_pump_task.done()
        # Riusare va bene solo se il processo serve DAVVERO questa chiamata: un
        # ffmpeg avviato in solo audio continua a mandare l'immagine nera anche
        # quando il video c'è.
        matches_call = _av_sdp_has_video == video_ready()
        if (
            not force and av_ffmpeg_proc and av_ffmpeg_proc.poll() is None
            and pump_alive and matches_call
        ):
            # Un secondo spettatore (es. HomeKit e la UI insieme) non deve
            # riavviare un processo sano: il riavvio azzera il probe e il
            # video riparte da zero per tutti.
            _LOGGER.info("AV ffmpeg already running — reusing it")
            return
        if av_ffmpeg_proc and av_ffmpeg_proc.poll() is None and not matches_call:
            _LOGGER.info(
                "AV ffmpeg riavviato: questa chiamata %s video",
                "ha" if video_ready() else "non ha",
            )
        await _stop_av_ffmpeg_locked()

        # Il video negoziato arriva pochi decimi dopo la risposta, mentre qui
        # si è già pronti a scrivere l'SDP. Aspettarlo un attimo evita di
        # partire in solo audio e doversi riavviare subito dopo: il riavvio
        # cambia codec e geometria sotto un lettore che ha già analizzato il
        # flusso, ed è la cosa più fragile di tutta la catena.
        if has_video and not video_ready():
            # Quanto aspettare lo decide ciò che sta davvero arrivando, non un
            # timeout fisso: finché i pacchetti scorrono i parametri stanno per
            # arrivare (viaggiano col keyframe successivo) e vale la pena
            # attenderli; se non arriva nulla, la chiamata è senza immagini e
            # aspettare oltre ritarda soltanto l'audio.
            start = time.monotonic()
            deadline = start + VIDEO_WAIT_BEFORE_START
            extended = False
            while time.monotonic() < deadline and not _video_is_flowing():
                if not extended and _video_packets_arriving():
                    # Una volta sola, e verso un tetto assoluto: prolungare a
                    # ogni giro terrebbe l'attesa aperta per tutta la chiamata.
                    deadline = start + VIDEO_WAIT_FOR_PARAMS
                    extended = True
                await asyncio.sleep(0.02)
            if extended:
                _LOGGER.info(
                    "Attesa parametri video: %s dopo %.1fs",
                    "arrivati" if _video_is_flowing() else "non arrivati",
                    time.monotonic() - start,
                )

        loop = asyncio.get_event_loop()
        try:
            sdp_path = await loop.run_in_executor(None, _write_av_sdp)
        except Exception as e:
            _LOGGER.error("AV SDP write error: %s", e)
            return

        # Flag di latenza, scelti per non perdere pacchetti:
        #  - analyzeduration/probesize: di default ffmpeg analizza fino a 5 s
        #    prima di emettere qualcosa, ma i codec sono già noti dall'SDP.
        #  - max_interleave_delta: il muxer attende fino a 10 s per interlacciare
        #    le tracce, cioè l'audio resta fermo ad aspettare il video. Limitarlo
        #    a 100 ms lascia scorrere l'audio anche se il video è in ritardo.
        # Restringere la coda di riordino RTP (reorder_queue_size) o usare
        # nobuffer/low_delay qui è controproducente: l'inoltro su loopback
        # arriva a raffiche e ffmpeg scarta i pacchetti "too late", rompendo
        # lo stream H.264 al punto che il video non parte affatto.
        cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-protocol_whitelist", "file,udp,rtp",
            "-fflags", "+genpts+discardcorrupt",
            # Buffer di ricezione ampio: l'inoltro arriva a raffiche su
            # loopback e con il buffer di default si perdono pacchetti
            # ("RTP: missed N packets"), che rompono il flusso H.264.
            "-buffer_size", "655360",
            # ffmpeg aspetta TUTTO l'analyzeduration prima di emettere un
            # byte: misurati 2,1 s con 2000000 contro 0,6 s con 500000. È
            # sicuro perché il video viene dichiarato solo quando SPS e PPS
            # sono già noti, quindi la geometria non va cercata nel flusso.
            # probesize invece va lasciato: è lui a decidere se la traccia
            # video sopravvive, e abbassarlo la fa sparire.
            "-analyzeduration", "500000",
            "-probesize", "200000",
            "-i", sdp_path,
        ]
        if not _av_sdp_has_video:
            # Chiamata senza video (posto esterno solo citofono, oppure il Tab
            # che chiama e non manda immagini): senza una traccia video i
            # lettori — l'app Casa in testa — restano a caricare all'infinito e
            # l'audio non si sente comunque. Se ne genera una minima, nera, a
            # 5 fps: costa quasi nulla anche su un Pi e rende la chiamata
            # utilizzabile per quello che è, una conversazione.
            cmd += [
                "-re", "-f", "lavfi",
                "-i", f"color=c=black:s={PLACEHOLDER_SIZE}:r={PLACEHOLDER_FPS}",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-g", str(PLACEHOLDER_FPS * 2),
            ]
        else:
            # dump_extra con freq=all antepone SPS e PPS a OGNI pacchetto, non
            # solo ai keyframe. Sembra uno spreco — sono quindici byte — ma è
            # ciò che permette a chi si collega di sapere subito geometria e
            # profilo. Altrimenti quei parametri passano solo insieme al
            # keyframe della targa, cioè ogni tre secondi, e chi legge il flusso
            # (l'ffmpeg che HomeKit lancia per conto suo) resta ad analizzare
            # finché non ne incontra uno: misurati 7-12 secondi prima della
            # prima immagine, o nessuna traccia video del tutto se nel
            # frattempo il suo tempo di analisi era scaduto.
            cmd += ["-c:v", "copy", "-bsf:v", "dump_extra=freq=all"]
        cmd += [
            # G.711 non è un codec valido in MPEG-TS: con "copy" finisce come
            # dati privati (bin_data) e chi legge il flusso — HomeKit compreso —
            # non lo riconosce come audio. AAC è leggerissimo da produrre a
            # 8 kHz mono e rende la traccia utilizzabile a valle.
            "-c:a", "aac",
            "-b:a", "32k",
            # 24 kHz non aggiunge informazione — la sorgente è G.711 a 8 kHz —
            # ma accorcia il fotogramma AAC. AAC-LC impacchetta sempre 1024
            # campioni: a 8 kHz sono 128 ms di ritardo prima ancora di uscire,
            # a 24 kHz sono 42,7 ms. A valle HomeKit ritranscodifica comunque
            # in Opus, quindi il guadagno resta.
            "-ar", "24000",
            "-ac", "1",
            "-avoid_negative_ts", "make_zero",
            "-max_interleave_delta", "100000",
            "-flush_packets", "1",
        ]
        if _av_sdp_has_video:
            # Azzerare i ritardi di mux fa guadagnare tempo quando le due
            # tracce vengono dallo stesso RTP e condividono la base dei tempi.
            # Con l'immagine di cortesia no: il video generato parte da zero
            # mentre l'audio porta i propri tempi, e presi alla lettera i due
            # flussi finiscono a decine di migliaia di secondi di distanza —
            # a valle l'audio viene semplicemente buttato via. Misurato:
            # primo audio a 95443 s contro un video a 0.
            cmd += ["-muxdelay", "0", "-muxpreload", "0"]
        cmd += [
            # Le uscite ora sono due, quindi la mappatura della prima va detta
            # per esteso: con due output la selezione implicita di ffmpeg non è
            # più quella di prima e il flusso principale ne uscirebbe cambiato.
            "-map", "0:v:0" if _av_sdp_has_video else "1:v:0",
            "-map", "0:a:0",
            "-f", "mpegts",
            "pipe:1",
        ]
        cmd += [
            # Un fotogramma al secondo su file: è l'anteprima che l'app Casa
            # mostra sulla scheda e dentro la notifica del campanello. Senza,
            # resta il rettangolo nero di cortesia, che per chi guarda è
            # indistinguibile da un'integrazione rotta. Costa circa un decimo
            # di secondo di CPU ogni venti di chiamata.
            "-map", "0:v:0" if _av_sdp_has_video else "1:v:0",
            "-an",
            # Codificatore esplicito: questa uscita non può ereditare "copy",
            # perché un filtro va applicato e servono fotogrammi decodificati.
            "-c:v", "mjpeg",
            "-vf", "fps=1",
            # JPEG a piena gamma. Con un filtro in mezzo ffmpeg 8 sceglie per
            # il codificatore il formato a gamma TV del video della targa, e
            # il codificatore JPEG si rifiuta di aprirsi («Non full-range YUV
            # is non-standard»): il file dell'anteprima restava a zero byte e
            # l'app Casa perdeva l'ultima immagine.
            "-pix_fmt", "yuvj420p",
            "-q:v", "5",
            "-f", "image2",
            "-update", "1",
            SNAPSHOT_PATH,
        ]
        try:
            av_ffmpeg_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            _LOGGER.error("AV ffmpeg start error: %s", e)
            av_ffmpeg_proc = None
            return

        _av_stopping = False
        _track(asyncio.create_task(_read_av_ffmpeg_stderr(av_ffmpeg_proc)))
        if _av_pump_task:
            _av_pump_task.cancel()
        _av_pump_task = asyncio.create_task(_av_pump())
        if not await _wait_until_ffmpeg_listens(with_video=_av_sdp_has_video):
            _LOGGER.warning("AV ffmpeg has not bound its input ports yet")
        if av_ffmpeg_proc and av_ffmpeg_proc.poll() is None:
            if video_proto:
                video_proto.forward_av = True
            if audio_proto:
                audio_proto.forward_av = True
            _LOGGER.info(
                "AV ffmpeg started (MPEG-TS output, %s), RTP forwarding enabled",
                "audio+video" if _av_sdp_has_video else "solo audio",
            )
            mark("ffmpeg interno avviato")
            if has_video and not _av_sdp_has_video:
                # Negoziato ma non ancora arrivato: si riparte in solo audio e
                # si riavvia appena il video si fa vivo.
                _track(asyncio.create_task(_restart_when_video_starts()))
        else:
            _LOGGER.error("AV ffmpeg exited immediately during startup")


async def _restart_when_video_starts(timeout: float = 25.0):
    """Riavvia ffmpeg se il video comincia dopo che siamo partiti in solo audio."""
    global _av_restarting
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        if not av_ffmpeg_proc or av_ffmpeg_proc.poll() is not None:
            return
        if _av_sdp_has_video or not has_video:
            return
        if not _av_subscribers:
            # Nessuno sta più guardando: riavviare adesso lascerebbe un ffmpeg
            # orfano acceso fino alla fine della chiamata.
            return
        # Qui non bastano i parametri: da quando li ricordiamo fra una
        # chiamata e l'altra, _sprop_parameter_sets() risponde anche quando il
        # video di QUESTA chiamata non arriva affatto. Su una chiamata dal Tab
        # — che dichiara il video e poi non ne manda — quel controllo diceva
        # "è arrivato", e si riavviava ffmpeg nel bel mezzo dell'analisi di
        # HomeKit, per poi ricostruire lo stesso identico flusso nero. Era il
        # riavvio distruttivo che mangiava l'audio e lasciava la rotella a
        # girare. Serve la prova che i pacchetti arrivino davvero.
        if video_ready():
            _LOGGER.info("Video arrivato dopo l'avvio: riavvio ffmpeg includendolo")
            _av_restarting = True
            try:
                await start_av_ffmpeg(force=True)
            finally:
                _av_restarting = False
            return


async def stop_av_ffmpeg():
    async with _av_lock:
        await _stop_av_ffmpeg_locked()


async def _stop_av_ffmpeg_locked():
    """Actual stop — caller must hold _av_lock."""
    global av_ffmpeg_proc, _av_pump_task, _av_stopping
    _av_stopping = True
    if _av_pump_task:
        _av_pump_task.cancel()
        _av_pump_task = None
    # Stop forwarding first so no more packets hit the (closing) ffmpeg.
    if video_proto:
        video_proto.forward_av = False
    if audio_proto:
        audio_proto.forward_av = False
    if av_ffmpeg_proc:
        proc = av_ffmpeg_proc
        av_ffmpeg_proc = None
        loop = asyncio.get_event_loop()
        # Un SIGTERM solo non basta, e costava tre secondi tondi a ogni
        # chiusura: ffmpeg lo ignora se è già oltre l'inizializzazione
        # (decode_interrupt_cb confronta received_nb_signals con
        # transcode_init_done), e il thread che legge l'SDP resta fermo nella
        # poll dell'UDP — che non si sveglierà mai, perché l'inoltro dei
        # pacchetti l'abbiamo appena spento due righe sopra. Si restava
        # quindi ad aspettare la wait(3) e a finire sempre con il kill.
        #
        # Quei tre secondi non erano innocui: sono la finestra in cui
        # in_call è già falso ma call_ended non è ancora arrivato, ed è lì
        # che una chiamata nuova si prendeva gli azzeramenti della vecchia.
        try:
            proc.terminate()
            await asyncio.sleep(0.1)
            if proc.poll() is None:
                proc.terminate()   # il secondo fa scattare l'interrupt
            await loop.run_in_executor(None, proc.wait, 0.5)
        except Exception:
            pass
        if proc.poll() is None:
            # Non c'è niente da svuotare: l'uscita è una pipe che stiamo
            # abbandonando e l'istantanea si riscrive da sola.
            try:
                proc.kill()
                await loop.run_in_executor(None, proc.wait, 1)
            except Exception:
                pass
        _LOGGER.info("AV ffmpeg stopped")


async def _read_av_ffmpeg_stderr(proc=None):
    loop = asyncio.get_event_loop()
    # Si segue IL processo che ci è stato affidato, non la variabile globale:
    # dopo un riavvio il lettore vecchio finirebbe a leggere lo stderr del
    # nuovo, dividendosene le righe con il lettore giusto.
    proc = proc or av_ffmpeg_proc
    while proc and proc.poll() is None:
        try:
            line = await loop.run_in_executor(None, proc.stderr.readline)
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            lowered = text.lower()
            benign = (
                # Il decodificatore che nasconde un pacchetto perso dal relay.
                any(c in lowered for c in DECODER_CONCEALMENT)
                # È così che finisce ogni input SDP quando la chiamata si
                # chiude e l'RTP smette di arrivare: non è un guasto.
                or "error during demuxing: operation timed out" in lowered
                or "no filtered frames for output stream" in lowered
                # "Immediate exit requested" è la parola che ffmpeg usa per
                # dire "mi avete chiesto voi di uscire". Ogni chiusura ne
                # produceva otto righe a WARNING nel log di Home Assistant —
                # muxer, trailer, file — tutte per una chiusura regolare.
                or "immediate exit requested" in lowered
                # Chiamata senza video: il muxer MPEG-TS lo dice alla prima
                # partenza dell'audio, e non cambia niente di quel che esce.
                or "poorly interleaved" in lowered
                # Le stesse righe, quando escono senza la frase sopra ma per
                # lo stesso motivo: si guarda se siamo noi ad aver chiuso.
                or (_av_stopping and (
                    "error muxing a packet" in lowered
                    or "error writing trailer" in lowered
                    or "error closing file" in lowered
                    or "task finished with error code" in lowered
                ))
            )
            if not benign and any(
                word in lowered for word in ("error", "failed", "invalid", "bind", "unable")
            ):
                # Il buffer interno è a DEBUG e in Home Assistant non arriva:
                # i guasti di ffmpeg sono rimasti invisibili per ore.
                _LOGGER.warning("AV ffmpeg: %s", text)
            else:
                _LOGGER.debug("AV ffmpeg: %s", text)
        except Exception:
            break
