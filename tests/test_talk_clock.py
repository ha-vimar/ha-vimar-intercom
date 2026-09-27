"""La voce verso il posto esterno: un pacchetto ogni 20 ms, non uno di più.

Misurato il 26 settembre: con la voce spedita subito e il silenzio di
riempimento a ogni tick in cui la voce era in ritardo anche di un
millisecondo, per 10 s di voce la targa riceveva 12,5-13,7 s di audio — da
122 a 186 pacchetti di silenzio infilati fra le parole. Voce a pezzi, orologio
RTP più veloce del vero, e il buffer della targa che cresceva: il secondo di
ritardo che si sentiva parlando dalla strada.
"""
import asyncio
import random
import time

import pytest

media = pytest.importorskip("custom_components.vimar_intercom.media_handler")

VOICE = b"\x11" * 320            # 20 ms di PCM a 8 kHz


class _Transport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, _addr):
        self.sent.append(data[12:])


def _proto(monkeypatch):
    p = media.RTPAudioProtocol.__new__(media.RTPAudioProtocol)
    p.transport, p.remote_addr, p.srtp_tx = _Transport(), ("127.0.0.1", 9), None
    p.rtp_seq = p.rtp_ts = 0
    p.rtp_ssrc = 1
    p.last_tx = 0.0
    monkeypatch.setattr(media, "audio_proto", p)
    getattr(media, "_reset_tx_voice", lambda: None)()
    return p


class TestFraming:
    def test_any_chunk_size_becomes_20ms_frames(self, monkeypatch):
        _proto(monkeypatch)
        media.send_audio(b"\x00" * 500)          # 250 campioni
        media.send_audio(b"\x00" * 140)          # 70: in tutto 320 = due fotogrammi
        assert [len(f) for f in media._tx_voice] == [160, 160]

    def test_the_queue_is_capped(self, monkeypatch):
        """Dopo un intoppo di rete il ritardo si butta, non si trascina."""
        _proto(monkeypatch)
        for _ in range(20):
            media.send_audio(VOICE)
        assert len(media._tx_voice) == media.TX_QUEUE_FRAMES


class TestTheClock:
    def test_one_packet_per_tick_and_voice_is_not_chopped(self, monkeypatch):
        p = _proto(monkeypatch)
        random.seed(3)

        async def main():
            tx = asyncio.create_task(media._audio_tx_loop())
            t0 = time.monotonic()
            due = t0
            for _ in range(75):                   # 1,5 s di voce con jitter
                due += 0.02
                await asyncio.sleep(max(0.0, due - time.monotonic()
                                        + random.gauss(0, 0.003)))
                media.send_audio(VOICE)
            await asyncio.sleep(0.05)
            tx.cancel()
            return time.monotonic() - t0

        elapsed = asyncio.run(main())
        sent = p.transport.sent
        silence = sum(1 for pl in sent if pl == media.SILENCE_ULAW)
        # l'orologio RTP segue il tempo vero (margine largo: CI lente)
        assert abs(len(sent) * 0.02 - elapsed) < 0.12, (len(sent), elapsed)
        # e fra una parola e l'altra non si infila silenzio
        assert silence <= 6, silence
