"""Foto della chiamata e dell'anteprima dello squillo, e clip MP4 dello squillo.

Un ffmpeg per chiamata (o anteprima) decodifica i NAL H.264 che arrivano da
`video_proto.frame_sink`, dal primo SPS in poi (quindi anche il primo IDR), e
tiene l'ultimo JPEG. Chi chiede una foto non aspetta il prossimo IDR (di notte,
a scena ferma, anche >8 s). Allo squillo (record) un secondo ffmpeg riceve gli
stessi NAL e li copia, senza ricodifica, in un MP4: dal primo IDR alla fine del
video (stop) o al tetto di durata. Se nel frattempo arriva del PCM della targa
(media.pcm_taps, lo stesso di av_passive) da quando il video è partito, un terzo
ffmpeg lo rimuxa in AAC a clip già chiuso; senza PCM il clip resta muto come prima.
"""

import asyncio
import contextlib
import logging
import os
import subprocess

from . import media_handler as media

_LOGGER = logging.getLogger(__name__)

last_jpeg: bytes | None = None
frames = 0                              # fotogrammi (IDR) decodificati in questa chiamata
_grabber: asyncio.Task | None = None
_proto = None
_clip_q: asyncio.Queue | None = None    # NAL per il clip in corso
_clip_req = None                        # (path, max_s, on_done) in attesa che il video parta
_clip_tasks: set[asyncio.Task] = set()  # clips recording: the loop alone holds only a weak ref

_SC = b"\x00\x00\x00\x01"
_AR = 8000  # PCM della targa (PCMU decodificato): 8 kHz, 16 bit, mono — come in av_passive


async def wait_frame(timeout: float = 6, after: int = 0) -> bytes | None:
    """L'ultimo JPEG, aspettando finché non ce n'è uno oltre i primi `after` (0: il
    primo che arriva). La targa manda un IDR ogni ~3 s e il primo può perdersi
    (sotto il timeout di HA, 10 s)."""
    for _ in range(int(timeout / 0.1)):
        if frames > after or not _grabber or _grabber.done():  # ffmpeg morto: inutile aspettare
            break
        await asyncio.sleep(0.1)
    return last_jpeg


def start(video_proto) -> None:
    global _grabber, _proto
    # Non stop(): un re-INVITE con SDP nuovo riavvia il grabber a metà chiamata, e la
    # foto già presa resta finché non ne esce una nuova (la chiamata è la stessa);
    # anche il clip dello squillo continua.
    _cancel(video_proto)
    _LOGGER.debug("Foto: grabber avviato")
    _proto = video_proto
    q: asyncio.Queue = asyncio.Queue(maxsize=600)
    # SPS/PPS della chiamata prima: ffmpeg decodifica dal primo IDR anche se la
    # targa manda l'SPS in banda solo dopo (40515: ~6 s).
    started = bool(ps := video_proto.sps_pps())
    for nal in ps or ():
        q.put_nowait(nal)

    def sink(nal: bytes) -> None:
        nonlocal started
        started = started or nal[0] & 0x1F == 7  # ffmpeg deve partire da un SPS
        if started and not q.full():
            q.put_nowait(nal)
        if _clip_q is not None and not _clip_q.full():
            _clip_q.put_nowait(nal)

    video_proto.frame_sink = sink
    _grabber = asyncio.create_task(_grab(q))
    if _clip_req:
        _start_clip()


def _cancel(video_proto) -> None:
    global _grabber
    if video_proto:
        video_proto.frame_sink = None
    if _grabber:
        _LOGGER.debug("Foto: grabber fermato")
        _grabber.cancel()
        _grabber = None


def stop(video_proto) -> None:
    """Fine della chiamata (o dell'anteprima): niente foto vecchie per quella dopo."""
    global last_jpeg, frames
    _cancel(video_proto)
    last_jpeg, frames = None, 0
    _end_clip()


def record(path: str, max_s: float, on_done) -> None:
    """Clip MP4 (H.264 copiato, niente ricodifica) del video di questo squillo: dal
    primo IDR fino a stop() o a `max_s` secondi; on_done(path | None) a file chiuso.
    Chiamata prima che il video parta, il clip parte con lui (start)."""
    global _clip_req
    _end_clip()
    _clip_req = (path, max_s, on_done)
    if _grabber:  # video già in corso (l'anteprima parte col 183, prima dello squillo)
        _start_clip()


def _start_clip() -> None:
    global _clip_q, _clip_req
    path, max_s, on_done = _clip_req
    _clip_req = None
    _clip_q = asyncio.Queue(maxsize=600)
    # Solo gli SPS/PPS di questa targa: quelli di un'altra (risoluzione diversa) resterebbero
    # nell'avcC per tutto il clip. Senza, _record aspetta quelli in banda prima di partire.
    task = asyncio.create_task(_record(_clip_q, _proto.sps_pps(own_only=True), path, max_s, on_done))
    _clip_tasks.add(task)
    task.add_done_callback(_clip_tasks.discard)


def _end_clip() -> None:
    """Fine del video: ffmpeg riceve EOF, chiude il file (moov) ed esce."""
    global _clip_q, _clip_req
    _clip_req = None
    if _clip_q is not None:
        if _clip_q.full():
            _clip_q.get_nowait()
        _clip_q.put_nowait(None)
        _clip_q = None


async def _record(q: asyncio.Queue, ps, path: str, max_s: float, on_done) -> None:
    part = path + ".part"  # rinominato solo a file chiuso bene: mai un clip a metà
    # PCM della targa in un buffer, tenuto solo da quando parte il video (in fase con
    # l'inizio del clip): a fine giro, se non è vuoto, ci si rimuxa sopra l'AAC. Un tap
    # fra tanti (media.pcm_taps): non sostituisce lo stream passivo continuo (av_passive)
    # se è già agganciato durante lo squillo.
    pcm_buf = bytearray()
    started = False

    def tap(pcm: bytes) -> None:
        if started:
            pcm_buf.extend(pcm)

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _claim, part)  # off the loop: /media can be a NAS
    except OSError as e:
        _LOGGER.warning("Clip squillo non salvato (%s): %s", path, e)
        on_done(None)
        return
    media.add_pcm_tap(tap)
    try:
        proc = await asyncio.create_subprocess_exec(
            # Timestamp dall'orologio: l'H.264 grezzo non ne ha, e la targa non va a 25 fps
            # fissi (ffmpeg li inventerebbe): un fotogramma perso resta un buco di tempo,
            # non un'accelerazione. faststart: moov in testa, il browser parte e cerca subito.
            "ffmpeg", "-y", "-loglevel", "error", "-use_wallclock_as_timestamps", "1",
            "-f", "h264", "-i", "pipe:0", "-c", "copy", "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart", "-f", "mp4", part,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        _LOGGER.warning("Clip squillo: ffmpeg non avviabile (%s)", e)
        media.remove_pcm_tap(tap)
        await loop.run_in_executor(None, _finish, part, None)
        on_done(None)
        return
    sps, pps = ps or (None, None)
    end = loop.time() + max_s
    try:
        while True:
            try:
                nal = await asyncio.wait_for(q.get(), max(0.0, end - loop.time()))
            except TimeoutError:
                break  # tetto di durata
            if nal is None:
                break
            t = nal[0] & 0x1F
            if t == 7:
                sps = nal
            elif t == 8:
                pps = nal
            if not started:
                if t != 5 or not (sps and pps):
                    continue  # dal primo IDR coi suoi SPS/PPS: prima ci sarebbe solo grigio
                started = True
                proc.stdin.write(_SC + sps + _SC + pps)
            elif t in (7, 8):
                continue  # ripetuti prima di ogni IDR: nell'MP4 stanno una volta sola (avcC)
            proc.stdin.write(_SC + nal)
            await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        media.remove_pcm_tap(tap)
    with contextlib.suppress(Exception):
        proc.stdin.close()  # EOF: ffmpeg scrive il moov ed esce
    try:
        rc = await asyncio.wait_for(proc.wait(), 15)
    except TimeoutError:
        proc.kill()
        rc = -1
    ok = started and rc == 0
    try:
        await loop.run_in_executor(None, _finish, part, path if ok else None)
    except OSError as e:
        _LOGGER.warning("Clip squillo non salvato (%s): %s", path, e)
        ok = False
    if ok and pcm_buf:
        await _add_audio(path, bytes(pcm_buf))
    on_done(path if ok else None)


def _claim(part: str) -> None:
    """An empty file of our own at `part`, before ffmpeg -y opens it by name. Whatever was
    there (a symlink, a hard link) is unlinked first, so the final component is never a
    link, and "x" (O_EXCL) fails rather than follow one that appears in between (#46).
    ponytail: ffmpeg still reopens the name, so a swap in that window is not covered;
    a private 0700 directory would close it."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(part)
    open(part, "xb").close()


def _finish(part: str, path: str | None) -> None:
    if path:
        try:
            os.replace(part, path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(part)
            raise
    else:
        with contextlib.suppress(OSError):  # whatever it is, the caller still gets on_done
            os.unlink(part)


async def _add_audio(path: str, pcm: bytes) -> None:
    """Rimuxa il PCM tappato durante la registrazione nel clip appena chiuso: un ffmpeg
    in più, senza ricodifica video, con l'audio (raw, via stdin) codificato in AAC. Se
    fallisce il clip resta quello già scritto, muto: niente perso."""
    tmp = path + ".a.part"
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _claim, tmp)
    except OSError as e:
        _LOGGER.warning("Audio clip squillo non aggiunto (%s)", e)
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", path,
            "-f", "s16le", "-ar", str(_AR), "-ac", "1", "-i", "pipe:0",
            "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "32k",
            "-movflags", "+faststart", "-f", "mp4", tmp,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        _LOGGER.warning("Audio clip squillo non aggiunto (%s)", e)
        await loop.run_in_executor(None, _finish, tmp, None)
        return
    try:
        await asyncio.wait_for(proc.communicate(pcm), 15)
    except TimeoutError:
        proc.kill()
        await proc.wait()
    try:
        await loop.run_in_executor(None, _finish, tmp, path if proc.returncode == 0 else None)
    except OSError as e:
        _LOGGER.warning("Audio clip squillo non aggiunto (%s)", e)


async def _grab(q: asyncio.Queue) -> None:
    global last_jpeg, frames
    try:
        proc = await asyncio.create_subprocess_exec(
            # JPEG solo dei fotogrammi completi (I/IDR, ~ogni 3 s): autonomi, quindi puliti
            # anche dopo una perdita di pacchetti, che sui P-frame dà immagini sgranate.
            # probesize minimo e un thread: il JPEG esce appena arriva l'IDR.
            "ffmpeg", "-loglevel", "error", "-probesize", "4096", "-analyzeduration", "0",
            "-threads", "1", "-f", "h264", "-i", "pipe:0",
            # Si decodificano anche i P (~1% di un core) e li scarta il select: con
            # -skip_frame nokey un decoder che riordina teneva l'IDR fino al successivo,
            # mai con un IDR solo per squillo (#129).
            "-vf", "select=eq(pict_type\\,I)",
            # image2pipe va a fps costanti: duplicherebbe l'I per riempire i buchi.
            "-fps_mode", "passthrough",
            # Encoder: col frame threading terrebbe ogni JPEG fino al fotogramma dopo.
            "-threads", "1",
            "-pix_fmt", "yuvj420p",
            "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "-flush_packets", "1", "pipe:1",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        _LOGGER.warning("Foto: ffmpeg non avviabile (%s)", e)
        return

    async def feed():
        try:
            while True:
                proc.stdin.write(_SC + await q.get())
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    err = b""  # ultimi byte di stderr; va letto sempre, o ffmpeg si blocca a pipe piena

    async def drain():
        nonlocal err
        while chunk := await proc.stderr.read(4096):
            err = (err + chunk)[-2048:]

    feeder = asyncio.create_task(feed())
    drainer = asyncio.create_task(drain())
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    n = 0  # JPEG di questo grabber (frames vale per tutta la chiamata)
    buf = b""
    try:
        while chunk := await proc.stdout.read(65536):
            buf += chunk
            while (end := buf.find(b"\xff\xd9")) != -1:
                start = buf.find(b"\xff\xd8")
                # Anche il primo IDR, spesso scuro (la telecamera regola l'esposizione):
                # con un IDR ogni ~3 s, o >8 s di notte, scartarlo lasciava una vista
                # da ~10 s senza foto. Chi vuole quello dopo chiede wait_frame(after=1).
                if 0 <= start < end:
                    last_jpeg = buf[start:end + 2]
                    frames += 1
                    n += 1
                    _LOGGER.debug("Foto: JPEG di %d byte dopo %.1f s dall'avvio del grabber",
                                  len(last_jpeg), loop.time() - t0)
                buf = buf[end + 2:]
        with contextlib.suppress(TimeoutError):  # stdout chiuso: sta uscendo, servono rc e coda di stderr
            await asyncio.wait_for(asyncio.gather(proc.wait(), drainer), 2)
    finally:
        feeder.cancel()
        drainer.cancel()
        proc.stdin.close()
        killed = proc.returncode is None
        if killed:
            proc.kill()
            await proc.wait()
        # Uscito da solo con errore e senza una foto: da vedere anche senza debug.
        failed = bool(proc.returncode) and not killed and not n
        _LOGGER.log(logging.WARNING if failed else logging.DEBUG,
                    "Foto: ffmpeg %s (rc=%s, %d JPEG), stderr: %s",
                    "terminato" if killed else "uscito", proc.returncode, n,
                    err.decode(errors="replace").strip() or "-")
