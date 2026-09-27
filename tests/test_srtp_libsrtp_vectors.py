"""SRTP and SRTCP packets checked against libsrtp, byte for byte.

A protect/unprotect round trip only proves the code agrees with itself: a
wrong IV or a wrong ROC on both sides still round-trips. These vectors were
produced once by libsrtp 2 (via pylibsrtp 1.0.0) with the key below and the
AES_CM_128_HMAC_SHA1_80 profile, and are pinned here. They cross a sequence
wrap, so the rollover counter is exercised too.
"""
import pytest

srtp = pytest.importorskip("custom_components.vimar_intercom.srtp")

KEY_B64 = "AQIDBAUGBwgJCgsMDQ4PEEBBQkNERUZHSElKS0xN"

# (plain RTP, SRTP as libsrtp protected it), sequence 65533 → 1
PACKETS = [
    ("8060fffd000103e511223344fdfeff000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f2021222324",
     "8060fffd000103e51122334487450f87875344cd4a5968e05bc2ba185d0a1340b40d178f2e6809db9686cb30cd8dbe360235443e5e7c7b07e6f1d6bd637a"),
    ("8060fffe000103e611223344feff000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425",
     "8060fffe000103e611223344c43407840f4922bfcc21729fc55ceecf546af82cd25f837546031f9f434509239765139204e4f3e408d234f55e17463843c1"),
    ("8060ffff000103e711223344ff000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20212223242526",
     "8060ffff000103e71122334419ef3d04bf54a86abe7c6e8c3832ced15841c6cc8d0dafaf7562f742a34cc0b6cb4d95951b0a9be008e8eda2d5b887ddc76c"),
    ("80600000000003e811223344000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f2021222324252627",
     "80600000000003e811223344f86750203f6881d23c0b818dc94fd070f5f47e9c19025fa86250478bc8c02bca69fbafccbe3339d4d3c65971a9001323a169"),
    ("80600001000003e9112233440102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728",
     "80600001000003e91122334434bd08343895890716dfe14d47ecbe31c0e90631f09d779ba04d22d0d68b72f7d829cc984da48e05109e732022c62c1288f5"),
]

# A sender report, SRTCP index 1
RTCP_SR = "80c8000611223344e123456789abcdef00015f900000002a00001068"
RTCP_SR_PROTECTED = "80c80006112233443db58e463007a6ff3e9bd6f1b1cd9e9b488a9e4b8000000137b4c876d0481d79d19a"


def test_we_decrypt_what_libsrtp_encrypted_across_the_wrap():
    rx = srtp.SRTPContext(KEY_B64)
    for plain, protected in PACKETS:
        assert rx.unprotect(bytes.fromhex(protected)) == bytes.fromhex(plain)
    assert rx.roc == 1, "the rollover counter moved on at the wrap"


def test_we_encrypt_exactly_what_libsrtp_does_across_the_wrap():
    tx = srtp.SRTPContext(KEY_B64)
    for plain, protected in PACKETS:
        assert tx.protect(bytes.fromhex(plain)) == bytes.fromhex(protected)


def test_a_flipped_bit_is_refused():
    rx = srtp.SRTPContext(KEY_B64)
    bad = bytearray.fromhex(PACKETS[0][1])
    bad[20] ^= 0x01
    assert rx.unprotect(bytes(bad)) is None


def test_srtcp_matches_libsrtp_both_ways():
    assert srtp.SRTCPContext(KEY_B64).unprotect(bytes.fromhex(RTCP_SR_PROTECTED)) == bytes.fromhex(RTCP_SR)
    assert srtp.SRTCPContext(KEY_B64).protect(bytes.fromhex(RTCP_SR)) == bytes.fromhex(RTCP_SR_PROTECTED)
