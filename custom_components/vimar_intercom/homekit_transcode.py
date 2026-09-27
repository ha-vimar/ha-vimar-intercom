"""Il video ricodificato sul Pi: più fluido, a qualche decimo di prezzo.

Il video diretto consegna al telefono i pacchetti della targa così come
arrivano dal relay. Quando il relay ne perde uno — succede, due o tre su cento
— il telefono non riesce più a ricostruire l'immagine: si ferma, chiede un
keyframe che la targa ignora, e aspetta il suo successivo, fino a tre secondi.
Misurato il 26 settembre: 13 richieste di keyframe in un'apertura con una
macchina che passava, e tutte le perdite a monte, nessuna sul Wi-Fi.

Qui il video della targa lo decodifica ffmpeg, che un pacchetto perso lo
nasconde — per un attimo si vede una sbavatura, ma l'immagine continua a
muoversi — e lo ricodifica con un keyframe ogni secondo: il peggio che può
capitare al telefono è un secondo, non tre. Costa un po' di CPU (320x240 è
niente per un Pi 5) e qualche decina di millisecondi.

Un solo ricodificatore per chiamata, condiviso fra tutte le viste: parte con
la chiamata — o con lo squillo, quando c'è il media in anticipo — e ogni vista
che si apre attinge a un flusso già caldo.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket

from .homekit_audio import _Datagrams, ffmpeg_binary, free_udp_port, is_rtcp, log_stderr, port_bound

_LOGGER = logging.getLogger(__name__)

# Un keyframe al secondo: il peggio dopo una perdita è un secondo, non tre.
KEYFRAME_SECONDS = 1.0
KEYFRAME_INTERVAL = 15            # 1 s a 15 fotogrammi al secondo
# La cadenza della targa (320x240 a 15 fotogrammi al secondo, misurata).
PANEL_FPS = 15
# Quanto aspettare che ffmpeg apra la porta d'ingresso.
BIND_TIMEOUT = 3.0


def _nal_types(packet: bytes) -> list[int]:
    """I NAL di un pacchetto RTP H.264, dentro STAP-A compresi."""
    hlen = 12 + (packet[0] & 0x0F) * 4
    if len(packet) <= hlen:
        return []
    payload = packet[hlen:]
    t = payload[0] & 0x1F
    if t == 24:
        out, i = [], 1
        while i + 2 < len(payload):
            size = int.from_bytes(payload[i:i + 2], "big")
            out.append(payload[i + 2] & 0x1F)
            i += 2 + size
        return out
    if t == 28 and len(payload) >= 2:
        return [payload[1] & 0x1F]
    return [t]


class EncodedGop:
    """L'ultimo gruppo uscito dal ricodificatore, dall'SPS in poi.

    Il ricodificatore rimette SPS e PPS davanti a ogni keyframe
    (``repeat-headers``), quindi il gruppo comincia sempre da un pacchetto
    che porta l'SPS, e lì lo si riparte.
    """

    def __init__(self) -> None:
        self.packets: list[bytes] = []
        self._idr_open = False
        self.has_keyframe = False

    def add(self, packet: bytes) -> None:
        types = _nal_types(packet)
        if 7 in types:
            self.packets = []
            self.has_keyframe = False
            self._idr_open = False
        self.packets.append(packet)
        if self.has_keyframe or 5 not in types:
            return
        hlen = 12 + (packet[0] & 0x0F) * 4
        payload = packet[hlen:]
        if (payload[0] & 0x1F) == 28:
            fu = payload[1]
            if fu & 0x80:
                self._idr_open = True
            if fu & 0x40 and self._idr_open:
                self.has_keyframe = True
        else:
            self.has_keyframe = True


class Transcoder:
    """ffmpeg che decodifica la targa e ricodifica per i telefoni."""

    def __init__(self, sps: bytes | None, pps: bytes | None) -> None:
        self._sps, self._pps = sps, pps
        self._in_port = free_udp_port()
        self._sdp = f"/tmp/vimar_intercom_transcode_{self._in_port}.sdp"  # noqa: S108
        self._feed = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._feed.setblocking(False)
        self._out_tr: asyncio.DatagramTransport | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._sinks: list = []
        self._ready = False
        self.gop = EncodedGop()
        self.stats = {"in": 0, "out": 0}

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self, backlog_fn, on_ready=None) -> bool:
        """Avvia ffmpeg e, appena ascolta, gli dà il gruppo della targa.

        ``backlog_fn`` restituisce ``(prefisso, gruppo)`` dei pacchetti della
        targa già in memoria: senza, il ricodificatore non avrebbe un keyframe
        da cui partire fino al successivo della targa, tre secondi dopo.
        """
        loop = asyncio.get_running_loop()
        self._out_tr, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self._from_encoder), local_addr=("127.0.0.1", 0))
        out_port = self._out_tr.get_extra_info("sockname")[1]
        sprop = ""
        if self._sps and self._pps:
            sprop = (";sprop-parameter-sets="
                     f"{base64.b64encode(self._sps).decode()},"
                     f"{base64.b64encode(self._pps).decode()}")
        # La cadenza va dichiarata: l'SPS della targa non porta informazioni di
        # tempo, e senza ffmpeg presume 90000 fotogrammi al secondo — l'orologio
        # RTP — e il codificatore spalma i 300 kbit su novantamila fotogrammi:
        # tre bit ciascuno, cioè una macchia grigia. Visto il 26 settembre.
        sdp = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Vimar transcode\r\n"
               "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
               f"m=video {self._in_port} RTP/AVP 96\r\n"
               "a=rtpmap:96 H264/90000\r\n"
               f"a=framerate:{PANEL_FPS}\r\n"
               f"a=fmtp:96 packetization-mode=1{sprop}\r\n")
        await loop.run_in_executor(None, _write, self._sdp, sdp)
        self._proc = await asyncio.create_subprocess_exec(
            ffmpeg_binary(), "-hide_banner", "-nostats", "-loglevel", "warning",
            "-protocol_whitelist", "file,udp,rtp",
            "-analyzeduration", "0", "-probesize", "32",
            # Un decimo di secondo per rimettere in ordine i pacchetti che il
            # relay consegna scambiati, poi si va avanti: di più sarebbe ritardo.
            "-max_delay", "100000",
            "-i", self._sdp,
            "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-profile:v", "baseline", "-pix_fmt", "yuv420p",
            # Un fotogramma in uscita per ognuno in ingresso: senza, ffmpeg non
            # conosce la cadenza della targa, ne presume una più alta e
            # duplica fotogrammi per riempirla — il doppio dei bit per niente.
            "-fps_mode", "passthrough",
            # Keyframe a tempo, non a conteggio: la cadenza della targa oscilla.
            "-force_key_frames", f"expr:gte(t,n_forced*{KEYFRAME_SECONDS})",
            "-g", str(KEYFRAME_INTERVAL * 2), "-sc_threshold", "0", "-bf", "0",
            "-b:v", "300k", "-maxrate", "450k", "-bufsize", "300k",
            # Un solo slice per fotogramma, un solo thread: la forma consueta per
            # HomeKit (zerolatency altrimenti taglia ogni fotogramma in tre), e a
            # 320x240 un core basta e avanza.
            # E la si ripete al codificatore, che ci regola i bit per fotogramma.
            "-threads", "1",
            "-x264-params", f"repeat-headers=1:slices=1:fps={PANEL_FPS}",
            "-payload_type", "96", "-f", "rtp",
            f"rtp://127.0.0.1:{out_port}?pkt_size=1200",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        # Il decodificatore segnala ogni pacchetto perso che nasconde: è il suo
        # lavoro, non un guasto, e con il relay che ne perde due su cento
        # inonderebbe il log di Home Assistant.
        self._tasks.append(asyncio.create_task(
            log_stderr(self._proc, "ffmpeg ricodifica", concealment_is_normal=True)))

        # ffmpeg apre la porta solo dopo aver letto l'SDP: quello che arriva
        # prima è perso. Si aspetta di vederla occupata.
        waited = 0.0
        while not port_bound(self._in_port) and waited < BIND_TIMEOUT:
            if not self.running:
                return False
            await asyncio.sleep(0.02)
            waited += 0.02
        # Niente await da qui all'aggancio: il gruppo e il dal vivo devono
        # combaciare senza un pacchetto perso o doppio.
        prefix, gop = backlog_fn()
        for pkt in list(prefix) + list(gop):
            self._send(pkt)
        self._ready = True
        if on_ready:
            on_ready()          # l'aggancio al dal vivo, nello stesso istante
        _LOGGER.info("Ricodifica avviata (%d pacchetti della targa per partire)",
                     len(prefix) + len(gop))
        return True

    def feed(self, packet: bytes) -> None:
        """Un pacchetto della targa, dal vivo."""
        if self._ready:
            self._send(packet)

    def _send(self, packet: bytes) -> None:
        try:
            self._feed.sendto(packet, ("127.0.0.1", self._in_port))
            self.stats["in"] += 1
        except OSError:
            pass

    def _from_encoder(self, data: bytes, _addr) -> None:
        if len(data) < 12 or is_rtcp(data):
            return
        self.stats["out"] += 1
        self.gop.add(data)
        for sink in tuple(self._sinks):
            try:
                sink(data)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Inoltro del video ricodificato")

    def add_sink(self, sink) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    async def stop(self) -> None:
        self._ready = False
        self._sinks.clear()
        for task in self._tasks:
            task.cancel()
        if self.running:
            self._proc.terminate()
            await asyncio.sleep(0.1)
            if self._proc.returncode is None:
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 2.0)
            except (TimeoutError, asyncio.TimeoutError):
                self._proc.kill()
                await self._proc.wait()
        if self._out_tr:
            self._out_tr.close()
        self._feed.close()
        await asyncio.get_running_loop().run_in_executor(None, _unlink, self._sdp)
        _LOGGER.info("Ricodifica fermata: %d pacchetti della targa in, %d ricodificati fuori",
                     self.stats["in"], self.stats["out"])


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
