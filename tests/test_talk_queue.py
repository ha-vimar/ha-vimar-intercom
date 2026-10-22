"""The voice queued for the panel is capped, so the delay cannot build up."""

from custom_components.vimar_intercom import media_handler as media


def test_a_burst_keeps_only_the_newest_80_ms(monkeypatch):
    proto = media.RTPAudioProtocol()
    proto.remote_addr = ("198.51.100.7", 40000)
    monkeypatch.setattr(media, "audio_proto", proto)
    for i in range(50):                       # 1 s of voice at once (20 ms blocks)
        media.send_audio(bytes([i]) * 320)
    assert len(proto.tx_buf) == media._TX_MAX == 640
    assert proto.tx_buf[-1] == media.ulaw_encode(bytes([49]) * 2)[0], "newest kept"
