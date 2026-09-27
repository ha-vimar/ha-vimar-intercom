"""Il ponte audio del videocitofono HomeKit, con socket UDP e SRTP veri.

Il ponte di Home Assistant apriva la porta dell'audio solo per trasmettere:
quello che il telefono mandava — la voce di chi risponde — arrivava a un
socket che nessuno leggeva. Il corriere del 23 settembre si sentiva, lui non
sentiva niente. Qui si prova che le due direzioni passano davvero, e che
passano solo i pacchetti giusti.
"""
import asyncio
import base64
import os
import socket
import struct

import pytest

audio = pytest.importorskip("custom_components.vimar_intercom.homekit_audio")
srtp = pytest.importorskip("custom_components.vimar_intercom.srtp")

KEY = base64.b64encode(bytes(range(30))).decode()


def rtp(seq: int, ts: int, pt: int = 110, payload: bytes = b"\x01" * 40,
        ssrc: int = 0x1234, marker: bool = False) -> bytes:
    return (bytes([0x80, (0x80 if marker else 0) | pt]) + struct.pack("!HII", seq, ts, ssrc)
            + payload)


class TestClockConversion:
    """ffmpeg timbra l'Opus a 48 kHz, HomeKit vuole la frequenza negoziata."""

    def test_to_the_phone_48k_becomes_16k(self):
        out = audio.rescale_timestamp(rtp(1, 48000), 16000, 48000)
        assert struct.unpack_from("!I", out, 4)[0] == 16000

    def test_from_the_phone_16k_becomes_48k(self):
        out = audio.rescale_timestamp(rtp(1, 16000), 48000, 16000)
        assert struct.unpack_from("!I", out, 4)[0] == 48000

    def test_the_rest_of_the_packet_is_untouched(self):
        pkt = rtp(7, 960, payload=b"voce")
        out = audio.rescale_timestamp(pkt, 16000, 48000)
        assert out[:4] == pkt[:4] and out[8:] == pkt[8:]

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
    """Un ponte vero, con il telefono e il decodificatore fatti da socket."""
    # Il decodificatore della voce è un ffmpeg: qui lo sostituisce un socket
    # in ascolto sulla sua porta, per vedere cosa gli arriva. «Parte» appena
    # glielo si chiede, e allora riceve quello che era stato tenuto da parte.
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
    talk.bind(("127.0.0.1", bridge._talk_port))
    talk.settimeout(2)

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
        assert plain is not None, "il telefono deve riuscire a decifrarlo"
        assert struct.unpack_from("!I", plain, 4)[0] == 16000 * 2
        assert plain.endswith(b"strada")
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
    """La direzione che al ponte di Home Assistant mancava del tutto."""

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
        """Un altro indirizzo non deve poter parlare al citofono."""
        loop, bridge, phone, ours, talk, pump = bridge_rig
        bridge._phone_addr = ("192.0.2.1", bridge._phone_addr[1])
        phone.sendto(srtp.SRTPContext(KEY).protect(rtp(5, 0)), ours)
        pump()
        assert bridge.stats["from_phone"] == 0


class TestVoiceToTheIntercom:
    def test_pcm_goes_out_in_20ms_frames(self):
        """Il citofono vuole pacchetti da 160 campioni: il PCM va spezzato così."""
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
        bridge.stats = {"pcm_frames": 0}
        asyncio.run(bridge._pump_pcm())
        assert [len(f) for f in frames] == [320, 320, 320]
        assert bridge.stats["pcm_frames"] == 3


class TestTheVoiceDecoderComesAndGoes:
    """ffmpeg che legge RTP esce da solo dopo dieci secondi senza pacchetti.

    Il 26 settembre: si apriva la vista, si aspettava un po', si premeva
    «Parla» — e dalla strada non si sentiva niente per il resto della vista,
    perché il decodificatore era già uscito. Adesso parte con la prima voce e
    riparte quando serve, senza perderne l'inizio.
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
        assert got == [0, 1, 2], "tutte e tre, in ordine"
        assert len(bridge._test_starts) == 1, "un solo avvio"

    def test_after_it_quits_the_next_voice_restarts_it(self, bridge_rig):
        loop, bridge, phone, ours, talk, pump = bridge_rig
        ctx = srtp.SRTPContext(KEY)
        phone.sendto(ctx.protect(rtp(1, 0)), ours)
        pump()
        talk.recvfrom(2048)
        bridge._talk_ready = False          # uscito dopo il silenzio
        bridge._talk_proc = None
        phone.sendto(ctx.protect(rtp(2, 320, payload=b"ancora")), ours)
        pump()
        assert talk.recvfrom(2048)[0].endswith(b"ancora")
        assert len(bridge._test_starts) == 2
