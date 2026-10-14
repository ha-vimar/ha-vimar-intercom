"""/av?autocall=0&idle=image: un MPEG-TS continuo per Scrypted, go2rtc e Frigate.

A riposo il fotogramma di standby (standby.png), durante squillo o chiamata il video
della targa. Un solo libx264 (640x480, 10 fps, IDR ogni secondo, baseline) per tutti i
client: nato col primo, fermato con l'ultimo, così i parametri del codec non cambiano
mai a metà stream. Python gli passa i fotogrammi grezzi a ritmo fisso: lo standby, o
l'ultimo decodificato dalla targa. Il live arriva dal fan-out MPEG-TS di av_stream
(come un client passivo qualunque) e lo decodifica un secondo ffmpeg, vivo solo finché
c'è video. Niente chiamate né spettatori per l'hub: il passivo non tocca la targa.
L'audio (AAC 48 kHz mono come /av) viaggia nello stesso TS: il PCM della targa durante
squillo o chiamata (media.pcm_taps, lo stesso PCMU decodificato per la card), silenzio
a riposo, sempre 100 ms per fotogramma dallo stesso orologio: video e audio in passo.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
from collections.abc import Callable

from . import av_stream
from . import media_handler as media

_LOGGER = logging.getLogger(__name__)

W, H, FPS = 640, 480, 10
_FRAME = W * H * 3 // 2  # yuv420p
AR = 8000                    # PCM della targa: PCMU decodificato, 16 bit mono
_CHUNK = AR * 2 // FPS       # byte di audio per fotogramma (100 ms)
_SILENCE = bytes(_CHUNK)
_pcm = bytearray()           # PCM live in attesa dell'encoder
_SCALE = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:-1:-1"
# Answering a cloud ring closes the ring before the 200 OK goes out and sets
# in_call only after it: the video looks gone for a moment while it goes on.
_DEAD_GRACE = 1.0  # s without video before the decoder is stopped
# A decoder restarted in a loop asked for a keyframe each time: one per this many s.
_KEYFRAME_EVERY = 3.0
_STANDBY_PNG = os.path.join(os.path.dirname(__file__), "standby.png")

_standby: bytes | None = None
_live: bytes | None = None          # ultimo fotogramma della targa, None a riposo
_clients: set[asyncio.Queue] = set()
_lock = asyncio.Lock()
_task: asyncio.Task | None = None
_enc = None  # l'encoder di _task: se il task è cancellato prima di partire, _run non lo chiude


async def standby_frame() -> bytes:
    """Lo standby in yuv420p WxH, reso una volta da standby.png e tenuto in memoria."""
    global _standby
    if _standby is None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", _STANDBY_PNG, "-frames:v", "1",
            "-vf", _SCALE, "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out, _ = await proc.communicate()
        if len(out) != _FRAME:
            raise RuntimeError(f"standby.png: ffmpeg ha reso {len(out)} byte, attesi {_FRAME}")
        _standby = out
    return _standby


async def subscribe(is_live: Callable[[], bool], on_live: Callable[[], None]) -> asyncio.Queue | None:
    """Aggancia un client; None se ffmpeg non parte. `is_live`: c'è video della targa
    (hub.video_active); `on_live`: chiesto un keyframe quando il decoder si aggancia."""
    global _task, _enc
    async with _lock:
        # done(): l'encoder è morto da solo e _run ha chiuso i client; senza questo un
        # client nuovo non avrebbe più un encoder e resterebbe a 0 byte.
        if _task is None or _task.done():
            try:
                standby = await standby_frame()
                # L'audio entra da una porta TCP locale (un secondo pipe non c'è su ogni
                # piattaforma), per primo: ffmpeg apre gli ingressi in ordine e aspetta la
                # connessione; il video dal pipe subito dopo. Stesso orologio per entrambi.
                port = _free_port()
                enc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-loglevel", "error",
                    # probesize/analyzeduration minimi: se no ffmpeg aspetta più audio di
                    # quanto ne mandiamo prima di aprire il video, e nessuno dei due parte.
                    "-probesize", "32", "-analyzeduration", "0",
                    "-f", "s16le", "-ar", str(AR), "-ac", "1", "-i", f"tcp://127.0.0.1:{port}?listen=1",
                    "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{W}x{H}", "-r", str(FPS),
                    "-i", "pipe:0", "-map", "1:v", "-map", "0:a",
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-profile:v", "baseline", "-g", str(FPS), "-keyint_min", str(FPS),
                    "-sc_threshold", "0",
                    "-c:a", "aac", "-ar", "48000", "-b:a", "32k",
                    "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1",
                    "pipe:1",
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except (OSError, RuntimeError) as e:
                _LOGGER.error("Stream passivo continuo: ffmpeg non parte (%s)", e)
                return None
            _enc = enc
            _task = asyncio.create_task(_run(enc, standby, is_live, on_live, port))
            _LOGGER.info("Stream passivo continuo avviato (%dx%d @%d fps)", W, H, FPS)
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        _clients.add(q)
        return q


async def _stop_task() -> None:
    global _task, _enc
    if not _task:
        return
    # Prima di aspettare: HA cancella l'handler di /av quando il client se ne va,
    # anche mentre siamo qui. _run si chiude da sé una volta cancellato.
    task, _task = _task, None
    enc, _enc = _enc, None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # Cancellato prima che _run partisse, il suo finally non è mai girato: l'encoder
    # (in ascolto su TCP) resterebbe orfano.
    if enc and enc.returncode is None:
        enc.kill()
        await enc.wait()
    _LOGGER.info("Stream passivo continuo fermato")


async def unsubscribe(q: asyncio.Queue) -> None:
    async with _lock:
        _clients.discard(q)
        if not _clients:
            await _stop_task()


async def stop() -> None:
    """Scarico dell'integrazione: ferma l'encoder e chiude i client rimasti."""
    async with _lock:
        for q in list(_clients):
            av_stream.end_client(q, _clients)
        await _stop_task()


def _free_port() -> int:
    # ponytail: porta scelta e subito lasciata; una corsa con un altro processo è
    # improbabile in HA e la si vede come «encoder uscito» al primo giro.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tap(pcm: bytes) -> None:
    """PCM decodificato dalla targa: in coda per l'encoder, tetto 400 ms così un
    ritardo non si accumula sul video (si perde un pezzetto, non il sincronismo)."""
    _pcm.extend(pcm)
    if len(_pcm) > 4 * _CHUNK:
        del _pcm[:len(_pcm) - 4 * _CHUNK]


async def _audio_in(port: int):
    """Connessione all'ingresso audio di ffmpeg, che si mette in ascolto appena parte."""
    for _ in range(50):
        try:
            _, w = await asyncio.open_connection("127.0.0.1", port)
            return w
        except OSError:
            await asyncio.sleep(0.1)
    raise OSError(f"ffmpeg non ascolta su {port}")


async def _run(enc, standby: bytes, is_live, on_live, port: int) -> None:
    """Un fotogramma e 100 ms di audio ogni 1/FPS all'encoder, finché c'è un client."""
    global _live
    pump = asyncio.create_task(_pump(enc.stdout))
    decoder: asyncio.Task | None = None
    audio = None
    loop = asyncio.get_running_loop()
    t0, n = loop.time(), 0
    last_live = t0  # last tick with the video, for _DEAD_GRACE
    cancelled = False
    _pcm.clear()
    # Un tap fra tanti (media.pcm_taps): non sostituisce quelli già agganciati, es. il
    # clip dello squillo in frame_grabber, che resta vivo anche dopo che questo esce.
    media.add_pcm_tap(_tap)
    try:
        audio = await _audio_in(port)
        while not pump.done():
            live = is_live()
            if live:
                last_live = loop.time()
            gone = loop.time() - last_live >= _DEAD_GRACE
            if live and (decoder is None or decoder.done()):
                decoder = asyncio.create_task(_decode(is_live, on_live))
            elif gone and decoder and not decoder.done() and not decoder.cancelling():
                # At call end the decoder can attach to /av again after the media
                # stopped but before in_call drops, then wait forever on an ffmpeg
                # with no RTP while _live keeps the panel's last frame. Cancelled
                # once: a second cancel would cut its cleanup (ffmpeg, av_unsubscribe).
                decoder.cancel()
            chunk = bytes(_pcm[:_CHUNK]) if live else b""
            del _pcm[:_CHUNK]
            audio.write(chunk + _SILENCE[len(chunk):])  # mancante = silenzio, mai un buco
            # The panel frame within the grace too: no standby flash while answering.
            enc.stdin.write((not gone and _live) or standby)
            await audio.drain()
            await enc.stdin.drain()
            n += 1
            await asyncio.sleep(max(0.0, t0 + n / FPS - loop.time()))
        _LOGGER.warning("Stream passivo continuo: encoder uscito")
    except (OSError, BrokenPipeError, ConnectionResetError) as e:
        _LOGGER.warning("Stream passivo continuo: encoder uscito (%s)", e)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        media.remove_pcm_tap(_tap)
        _pcm.clear()
        if audio:
            audio.close()
        if decoder:
            if not decoder.cancelling():
                decoder.cancel()
            await asyncio.gather(decoder, return_exceptions=True)
        enc.stdin.close()
        if enc.returncode is None:
            enc.kill()
            await enc.wait()
        # Mai aspettare la pipe dopo il kill: con un wrapper di ffmpeg (shim) muore
        # il wrapper, il figlio tiene la pipe aperta e l'attesa non finirebbe mai.
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        if not cancelled:  # morto da solo: i client vanno chiusi (cancellato = nessun client)
            for q in list(_clients):
                av_stream.end_client(q, _clients)


async def _pump(stdout) -> None:
    while chunk := await stdout.read(65536):
        av_stream.fanout(chunk, _clients)


async def _decode(is_live, on_live) -> None:
    """Finché c'è video: client del fan-out di av_stream → ffmpeg → fotogrammi WxH in _live."""
    global _live
    loop = asyncio.get_running_loop()
    asked = None  # loop time of the last keyframe request
    try:
        while is_live():
            q = await av_stream.av_subscribe()
            if q is None:
                await asyncio.sleep(1)
                continue
            try:
                if asked is None or loop.time() - asked >= _KEYFRAME_EVERY:
                    asked = loop.time()
                    on_live()  # IDR subito: si decodifica dal primo fotogramma, non al prossimo
                # analyzeduration 1 µs: il TS di /av porta anche l'AAC, e senza un tetto
                # ffmpeg sondava fino a probesize (~1,5 s in più al primo fotogramma live).
                dec = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-loglevel", "error", "-probesize", "32768", "-analyzeduration", "1",
                    "-fpsprobesize", "0", "-threads", "1", "-f", "mpegts", "-i", "pipe:0", "-an",
                    # The encoder takes FPS frames a second: the panel's other ones were
                    # raw video read through a pipe on HA's loop for nothing. Dropped
                    # before scaling, so they are not scaled either.
                    "-vf", f"fps={FPS},{_SCALE}", "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1",
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                feeder = asyncio.create_task(_feed(q, dec.stdin))
                try:
                    while frame := await dec.stdout.readexactly(_FRAME):
                        _live = frame
                except asyncio.IncompleteReadError:
                    pass  # fine del TS (chiamata finita) o ffmpeg morto: si riprova se serve
                finally:
                    feeder.cancel()
                    await asyncio.gather(feeder, return_exceptions=True)
                    if dec.returncode is None:
                        dec.kill()
                        try:  # come frame_grabber._record: kill() non basta se ffmpeg è impuntato
                            await asyncio.wait_for(dec.wait(), 15)
                        except TimeoutError:
                            pass
            finally:
                # Shielded: a cancel here would leave q in /av's fan-out, and
                # ffmpeg would never get its "last client left" stop.
                await asyncio.shield(av_stream.av_unsubscribe(q))
    finally:
        _live = None


async def _feed(q: asyncio.Queue, stdin) -> None:
    try:
        while (chunk := await q.get()) is not None:
            stdin.write(chunk)
            await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stdin.close()
