"""μ-law decoding through translation tables gives the same PCM as the loop."""
from __future__ import annotations

import random
import struct

from custom_components.vimar_intercom import media_handler as media


def _reference(data: bytes) -> bytes:
    """The per-sample implementation ulaw_decode replaced."""
    pcm = bytearray(len(data) * 2)
    for i, b in enumerate(data):
        struct.pack_into("<h", pcm, i * 2, media._ULAW_DECODE[b])
    return bytes(pcm)


def test_every_code_decodes_as_before():
    data = bytes(range(256))
    assert media.ulaw_decode(data) == _reference(data)


def test_a_random_buffer_decodes_as_before():
    data = random.Random(711).randbytes(4000)
    assert media.ulaw_decode(data) == _reference(data)


def test_decoding_goes_through_the_byte_tables():
    assert len(media._ULAW_LO) == len(media._ULAW_HI) == 256
    assert media.ulaw_decode(b"") == b""


def test_encode_then_decode_round_trips_through_the_tables():
    silence = media.ulaw_encode(bytes(320))
    assert media.ulaw_decode(silence) == _reference(silence)
