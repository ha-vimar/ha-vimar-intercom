"""SRTP — AES_CM_128_HMAC_SHA1_80 encrypt/decrypt (RFC 3711).

AES-CTR nativo via pycryptodome (già fra i requirements del manifest): nessun
loop Python e nessuna dipendenza non dichiarata. Il contatore è il blocco da
128 bit completo, come richiede AES-CM di RFC 3711.
"""

import base64
import hashlib
import hmac
import struct

from Crypto.Cipher import AES
from Crypto.Util import Counter


def _aes_ctr(key: bytes, iv: bytes):
    """Cifrario AES-CTR con contatore a 128 bit inizializzato a `iv`."""
    ctr = Counter.new(128, initial_value=int.from_bytes(iv, "big"))
    return AES.new(key, AES.MODE_CTR, counter=ctr)


def _aes_cm_keystream(key: bytes, iv: bytes, length: int) -> bytes:
    """Keystream AES-CM (Counter Mode), RFC 3711 §4.1.1."""
    return _aes_ctr(key, iv).encrypt(bytes(length))


def _aes_cm_xor(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Cifra/decifra AES-CM (XOR con il keystream)."""
    return _aes_ctr(key, iv).encrypt(data)


def _kdf(master_key: bytes, master_salt: bytes, label: int, length: int) -> bytes:
    """SRTP Key Derivation Function (RFC 3711 §4.3.1)."""
    key_id = (label << 48).to_bytes(7, "big")
    salt = master_salt[:14]
    key_id_padded = b"\x00" * 7 + key_id
    x = bytes(a ^ b for a, b in zip(key_id_padded, salt))
    iv = x + b"\x00\x00"
    return _aes_cm_keystream(master_key, iv, length)


class SRTPContext:
    """SRTP encryption/decryption context for one direction."""

    AUTH_TAG_LEN = 10  # 80-bit HMAC-SHA1

    def __init__(self, master_key_b64: str):
        """Initialize from base64-encoded inline key (30 bytes = 16 key + 14 salt)."""
        raw = base64.b64decode(master_key_b64)
        if len(raw) < 30:
            raise ValueError(f"SRTP key too short: {len(raw)} bytes (need 30)")
        self.master_key = raw[:16]
        self.master_salt = raw[16:30]

        # Derive session keys
        self.cipher_key = _kdf(self.master_key, self.master_salt, 0x00, 16)
        self.auth_key = _kdf(self.master_key, self.master_salt, 0x01, 20)
        self.salt = _kdf(self.master_key, self.master_salt, 0x02, 14)

        # ROC (Rollover Counter) — tracks SEQ wraparounds
        self.roc = 0
        self._last_seq = None
        self._initialized = False

    def _estimate_index(self, seq: int) -> tuple[int, int]:
        """Estimate packet index using libsrtp-style algorithm (RFC 3711 §3.3.1).

        Returns (estimated_roc, packet_index) — handles out-of-order packets
        correctly by picking the ROC that produces the closest index to the
        last known good index.
        """
        if not self._initialized:
            return self.roc, (self.roc << 16) | seq

        last_idx = (self.roc << 16) | self._last_seq

        # Three candidate ROCs: current, current-1, current+1
        # Pick the one that produces the index closest to last_idx
        candidates = []
        for roc_delta in (0, 1, -1):
            r = self.roc + roc_delta
            if r < 0:
                continue
            idx = (r << 16) | seq
            candidates.append((abs(idx - last_idx), r, idx))

        candidates.sort()
        return candidates[0][1], candidates[0][2]

    def _update_roc(self, seq: int, estimated_roc: int):
        """Update ROC and last_seq after successful authentication."""
        if not self._initialized:
            self._last_seq = seq
            self._initialized = True
            return
        estimated_idx = (estimated_roc << 16) | seq
        current_idx = (self.roc << 16) | self._last_seq
        if estimated_idx > current_idx:
            self._last_seq = seq
            self.roc = estimated_roc

    def _compute_iv(self, ssrc: int, packet_index: int) -> bytes:
        """Compute IV for AES-CM encryption (RFC 3711 §4.1)."""
        salt_padded = self.salt + b"\x00\x00"
        ssrc_index = (
            b"\x00\x00\x00\x00"
            + ssrc.to_bytes(4, "big")
            + packet_index.to_bytes(6, "big")
            + b"\x00\x00"
        )
        return bytes(a ^ b for a, b in zip(salt_padded, ssrc_index))

    def _compute_auth_tag(self, rtp_packet: bytes, roc: int) -> bytes:
        """HMAC-SHA1 over (packet || ROC), truncated to 80 bits."""
        data = rtp_packet + struct.pack("!I", roc)
        return hmac.new(self.auth_key, data, hashlib.sha1).digest()[:self.AUTH_TAG_LEN]

    def unprotect(self, srtp_packet: bytes) -> bytes | None:
        """Decrypt SRTP packet → plain RTP packet. Returns None on auth failure."""
        if len(srtp_packet) < 12 + self.AUTH_TAG_LEN:
            return None

        auth_tag = srtp_packet[-self.AUTH_TAG_LEN:]
        authenticated_portion = srtp_packet[:-self.AUTH_TAG_LEN]

        # Parse RTP header
        cc = authenticated_portion[0] & 0x0F
        hdr_len = 12 + cc * 4

        if authenticated_portion[0] & 0x10:  # X bit
            if len(authenticated_portion) > hdr_len + 4:
                ext_len = struct.unpack_from("!HH", authenticated_portion, hdr_len)
                hdr_len += 4 + ext_len[1] * 4

        if len(authenticated_portion) <= hdr_len:
            return None

        seq = struct.unpack_from("!H", authenticated_portion, 2)[0]
        ssrc = struct.unpack_from("!I", authenticated_portion, 8)[0]

        est_roc, idx = self._estimate_index(seq)

        # Verify auth tag with estimated ROC
        expected_tag = self._compute_auth_tag(authenticated_portion, est_roc)
        if not hmac.compare_digest(auth_tag, expected_tag):
            return None

        # Auth passed — update ROC state
        self._update_roc(seq, est_roc)

        # Decrypt payload — single native AES-CTR call
        header = authenticated_portion[:hdr_len]
        encrypted_payload = authenticated_portion[hdr_len:]
        iv = self._compute_iv(ssrc, idx)
        decrypted = _aes_cm_xor(self.cipher_key, iv, encrypted_payload)

        return header + decrypted

    def protect(self, rtp_packet: bytes) -> bytes:
        """Encrypt plain RTP packet → SRTP packet."""
        cc = rtp_packet[0] & 0x0F
        hdr_len = 12 + cc * 4

        if rtp_packet[0] & 0x10:  # X bit
            if len(rtp_packet) > hdr_len + 4:
                ext_len = struct.unpack_from("!HH", rtp_packet, hdr_len)
                hdr_len += 4 + ext_len[1] * 4

        seq = struct.unpack_from("!H", rtp_packet, 2)[0]
        ssrc = struct.unpack_from("!I", rtp_packet, 8)[0]

        est_roc, idx = self._estimate_index(seq)
        self._update_roc(seq, est_roc)

        # Encrypt payload — single native AES-CTR call
        header = rtp_packet[:hdr_len]
        payload = rtp_packet[hdr_len:]
        iv = self._compute_iv(ssrc, idx)
        encrypted = _aes_cm_xor(self.cipher_key, iv, payload)

        srtp_no_tag = header + encrypted
        auth_tag = self._compute_auth_tag(srtp_no_tag, self.roc)

        return srtp_no_tag + auth_tag


class SRTCPContext:
    """SRTCP per un verso solo (RFC 3711 §3.4).

    Non è SRTP con un'altra chiave: cambia il formato e cambia cosa si
    autentica. Le differenze che contano, tutte già costate a qualcuno:

    * le chiavi di sessione nascono da etichette diverse — 3 cifratura,
      4 autenticazione, 5 sale — dalla stessa chiave madre dell'``a=crypto``;
    * l'intestazione del primo record resta in chiaro, SSRC compreso: si
      cifra dall'ottavo byte in poi;
    * in coda al pacchetto va una parola da 32 bit con il bit E (cifrato) e
      un indice a 31 bit che cresce di uno a ogni pacchetto mandato e non
      riparte quando si cambia chiave;
    * il tag di autenticazione copre anche quella parola, e NON ci si
      aggiunge il rollover counter come si fa in SRTP;
    * con ``AES_CM_128_HMAC_SHA1_80`` il tag su RTCP resta di 80 bit anche
      quando su RTP è di 32 (§5.2).
    """

    AUTH_TAG_LEN = 10
    _E_BIT = 0x80000000

    def __init__(self, master_key_b64: str):
        raw = base64.b64decode(master_key_b64)
        if len(raw) < 30:
            raise ValueError(f"SRTCP key too short: {len(raw)} bytes (need 30)")
        self.master_key = raw[:16]
        self.master_salt = raw[16:30]
        self.cipher_key = _kdf(self.master_key, self.master_salt, 0x03, 16)
        self.auth_key = _kdf(self.master_key, self.master_salt, 0x04, 20)
        self.salt = _kdf(self.master_key, self.master_salt, 0x05, 14)
        self.index = 0

    def _compute_iv(self, ssrc: int, index: int) -> bytes:
        salt_padded = self.salt + b"\x00\x00"
        ssrc_index = (
            b"\x00\x00\x00\x00"
            + ssrc.to_bytes(4, "big")
            + index.to_bytes(6, "big")
            + b"\x00\x00"
        )
        return bytes(a ^ b for a, b in zip(salt_padded, ssrc_index))

    def unprotect(self, packet: bytes) -> bytes | None:
        """SRTCP → RTCP in chiaro, oppure ``None`` se non è autentico."""
        if len(packet) < 8 + 4 + self.AUTH_TAG_LEN:
            return None
        tag = packet[-self.AUTH_TAG_LEN:]
        signed = packet[:-self.AUTH_TAG_LEN]
        expected = hmac.new(self.auth_key, signed, hashlib.sha1).digest()[:self.AUTH_TAG_LEN]
        if not hmac.compare_digest(tag, expected):
            return None
        e_index = struct.unpack("!I", signed[-4:])[0]
        header, payload = signed[:8], signed[8:-4]
        if not e_index & self._E_BIT:
            return header + payload
        ssrc = struct.unpack("!I", packet[4:8])[0]
        iv = self._compute_iv(ssrc, e_index & ~self._E_BIT)
        return header + _aes_cm_xor(self.cipher_key, iv, payload)

    def protect(self, rtcp_packet: bytes) -> bytes:
        """RTCP in chiaro → SRTCP, cifrato e autenticato."""
        self.index = (self.index + 1) & 0x7FFFFFFF
        ssrc = struct.unpack_from("!I", rtcp_packet, 4)[0]
        header, payload = rtcp_packet[:8], rtcp_packet[8:]
        iv = self._compute_iv(ssrc, self.index)
        encrypted = _aes_cm_xor(self.cipher_key, iv, payload)
        body = header + encrypted + struct.pack("!I", self._E_BIT | self.index)
        tag = hmac.new(self.auth_key, body, hashlib.sha1).digest()[:self.AUTH_TAG_LEN]
        return body + tag
