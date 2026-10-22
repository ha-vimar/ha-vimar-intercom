"""Frame grabber with fake ffmpeg processes: the photo wait, the bounded queues,
the ring clip (from the first IDR, cap, broken pipe, stuck ffmpeg, audio remux)
and the cleanup. No ffmpeg needed (CI has none)."""
from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace

import pytest

from custom_components.vimar_intercom import frame_grabber as fg
from custom_components.vimar_intercom import media_handler as media

SPS, PPS = b"\x67sps", b"\x68pps"
IDR, P = b"\x65idr", b"\x41p"
JPEG = b"\xff\xd8" + b"J" * 8 + b"\xff\xd9"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k, val in dict(last_jpeg=None, frames=0, _grabber=None, _proto=None,
                       _clip_q=None, _clip_req=None).items():
        monkeypatch.setattr(fg, k, val)
    monkeypatch.setattr(media, "pcm_taps", [])


class _Stdin:
    def __init__(self, broken=False):
        self.writes: list[bytes] = []
        self.closed = False
        self.broken = broken

    def write(self, data):
        if self.broken:
            raise BrokenPipeError
        self.writes.append(bytes(data))

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True


class _Stdout:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.eof = asyncio.Event()

    async def read(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        await self.eof.wait()
        return b""


class _Proc:
    """A fake ffmpeg. `on_wait` runs when the process is waited for (it writes
    the output file, as ffmpeg does when its stdin closes)."""

    def __init__(self, rc=0, stdout=None, broken_stdin=False, stuck=False, on_wait=None,
                 stderr=b""):
        self.args = ()
        self.stdin = _Stdin(broken_stdin)
        self.stdout = stdout or _Stdout()
        self.stderr = _Stdout([stderr] if stderr else ())
        self.stderr.eof.set()  # EOF once the chunks are read
        self._rc = rc
        self.returncode = None
        self.killed = False
        self.stuck = stuck
        self.on_wait = on_wait
        self.communicated = None

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.stuck and not self.killed:
            raise TimeoutError
        if self.returncode is None:
            if self.on_wait:
                self.on_wait(self)
            self.returncode = self._rc
        return self.returncode

    async def communicate(self, data=None):
        if self.stuck:
            raise TimeoutError
        self.communicated = data
        if self.on_wait:
            self.on_wait(self)
        self.returncode = self._rc
        return b"", b""


def _spawner(monkeypatch, *procs):
    queue = list(procs)

    async def spawn(*args, **kwargs):
        proc = queue.pop(0)
        if isinstance(proc, Exception):
            raise proc
        proc.args = args
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return queue


def _proto(ps=None):
    return SimpleNamespace(frame_sink=None, sps_pps=lambda own_only=False: ps)


# ─── the photo ───────────────────────────────────────────────────────────────

def test_wait_frame_waits_for_the_next_photo():
    async def run():
        fg._grabber = asyncio.create_task(asyncio.Event().wait())

        async def later():
            await asyncio.sleep(0.15)
            fg.last_jpeg, fg.frames = JPEG, 1

        asyncio.create_task(later())
        got = await fg.wait_frame(timeout=5)
        fg._grabber.cancel()
        return got

    assert asyncio.run(run()) == JPEG


def test_wait_frame_gives_up_at_the_timeout_with_the_photo_it_has():
    async def run():
        fg.last_jpeg = JPEG
        fg._grabber = asyncio.create_task(asyncio.Event().wait())
        got = await fg.wait_frame(timeout=0.2, after=1)
        fg._grabber.cancel()
        return got

    assert asyncio.run(run()) == JPEG


def test_wait_frame_does_not_wait_without_a_grabber():
    assert asyncio.run(fg.wait_frame(timeout=100)) is None


def test_the_grabber_keeps_the_last_whole_jpeg(monkeypatch):
    # A chunk ending before any JPEG start, then a JPEG split across chunks.
    proc = _Proc(stdout=_Stdout([b"junk\xff\xd9", JPEG[:5], JPEG[5:]]))
    _spawner(monkeypatch, proc)
    proto = _proto()

    async def run():
        fg.start(proto)
        while fg.frames < 1:
            await asyncio.sleep(0.01)
        grabber = fg._grabber
        fg.stop(proto)
        await asyncio.gather(grabber, return_exceptions=True)

    asyncio.run(run())
    assert fg.last_jpeg is None and fg.frames == 0, "stop() drops the photo"
    assert proc.killed and proc.stdin.closed and proto.frame_sink is None
    assert "image2pipe" in proc.args


def test_the_photo_comes_from_one_jpeg(monkeypatch):
    proc = _Proc(stdout=_Stdout([b"junk\xff\xd9", JPEG[:5], JPEG[5:]]))
    _spawner(monkeypatch, proc)

    async def run():
        fg.start(_proto())
        while fg.frames < 1:
            await asyncio.sleep(0.01)
        fg._grabber.cancel()
        return fg.last_jpeg

    assert asyncio.run(run()) == JPEG


def test_nals_before_the_first_sps_are_dropped_and_the_queue_is_bounded(monkeypatch):
    proc = _Proc()
    _spawner(monkeypatch, proc)
    proto = _proto()

    async def run():
        fg.start(proto)
        proto.frame_sink(P)          # no SPS yet: ffmpeg must start from one
        for _ in range(700):         # ffmpeg stalled: at most 600 wait
            proto.frame_sink(SPS)
        for _ in range(2000):
            await asyncio.sleep(0)
        fg._grabber.cancel()
        await asyncio.gather(fg._grabber, return_exceptions=True)

    asyncio.run(run())
    assert len(proc.stdin.writes) == 600
    assert all(w == fg._SC + SPS for w in proc.stdin.writes)


def test_a_grabber_whose_ffmpeg_does_not_start_leaves_no_photo(monkeypatch):
    _spawner(monkeypatch, FileNotFoundError("ffmpeg"))

    async def run():
        fg.start(_proto(ps=(SPS, PPS)))
        await fg._grabber
        return await fg.wait_frame(timeout=5)

    assert asyncio.run(run()) is None


def _grab_logs(monkeypatch, caplog, proc, *, jpeg=False, cancel=False):
    """Runs one grabber over `proc`; returns its log records."""
    # once log_buffer.install() has run, the package logger has propagate=False: use a plain one
    monkeypatch.setattr(fg, "_LOGGER", logging.getLogger("test_frame_grabber_log"))
    caplog.set_level("DEBUG", logger="test_frame_grabber_log")
    _spawner(monkeypatch, proc)

    async def run():
        fg.start(_proto(ps=(SPS, PPS)))
        grabber = fg._grabber
        if jpeg:
            while fg.frames < 1:
                await asyncio.sleep(0.01)
        if cancel:
            while not proc.args:  # let it start: a task cancelled before its first step never runs
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            fg._cancel(None)
        else:
            proc.stdout.eof.set()
        await asyncio.gather(grabber, return_exceptions=True)

    asyncio.run(run())
    return caplog.records


def test_the_photo_decoder_logs_its_jpeg_and_exit(monkeypatch, caplog):
    records = _grab_logs(monkeypatch, caplog, _Proc(stdout=_Stdout([JPEG]), stderr=b"fine"), jpeg=True)
    text = caplog.text
    assert "grabber avviato" in text and "JPEG di 12 byte" in text
    assert "ffmpeg uscito (rc=0, 1 JPEG), stderr: fine" in text
    assert not [r for r in records if r.levelname == "WARNING"]


def test_an_ffmpeg_that_fails_before_any_photo_is_a_warning(monkeypatch, caplog):
    records = _grab_logs(monkeypatch, caplog, _Proc(rc=1, stderr=b"Invalid data found"))
    assert [r.getMessage() for r in records if r.levelname == "WARNING"] == [
        "Foto: ffmpeg uscito (rc=1, 0 JPEG), stderr: Invalid data found"]


def test_a_failure_after_a_photo_or_a_stop_is_not_a_warning(monkeypatch, caplog):
    records = _grab_logs(monkeypatch, caplog, _Proc(rc=1, stdout=_Stdout([JPEG])), jpeg=True)
    assert "(rc=1, 1 JPEG)" in caplog.text and not [r for r in records if r.levelname == "WARNING"]
    caplog.clear()
    records = _grab_logs(monkeypatch, caplog, _Proc(), cancel=True)
    assert "grabber fermato" in caplog.text and "ffmpeg terminato (rc=-9, 0 JPEG)" in caplog.text
    assert not [r for r in records if r.levelname == "WARNING"]


def test_a_broken_pipe_to_the_grabber_ends_its_feeder_quietly(monkeypatch):
    proc = _Proc(broken_stdin=True, stdout=_Stdout())
    _spawner(monkeypatch, proc)

    async def run():
        fg.start(_proto(ps=(SPS, PPS)))
        for _ in range(10):
            await asyncio.sleep(0)
        proc.returncode = 1  # ffmpeg gone: no kill
        proc.stdout.eof.set()
        await fg._grabber

    asyncio.run(run())
    assert not proc.killed and proc.stdin.closed


# ─── the ring clip ───────────────────────────────────────────────────────────

def _writes_part(proc):
    path = proc.args[-1]
    with open(path, "wb") as f:
        f.write(b"MP4")


def _run_record(nals, tmp_path, proc, *, ps=None, max_s=5.0, pcm=None, after=None, pcm_at=None):
    """Runs _record over `nals` (then EOF); returns (on_done result, proc)."""
    done = []

    async def run():
        q: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(fg._record(q, ps, str(tmp_path / "clip.mp4"), max_s, done.append))
        await asyncio.sleep(0.05)  # the .part is claimed in an executor thread first
        for i, nal in enumerate(nals):
            q.put_nowait(nal)
            for _ in range(5):
                await asyncio.sleep(0)
            if pcm and i in (pcm_at or {len(nals) - 1}):
                for tap in list(media.pcm_taps):
                    tap(pcm)
        if after is None:
            q.put_nowait(None)
        await asyncio.wait_for(task, 10)

    asyncio.run(run())
    return done[0], proc


def test_the_clip_task_is_kept_referenced_until_it_ends(monkeypatch):
    # The loop holds only a weak reference to a task: a clip nobody references
    # can be garbage-collected mid-recording (asyncio docs, create_task).
    monkeypatch.setattr(fg, "_clip_tasks", set())
    monkeypatch.setattr(fg, "_proto", SimpleNamespace(sps_pps=lambda own_only=False: None))
    release = None

    async def _record(*_a):
        await release.wait()

    monkeypatch.setattr(fg, "_record", _record)

    async def run():
        nonlocal release
        release = asyncio.Event()
        fg._clip_req = ("clip.mp4", 5.0, None)
        fg._start_clip()
        held = list(fg._clip_tasks)
        release.set()
        await asyncio.gather(*held)
        await asyncio.sleep(0)
        return held

    assert len(asyncio.run(run())) == 1
    assert fg._clip_tasks == set(), "dropped once done"


def test_the_clip_starts_at_the_first_idr_and_keeps_sps_pps_once(monkeypatch, tmp_path):
    proc = _Proc(on_wait=_writes_part)
    _spawner(monkeypatch, proc)
    result, _ = _run_record([P, SPS, PPS, IDR, P, SPS, PPS, IDR], tmp_path, proc)
    assert result == str(tmp_path / "clip.mp4")
    assert (tmp_path / "clip.mp4").read_bytes() == b"MP4"
    assert not (tmp_path / "clip.mp4.part").exists()
    sc = fg._SC
    assert proc.stdin.writes == [sc + SPS + sc + PPS, sc + IDR, sc + P, sc + IDR]
    assert proc.stdin.closed and media.pcm_taps == []


def test_the_clip_uses_the_known_sps_pps_of_the_panel(monkeypatch, tmp_path):
    proc = _Proc(on_wait=_writes_part)
    _spawner(monkeypatch, proc)
    result, _ = _run_record([IDR], tmp_path, proc, ps=(SPS, PPS))
    assert result and proc.stdin.writes[0] == fg._SC + SPS + fg._SC + PPS


def test_a_clip_without_video_leaves_no_file(monkeypatch, tmp_path):
    proc = _Proc()  # writes nothing
    _spawner(monkeypatch, proc)
    result, _ = _run_record([P, P], tmp_path, proc)
    assert result is None and list(tmp_path.iterdir()) == []


def test_a_clip_ends_by_itself_at_the_cap(monkeypatch, tmp_path):
    proc = _Proc(on_wait=_writes_part)
    _spawner(monkeypatch, proc)
    result, _ = _run_record([SPS, PPS, IDR], tmp_path, proc, max_s=0.3, after="no EOF")
    assert result == str(tmp_path / "clip.mp4")


def test_a_clip_whose_ffmpeg_does_not_start_reports_none(monkeypatch, tmp_path):
    _spawner(monkeypatch, OSError("no ffmpeg"))
    done = []
    asyncio.run(fg._record(asyncio.Queue(), None, str(tmp_path / "c.mp4"), 1, done.append))
    assert done == [None] and media.pcm_taps == []


def test_a_clip_whose_ffmpeg_dies_is_not_saved(monkeypatch, tmp_path):
    proc = _Proc(rc=1, broken_stdin=True, on_wait=_writes_part)
    _spawner(monkeypatch, proc)
    result, _ = _run_record([SPS, PPS, IDR, P], tmp_path, proc)
    assert result is None and list(tmp_path.iterdir()) == [], "the half-written .part is removed"


def test_a_stuck_clip_ffmpeg_is_killed(monkeypatch, tmp_path):
    proc = _Proc(stuck=True)
    _spawner(monkeypatch, proc)
    result, _ = _run_record([SPS, PPS, IDR], tmp_path, proc)
    assert result is None and proc.killed


def test_a_clip_that_cannot_be_renamed_is_reported_as_not_saved(monkeypatch, tmp_path):
    proc = _Proc(on_wait=_writes_part)
    _spawner(monkeypatch, proc)

    def no_rename(src, dst):
        raise PermissionError(dst)

    monkeypatch.setattr(fg.os, "replace", no_rename)
    result, _ = _run_record([SPS, PPS, IDR], tmp_path, proc)
    assert result is None


def test_the_clip_file_is_claimed_off_the_event_loop(monkeypatch, tmp_path):
    import threading

    seen = []
    real = fg._claim
    monkeypatch.setattr(fg, "_claim", lambda p: (seen.append(threading.get_ident()), real(p)))
    proc = _Proc(on_wait=_writes_part)
    _spawner(monkeypatch, proc)
    _run_record([SPS, PPS, IDR], tmp_path, proc)
    assert seen and threading.get_ident() not in seen


def test_on_done_is_called_when_the_part_cannot_be_removed(monkeypatch, tmp_path):
    proc = _Proc()  # no .part written by ffmpeg, ffmpeg fails
    proc.returncode = 1
    _spawner(monkeypatch, proc)
    real = os.unlink

    def deny(p):
        if str(p).endswith(".part"):
            raise PermissionError(p)
        real(p)

    monkeypatch.setattr(fg.os, "unlink", deny)
    result, _ = _run_record([SPS, PPS, IDR], tmp_path, proc)
    assert result is None


def test_the_audio_part_is_removed_when_the_rename_fails(monkeypatch, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"MP4")
    remux = _Proc(on_wait=lambda p: open(p.args[-1], "wb").write(b"MP4+AAC"))
    _spawner(monkeypatch, remux)

    def no_rename(src, dst):
        raise PermissionError(dst)

    monkeypatch.setattr(fg.os, "replace", no_rename)
    asyncio.run(fg._add_audio(str(path), b"\x00\x00"))
    assert path.read_bytes() == b"MP4" and not (tmp_path / "clip.mp4.a.part").exists()


def _plant(link, victim, kind):
    try:
        (os.link if kind == "hard" else os.symlink)(victim, link)
    except OSError:
        pytest.skip(f"cannot create a {kind} link here")


@pytest.mark.parametrize("kind", ["hard", "sym"])
def test_a_link_planted_at_the_clip_part_is_not_written_through(monkeypatch, tmp_path, kind):
    """#46: ffmpeg -y opens the .part by name. A link left at that name is replaced by a
    file of our own first, so the clip never lands in the file it points at."""
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep me")
    folder = tmp_path / "foto"
    folder.mkdir()
    _plant(folder / "clip.mp4.part", victim, kind)
    proc = _Proc(on_wait=_writes_part)
    _spawner(monkeypatch, proc)
    result, _ = _run_record([SPS, PPS, IDR], folder, proc)
    assert victim.read_bytes() == b"keep me"
    assert result == str(folder / "clip.mp4")
    assert not os.path.islink(result) and os.stat(result).st_nlink == 1
    assert (folder / "clip.mp4").read_bytes() == b"MP4"


@pytest.mark.parametrize("kind", ["hard", "sym"])
def test_a_link_planted_at_the_audio_part_is_not_written_through(monkeypatch, tmp_path, kind):
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep me")
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"MP4")
    _plant(tmp_path / "clip.mp4.a.part", victim, kind)
    remux = _Proc(on_wait=lambda p: open(p.args[-1], "wb").write(b"MP4+AAC"))
    _spawner(monkeypatch, remux)
    asyncio.run(fg._add_audio(str(path), b"\x00\x00"))
    assert victim.read_bytes() == b"keep me" and path.read_bytes() == b"MP4+AAC"
    assert not path.is_symlink() and os.stat(path).st_nlink == 1


def test_the_panel_audio_is_remuxed_into_the_clip(monkeypatch, tmp_path):
    def with_audio(proc):
        with open(proc.args[-1], "wb") as f:
            f.write(b"MP4+AAC")

    clip = _Proc(on_wait=_writes_part)
    remux = _Proc(on_wait=with_audio)
    _spawner(monkeypatch, clip, remux)
    pcm = b"\x01\x02" * 160
    result, _ = _run_record([SPS, PPS, IDR], tmp_path, clip, pcm=pcm)
    assert result == str(tmp_path / "clip.mp4")
    assert (tmp_path / "clip.mp4").read_bytes() == b"MP4+AAC"
    assert remux.communicated == pcm
    assert "s16le" in remux.args and str(tmp_path / "clip.mp4") in remux.args


def test_audio_before_the_first_idr_is_not_in_the_clip(monkeypatch, tmp_path):
    clip = _Proc(on_wait=_writes_part)
    remux = _Proc(on_wait=lambda p: open(p.args[-1], "wb").close())
    _spawner(monkeypatch, clip, remux)
    pcm = b"\x01\x02" * 160
    # PCM after the SPS (no video yet) and after the IDR: only the second is kept.
    _run_record([SPS, PPS, IDR], tmp_path, clip, pcm=pcm, pcm_at={0, 2})
    assert remux.communicated == pcm


def test_a_failed_audio_remux_keeps_the_silent_clip(monkeypatch, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"MP4")
    remux = _Proc(rc=1, on_wait=lambda p: open(p.args[-1], "wb").close())
    _spawner(monkeypatch, remux)
    asyncio.run(fg._add_audio(str(path), b"\x00\x00"))
    assert path.read_bytes() == b"MP4" and list(tmp_path.iterdir()) == [path]


def test_a_stuck_audio_remux_is_killed_and_the_clip_kept(monkeypatch, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"MP4")
    remux = _Proc(stuck=True)
    _spawner(monkeypatch, remux)

    async def killed_wait():
        return -9

    remux.wait = killed_wait
    asyncio.run(fg._add_audio(str(path), b"\x00\x00"))
    assert remux.killed and path.read_bytes() == b"MP4"


def test_an_audio_remux_that_cannot_start_keeps_the_clip(monkeypatch, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"MP4")
    _spawner(monkeypatch, OSError("no ffmpeg"))
    asyncio.run(fg._add_audio(str(path), b"\x00\x00"))
    assert path.read_bytes() == b"MP4"


# ─── record() and the video starting ─────────────────────────────────────────

def test_a_clip_asked_before_the_video_starts_with_it(monkeypatch, tmp_path):
    grab = _Proc()
    clip = _Proc(on_wait=_writes_part)
    _spawner(monkeypatch, grab, clip)
    proto = _proto(ps=(SPS, PPS))
    done = []

    async def run():
        fg.record(str(tmp_path / "clip.mp4"), 5, done.append)
        assert fg._clip_q is None, "no video yet: the clip waits"
        fg.start(proto)
        assert fg._clip_q is not None
        proto.frame_sink(IDR)  # to the photo and to the clip
        for _ in range(20):
            await asyncio.sleep(0)
        fg.stop(proto)
        while not done:
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert done == [str(tmp_path / "clip.mp4")]
    assert clip.stdin.writes[-1] == fg._SC + IDR


def test_a_clip_asked_during_the_preview_starts_at_once(monkeypatch, tmp_path):
    grab = _Proc()
    clip = _Proc(on_wait=_writes_part)
    _spawner(monkeypatch, grab, clip)
    proto = _proto(ps=(SPS, PPS))
    done = []

    async def run():
        fg.start(proto)
        fg.record(str(tmp_path / "clip.mp4"), 5, done.append)
        assert fg._clip_q is not None and fg._clip_req is None
        proto.frame_sink(IDR)
        for _ in range(20):
            await asyncio.sleep(0)
        fg.stop(proto)
        while not done:
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert done == [str(tmp_path / "clip.mp4")]


def test_ending_a_full_clip_queue_still_ends_the_clip():
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    q.put_nowait(IDR)
    fg._clip_q = q
    fg._end_clip()
    assert q.get_nowait() is None and fg._clip_q is None
