"""The HomeKit video doorbell audio bridge, with real UDP sockets and SRTP.

Home Assistant's bridge opened the audio port only to transmit. What the phone
sent (the answering person's voice) reached a socket that nobody read. The
courier on 23 September could be heard, but he heard nothing. These tests check
that both directions really pass, and that only the right packets pass.
"""
import asyncio
import base64
import os
import socket
import struct
import time
import types

import pytest
from harness.homekit import FakeVoiceFfmpeg  # noqa: E402

from custom_components.vimar_intercom import homekit_audio as audio
from custom_components.vimar_intercom import srtp

KEY = base64.b64encode(bytes(range(30))).decode()


def rtp(seq: int, ts: int, pt: int = 110, payload: bytes = b"\x01" * 40,
        ssrc: int = 0x1234, marker: bool = False) -> bytes:
    return (bytes([0x80, (0x80 if marker else 0) | pt]) + struct.pack("!HII", seq, ts, ssrc)
            + payload)


class TestClockConversion:
    """ffmpeg stamps Opus at 48 kHz, HomeKit wants the negotiated rate."""

    def test_to_the_phone_48k_becomes_16k(self):
        out = audio.TimestampScaler(16000, 48000)(rtp(1, 48000))
        assert struct.unpack_from("!I", out, 4)[0] == 16000

    def test_from_the_phone_16k_becomes_48k(self):
        out = audio.TimestampScaler(48000, 16000)(rtp(1, 16000))
        assert struct.unpack_from("!I", out, 4)[0] == 48000

    def test_the_rest_of_the_packet_is_untouched(self):
        pkt = rtp(7, 960, payload=b"voce")
        out = audio.TimestampScaler(16000, 48000)(pkt)
        assert out[:4] == pkt[:4] and out[8:] == pkt[8:]

    def test_the_clock_stays_continuous_across_the_32_bit_wrap(self):
        """``ts * num // den`` on its own jumped when the input wrapped: from
        about 1.4e9 back to 0, as if the phone had skipped a day of audio."""
        for num, den in ((16000, 48000), (48000, 16000), (24000, 48000)):
            scale = audio.TimestampScaler(num, den)
            step = 960 if den == 48000 else 320
            start = (1 << 32) - 3 * step
            outs = [struct.unpack_from("!I", scale(rtp(n, (start + n * step) & 0xFFFFFFFF)), 4)[0]
                    for n in range(7)]
            deltas = {(b - a) & 0xFFFFFFFF for a, b in zip(outs, outs[1:], strict=False)}
            assert deltas == {step * num // den}, (num, den, outs)

    def test_a_reordered_packet_is_not_thrown_a_day_ahead(self):
        scale = audio.TimestampScaler(16000, 48000)
        first = struct.unpack_from("!I", scale(rtp(2, 96000)), 4)[0]
        older = struct.unpack_from("!I", scale(rtp(1, 95040)), 4)[0]
        assert (first - older) & 0xFFFFFFFF == 320

    def test_payload_type_changes_but_the_marker_stays(self):
        out = audio.set_payload_type(bytearray(rtp(1, 0, pt=101, marker=True)), 110)
        assert out[1] == 0x80 | 110

    def test_rtcp_is_recognised(self):
        assert audio.is_rtcp(bytes([0x81, 200]) + bytes(10))   # SR
        assert audio.is_rtcp(bytes([0x81, 201]) + bytes(10))   # RR
        assert not audio.is_rtcp(rtp(1, 0, pt=110))
        assert not audio.is_rtcp(rtp(1, 0, pt=110, marker=True))


@pytest.fixture
def bridge_rig(monkeypatch):
    """A real bridge, with the phone and the decoder played by sockets."""
    # The voice decoder is an ffmpeg. Here a socket listening on its port
    # replaces it, to see what reaches it. It "starts" as soon as asked, and
    # then receives what was held back.
    starts = []

    async def fake_decoder(self):
        starts.append(1)
        self._talk_ready = True
        while self._talk_pending:
            self._talk_tr.sendto(self._talk_pending.popleft(), ("127.0.0.1", self._talk_port))
        self._talk_starting = False
    monkeypatch.setattr(audio.AudioBridge, "_start_talk_decoder", fake_decoder)

    loop = asyncio.new_event_loop()
    phone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    phone.bind(("127.0.0.1", 0))
    phone.settimeout(2)
    ours = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ours.bind(("127.0.0.1", 0))
    ours.setblocking(False)
    pcm: list[bytes] = []
    bridge = audio.AudioBridge(ours, phone.getsockname(), KEY, 16000, pcm.append)
    loop.run_until_complete(bridge.start())
    talk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    talk.bind(("127.0.0.1", 0))
    talk.settimeout(2)
    bridge._talk_port = talk.getsockname()[1]   # the decoder's port, here ours

    def pump(seconds: float = 0.1):
        loop.run_until_complete(asyncio.sleep(seconds))

    bridge._test_starts = starts
    yield loop, bridge, phone, ours.getsockname(), talk, pump
    loop.run_until_complete(bridge.stop())
    for s in (phone, talk):
        s.close()
    loop.close()


class TestToThePhone:
    def test_arrives_encrypted_with_the_negotiated_clock(self, bridge_rig):
        loop, bridge, phone, _ours, _talk, pump = bridge_rig
        enc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        enc.sendto(rtp(1, 48000 * 2, payload=b"strada"), ("127.0.0.1", bridge.encoder_port))
        pump()
        data, _ = phone.recvfrom(2048)
        plain = srtp.SRTPContext(KEY).unprotect(data)
        assert plain is not None, "the phone must be able to decrypt it"
        assert struct.unpack_from("!I", plain, 4)[0] == 16000 * 2
        assert plain.endswith(b"strada")
        enc.close()

    def test_the_first_audio_to_the_phone_is_reported_once(self, bridge_rig):
        """The view's timeline marks when the street becomes audible."""
        loop, bridge, phone, _ours, _talk, pump = bridge_rig
        seen = []
        bridge.on_first_audio = lambda: seen.append(bridge.stats["to_phone"])
        enc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for seq in range(3):
            enc.sendto(rtp(seq, seq * 960), ("127.0.0.1", bridge.encoder_port))
        pump()
        assert bridge.stats["to_phone"] == 3
        assert seen == [0], "once, as the first packet leaves"
        enc.close()

    def test_only_our_encoder_may_feed_it(self, bridge_rig):
        loop, bridge, phone, _ours, _talk, pump = bridge_rig
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        intruder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        first.sendto(rtp(1, 0), ("127.0.0.1", bridge.encoder_port))
        pump()
        intruder.sendto(rtp(2, 960), ("127.0.0.1", bridge.encoder_port))
        pump()
        assert bridge.stats["to_phone"] == 1
        first.close()
        intruder.close()


class TestFromThePhone:
    """The direction that Home Assistant's bridge lacked entirely."""

    def test_the_voice_is_decrypted_and_handed_to_the_decoder(self, bridge_rig):
        loop, bridge, phone, ours, talk, pump = bridge_rig
        voice = srtp.SRTPContext(KEY).protect(rtp(5, 16000, pt=101, payload=b"pronto"))
        phone.sendto(voice, ours)
        pump()
        data, _ = talk.recvfrom(2048)
        assert data[1] & 0x7F == audio.TALK_PT
        assert struct.unpack_from("!I", data, 4)[0] == 48000
        assert data.endswith(b"pronto")
        assert bridge.stats["from_phone"] == 1

    def test_a_wrong_key_is_counted_not_forwarded(self, bridge_rig):
        loop, bridge, phone, ours, talk, pump = bridge_rig
        other = base64.b64encode(os.urandom(30)).decode()
        phone.sendto(srtp.SRTPContext(other).protect(rtp(5, 0)), ours)
        pump()
        assert bridge.stats["auth_fail"] == 1
        assert bridge.stats["from_phone"] == 0

    def test_rtcp_from_the_phone_is_not_voice(self, bridge_rig):
        loop, bridge, phone, ours, talk, pump = bridge_rig
        phone.sendto(bytes([0x81, 201, 0, 7]) + bytes(40), ours)
        pump()
        assert bridge.stats["from_phone"] == 0 and bridge.stats["auth_fail"] == 0

    def test_only_the_phone_may_speak(self, bridge_rig):
        """Another address must not be able to talk to the intercom."""
        loop, bridge, phone, ours, talk, pump = bridge_rig
        bridge._phone_addr = ("192.0.2.1", bridge._phone_addr[1])
        phone.sendto(srtp.SRTPContext(KEY).protect(rtp(5, 0)), ours)
        pump()
        assert bridge.stats["from_phone"] == 0


class TestVoiceToTheIntercom:
    def test_pcm_goes_out_in_20ms_frames(self):
        """The intercom wants 160-sample packets, so the PCM is split that way."""
        frames: list[bytes] = []

        class FakeStdout:
            def __init__(self, chunks):
                self._chunks = list(chunks)

            async def read(self, _n):
                return self._chunks.pop(0) if self._chunks else b""

        class FakeProc:
            stdout = FakeStdout([b"\x00" * 500, b"\x00" * 460])

        bridge = audio.AudioBridge.__new__(audio.AudioBridge)
        bridge._talk_proc = FakeProc()
        bridge._on_pcm = frames.append
        bridge._on_first_voice, bridge._voice_fired, bridge._loud_ms = None, False, 0
        bridge.stats = {"pcm_frames": 0}
        asyncio.run(bridge._pump_pcm())
        assert [len(f) for f in frames] == [320, 320, 320]
        assert bridge.stats["pcm_frames"] == 3


class TestTheVoiceDecoderComesAndGoes:
    """ffmpeg reading RTP exits on its own after ten seconds without packets.

    On 26 September: you opened the view, waited a while, pressed "Talk", and
    the street heard nothing for the rest of the view, because the decoder had
    already exited. Now it starts with the first voice packet and restarts when
    needed, without losing the start.
    """

    def test_nothing_starts_until_someone_speaks(self, bridge_rig):
        loop, bridge, phone, ours, talk, pump = bridge_rig
        pump()
        assert bridge._test_starts == []

    def test_the_first_words_are_held_not_lost(self, bridge_rig):
        loop, bridge, phone, ours, talk, pump = bridge_rig
        ctx = srtp.SRTPContext(KEY)
        for n in range(3):
            phone.sendto(ctx.protect(rtp(10 + n, 320 * n, payload=bytes([n]) * 20)), ours)
        pump()
        got = [talk.recvfrom(2048)[0][-1] for _ in range(3)]
        assert got == [0, 1, 2], "all three, in order"
        assert len(bridge._test_starts) == 1, "a single start"

    def test_after_it_quits_the_next_voice_restarts_it(self, bridge_rig):
        loop, bridge, phone, ours, talk, pump = bridge_rig
        ctx = srtp.SRTPContext(KEY)
        phone.sendto(ctx.protect(rtp(1, 0)), ours)
        pump()
        talk.recvfrom(2048)
        bridge._talk_ready = False          # exited after the silence
        bridge._talk_proc = None
        phone.sendto(ctx.protect(rtp(2, 320, payload=b"ancora")), ours)
        pump()
        assert talk.recvfrom(2048)[0].endswith(b"ancora")
        assert len(bridge._test_starts) == 2


class TestABrokenVoiceDecoder:
    """ffmpeg that fails to start, or exits at once, was relaunched on every
    voice packet (fifty times a second) and an exception from the launch was
    never retrieved."""

    def _talk(self, monkeypatch, spawn):
        monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(audio, "TALK_RELAUNCH_S", 0.05)
        loop = asyncio.new_event_loop()
        ours = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ours.bind(("127.0.0.1", 0))
        ours.setblocking(False)
        bridge = audio.AudioBridge(ours, ("127.0.0.1", 9), KEY, 16000, lambda _pcm: None)
        loop.run_until_complete(bridge.start())

        async def voice_for(seconds):
            end = loop.time() + seconds
            n = 0
            while loop.time() < end:
                bridge._deliver_voice(rtp(n, n * 960))
                n += 1
                await asyncio.sleep(0.02)
        return loop, bridge, voice_for

    def test_it_is_retried_at_most_once_per_interval_then_given_up(self, monkeypatch, caplog):
        launches = []

        async def spawn(*args, **_kw):
            launches.append(args)
            return FakeVoiceFfmpeg()

        loop, bridge, voice_for = self._talk(monkeypatch, spawn)
        try:
            loop.run_until_complete(voice_for(0.12))
            # 0.12 s at one launch per 0.05 s: three at most, not six.
            assert 1 <= len(launches) <= 3
            loop.run_until_complete(voice_for(0.6))
            assert len(launches) == audio.TALK_MAX_FAILURES, "then it stops trying"
            assert "failed to start" in caplog.text
            assert len(bridge._tasks) <= 2, "finished tasks are not kept"
        finally:
            loop.run_until_complete(bridge.stop())
            loop.close()

    def test_an_exception_from_the_launch_is_handled(self, monkeypatch):
        async def spawn(*_a, **_kw):
            raise FileNotFoundError("ffmpeg")

        loop, bridge, voice_for = self._talk(monkeypatch, spawn)
        errors = []
        loop.set_exception_handler(lambda _loop, ctx: errors.append(ctx))
        try:
            loop.run_until_complete(voice_for(0.4))
            assert bridge._talk_failures == audio.TALK_MAX_FAILURES
        finally:
            loop.run_until_complete(bridge.stop())
            loop.close()
        assert errors == []

    def test_the_decoder_listens_on_loopback_only_from_a_private_sdp(self, monkeypatch, assert_private_mode):
        """ffmpeg 8.1 bound the RTCP port of an SDP input on 0.0.0.0."""
        launches = []

        async def spawn(*args, **_kw):
            path = args[args.index("-i") + 1]
            with open(path) as f:
                launches.append((args, f.read(), os.stat(path).st_mode & 0o777))
            return FakeVoiceFfmpeg()

        loop, bridge, voice_for = self._talk(monkeypatch, spawn)
        try:
            loop.run_until_complete(voice_for(0.05))
            args, sdp, mode = launches[-1]
            i = args.index("-i")
            assert args[i - 2:i] == ("-localaddr", "127.0.0.1")
            path = args[i + 1]
            assert path != f"/tmp/vimar_intercom_talk_{bridge._talk_port}.sdp", "not guessable"
            assert_private_mode(mode)
            assert f"m=audio {bridge._talk_port} " in sdp
        finally:
            loop.run_until_complete(bridge.stop())
            loop.close()
        assert not os.path.exists(path), "the SDP goes with the bridge"


class TestReplayedVoice:
    """SRTP from the phone had no replay protection: one captured voice packet,
    sent again, was decoded and played to the street every time."""

    def test_a_packet_sent_twice_reaches_the_intercom_once(self, bridge_rig):
        loop, bridge, phone, ours, talk, pump = bridge_rig
        voice = srtp.SRTPContext(KEY).protect(rtp(5, 16000, payload=b"pronto"))
        phone.sendto(voice, ours)
        phone.sendto(voice, ours)
        pump()
        assert bridge.stats["from_phone"] == 1 and bridge.stats["replayed"] == 1
        talk.recvfrom(2048)
        talk.settimeout(0.1)
        with pytest.raises(socket.timeout):
            talk.recvfrom(2048)


class TestStopping:
    def test_a_decoder_already_gone_does_not_break_the_stop(self):
        class Gone:
            returncode = None

            def kill(self):
                raise ProcessLookupError

            async def wait(self):
                return 0

        async def scenario():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            bridge = audio.AudioBridge(sock, ("127.0.0.1", 9), KEY, 16000, lambda _p: None)
            await bridge.start()
            bridge._talk_proc = Gone()
            await bridge.stop()

        asyncio.run(scenario())

    def test_a_stop_during_the_sdp_write_leaves_no_file(self, monkeypatch, tmp_path):
        """stop() cancels the decoder's start while the executor writes its
        SDP: the name was never assigned, and the file stayed in /tmp."""
        import threading
        release = threading.Event()
        written = []
        real = audio.write_sdp

        def slow_write(prefix, text):
            release.wait(2)
            path = real(prefix, text)
            written.append(path)
            return path

        monkeypatch.setattr(audio, "write_sdp", slow_write)

        async def scenario():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            bridge = audio.AudioBridge(sock, ("127.0.0.1", 9), KEY, 16000, lambda _p: None)
            await bridge.start()
            bridge._deliver_voice(rtp(1, 0))
            await asyncio.sleep(0.05)               # the write is under way
            await bridge.stop()
            release.set()
            for _ in range(100):
                if written and not os.path.exists(written[0]):
                    break
                await asyncio.sleep(0.01)

        asyncio.run(scenario())
        assert written and not os.path.exists(written[0])



class TestFirstVoice:
    """Answering on Talk needs someone talking, not just packets from the phone:
    if iOS sends audio with the microphone closed, opening the view must not
    answer the ring (same rule as the voice answer on /audio_ws)."""

    @staticmethod
    def bridge(fired):
        b = audio.AudioBridge.__new__(audio.AudioBridge)
        b._on_first_voice = lambda: fired.append(True)
        b._voice_fired, b._loud_ms = False, 0
        return b

    @staticmethod
    def frame(amplitude):
        import struct as _s
        return _s.pack("<160h", *([amplitude, -amplitude] * 80))

    def test_silence_from_the_phone_never_answers(self):
        fired = []
        b = self.bridge(fired)
        for _ in range(100):                      # 2 s of digital silence
            b._note_voice(self.frame(0))
        for _ in range(100):                      # 2 s of room noise
            b._note_voice(self.frame(200))
        assert fired == []

    def test_a_short_blip_does_not_answer(self):
        fired = []
        b = self.bridge(fired)
        n = audio.media.VOICE_ANSWER_MS // audio.PCM_FRAME_MS - 1
        for _ in range(n):
            b._note_voice(self.frame(5000))
        b._note_voice(self.frame(0))              # the blip ends: the count restarts
        for _ in range(n):
            b._note_voice(self.frame(5000))
        assert fired == []

    def test_sustained_speech_answers_once(self):
        fired = []
        b = self.bridge(fired)
        for _ in range(50):
            b._note_voice(self.frame(5000))
        assert fired == [True]


class TestPortPairs:
    """Every ffmpeg RTP input binds its port and the next one (RTCP)."""

    def test_ports_come_in_even_pairs_that_are_free(self):
        port = audio.free_udp_port()
        assert port % 2 == 0
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as a, \
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as b:
            a.bind(("127.0.0.1", port))
            b.bind(("127.0.0.1", port + 1))

    def test_a_pair_just_handed_out_is_not_handed_out_again(self, monkeypatch):
        """The transcoder's and a view's ffmpeg start together, before either has
        bound its ports: the kernel may suggest the same port twice. One of them
        got "bind failed: Address in use" on a 40515."""
        first = audio.free_udp_port()
        again = iter([first + 1, first])
        real = audio._ephemeral_port
        monkeypatch.setattr(audio, "_ephemeral_port", lambda: next(again, None) or real())
        second = audio.free_udp_port()
        assert {second, second + 1}.isdisjoint({first, first + 1})

    def test_two_threads_never_get_the_same_pair(self, monkeypatch):
        """The audio tap picks its port in an executor thread while the transcoder
        picks on the loop: both could probe the same pair before either reserved
        it (review of #45). Here the kernel suggests the same port to both, and
        each pauses between closing its probe and reserving the pair."""
        import threading
        import types
        first = audio.free_udp_port()           # a pair known to be free...
        audio._RESERVED.pop(first)              # ...and not reserved any more

        class SlowClose(socket.socket):
            def __exit__(self, *exc):
                super().__exit__(*exc)
                time.sleep(0.05)                # the gap before _RESERVED is written

        monkeypatch.setattr(audio, "socket", types.SimpleNamespace(
            socket=SlowClose, AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM))
        real, asked = audio._ephemeral_port, []

        def suggest():                          # the same port for the first two probes
            asked.append(1)
            return first if len(asked) <= 2 else real()

        monkeypatch.setattr(audio, "_ephemeral_port", suggest)
        got = []
        threads = [threading.Thread(target=lambda: got.append(audio.free_udp_port())) for _ in range(2)]
        threads[0].start()
        time.sleep(0.07)                        # both probe sockets closed, not yet reserved
        threads[1].start()
        for t in threads:
            t.join(5)
        assert len(got) == 2 and len(set(got)) == 2, got

    def test_a_reservation_expires(self, monkeypatch):
        # A clock of our own: on Windows time.monotonic() moves in steps of about
        # 15 ms, so after a 10 ms sleep the reservation was often still "now" and
        # the only free pair stayed reserved ("no free UDP port pair on loopback").
        clock = [1000.0]
        monkeypatch.setattr(audio, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
        first = audio.free_udp_port()
        monkeypatch.setattr(audio, "_ephemeral_port", lambda: first)
        clock[0] += audio.RESERVE_SECONDS + 0.001
        assert audio.free_udp_port() == first
