"""Oscuramento delle credenziali nel buffer di debug.

Il buffer che alimenta `/api/vimar_intercom/debug` cattura i record DEBUG a
prescindere da come è configurato `logger:`. Una riga di diagnostica scritta
senza pensarci diventerebbe una credenziale leggibile via HTTP: questi test
fissano la rete di sicurezza che sta in mezzo.
"""
from __future__ import annotations

import pytest

lr = pytest.importorskip("custom_components.vimar_intercom.log_redact")


# ─── la forma del payload del QR di abbinamento ──────────────────────────

def test_password_del_qr_non_sopravvive():
    out = lr.redact("QR payload: ID=12345\nPWD=segretissima\nDOMAIN=x.y")
    assert "segretissima" not in out
    assert "ID=12345" in out, "gli altri campi restano leggibili"


@pytest.mark.parametrize("chiave", ["PWD", "pwd", "password", "passwd", "ha1", "secret"])
def test_tutti_i_nomi_che_portano_un_segreto(chiave):
    assert "valoresegreto" not in lr.redact(f"{chiave}=valoresegreto")


def test_il_nome_della_chiave_resta_visibile():
    """Serve a capire cosa stava succedendo: si oscura il valore, non il campo."""
    assert "ha1" in lr.redact("ha1=0123456789abcdef0123456789abcdef")


# ─── digest SIP: da lì si attacca la password offline ──────────────────────

@pytest.mark.parametrize("header", ["Authorization", "authorization", "Proxy-Authorization"])
def test_header_di_autorizzazione(header):
    riga = f'{header}: Digest username="101", realm="r", response="a1b2c3d4e5f6a1b2"'
    out = lr.redact(riga)
    assert "a1b2c3d4e5f6a1b2" not in out
    assert header.split(":")[0].lower() in out.lower()


def test_response_digest_isolata():
    assert "deadbeefcafe1234" not in lr.redact('response="deadbeefcafe1234"')


# ─── token della rubrica: va trattato come la password SIP ─────────────────

def test_token_in_get_init_status_reply():
    body = 'GET_INIT_STATUS_REPLY;[{"PARAM":"dnd","VALUE":"1"},{"PARAM":"token","VALUE":"abc123def"}]'
    out = lr.redact(body)
    assert "abc123def" not in out
    assert '"PARAM":"dnd","VALUE":"1"' in out, "i parametri non sensibili restano"


# ─── niente falsi positivi: il log deve restare utile ─────────────────────

def test_una_riga_innocua_non_viene_toccata():
    riga = "Door command: uri=sip:55001@x.ipvdes.vimar.cloud body=OPEN_2F registered=True"
    assert lr.redact(riga) == riga


def test_la_parola_token_in_prosa_non_attiva_l_oscuramento():
    riga = "Registered push token for iPhone di Lorenzo"
    assert lr.redact(riga) == riga


# ─── robustezza: gira dentro un logging handler ─────────────────────────

def test_non_solleva_mai(monkeypatch):
    """Un'eccezione qui farebbe perdere la riga, o peggio la lascerebbe passare."""
    monkeypatch.setattr(lr, "_ASSIGN", None)  # rompe la regex di proposito
    assert lr.redact("PWD=x") == lr.MASK


@pytest.mark.parametrize("vuoto", ["", None])
def test_input_vuoto(vuoto):
    assert lr.redact(vuoto) == vuoto


# ─── chiavi SRTP (revisione del 26/09) ───────────────────────────────────────

SRTP_KEY = "WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz"


def test_the_srtp_key_in_an_sdp_line_is_hidden():
    line = f"[SDP <<<]   a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{SRTP_KEY}"
    out = lr.redact(line)
    assert SRTP_KEY not in out
    assert "AES_CM_128_HMAC_SHA1_80 inline:" in out, "the suite stays readable"


@pytest.mark.parametrize("name", ["crypto_key", "a_srtp_key", "v_srtp_key"])
def test_the_srtp_key_in_a_parsed_sdp_dict_is_hidden(name):
    out = lr.redact(f"SDP: audio={{'port': 4000, '{name}': '{SRTP_KEY}'}}")
    assert SRTP_KEY not in out
    assert "'port': 4000" in out
