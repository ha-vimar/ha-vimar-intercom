"""Issue #6: il citofono trovato in rete via mDNS (`_eipvdes._tcp`).

Record come quelli osservati: 40507 (4 chiavi, `domain` = dominio cloud) e
40515 fw 2.1.0203 (11 chiavi, `domain` = IP del Tab, QR con domain=127.0.0.1).
"""
from __future__ import annotations

import asyncio
import importlib
import types

import pytest

CLOUD = "aabbccddeeff.aabbccddeeff1234567890.ipvdes.vimar.cloud"
TXT_40507 = {b"mac": b"AA:BB:CC:DD:EE:FF", b"domain": CLOUD.encode(), b"proxy": b"192.0.2.10",
             b"timestemp": b"1789696090"}
TXT_40515 = {b"dev": b"40515", b"mac": b"11:22:33:44:55:66", b"fver": b"2.1.0203",
             b"proxy": b"192.0.2.20", b"domain": b"192.0.2.20", b"sta": b"2"}


@pytest.fixture(scope="module")
def cf():
    # conftest stubs the HA modules it imports, ConfigFlow included.
    return importlib.import_module("custom_components.vimar_intercom.config_flow")


class _Entry:
    def __init__(self, data, options=None, state="loaded"):
        self.entry_id = "e1"
        self.data = data
        self.options = options or {}
        self.state = types.SimpleNamespace(value=state)


def _flow(cf, entries=()):
    flow = cf.VimarIntercomConfigFlow()
    flow.context = {}
    calls = {"update": [], "reload": [], "unique_id": None}

    def update(entry, data=None, options=None):
        calls["update"].append((data, options))

    flow.hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(
        async_update_entry=update, async_schedule_reload=lambda eid: calls["reload"].append(eid)))
    flow._async_current_entries = lambda include_ignore=True: list(entries)
    flow.async_abort = lambda reason: {"type": "abort", "reason": reason}
    flow.async_show_form = lambda **kw: {"type": "form", **kw}

    async def set_uid(uid):
        calls["unique_id"] = uid

    flow.async_set_unique_id = set_uid
    flow._abort_if_unique_id_configured = lambda **kw: None
    return flow, calls


def _info(host, txt):
    return types.SimpleNamespace(host=host, properties=txt)


def test_senza_mac_non_apre_il_flusso(cf):
    flow, _ = _flow(cf)
    r = asyncio.run(flow.async_step_zeroconf(_info("192.0.2.10", {b"proxy": b"192.0.2.10"})))
    assert r == {"type": "abort", "reason": "no_mac"}


def test_citofono_nuovo_chiede_conferma_con_i_dati_del_record(cf):
    flow, calls = _flow(cf)
    r = asyncio.run(flow.async_step_zeroconf(_info("192.0.2.20", TXT_40515)))
    assert r["type"] == "form" and r["step_id"] == "zeroconf_confirm"
    assert r["description_placeholders"] == {"host": "192.0.2.20", "mac": "11:22:33:44:55:66",
                                             "model": "40515", "firmware": "2.1.0203"}
    assert calls["unique_id"] == "112233445566"
    assert flow.context["title_placeholders"] == {"name": "Vimar 40515", "host": "192.0.2.20"}


def test_gia_configurato_col_mac_del_qr_aggiorna_l_ip_e_ricarica(cf):
    """Il MAC nel QR ha un'altra forma: si confronta normalizzato. Il DHCP ha cambiato l'IP:
    the new address is offered, and applied only once the user confirms (#46)."""
    entry = _Entry({"mac": "aabbccddeeff", "local_proxy": "192.0.2.99", "use_local_udp": True},
                   {"local_proxy": "192.0.2.99"})
    flow, calls = _flow(cf, [entry])
    r = asyncio.run(flow.async_step_zeroconf(_info("192.0.2.10", TXT_40507)))
    assert r["type"] == "form" and r["step_id"] == "zeroconf_moved"
    assert r["description_placeholders"] == {"old": "192.0.2.99", "new": "192.0.2.10"}
    assert calls["update"] == [] and calls["reload"] == []
    assert calls["unique_id"] == "AABBCCDDEEFF"
    r = asyncio.run(flow.async_step_zeroconf_moved({}))
    assert r == {"type": "abort", "reason": "address_updated"}
    data, options = calls["update"][0]
    assert data["local_proxy"] == "192.0.2.10" and options["local_proxy"] == "192.0.2.10"
    # No reload from here: local_proxy is connection data, the update listener reloads.
    assert calls["reload"] == []


# ─── #46: nothing in an mDNS record is authenticated, the MAC is public ─────

def _moved_entry():
    return _Entry({"mac": "AA-BB-CC-DD-EE-FF", "local_proxy": "192.0.2.99", "use_local_udp": True})


def test_an_address_in_the_txt_that_is_not_the_sender_is_ignored(cf):
    """Anyone on the LAN can announce the Tab's MAC with `proxy=` pointing elsewhere."""
    txt = {**TXT_40507, b"proxy": b"192.0.2.66"}
    flow, calls = _flow(cf, [_moved_entry()])
    r = asyncio.run(flow.async_step_zeroconf(_info("192.0.2.10", txt)))
    assert r == {"type": "abort", "reason": "already_configured"}
    assert calls["update"] == [] and calls["reload"] == []


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "169.254.1.10", "fd00::10"])
def test_only_a_private_ipv4_address_is_offered(cf, address):
    txt = {**TXT_40507, b"proxy": address.encode()}
    flow, calls = _flow(cf, [_moved_entry()])
    r = asyncio.run(flow.async_step_zeroconf(_info(address, txt)))
    assert r == {"type": "abort", "reason": "already_configured"}
    assert calls["update"] == []


def test_a_name_from_the_sender_is_never_offered(cf):
    """A TXT `proxy` that is not routable falls back to the sender, which must be an IP."""
    txt = {k: v for k, v in TXT_40507.items() if k != b"proxy"}
    flow, calls = _flow(cf, [_moved_entry()])
    r = asyncio.run(flow.async_step_zeroconf(_info("tab.example.test", txt)))
    assert r == {"type": "abort", "reason": "already_configured"}
    assert calls["update"] == []


def test_the_address_is_not_applied_without_confirmation(cf):
    flow, calls = _flow(cf, [_moved_entry()])
    r = asyncio.run(flow.async_step_zeroconf(_info("192.0.2.10", TXT_40507)))
    assert r["step_id"] == "zeroconf_moved"
    assert flow.context["title_placeholders"] == {"name": "Vimar Intercom", "host": "192.0.2.10"}
    assert asyncio.run(flow.async_step_zeroconf_moved())["step_id"] == "zeroconf_moved"
    assert calls["update"] == [] and calls["reload"] == []


def test_gia_configurato_in_cloud_non_tocca_niente(cf):
    entry = _Entry({"mac": "AA-BB-CC-DD-EE-FF", "local_proxy": "192.0.2.99", "use_local_udp": False})
    flow, calls = _flow(cf, [entry])
    r = asyncio.run(flow.async_step_zeroconf(_info("192.0.2.10", TXT_40507)))
    assert r["reason"] == "already_configured" and calls["update"] == [] and calls["reload"] == []


def test_installazione_manuale_senza_mac_riconosciuta_dall_ip(cf):
    entry = _Entry({"mac": "", "local_proxy": "192.0.2.10", "use_local_udp": True})
    flow, calls = _flow(cf, [entry])
    r = asyncio.run(flow.async_step_zeroconf(_info("192.0.2.10", TXT_40507)))
    assert r["reason"] == "already_configured" and calls["update"] == []


def test_40515_il_dominio_locale_viene_dal_record_non_dal_qr(cf, monkeypatch):
    """QR con domain=127.0.0.1: il decoder ripiega sul cdomain, e la registrazione
    locale con quello riceve 503. Il test di registrazione usa il dominio annunciato."""
    flow, _ = _flow(cf)
    asyncio.run(flow.async_step_zeroconf(_info("192.0.2.20", TXT_40515)))
    flow._credentials = {"sip_user": "60902", "sip_password": "pw", "sip_domain": CLOUD,
                         "local_domain": "127.0.0.1", "cloud_domain": CLOUD, "mac": ""}
    flow._apply_discovered()
    assert flow._credentials["local_domain"] == "192.0.2.20"
    assert flow._credentials["mac"] == "11:22:33:44:55:66"
    visti = []

    async def fake_test(**kw):
        visti.append(kw)
        return True, "200"

    monkeypatch.setattr(cf, "_test_sip_registration", fake_test)
    monkeypatch.setattr(cf.discovery, "is_ip", lambda ip: True)

    async def set_uid(uid):
        pass

    flow.async_set_unique_id = set_uid
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    r = asyncio.run(flow.async_step_network({"local_proxy": "192.0.2.20", "use_local_udp": True,
                                             "local_udp_port": 5060}))
    assert visti[0]["sip_domain"] == "192.0.2.20"
    assert r["type"] == "create_entry"
    assert r["data"]["local_domain"] == "192.0.2.20" and r["data"]["sip_domain"] == CLOUD


def test_senza_discovery_nulla_cambia(cf):
    flow, _ = _flow(cf)
    flow._credentials = {"sip_user": "1", "sip_domain": CLOUD, "local_domain": "", "mac": "x"}
    flow._apply_discovered()
    assert flow._credentials == {"sip_user": "1", "sip_domain": CLOUD, "local_domain": "", "mac": "x"}
    assert flow._local_test_domain() == CLOUD


def test_dominio_cloud_precompilato_solo_se_e_un_nome(cf):
    from custom_components.vimar_intercom import discovery
    assert not discovery.is_ip(CLOUD) and discovery.is_ip("192.0.2.20")
    assert cf._default("") == {} and cf._default("x") == {"default": "x"}


# ─── una sola installazione, senza `single_config_entry` nel manifest ────────
# Con quel flag Home Assistant fermava il discovery prima di arrivare qui, e il
# Tab che cambia IP in UDP locale non veniva più seguito.

def test_il_manifest_non_blocca_il_discovery():
    import json
    import pathlib
    manifest = json.loads((pathlib.Path(__file__).parents[1] / "custom_components"
                           / "vimar_intercom" / "manifest.json").read_text(encoding="utf-8"))
    assert "single_config_entry" not in manifest


def test_con_una_entry_il_cambio_ip_e_seguito(cf):
    entry = _Entry({"mac": "AA-BB-CC-DD-EE-FF", "local_proxy": "192.0.2.99", "use_local_udp": True})
    flow, calls = _flow(cf, [entry])
    r = asyncio.run(flow.async_step_zeroconf(_info("192.0.2.10", TXT_40507)))
    assert r["step_id"] == "zeroconf_moved"
    asyncio.run(flow.async_step_zeroconf_moved({}))
    # No reload from here: local_proxy is connection data, the update listener reloads.
    assert calls["reload"] == []
    assert calls["update"][0][0]["local_proxy"] == "192.0.2.10"


def test_un_secondo_tab_non_crea_una_seconda_entry(cf):
    entry = _Entry({"mac": "AA-BB-CC-DD-EE-FF", "local_proxy": "192.0.2.10"})
    flow, calls = _flow(cf, [entry])
    r = asyncio.run(flow.async_step_zeroconf(_info("192.0.2.20", TXT_40515)))
    assert r == {"type": "abort", "reason": "single_instance_allowed"}
    assert calls["update"] == [] and calls["unique_id"] is None


def test_aggiunta_a_mano_con_una_entry_gia_presente(cf):
    flow, _ = _flow(cf, [_Entry({"mac": "AA-BB-CC-DD-EE-FF"})])
    r = asyncio.run(flow.async_step_user())
    assert r == {"type": "abort", "reason": "single_instance_allowed"}


def test_aggiunta_a_mano_senza_entry_mostra_il_form(cf):
    flow, _ = _flow(cf)
    r = asyncio.run(flow.async_step_user())
    assert r["type"] == "form" and r["step_id"] == "user"


def test_an_ignored_discovery_is_not_offered_again(cf):
    """The user pressed Ignore on this Tab's discovery: HA keeps an ignored entry with
    the MAC as unique id, and the address change must not come back at every announcement."""
    flow, calls = _flow(cf, [_moved_entry()])
    checked = []
    flow._abort_if_unique_id_configured = lambda **kw: checked.append(calls["unique_id"])
    asyncio.run(flow.async_step_zeroconf(_info("192.0.2.10", TXT_40507)))
    assert checked == ["AABBCCDDEEFF"]


def test_a_new_address_for_an_entry_not_set_up_schedules_its_reload(cf):
    """An entry waiting in setup_retry (the Tab moved and the old address does not
    answer) has no update listener: the flow reloads it with the new address."""
    entry = _Entry({"mac": "AA-BB-CC-DD-EE-FF", "local_proxy": "192.0.2.99", "use_local_udp": True},
                   state="setup_retry")
    flow, calls = _flow(cf, [entry])
    asyncio.run(flow.async_step_zeroconf(_info("192.0.2.10", TXT_40507)))
    asyncio.run(flow.async_step_zeroconf_moved({}))
    assert calls["reload"] == ["e1"]
