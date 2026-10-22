"""μ-law encoding through a lookup table gives the same bytes as the loop."""
from __future__ import annotations

import random
import struct
import time

from custom_components.vimar_intercom import media_handler as media


def _ulaw_encode_sample(sample: int) -> int:
    """One 16-bit signed sample → its μ-law code (the reference algorithm)."""
    BIAS = 0x84
    CLIP = 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, CLIP) + BIAS
    exp = 7
    mask = 0x4000
    while exp > 0 and not (sample & mask):
        exp -= 1
        mask >>= 1
    mantissa = (sample >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mantissa)) & 0xFF


def _reference(pcm_data: bytes) -> bytes:
    """The per-sample implementation ulaw_encode replaced (the exponent loop)."""
    n = len(pcm_data) // 2
    return bytes(_ulaw_encode_sample(struct.unpack_from("<h", pcm_data, i * 2)[0])
                 for i in range(n))


def test_every_sample_encodes_as_before():
    pcm = b"".join(struct.pack("<h", s) for s in range(-32768, 32768))
    assert media.ulaw_encode(pcm) == _reference(pcm)


def test_the_table_is_the_reference_algorithm():
    assert len(media._ULAW_ENCODE) == 65536
    for unsigned in (0, 1, 0x7FFF, 0x8000, 0xFFFF, 132, 133, 32635, 32636, 65536 - 32635):
        signed = unsigned - 65536 if unsigned >= 32768 else unsigned
        assert media._ULAW_ENCODE[unsigned] == _ulaw_encode_sample(signed)


def test_a_random_buffer_encodes_as_before():
    pcm = random.Random(711).randbytes(4000)
    assert media.ulaw_encode(pcm) == _reference(pcm)


def test_an_odd_trailing_byte_is_ignored_as_before():
    pcm = b"\x10\x20\x30"
    assert media.ulaw_encode(pcm) == _reference(pcm) == media.ulaw_encode(pcm[:2])
    assert media.ulaw_encode(b"") == b""


def test_silence_is_the_known_code():
    # 0 → magnitude 0x84 → exponent 0, mantissa 0 → complement 0xFF.
    assert media.ulaw_encode(bytes(320)) == b"\xff" * 160
    assert media.SILENCE_ULAW == b"\xff" * 160


def test_the_table_builds_quickly():
    """At import; the one-sample loop took 80 ms for 65536 samples on a Pi 5."""
    t0 = time.perf_counter()
    table = media._build_ulaw_encode_table()
    assert time.perf_counter() - t0 < 0.05
    assert table == media._ULAW_ENCODE
