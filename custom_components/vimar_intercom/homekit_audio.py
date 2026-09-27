"""Il ponte audio fra il telefono e il citofono, nei due sensi.

Separato dall'accessorio perché non dipende da Home Assistant né da pyhap:
si prova da solo, con ffmpeg vero e pacchetti SRTP veri.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
import os
import shutil
import socket
import struct

from .srtp import SRTCPContext, SRTPContext

_LOGGER = logging.getLogger(__name__)

# ffmpeg timbra l'Opus in RTP a 48 kHz qualunque sia la frequenza vera (RFC
# 7587); HomeKit invece si aspetta l'orologio alla frequenza negoziata. Si
# converte nei due sensi. È la stessa correzione del proxy di Home Assistant.
OPUS_RTP_CLOCK = 48000
# Payload type con cui l'audio del telefono arriva al nostro decodificatore:
# lo fissiamo noi, così l'SDP del decodificatore si scrive prima di sapere
# cosa sceglierà il telefono.
TALK_PT = 110
# 20 ms di PCM a 8 kHz, 16 bit mono: un pacchetto RTP del posto esterno.
PCM_FRAME = 320


# ─── Utilità RTP ────────────────────────────────────────────────────


def rescale_timestamp(packet: bytes, num: int, den: int) -> bytearray:
    """L'RTP con il timestamp portato da un orologio a un altro (× num/den)."""
    out = bytearray(packet)
    ts = struct.unpack_from("!I", out, 4)[0]
    struct.pack_into("!I", out, 4, (ts * num // den) & 0xFFFFFFFF)
    return out


def set_payload_type(packet: bytearray, pt: int) -> bytearray:
    """Cambia il payload type lasciando stare il bit di marker."""
    packet[1] = (packet[1] & 0x80) | (pt & 0x7F)
    return packet


def is_rtcp(packet: bytes) -> bool:
    """RTCP multiplexato sulla stessa porta dell'RTP (RFC 5761)."""
    return len(packet) >= 2 and 200 <= packet[1] <= 206


def free_udp_port() -> int:
    """Una porta UDP libera adesso: la chiuderà e riaprirà ffmpeg."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("0.0.0.0", 0))  # noqa: S104
        return sock.getsockname()[1]


def ffmpeg_binary() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


class _Datagrams(asyncio.DatagramProtocol):
    """Un endpoint UDP che passa ogni pacchetto a una funzione."""

    def __init__(self, on_packet) -> None:
        self._on_packet = on_packet

    def datagram_received(self, data: bytes, addr) -> None:
        self._on_packet(data, addr)

    def error_received(self, exc: Exception) -> None:
        # ICMP «porta irraggiungibile» e simili: sul loopback, mentre un
        # ffmpeg si alza o si spegne, sono normali e non vanno in log.
        pass


# ─── Il ponte audio: le due direzioni su una porta sola ────────────


class AudioBridge:
    """La voce nei due sensi, sulla porta che abbiamo dichiarato al telefono.

    Un socket solo verso il telefono, perché l'RTP simmetrico lo vuole: da lì
    parte l'audio della strada, e lì arriva la voce di chi risponde.

    - strada → telefono: ffmpeg codifica in Opus e manda RTP in chiaro su un
      socket locale; qui si corregge l'orologio, si cifra e si spedisce.
    - telefono → strada: si decifra, si rimette l'orologio a 48 kHz, si passa
      a un secondo ffmpeg che decodifica in PCM a 8 kHz, e il PCM va al posto
      esterno con ``on_pcm`` — cioè ``media.send_audio``.
    """

    def __init__(self, phone_sock: socket.socket, phone_addr: tuple[str, int],
                 srtp_key_b64: str, rate_hz: int, on_pcm) -> None:
        self._phone_sock = phone_sock
        self._phone_addr = phone_addr
        self._rate = rate_hz
        self._on_pcm = on_pcm
        # Due contesti: il contatore di rollover è per direzione.
        self._tx = SRTPContext(srtp_key_b64)
        self._rx = SRTPContext(srtp_key_b64)
        self._tx_rtcp = SRTCPContext(srtp_key_b64)
        # L'SSRC lo sceglie ffmpeg (-ssrc): lo si impara dal primo pacchetto.
        self._ssrc: int | None = None
        self._phone_tr: asyncio.DatagramTransport | None = None
        self._enc_tr: asyncio.DatagramTransport | None = None
        self._talk_tr: asyncio.DatagramTransport | None = None
        self._enc_sender = None
        self._talk_port = 0
        self._talk_sdp = ""
        self._talk_proc: asyncio.subprocess.Process | None = None
        self._talk_ready = False
        self._talk_starting = False
        self._talk_pending: deque[bytes] = deque(maxlen=100)
        self._stopping = False
        self._tasks: list[asyncio.Task] = []
        self.encoder_port = 0
        self.stats = {"to_phone": 0, "from_phone": 0, "auth_fail": 0, "pcm_frames": 0}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._phone_tr, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self._from_phone), sock=self._phone_sock)
        self._enc_tr, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self._from_encoder), local_addr=("127.0.0.1", 0))
        self.encoder_port = self._enc_tr.get_extra_info("sockname")[1]
        self._talk_tr, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(lambda *_: None), local_addr=("127.0.0.1", 0))
        self._talk_port = free_udp_port()
        # Il decodificatore della voce parte con la prima voce, non prima: un
        # ffmpeg che legge RTP e non riceve niente per dieci secondi esce da
        # solo («Operation timed out»), e chi apriva la vista e aspettava un
        # po' prima di premere «Parla» non veniva più sentito dalla strada.

    def _deliver_voice(self, packet: bytes) -> None:
        """La voce al decodificatore, che si (ri)avvia se non c'è."""
        if self._talk_ready and self._talk_tr:
            self._talk_tr.sendto(packet, ("127.0.0.1", self._talk_port))
            return
        # Nel frattempo la si tiene da parte: la prima sillaba non si perde.
        self._talk_pending.append(packet)
        if not self._talk_starting and not self._stopping:
            self._talk_starting = True
            self._tasks.append(asyncio.get_running_loop().create_task(
                self._start_talk_decoder()))

    async def _start_talk_decoder(self) -> None:
        try:
            await self._launch_talk_decoder()
        finally:
            self._talk_starting = False

    async def _launch_talk_decoder(self) -> None:
        self._talk_sdp = f"/tmp/vimar_intercom_talk_{self._talk_port}.sdp"  # noqa: S108
        sdp = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 127.0.0.1\r\n"
            "s=Vimar talkback\r\n"
            "c=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\n"
            f"m=audio {self._talk_port} RTP/AVP {TALK_PT}\r\n"
            f"a=rtpmap:{TALK_PT} opus/48000/2\r\n"
        )
        await asyncio.get_running_loop().run_in_executor(
            None, _write_text, self._talk_sdp, sdp)
        self._talk_proc = await asyncio.create_subprocess_exec(
            ffmpeg_binary(), "-hide_banner", "-nostats", "-loglevel", "warning",
            "-protocol_whitelist", "file,udp,rtp",
            # Il codec è dichiarato nell'SDP: niente da analizzare, e ogni
            # decimo qui è un decimo di ritardo sulla voce di chi risponde.
            "-analyzeduration", "0", "-probesize", "32",
            # Un pacchetto perso non deve fermare la voce: ffmpeg lo aspetta
            # 100 ms per impostazione, 40 bastano per quelli solo in ritardo.
            "-max_delay", "40000",
            "-i", self._talk_sdp,
            "-f", "s16le", "-ac", "1", "-ar", "8000", "pipe:1",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc = self._talk_proc
        self._tasks.append(asyncio.create_task(self._pump_pcm()))
        self._tasks.append(asyncio.create_task(
            log_stderr(proc, "ffmpeg voce", timeout_is_normal=True)))
        # Pronto quando ascolta: allora gli si dà quello tenuto da parte.
        waited = 0.0
        while not port_bound(self._talk_port) and waited < 3.0:
            if proc.returncode is not None or self._stopping:
                return
            await asyncio.sleep(0.02)
            waited += 0.02
        self._talk_ready = True
        while self._talk_pending and self._talk_tr:
            self._talk_tr.sendto(self._talk_pending.popleft(),
                                 ("127.0.0.1", self._talk_port))
        self._tasks.append(asyncio.create_task(self._watch_talk_decoder(proc)))

    async def _watch_talk_decoder(self, proc) -> None:
        """Quando esce (silenzio lungo), al prossimo «Parla» si riparte."""
        await proc.wait()
        if self._talk_proc is proc:
            self._talk_ready = False
            self._talk_proc = None
            if not self._stopping:
                _LOGGER.debug("HomeKit: decodificatore della voce fermo "
                              "(nessuna voce da un po'), ripartirà alla prossima")

    def _from_encoder(self, data: bytes, addr) -> None:
        """Strada → telefono: dall'ffmpeg che codifica, al telefono cifrato."""
        if len(data) < 12 or is_rtcp(data) or not self._phone_tr:
            return
        # Solo il nostro ffmpeg: il primo che scrive su questo socket.
        if self._enc_sender is None:
            self._enc_sender = addr
        elif addr != self._enc_sender:
            return
        if self._ssrc is None:
            self._ssrc = struct.unpack_from("!I", data, 8)[0]
        packet = rescale_timestamp(data, self._rate, OPUS_RTP_CLOCK)
        self._phone_tr.sendto(self._tx.protect(bytes(packet)), self._phone_addr)
        self.stats["to_phone"] += 1

    def _from_phone(self, data: bytes, addr) -> None:
        """Telefono → strada: la voce di chi risponde."""
        if addr[0] != self._phone_addr[0] or len(data) < 12 or is_rtcp(data):
            return
        plain = self._rx.unprotect(data)
        if plain is None:
            self.stats["auth_fail"] += 1
            if self.stats["auth_fail"] in (1, 50):
                _LOGGER.warning("HomeKit: audio dal telefono che non si decifra "
                                "(%d pacchetti)", self.stats["auth_fail"])
            return
        if self.stats["from_phone"] == 0:
            _LOGGER.info("HomeKit: arriva la voce dal telefono (%dB)", len(data))
        self.stats["from_phone"] += 1
        packet = set_payload_type(
            rescale_timestamp(plain, OPUS_RTP_CLOCK, self._rate), TALK_PT)
        self._deliver_voice(bytes(packet))

    async def _pump_pcm(self) -> None:
        """Il PCM decodificato, a pacchetti da 20 ms, verso il posto esterno."""
        proc = self._talk_proc
        if proc is None:
            return
        if not proc or not proc.stdout:
            return
        buf = b""
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    return
                buf += chunk
                while len(buf) >= PCM_FRAME:
                    frame, buf = buf[:PCM_FRAME], buf[PCM_FRAME:]
                    self._on_pcm(frame)
                    self.stats["pcm_frames"] += 1
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.exception("HomeKit: errore inoltrando la voce al citofono")

    def send_bye(self) -> None:
        """RTCP BYE sull'audio: «questo flusso è finito»."""
        if self._phone_tr is not None and self._ssrc is not None:
            bye = struct.pack("!BBHI", 0x81, 203, 1, self._ssrc)
            self._phone_tr.sendto(self._tx_rtcp.protect(bye), self._phone_addr)

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        if self._talk_proc and self._talk_proc.returncode is None:
            self._talk_proc.kill()
            try:
                await asyncio.wait_for(self._talk_proc.wait(), 2.0)
            except (TimeoutError, asyncio.TimeoutError):
                pass
        for tr in (self._phone_tr, self._enc_tr, self._talk_tr):
            if tr:
                tr.close()
        if self._talk_sdp:
            await asyncio.get_running_loop().run_in_executor(
                None, _unlink, self._talk_sdp)
        _LOGGER.info("HomeKit: ponte audio chiuso — al telefono %d pacchetti, "
                     "dal telefono %d (%d non decifrati), %d fotogrammi di voce "
                     "al citofono", self.stats["to_phone"], self.stats["from_phone"],
                     self.stats["auth_fail"], self.stats["pcm_frames"])


def port_bound(port: int) -> bool:
    """Se qualcuno ha già aperto questa porta UDP sul loopback."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# Quello che il decodificatore H.264 dice quando nasconde un pacchetto perso.
DECODER_CONCEALMENT = ("error while decoding mb", "invalid level prefix", "concealing",
                "left block unavailable", "top block unavailable", "cbp too large",
                "negative number of zero coeffs", "out of range intra chroma",
                "corrupt decoded frame", "ac-tex damaged", "dquant out of range",
                "mb_type", "rtp: missed", "max delay reached", "no frame!",
                "non-existing pps", "decode_slice_header error")


async def log_stderr(proc: asyncio.subprocess.Process, label: str,
                     concealment_is_normal: bool = False,
                     timeout_is_normal: bool = False) -> None:
    """Quello che ffmpeg dice: a DEBUG, tranne i guasti veri."""
    if not proc.stderr:
        return
    closing = False
    try:
        while line := await proc.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            low = text.lower()
            # Dopo «immediate exit requested» è una chiusura chiesta da noi:
            # le righe che seguono (muxer, trailer, file) non sono guasti.
            closing = closing or "immediate exit requested" in low
            benign = (closing or "exiting normally" in low
                      or (concealment_is_normal and any(c in low for c in DECODER_CONCEALMENT))
                      or (timeout_is_normal and ("operation timed out" in low
                                                 or "no filtered frames" in low
                                                 or "output file is empty" in low)))
            if not benign and any(w in low for w in ("error", "failed", "invalid", "unable")):
                _LOGGER.warning("%s: %s", label, text)
            else:
                _LOGGER.debug("%s: %s", label, text)
    except asyncio.CancelledError:
        pass
