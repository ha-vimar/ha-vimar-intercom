"""QR sintetico: Base64(AESkey[32] | AES-256-CBC-PKCS5(text) | IV[16]) → dict campi."""
import base64
import os

import pytest

Crypto = pytest.importorskip("Crypto")
from Crypto.Cipher import AES  # noqa: E402
from Crypto.Util.Padding import pad  # noqa: E402

qr = pytest.importorskip("custom_components.vimar_intercom.qr_decoder")

PAYLOAD = (
    "ID=12345\nPWD=secret\nPROXY=192.168.1.50\nDOMAIN=abc.ipvdes.vimar.cloud\n"
    "CPROXY=ipvdes.vimar.cloud\nCDOMAIN=abc.ipvdes.vimar.cloud\nGID=101\nMAC=AA:BB:CC:DD:EE:FF\n"
    "PC=40507\nPLANTTYPE=2F\nCLOUD=0\nVIDEO=1\n"
)


def _make_qr(text: str) -> str:
    key, iv = os.urandom(32), os.urandom(16)
    ct = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(text.encode(), 16))
    return base64.b64encode(key + ct + iv).decode()


def _decode(s: str) -> dict:
    # tollera nomi diversi della funzione pubblica
    for name in ("decode_vimar_qr", "decode_qr", "decode", "parse_qr"):
        fn = getattr(qr, name, None)
        if fn:
            out = fn(s)
            return out if isinstance(out, dict) else getattr(out, "__dict__", {})
    pytest.skip("funzione di decodifica QR non trovata in qr_decoder")


def test_qr_roundtrip_fields():
    d = {k.lower(): v for k, v in _decode(_make_qr(PAYLOAD)).items()}
    assert d.get("id") in ("12345", 12345)
    assert d.get("gid") in ("101", 101)
    assert str(d.get("planttype", d.get("plant_type", ""))).upper() == "2F"


def test_qr_invalid_base64_raises_or_returns_falsy():
    try:
        out = _decode("!!!non-base64!!!")
    except Exception:
        return
    assert not out


def test_la_password_non_finisce_nei_log(caplog):
    """Il payload decrittato contiene «PWD=...»: non deve comparire in nessun
    record, a nessun livello. Il buffer di debug interno cattura i DEBUG comunque
    e li serve via HTTP su /api/vimar_intercom/debug."""
    import logging

    caplog.set_level(logging.DEBUG)
    _decode(_make_qr(PAYLOAD))

    testo = "\n".join(r.getMessage() for r in caplog.records)
    assert "secret" not in testo, "la password SIP e' finita in un log"
    assert "PWD" not in testo


def test_i_nomi_dei_campi_restano_diagnosticabili(caplog):
    """Togliere il payload dal log non deve togliere la possibilità di capire
    perché un QR sbagliato non viene accettato."""
    import logging

    caplog.set_level(logging.DEBUG)
    _decode(_make_qr(PAYLOAD))

    testo = "\n".join(r.getMessage() for r in caplog.records)
    assert "domain" in testo and "planttype" in testo


def _raw_qr(plaintext: bytes) -> str:
    """A QR whose decrypted bytes are exactly `plaintext` (no padding added)."""
    key, iv = os.urandom(32), os.urandom(16)
    ct = AES.new(key, AES.MODE_CBC, iv).encrypt(plaintext)
    return base64.b64encode(key + ct + iv).decode()


@pytest.mark.parametrize("text, reason", [
    ("   ", "Empty"),
    (base64.b64encode(b"x" * 48).decode(), "too short"),
    # 33 ciphertext bytes: not a multiple of the AES block, the decrypt fails.
    (base64.b64encode(b"k" * 32 + b"c" * 17 + b"i" * 16).decode(), "AES"),
])
def test_a_malformed_qr_is_refused_with_a_reason(text, reason):
    with pytest.raises(qr.QRDecodeError, match=reason):
        qr.decode(text)


def test_a_qr_with_no_key_value_lines_is_refused():
    with pytest.raises(qr.QRDecodeError, match="No field"):
        qr.decode(_make_qr("\n\njust words\n"))


def test_a_qr_without_the_sip_fields_is_refused():
    with pytest.raises(qr.QRDecodeError, match="required SIP fields"):
        qr.decode(_make_qr("GID=101\nPLANTTYPE=2F\n"))


def test_blank_lines_and_lines_without_equals_are_skipped():
    fields = qr.decode(_make_qr("\nID=7\nnoise\n\nDOMAIN=example.test\n"))
    assert fields == {"id": "7", "domain": "example.test"}


def test_a_payload_without_valid_padding_is_kept_whole():
    # Last byte 0x41 ("A") is no PKCS7 pad length: nothing is stripped.
    fields = qr.decode(_raw_qr(b"ID=7\nDOMAIN=ab.test\nX=AAAAAAAAAA"))  # 32 bytes, two blocks
    assert fields["x"] == "AAAAAAAAAA"


def test_the_cryptography_backend_decrypts_when_pycryptodome_is_missing(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "Crypto.Cipher", None)  # import -> ImportError
    fields = qr.decode(_make_qr(PAYLOAD))
    assert fields["id"] == "12345" and fields["pwd"] == "secret"


def test_no_aes_library_is_a_readable_error(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "Crypto.Cipher", None)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers", None)
    with pytest.raises(qr.QRDecodeError, match="pycryptodome"):
        qr._aes_decrypt(b"k" * 32, b"c" * 16, b"i" * 16)


def test_an_empty_plaintext_decrypts_to_an_empty_string():
    assert qr._aes_decrypt(b"k" * 32, b"", b"i" * 16) == ""
