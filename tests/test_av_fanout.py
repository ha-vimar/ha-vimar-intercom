"""One ffmpeg stdout, many clients.

HomeKit opens more than one connection to the AV stream. When each client read
the pipe directly they split the bytes between them, so neither received a
decodable MPEG-TS, and the first client to disconnect stopped ffmpeg for
everyone still watching.
"""
import asyncio

import pytest

media = pytest.importorskip("custom_components.vimar_intercom.media_handler")


@pytest.fixture(autouse=True)
def clean_subscribers():
    media._av_subscribers.clear()
    yield
    media._av_subscribers.clear()


def test_every_subscriber_receives_the_same_bytes():
    async def main():
        first, second = media.subscribe_av(), media.subscribe_av()
        media._broadcast_av(b"chunk-a")
        media._broadcast_av(b"chunk-b")
        return [
            [first.get_nowait(), first.get_nowait()],
            [second.get_nowait(), second.get_nowait()],
        ]

    first, second = asyncio.run(main())
    assert first == [b"chunk-a", b"chunk-b"]
    assert second == first, "clients must not split the stream between them"


def test_unsubscribe_reports_who_is_left():
    async def main():
        first, second = media.subscribe_av(), media.subscribe_av()
        return media.unsubscribe_av(first), media.unsubscribe_av(second)

    assert asyncio.run(main()) == (1, 0), "ffmpeg stops only when the last one leaves"


def test_a_slow_client_drops_old_chunks_instead_of_blocking_others():
    async def main():
        slow, fast = media.subscribe_av(), media.subscribe_av()
        for index in range(media._AV_QUEUE_CHUNKS + 10):
            media._broadcast_av(bytes([index % 256]))
        # The fast client keeps up; drain it so only the slow one is backed up.
        while not fast.empty():
            fast.get_nowait()
        media._broadcast_av(b"latest")
        return slow.qsize(), fast.get_nowait()

    backlog, newest = asyncio.run(main())
    assert backlog <= media._AV_QUEUE_CHUNKS, "the backlog must stay bounded"
    assert newest == b"latest", "a slow client must not stall a healthy one"


def test_unsubscribing_an_unknown_queue_is_harmless():
    async def main():
        media.subscribe_av()
        return media.unsubscribe_av(asyncio.Queue())

    assert asyncio.run(main()) == 1


class TestLifecycle:
    """Starting and stopping must be decided under one lock.

    HomeKit opens and closes connections in quick succession, so a client
    leaving while another is arriving is the normal case, not a rare race.
    """

    def test_ffmpeg_stops_only_after_the_last_viewer_leaves(self, monkeypatch):
        """And not the instant they leave: the grace window comes first."""
        stops = []

        async def fake_stop():
            stops.append(True)

        monkeypatch.setattr(media, "_stop_av_ffmpeg_locked", fake_stop)
        monkeypatch.setattr(media, "AV_IDLE_GRACE", 0.01)

        async def main():
            first, second = media.subscribe_av(), media.subscribe_av()
            await media.release_av(first)
            after_first = len(stops)
            await media.release_av(second)
            straight_after = len(stops)
            await asyncio.sleep(0.05)
            return after_first, straight_after, len(stops)

        after_first, straight_after, after_grace = asyncio.run(main())
        assert after_first == 0, "a leaver must not stop the stream for those still watching"
        assert straight_after == 0, "the last leaver starts the grace, it does not stop ffmpeg"
        assert after_grace == 1

    def test_a_viewer_returning_during_the_grace_keeps_ffmpeg_alive(self, monkeypatch):
        """The whole point: a re-tap must not pay the cold start again."""
        stops = []

        async def fake_stop():
            stops.append(True)

        monkeypatch.setattr(media, "_stop_av_ffmpeg_locked", fake_stop)
        monkeypatch.setattr(media, "AV_IDLE_GRACE", 0.05)

        async def main():
            only = media.subscribe_av()
            await media.release_av(only)
            await asyncio.sleep(0.01)
            media.subscribe_av()          # comes back before the grace expires
            await asyncio.sleep(0.1)
            return len(stops)

        assert asyncio.run(main()) == 0

    def test_a_viewer_arriving_during_a_release_keeps_the_stream(self, monkeypatch):
        stops = []

        async def fake_stop():
            stops.append(True)

        monkeypatch.setattr(media, "_stop_av_ffmpeg_locked", fake_stop)

        async def main():
            leaving = media.subscribe_av()
            # The arriving client subscribes before the leaver's release runs,
            # which is what makes holding the lock over both decisions matter.
            arriving = media.subscribe_av()
            await media.release_av(leaving)
            return len(stops), arriving in media._av_subscribers

        stopped, still_subscribed = asyncio.run(main())
        assert stopped == 0
        assert still_subscribed

    def test_the_end_of_stream_sentinel_reaches_every_viewer(self):
        async def main():
            first, second = media.subscribe_av(), media.subscribe_av()
            media._broadcast_av(b"")
            return first.get_nowait(), second.get_nowait()

        assert asyncio.run(main()) == (b"", b"")


class TestProcessMatchesTheCall:
    """An ffmpeg started for one call must not be reused by a different one.

    Observed live: an audio-only call left its process running; the next call
    had video, reused that process, and the viewer heard the street while
    seeing the black placeholder.
    """

    def test_a_finished_call_takes_its_ffmpeg_with_it(self, monkeypatch):
        stopped = []

        async def fake_stop():
            stopped.append(True)

        monkeypatch.setattr(media, "stop_av_ffmpeg", fake_stop)
        monkeypatch.setattr(media, "audio_proto", None)
        monkeypatch.setattr(media, "video_proto", None)
        monkeypatch.setattr(media, "_stun_task", None)
        monkeypatch.setattr(media, "_audio_task", None)
        monkeypatch.setattr(media, "_audio_tx_task", None)
        asyncio.run(media.stop_media())
        assert stopped, "a finished call must not leave its ffmpeg running"

    @pytest.mark.parametrize(
        "sdp_had_video,call_has_video,flowing,reusable",
        [
            (True, True, True, True),     # video then video: reuse
            (False, False, False, True),  # audio then audio: reuse
            (False, True, True, False),   # audio-only process, call now has video
            (True, False, False, False),  # video process, call now has none
        ],
    )
    def test_reuse_requires_the_same_media_shape(
        self, monkeypatch, sdp_had_video, call_has_video, flowing, reusable,
    ):
        monkeypatch.setattr(media, "_av_sdp_has_video", sdp_had_video)
        monkeypatch.setattr(media, "has_video", call_has_video)
        monkeypatch.setattr(
            media, "video_proto",
            type("P", (), {"_last_sps": b"x", "_last_pps": b"y"})() if flowing else None,
        )
        assert (media._av_sdp_has_video == (media.has_video and media._video_is_flowing())) is reusable
