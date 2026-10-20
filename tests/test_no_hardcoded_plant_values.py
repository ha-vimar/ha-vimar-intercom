"""Nessun valore specifico di un impianto può tornare costante nel sorgente.

L'integrazione è stata sviluppata su un solo impianto: ogni valore che vale solo
lì (indirizzo SGA, dominio/proxy cloud del QR, identità dispositivo) deve stare
nel config entry. Questi test sono guardie di regressione: falliscono se uno di
quei valori rientra nel codice come costante.
"""

from pathlib import Path

import pytest

from custom_components.vimar_intercom import const

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "vimar_intercom"


@pytest.mark.parametrize("name", ["SIP_SNI", "SIP_ROUTE"])
def test_const_exposes_no_cloud_host(name):
    """SNI e Route valgono <cproxy>, che arriva dal QR (runtime.SIP_PROXY)."""
    assert not hasattr(const, name), (
        f"const.{name} è tornata: su un impianto con cproxy diverso manderebbe "
        f"in handshake TLS il nome sbagliato"
    )


@pytest.mark.parametrize("token", ["C.SIP_SNI", "C.SIP_ROUTE"])
def test_sip_client_takes_the_cloud_host_from_the_entry(token):
    src = (COMPONENT / "sip_client.py").read_text(encoding="utf-8")
    assert token not in src, f"sip_client.py usa ancora {token} invece di R.SIP_PROXY"


def test_door_stats_do_not_hardcode_the_sga():
    """`SGA_TARGET` è configurabile dalla 1.0.0: anche le stats devono usarlo."""
    src = (COMPONENT / "hub.py").read_text(encoding="utf-8")
    assert 'target or "55001"' not in src, (
        "hub.py registra un SGA fisso nelle statistiche: usa R.SGA_TARGET"
    )


# ─── Le entità che aprono la porta devono seguire l'SGA configurato ──────────

def _codice(nome: str) -> str:
    """Sorgente del modulo senza commenti: interessa cosa fa, non cosa spiega."""
    righe = (COMPONENT / nome).read_text(encoding="utf-8").splitlines()
    return "\n".join(r for r in righe if not r.lstrip().startswith("#"))


# hub.py resta fuori: contiene SIP_ID_NAMES e MODEL_PROBE_TARGETS, che sono una
# tabella di nomi leggibili e una lista di indirizzi da sondare, non un
# destinatario di comandi. Per hub.py c'e' gia' la guardia mirata sopra.
@pytest.mark.parametrize("modulo", ["lock.py", "button.py", "switch.py"])
def test_nessun_indirizzo_di_impianto_cablato(modulo):
    """`SGA_TARGET` è configurabile dalla 1.0.0, ma `lock.py` passava `"55001"`
    come letterale e non importava nemmeno `runtime`.

    Su un impianto con `SYSTEM.MAGIC_APT_INTERCOM` diverso il comando partiva
    verso l'indirizzo sbagliato: risposta `200` senza effetto, che `async_door`
    conta come successo. L'utente vedeva la serratura "sbloccata" per cinque
    secondi con la porta chiusa — il modo peggiore di fallire.
    """
    src = _codice(modulo)
    assert '"55001"' not in src, (
        f"{modulo} cabla l'SGA dell'impianto di sviluppo: usa runtime.SGA_TARGET "
        f"(o target=None, che fa risolvere il default all'hub)"
    )


def test_le_targhe_da_chiamare_vengono_dalle_opzioni():
    """Issue #3: targa video e pannello interno sono opzioni (runtime), con il
    default storico isolato in const; button.py non li ripete e non chiama l'SGA."""
    assert const.INTERNAL_PANEL_TARGET == "55002"
    assert const.CAMERA_TARGET == "55100"
    src = _codice("button.py")
    assert '"55002"' not in src and '"55100"' not in src
    assert "C.INTERNAL_PANEL_TARGET" not in src, "button.py deve leggere R.INTERNAL_PANEL_TARGET"
    assert "R.INTERNAL_PANEL_TARGET" in src
    assert "R.CAMERA_TARGET" in src
    # «Chiama Video (esterno)» non deve più chiamare l'SGA.
    riga = next(r for r in src.splitlines() if '"call_ext"' in r)
    assert "SGA_TARGET" not in riga


def test_l_autoaccensione_segue_l_opzione():
    src = _codice("hub.py")
    assert "C.CAMERA_TARGET" not in src, "hub.py deve usare R.CAMERA_TARGET (configurabile)"
