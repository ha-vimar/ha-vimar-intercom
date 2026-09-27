"""RTP sequence numbers are 16-bit and wrap, so differences need a sign.

Read as unsigned, a packet arriving 61 places late looks like 65475 missing
packets, and the frame being assembled was thrown away every time one arrived
out of order — which on a lossy link is often.
"""
import pytest

media = pytest.importorskip("custom_components.vimar_intercom.media_handler")


class TestTheReorderBuffer:
    """The real ReorderBuffer, not a copy of its arithmetic."""

    def feed(self, seqs, size=5):
        buf = media.ReorderBuffer(size)
        out = []
        for s in seqs:
            out += [q for q, _ in buf.push(s, b"x")]
        return buf, out

    def test_in_order_passes_straight_through(self):
        assert self.feed([10, 11, 12])[1] == [10, 11, 12]

    def test_a_swap_is_put_right(self):
        assert self.feed([10, 12, 11, 13])[1] == [10, 11, 12, 13]

    def test_a_lost_packet_is_skipped_once_the_buffer_fills(self):
        buf, out = self.feed([10, 12, 13, 14, 15, 16, 17])
        assert out == [10, 12, 13, 14, 15, 16, 17]
        assert buf.pending == {}

    def test_a_packet_later_than_its_slot_is_dropped(self):
        """The case seen live: expected 176, got 115. It used to stay forever."""
        buf, out = self.feed(list(range(100, 177)) + [115, 177])
        assert out == list(range(100, 178))
        assert buf.late == 1 and buf.pending == {}

    def test_wrapping_forward_is_in_order(self):
        assert self.feed([65534, 65535, 0, 1])[1] == [65534, 65535, 0, 1]

    def test_wrapping_backwards_is_late(self):
        buf, out = self.feed([1, 2, 65534])
        assert out == [1, 2] and buf.late == 1

    def test_a_new_stream_restarts_the_count(self):
        """A jump of thousands is a new call, not 40000 lost packets."""
        buf, out = self.feed([100, 101, 40000, 40001])
        assert out == [100, 101, 40000, 40001]

    def test_nothing_ever_grows_past_the_size(self):
        buf = media.ReorderBuffer(5)
        import random
        rnd = random.Random(7)
        seq = 0
        for _ in range(5000):
            seq = (seq + rnd.choice([1, 1, 1, 2, 3, -2, -40])) & 0xFFFF
            buf.push(seq, b"x")
            assert len(buf.pending) <= 5


class TestThresholds:
    def test_the_tolerance_is_a_handful_of_packets(self):
        assert 0 < media.MAX_FUA_GAP <= 10




class TestLossRecovery:
    def test_a_keyframe_is_requested_after_a_lost_frame(self, monkeypatch):
        """Without asking, the picture stays frozen until the panel's own
        keyframe, which arrives only every few seconds."""
        import asyncio

        asked = []

        async def fake_request():
            asked.append(True)

        monkeypatch.setattr(media, "request_keyframe", fake_request)
        monkeypatch.setattr(media, "_last_keyframe_request", 0.0)

        async def main():
            media._recover_from_loss()
            await asyncio.sleep(0)

        asyncio.run(main())
        assert asked == [True]

    def test_requests_are_rate_limited(self, monkeypatch):
        import asyncio

        asked = []

        async def fake_request():
            asked.append(True)

        monkeypatch.setattr(media, "request_keyframe", fake_request)
        monkeypatch.setattr(media, "_last_keyframe_request", 0.0)

        async def main():
            for _ in range(5):        # a burst of loss must not become a burst of INFOs
                media._recover_from_loss()
            await asyncio.sleep(0)

        asyncio.run(main())
        assert len(asked) == 1

    def test_nothing_happens_when_no_requester_is_wired(self, monkeypatch):
        monkeypatch.setattr(media, "request_keyframe", None)
        monkeypatch.setattr(media, "_last_keyframe_request", 0.0)
        media._recover_from_loss()  # must not raise
