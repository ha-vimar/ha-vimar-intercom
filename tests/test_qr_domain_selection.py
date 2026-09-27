"""Scelta del dominio SIP fra «domain» (locale) e «cdomain» (cloud).

Regressione per l'issue #1 (TAB 5S UP): quando il QR porta domain=127.0.0.1
il dominio locale non è usabile da Home Assistant e va preso il cloud, e in
modalità cloud il dominio attivo deve essere cdomain anche quando il locale
è instradabile.
"""
from __future__ import annotations

from custom_components.vimar_intercom import qr_decoder, runtime

LOCAL = "192.0.2.10"
CLOUD = "abc.ipvdes.vimar.cloud"


def _fields(**over) -> dict:
    f = {
        "id": "12345",
        "pwd": "secret",
        "domain": LOCAL,
        "cdomain": CLOUD,
        "cproxy": "ipvdes.vimar.cloud",
        "gid": "101",
        "planttype": "2F",
        "mac": "AA:BB:CC:DD:EE:FF",
    }
    f.update(over)
    return f


# ─── qr_decoder ──────────────────────────────────────────────────────────────

def test_both_domains_are_kept():
    c = qr_decoder.extract_sip_credentials(_fields())
    assert c["local_domain"] == LOCAL
    assert c["cloud_domain"] == CLOUD


def test_routable_local_domain_is_preferred_by_default():
    c = qr_decoder.extract_sip_credentials(_fields())
    assert c["sip_domain"] == LOCAL


def test_loopback_local_domain_falls_back_to_cloud():
    # Caso dell'issue #1: domain=127.0.0.1
    c = qr_decoder.extract_sip_credentials(_fields(domain="127.0.0.1"))
    assert c["sip_domain"] == CLOUD


def test_missing_local_domain_falls_back_to_cloud():
    f = _fields()
    del f["domain"]
    c = qr_decoder.extract_sip_credentials(f)
    assert c["sip_domain"] == CLOUD


def test_missing_cloud_domain_keeps_local():
    f = _fields()
    del f["cdomain"]
    c = qr_decoder.extract_sip_credentials(f)
    assert c["sip_domain"] == LOCAL
    assert c["cloud_domain"] == ""


def test_loopback_local_without_cloud_is_not_silently_dropped():
    f = _fields(domain="127.0.0.1")
    del f["cdomain"]
    c = qr_decoder.extract_sip_credentials(f)
    # Niente cdomain: si tiene il valore del QR, l'utente lo correggerà a mano
    assert c["sip_domain"] == "127.0.0.1"


# ─── runtime.configure ───────────────────────────────────────────────────────

def _entry(**over) -> dict:
    d = {
        "sip_user": "12345",
        "sip_password": "secret",
        "sip_domain": LOCAL,
        "local_domain": LOCAL,
        "cloud_domain": CLOUD,
    }
    d.update(over)
    return d


def test_local_mode_uses_local_domain():
    runtime.configure(_entry(use_local_udp=True))
    assert runtime.SIP_DOMAIN == LOCAL
    assert runtime.INTERCOM.endswith(f"@{LOCAL}")


def test_cloud_mode_uses_cloud_domain():
    runtime.configure(_entry(use_local_udp=False))
    assert runtime.SIP_DOMAIN == CLOUD
    assert runtime.INTERCOM.endswith(f"@{CLOUD}")


def test_cloud_mode_recomputes_ha1_on_the_active_domain():
    import hashlib

    runtime.configure(_entry(use_local_udp=False))
    expected = hashlib.md5(f"12345:{CLOUD}:secret".encode()).hexdigest()
    assert runtime.SIP_HA1 == expected


def test_entry_without_the_new_keys_is_unchanged():
    """Config entry creato da una versione precedente: nessun local/cloud
    domain salvato → si continua a usare sip_domain così com'è."""
    runtime.configure({
        "sip_user": "12345",
        "sip_password": "secret",
        "sip_domain": LOCAL,
        "sip_ha1": "deadbeef",
        "use_local_udp": False,
    })
    assert runtime.SIP_DOMAIN == LOCAL
    # Con la password l'HA1 si ricalcola sempre: su un'entry sana è lo stesso
    # valore salvato, su una salvata per il realm sbagliato è quello giusto.
    import hashlib
    assert runtime.SIP_HA1 == hashlib.md5(f"12345:{LOCAL}:secret".encode()).hexdigest()


def test_without_a_password_the_stored_ha1_is_kept():
    runtime.configure({
        "sip_user": "12345",
        "sip_domain": LOCAL,
        "sip_ha1": "deadbeef",
        "use_local_udp": False,
    })
    assert runtime.SIP_HA1 == "deadbeef"
