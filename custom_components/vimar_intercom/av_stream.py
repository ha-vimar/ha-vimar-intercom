"""Vimar Intercom — /av: RTP della chiamata → un ffmpeg → MPEG-TS per go2rtc, lo stream worker di HA e HomeKit."""

import asyncio
import base64
import collections
import contextlib
import logging
import os
import struct
import subprocess

from . import homekit_audio
from . import media_handler as media
from .const import DECODER_CONCEALMENT, FFMPEG_AV_AUDIO_PORT, FFMPEG_AV_VIDEO_PORT

_LOGGER = logging.getLogger(__name__)

av_ffmpeg_proc = None
_stderr_task: asyncio.Task | None = None
# Set by _stop_av_ffmpeg_locked: from then on ffmpeg's lines are about the exit
# we asked for (muxer, trailer, "Immediate exit requested"), not faults.
_av_stopping = False

# ffmpeg lines that are not faults: the decoder concealing a packet the relay
# lost, the jitter buffer giving up on a late one, and the input timing out
# when the call ends and RTP stops arriving.
_HARMLESS_STDERR = DECODER_CONCEALMENT + (
    "dropping old packet",
    "error during demuxing: operation timed out", "no filtered frames",
    "immediate exit requested", "poorly interleaved",
)
_FAULT_WORDS = ("error", "failed", "invalid", "bind", "unable")


def _stderr_is_fault(text: str) -> bool:
    """Whether an ffmpeg stderr line is a real fault, worth a WARNING."""
    low = text.lower()
    if any(h in low for h in _HARMLESS_STDERR):
        return False
    return any(w in low for w in _FAULT_WORDS)


class AvRtp:
    """RTP verso l'ffmpeg di /av come un flusso solo, continuo.

    Se la targa riparte (SSRC nuovo: encoder o relay riavviato) ffmpeg scarta
    ogni pacchetto «received too late» e /av resta fermo fino alla fine della
    chiamata. Qui SSRC, seq e timestamp ripartono da dove erano rimasti; il
    primo flusso passa invariato. Solo l'SSRC conta: una seq che torna indietro
    è un pacchetto vecchio rimandato (40515), non un riavvio."""

    def __init__(self, ts_step: int):
        self.ts_step = ts_step
        self.ssrc = self.out_ssrc = self.last = None
        self.head = None  # highest seq sent: `last` can be a late packet
        self.seq_off = self.ts_off = 0

    def follow(self, seq: int) -> None:
        """Number seq as the one after the newest packet sent. Alone (a jump on
        the same SSRC) it leaves the clock alone: ffmpeg syncs audio on it."""
        if self.head is not None:
            self.seq_off = (self.head + 1 - seq) & 0xFFFF

    def fix(self, rtp: bytes, pt: int) -> bytes:
        seq, ts, ssrc = struct.unpack_from('!HII', rtp, 2)
        if self.last is not None and ssrc != self.ssrc:
            self.follow(seq)
            self.ts_off = (self.last[1] + self.ts_step - ts) & 0xFFFFFFFF
        if self.out_ssrc is None:
            self.out_ssrc = ssrc
        self.ssrc = ssrc
        seq, ts = (seq + self.seq_off) & 0xFFFF, (ts + self.ts_off) & 0xFFFFFFFF
        self.last = (seq, ts)
        if self.head is None or 0 < (seq - self.head) & 0xFFFF < 0x8000:
            self.head = seq
        return (rtp[:1] + bytes([(rtp[1] & 0x80) | pt])
                + struct.pack('!HII', seq, ts, self.out_ssrc) + rtp[12:])


# The private temp file holding ffmpeg's input SDP (homekit_audio.write_sdp:
# 0600, unpredictable name). Removed when ffmpeg is stopped.
_av_sdp_file: str | None = None

# How long ffmpeg stays up after the last /av client left, while the call's
# video is still coming: HA's stream worker and go2rtc reconnect within seconds,
# and they find the process running instead of paying for a new one. The end
# of the call (or of its video) still stops it at once (stop_av_ffmpeg).
AV_IDLE_GRACE = 10.0
_idle_stop: asyncio.Task | None = None

# Where the kernel lists the bound UDP sockets (see _bound_udp_ports).
_PROC_NET_UDP = ("/proc/net/udp", "/proc/net/udp6")
# ffmpeg's RTP input ports, where the forwarding sends.
_AV_INPUT_PORTS = frozenset({FFMPEG_AV_VIDEO_PORT, FFMPEG_AV_AUDIO_PORT})
# ffmpeg used to get a fixed 0.3 s to bind its ports. The wait now ends as
# soon as they are bound, and gives up after this long.
FFMPEG_LISTEN_TIMEOUT = 0.5
_FFMPEG_LISTEN_FALLBACK = 0.3

# Serializza start/stop dell'ffmpeg AV: due /av concorrenti non devono
# lanciare due processi che si contendono le stesse porte UDP.
_av_lock = asyncio.Lock()


def _write_av_sdp():
    """Write the ffmpeg input SDP to disk (blocking — run in executor).

    PT/porte devono combaciare con ciò che RTPVideo/RTPAudioProtocol
    inoltrano su 127.0.0.1 (FFMPEG_AV_VIDEO_PORT / FFMPEG_AV_AUDIO_PORT).
    """
    # Con SPS/PPS già visti ffmpeg decodifica dal primo IDR, invece di
    # aspettarne uno che li porti in banda (sulla 40515 ~8 s, oltre la chiamata).
    sprop = ""
    if media.video_proto and (ps := media.video_proto.sps_pps()):
        sprop = ";sprop-parameter-sets=" + ",".join(base64.b64encode(n).decode() for n in ps)
    sdp = (
        "v=0\r\n"
        "o=- 0 0 IN IP4 127.0.0.1\r\n"
        "s=AV\r\n"
        "c=IN IP4 127.0.0.1\r\n"
        "t=0 0\r\n"
        f"m=audio {FFMPEG_AV_AUDIO_PORT} RTP/AVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        f"m=video {FFMPEG_AV_VIDEO_PORT} RTP/AVP 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        f"a=fmtp:96 profile-level-id=42801F;packetization-mode=1{sprop}\r\n"
    )
    return homekit_audio.write_sdp("vimar_intercom_av_", sdp)


def _seed_silence(audio_proto) -> None:
    """3 pacchetti PCMU di silenzio (60 ms) subito dopo l'avvio: l'encoder AAC non
    parte, e con lui l'uscita di ffmpeg, finché non riceve il primo audio, e se la
    targa non ne manda (anteprima 183 muta, SRTP audio che fallisce) /av restava a
    0 byte. Passano da av_rtp: l'audio vero poi continua da qui (SSRC nuovo)."""
    for i in range(3):
        rtp = struct.pack("!BBHII", 0x80, 0, i, i * 160, 0) + media.SILENCE_ULAW
        try:
            audio_proto.ffmpeg_av_sock.sendto(
                audio_proto.av_rtp.fix(rtp, 0), ("127.0.0.1", FFMPEG_AV_AUDIO_PORT))
        except OSError:
            pass


def _bound_udp_ports() -> set[int] | None:
    """Local ports of the UDP sockets bound on this host (network namespace).

    Read from /proc/net/udp{,6} (blocking: run in an executor), None where it
    does not exist. Reading is used instead of trying to bind the port
    ourselves: a probe that holds the port for an instant can be the reason
    ffmpeg's own bind fails.
    """
    ports: set[int] = set()
    found = False
    for path in _PROC_NET_UDP:
        try:
            with open(path, encoding="ascii", errors="replace") as f:
                next(f, None)  # header
                for line in f:
                    fields = line.split()
                    if len(fields) > 1 and ":" in fields[1]:
                        with contextlib.suppress(ValueError):
                            ports.add(int(fields[1].rsplit(":", 1)[1], 16))
            found = True
        except OSError:
            continue
    return ports if found else None


async def _wait_until_ffmpeg_listens(proc, timeout: float = FFMPEG_LISTEN_TIMEOUT) -> bool:
    """Wait until ffmpeg has bound its UDP input ports; True if it did.

    RTP sent to a port nobody has bound yet is lost, and the first packets can
    be the IDR the picture starts from. A fixed 0.3 s was usually too long and,
    on a loaded machine, sometimes too short. Where the bound ports cannot be
    read, the old fixed wait is kept; on timeout, or if ffmpeg exits, the
    caller goes on as before (forward, or report the exit).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while proc.poll() is None:
        bound = await loop.run_in_executor(None, _bound_udp_ports)
        if bound is None:
            await asyncio.sleep(_FFMPEG_LISTEN_FALLBACK)
            return False
        if _AV_INPUT_PORTS <= bound:
            return True
        if loop.time() >= deadline:
            _LOGGER.debug("AV ffmpeg: input ports not bound after %.1f s", timeout)
            return False
        await asyncio.sleep(0.01)
    return False


# An ffmpeg whose starter was cancelled (an /av client leaving, the passive
# decoder stopped) being killed. The next start waits for it: until it exits
# it holds the UDP ports, and the new one fails with "bind failed".
_reap_task: asyncio.Task | None = None


async def _reap(spawn: asyncio.Future) -> None:
    """Kill the ffmpeg `spawn` starts and wait until it is gone."""
    loop = asyncio.get_running_loop()
    # Anything: a failed Popen (nothing to kill), an executor already shut
    # down (HA stopping).
    with contextlib.suppress(Exception):
        proc = await spawn
        proc.kill()
        try:
            await loop.run_in_executor(None, proc.wait, 5)  # under _av_lock: never forever
        finally:
            loop.run_in_executor(None, _close_av_pipes, proc)


async def _start_av_ffmpeg_locked():
    """Start ffmpeg that reads H264+PCMU RTP and outputs MPEG-TS to pipe.

    Il chiamante deve tenere _av_lock. Ferma l'istanza precedente
    (attendendone la terminazione, così le porte UDP si liberano prima del
    nuovo bind), scrive l'SDP in executor, poi abilita il forward RTP verso
    ffmpeg SOLO dopo lo start.
    """
    global av_ffmpeg_proc, _stderr_task, _av_stopping, _av_sdp_file, _reap_task
    if _reap_task and not _reap_task.done():
        # Shielded: a caller cancelled here must not cut the kill short.
        await asyncio.shield(_reap_task)
    await _stop_av_ffmpeg_locked()
    _av_stopping = False

    loop = asyncio.get_running_loop()
    try:
        sdp_path = _av_sdp_file = await loop.run_in_executor(None, _write_av_sdp)
    except Exception as e:
        _LOGGER.error("AV SDP write error: %s", e)
        return

    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-protocol_whitelist", "file,udp,rtp",
        "-fflags", "+genpts+discardcorrupt",
        # Senza, find_stream_info trattiene l'uscita ~1,9 s dopo il primo IDR: 20
        # fotogrammi per stimare gli fps, poi il first_dts che per l'H.264 arriva
        # solo dopo 7 fotogrammi decodificati (has_decode_delay_been_guessed).
        # Sul campo la targa chiude dopo ~10 s. Misurato: 1,88 s → 0,08 s.
        "-fpsprobesize", "0", "-max_ts_probe", "0",
        # Receive buffer of the RTP input. The forwarding reaches ffmpeg in bursts
        # on loopback, and with the default buffer packets were lost ("RTP:
        # missed N packets"), which broke the H.264 stream (fork, 0287148).
        "-buffer_size", "655360",
        # The forward reaches ffmpeg in arrival order. With the default 0.1 s
        # max_delay ffmpeg gave late packets up ("max delay reached") and
        # +discardcorrupt dropped the frame.
        "-max_delay", "300000", "-reorder_queue_size", "1024",
        "-i", sdp_path,
        "-c:v", "copy",
        # G.711 non è un codec valido in MPEG-TS: con «copy» finiva come dati
        # privati (bin_data) e lo stream worker di HA (HLS, camera.record) e
        # HomeKit non lo vedevano. AAC-LC mono a 32 kb/s, come nella PR #21
        # (@m4r1k). La frequenza non aggiunge nulla a una sorgente a 8 kHz ma
        # decide quanto il mux trattiene il primo pacchetto video: esce solo con
        # il primo AAC, e l'encoder ne dà uno dopo 2048 campioni (priming). A
        # 24 kHz sono 85 ms di audio, a 48 kHz 43. Misurato (test_av_latency):
        # primo fotogramma decodificabile +60 ms a 24 kHz, +10 ms a 48 kHz.
        "-c:a", "aac", "-b:a", "32k", "-ar", "48000", "-ac", "1",
        # Mux senza attese: niente ritardo iniziale (default 0,7 s), le due
        # tracce escono al massimo 0,1 s l'una dall'altra, ogni pacchetto è
        # scritto subito sulla pipe (PR #21). -muxpreload 0 drops the default
        # 0.5 s preload as well: both tracks come from the same call and share
        # its time base, so there is nothing to wait for.
        "-muxdelay", "0", "-muxpreload", "0",
        # The AAC encoder's first packet is timestamped -1024 samples (its
        # priming). When the panel's audio arrives before its video, as it
        # usually does, that is -1920 in MPEG-TS's 90 kHz clock, and the muxer
        # writes it wrapped: 2^33 - 1920, then 0. Home Assistant's stream
        # worker takes that as a timestamp discontinuity and restarts. 50 ms
        # keeps every timestamp positive; it shifts them, it holds nothing back.
        # Reproduced on loopback, with 1.0.14's flags too (not from -muxpreload 0).
        "-output_ts_offset", "0.05",
        "-max_interleave_delta", "100000", "-flush_packets", "1",
        "-f", "mpegts",
        "pipe:1",
    ]
    # Popen (fork + exec) fuori dall'event loop. Shielded: a cancelled caller
    # would lose the process otherwise; it is reaped instead (_reap_task),
    # here or while it binds its ports.
    spawn = loop.run_in_executor(
        None, lambda: subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    try:
        try:
            av_ffmpeg_proc = await asyncio.shield(spawn)
        except Exception as e:
            _LOGGER.error("AV ffmpeg start error: %s", e)
            av_ffmpeg_proc = None
            return

        proc = av_ffmpeg_proc
        stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        # Riferimento tenuto: un task senza riferimenti può essere raccolto a metà lettura.
        _stderr_task = stderr_task = asyncio.create_task(_read_av_ffmpeg_stderr(proc, stderr_tail))
        # RTP only once ffmpeg's UDP ports are bound: packets sent before are lost.
        await _wait_until_ffmpeg_listens(proc)
    except asyncio.CancelledError:
        av_ffmpeg_proc = None
        _reap_task = asyncio.create_task(_reap(spawn))
        raise
    if av_ffmpeg_proc and av_ffmpeg_proc.poll() is None:
        if media.video_proto:
            media.video_proto.av_rtp = AvRtp(3000)  # ffmpeg nuovo: flusso nuovo
            media.video_proto.forward_av = True
            media.video_proto.replay_gop()  # l'IDR arrivato prima non va perso
        if media.audio_proto:
            media.audio_proto.av_rtp = AvRtp(160)
            media.audio_proto.forward_av = True
            _seed_silence(media.audio_proto)
        _LOGGER.info("AV ffmpeg started (MPEG-TS output), RTP forwarding enabled")
    else:
        # Il motivo sta nello stderr (es. «bind failed» con porte che si
        # sovrappongono, issue #8): aspettiamo che il lettore arrivi a EOF
        # e lo riportiamo, invece di un errore muto.
        try:
            await asyncio.wait_for(stderr_task, timeout=2)
        except Exception:  # noqa: BLE001 — timeout o lettore fallito
            pass
        _LOGGER.error(
            "AV ffmpeg exited immediately during startup (rc=%s): %s",
            proc.poll(), " | ".join(stderr_tail) or "no stderr output",
        )


# Un solo ffmpeg per tutti i client di /av (go2rtc e lo stream worker di HA lo
# aprono insieme): il primo lo avvia, l'ultimo lo ferma (after AV_IDLE_GRACE while
# the call's video goes on), tutto sotto _av_lock.
_av_clients: set[asyncio.Queue] = set()
_av_pump: asyncio.Task | None = None


def end_client(q: asyncio.Queue, clients: set[asyncio.Queue]) -> None:
    """Stacca un client: None in coda = fine dello stream."""
    clients.discard(q)
    if q.full():
        q.get_nowait()
    q.put_nowait(None)


def fanout(chunk: bytes, clients: set[asyncio.Queue]) -> None:
    """Lo stesso pezzo a tutti i client; uno troppo lento va staccato, non si
    corrompe il TS (anche per av_passive)."""
    for q in list(clients):
        if q.full():
            end_client(q, clients)
        else:
            q.put_nowait(chunk)


async def _av_pump_run(proc) -> None:
    loop = asyncio.get_running_loop()
    try:
        while proc.poll() is None:
            chunk = await loop.run_in_executor(None, proc.stdout.read1, 4096)
            if not chunk:
                break
            fanout(chunk, _av_clients)
    except (OSError, ValueError) as e:
        _LOGGER.debug("AV pump ended: %s", e)
    finally:
        # _stop_av_ffmpeg_locked() stacca già i client per conto suo (vedi lì): non
        # aspetta questo pump, che può restare bloccato in read1() per secondi dopo il
        # kill. Se nel frattempo è ripartito un ffmpeg nuovo (_av_pump punta già a
        # un'altra pump) questa è quella vecchia: non deve toccare i client della nuova.
        if asyncio.current_task() is _av_pump:
            for q in list(_av_clients):
                end_client(q, _av_clients)


async def av_subscribe() -> asyncio.Queue | None:
    """Aggancia un client allo stream MPEG-TS; None se ffmpeg non parte."""
    global _av_pump
    async with _av_lock:
        # av_ffmpeg_proc is None: lo stop precedente ha già ucciso il processo, anche se
        # la pump vecchia non se n'è ancora accorta (bloccata in read1(), vedi sopra) —
        # non aspettarla per ripartire, o una chiamata veloce dopo l'altra resterebbe
        # agganciata a una pump morente invece che a un ffmpeg nuovo.
        _cancel_idle_stop()  # a client is back within the grace: keep ffmpeg
        if av_ffmpeg_proc is None or _av_pump is None or _av_pump.done():
            await _start_av_ffmpeg_locked()
            proc = av_ffmpeg_proc
            if not proc or proc.poll() is not None:
                return None
            _av_pump = asyncio.create_task(_av_pump_run(proc))
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        _av_clients.add(q)
        return q


def _call_video_live() -> bool:
    """The call's video is still coming (stop_media clears remote_addr)."""
    vp = media.video_proto
    return bool(vp and vp.remote_addr)


def _cancel_idle_stop() -> None:
    global _idle_stop
    if _idle_stop and _idle_stop is not asyncio.current_task():
        _idle_stop.cancel()
    _idle_stop = None


async def av_unsubscribe(q: asyncio.Queue) -> None:
    global _idle_stop
    async with _av_lock:
        _av_clients.discard(q)
        if _av_clients:
            return
        if av_ffmpeg_proc is not None and _call_video_live():
            # The last client left but the call goes on: a client reconnecting
            # within the grace finds ffmpeg running.
            _cancel_idle_stop()
            _idle_stop = asyncio.create_task(_stop_when_idle())
            return
        await _stop_av_ffmpeg_locked()


async def _stop_when_idle() -> None:
    """Stop ffmpeg if nobody came back to /av within AV_IDLE_GRACE."""
    await asyncio.sleep(AV_IDLE_GRACE)
    async with _av_lock:
        if _av_clients or asyncio.current_task() is not _idle_stop:
            return
        _LOGGER.info("AV: no client for %.0f s, stopping ffmpeg", AV_IDLE_GRACE)
        await _stop_av_ffmpeg_locked()


async def stop_av_ffmpeg():
    async with _av_lock:
        await _stop_av_ffmpeg_locked()


def _close_av_pipes(proc) -> None:
    """proc.stdout/stderr.close(), fuori dal loop. Se il thread che le legge (la pump,
    lo stderr reader) è ancora bloccato in una read1()/readline(), close() qui aspetta
    la stessa lock del BufferedReader e può restare ferma per secondi — misurato fino a
    ~11 s a testa. Farlo sul thread del loop bloccava tutto asyncio; qui no."""
    for pipe in (proc.stdout, proc.stderr):
        with contextlib.suppress(Exception):
            pipe.close()


async def _stop_av_ffmpeg_locked():
    """Actual stop — caller must hold _av_lock."""
    global av_ffmpeg_proc, _av_stopping, _av_sdp_file
    _cancel_idle_stop()
    _av_stopping = True
    # Stop forwarding first so no more packets hit the (closing) ffmpeg.
    if media.video_proto:
        media.video_proto.forward_av = False
    if media.audio_proto:
        media.audio_proto.forward_av = False
    if _av_sdp_file:
        # ffmpeg has read it by now; a failed start leaves it behind too.
        with contextlib.suppress(OSError):
            os.unlink(_av_sdp_file)
        _av_sdp_file = None
    if av_ffmpeg_proc:
        proc = av_ffmpeg_proc
        av_ffmpeg_proc = None
        # kill subito: l'uscita è una pipe, non c'è file da chiudere bene. Con
        # terminate + attesa la fine chiamata arrivava all'hub 3 s dopo il BYE.
        try:
            proc.kill()
            await asyncio.get_running_loop().run_in_executor(None, proc.wait, 2)
        except Exception:  # noqa: BLE001 — già uscito
            pass
        # I client non aspettano che la pump se ne accorga da sola (può restare bloccata
        # in read1() per secondi dopo il kill, vedi _close_av_pipes): staccati subito.
        for q in list(_av_clients):
            end_client(q, _av_clients)
        _LOGGER.info("AV ffmpeg stopped")
        # In background: può bloccare per secondi (vedi sopra), mai sul thread del loop.
        # ponytail: fire-and-forget voluto (vedi docstring); a raffica di riconnessioni può occupare
        # thread dell executor per secondi: un semaforo se succede davvero.
        asyncio.get_running_loop().run_in_executor(None, _close_av_pipes, proc)


async def _read_av_ffmpeg_stderr(proc, tail: collections.deque[str]):
    """Legge lo stderr di ffmpeg fino a EOF, tenendone la coda in `tail`.

    Legato al processo passato, non al globale, e senza guardare `poll()`:
    prima il ciclo usciva appena il processo era già morto, cioè proprio nel
    caso in cui lo stderr spiega il perché (issue #8).
    """
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, proc.stderr.readline)
        except Exception:  # noqa: BLE001
            break
        if not line:
            break
        text = line.decode(errors="replace").strip()
        if text:
            tail.append(text)
            # A fault at WARNING, so it reaches the Home Assistant log; the
            # rest at DEBUG. Once we asked it to stop (or started another),
            # everything this process says is about that exit.
            stopping = _av_stopping or proc is not av_ffmpeg_proc
            if not stopping and _stderr_is_fault(text):
                _LOGGER.warning("AV ffmpeg: %s", text)
            else:
                _LOGGER.debug("AV ffmpeg: %s", text)
