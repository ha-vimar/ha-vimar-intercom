"""Issue #5: rubrica dal cloud Vimar col token della risposta lunga a GET_INIT_STATUS.

Valori finti con la forma di quelli di @CPietro (40515 / 2FV2): il cdomain va
COMPLETO nel Digest e SENZA ".<cproxy>" nel percorso.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest
import requests

from custom_components.vimar_intercom import cloud_phonebook as cp

CPROXY = "ipvdes.vimar.cloud"
CDOMAIN = "1234567890ab.FFFFFFFFFF.ipvdes.vimar.cloud"
TOKEN = "8f4b2c1d9a3e6f50c8b7d4a2e1f903c5"
VER = "d3e1a8b9c0f24567a1d8e3b9f4c2075a"
SQLITE = b"SQLite format 3\x00" + b"\x00" * 100


def test_due_forme_del_cdomain():
    assert cp.path_domain(CDOMAIN, CPROXY) == "1234567890ab.FFFFFFFFFF"
    assert cp.phonebook_url(CDOMAIN, CPROXY, VER) == (
        f"https://{CPROXY}/phonebook/domains/1234567890ab.FFFFFFFFFF/{VER}")
    assert cp.path_domain("altro.dominio", CPROXY) == "altro.dominio"


@pytest.mark.parametrize("args, manca", [
    ((CDOMAIN, CPROXY, "", VER), "token"),
    ((CDOMAIN, CPROXY, TOKEN, None), "rubrica_ver"),
    (("", CPROXY, TOKEN, VER), "cdomain"),
    ((CDOMAIN, CPROXY, TOKEN, "../x"), "formato"),
    ((CDOMAIN, "evil.com/x?", TOKEN, VER), "formato"),
    ((CDOMAIN, CPROXY, TOKEN, VER), None),
])
def test_dati_in_ingresso(args, manca):
    assert cp.check_inputs(*args) == manca


class _Resp:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content


class _Session:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def get(self, url, auth=None, headers=None, timeout=None):
        self.calls.append((url, auth, headers))
        if self.exc:
            raise self.exc
        return self.resp


def test_download_richiesta_come_quella_verificata():
    s = _Session(_Resp(200, SQLITE))
    assert cp.download(CDOMAIN, CPROXY, TOKEN, VER, session=s) == SQLITE
    url, auth, headers = s.calls[0]
    assert url.endswith("/phonebook/domains/1234567890ab.FFFFFFFFFF/" + VER)
    assert (auth.username, auth.password) == (CDOMAIN, TOKEN)     # cdomain completo nel Digest
    assert headers == {"User-Agent": "TOGA/2.4.0"}


@pytest.mark.parametrize("resp, exc, errore", [
    (_Resp(401), None, cp.CloudAuthError),
    (_Resp(403), None, cp.CloudAuthError),
    (_Resp(404), None, cp.CloudPhonebookError),
    (_Resp(500), None, cp.CloudPhonebookError),
    (_Resp(200, b"<html>login</html>"), None, cp.CloudPhonebookError),
    (None, requests.ConnectionError("x"), cp.CloudPhonebookError),
])
def test_download_errori_senza_token_nel_messaggio(resp, exc, errore):
    with pytest.raises(errore) as e:
        cp.download(CDOMAIN, CPROXY, TOKEN, VER, session=_Session(resp, exc))
    assert TOKEN not in str(e.value)


def test_il_token_non_finisce_nel_sensore_ultimo_messaggio(hub):
    raw = ('GET_INIT_STATUS_REPLY;[{"PARAM":"token","VALUE":"' + TOKEN + '"},'
           '{"PARAM":"rubrica_ver","VALUE":"' + VER + '"}]')
    hub._update_stats("message", raw)
    assert TOKEN not in hub.stats["last_message_in"]
    assert hub.stats["init_status"]["token"] == TOKEN   # resta in memoria per il download


# --- options flow ------------------------------------------------------------------------

def _stub(name, **attrs):
    module = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(module, k, v)
    sys.modules[name] = module


@pytest.fixture(scope="module")
def of():
    _stub("homeassistant.components.file_upload", process_uploaded_file=None)
    _stub("homeassistant.data_entry_flow", FlowResult=dict)

    class _ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    if getattr(sys.modules["homeassistant"], "_is_stub", False):
        sys.modules["homeassistant.config_entries"].ConfigFlow = _ConfigFlow
    return pytest.importorskip("custom_components.vimar_intercom.options_flow")


class _Hub:
    registered = True

    def __init__(self, stats):
        self.stats = stats
        self.richieste = 0
        self._init_seq = 0

    async def async_request_status(self):
        self.richieste += 1


def _flow(of, hub, data=None):
    entry = types.SimpleNamespace(entry_id="e1", options={}, data=data or {
        "sip_user": "60902", "sip_domain": CDOMAIN, "cloud_domain": CDOMAIN,
        "cloud_proxy": CPROXY, "gid": "101"})
    flow = of.OptionsFlowHandler(entry)

    async def job(fn, *a):
        return fn(*a)

    flow.hass = types.SimpleNamespace(data={"vimar_intercom": {"e1": {"hub": hub}}},
                                      async_add_executor_job=job)
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    return flow


def test_senza_token_lo_chiede_e_spiega(of, monkeypatch):
    vero_sleep = asyncio.sleep

    async def niente(_s):
        await vero_sleep(0)

    monkeypatch.setattr(of.asyncio, "sleep", niente)
    hub = _Hub({"init_status": {"dnd": "0"}, "rubrica_ver": VER})
    r = asyncio.run(_flow(of, hub).async_step_fetch_rubrica_cloud())
    assert r["errors"] == {"base": "no_cloud_status"} and hub.richieste == 1


def test_con_token_scarica_e_passa_all_import(of, monkeypatch):
    hub = _Hub({"init_status": {"token": TOKEN}, "rubrica_ver": VER, "apt_gid": 7})
    visti = {}

    def fake_download(cdomain, cproxy, token, ver):
        visti.update(cdomain=cdomain, cproxy=cproxy, token=token, ver=ver)
        return SQLITE

    def fake_parse(path, gid):
        visti["gid"] = gid
        with open(path, "rb") as fh:
            visti["file"] = fh.read()
        return {"actuators": [{"name": "Porta", "msg": "OPEN_2F", "target": "AUTO", "icon": "door"}]}

    monkeypatch.setattr(of.cloud_phonebook, "download", fake_download)
    monkeypatch.setattr(of.rubrica_import, "parse_rubrica_file", fake_parse)
    flow = _flow(of, hub)

    async def confirm():
        return {"type": "confirm"}

    flow.async_step_import_confirm = confirm
    form = asyncio.run(flow.async_step_fetch_rubrica_cloud())
    assert form["type"] == "form" and form["errors"] == {}
    r = asyncio.run(flow.async_step_fetch_rubrica_cloud({"rubrica_gid": ""}))
    assert r == {"type": "confirm"}
    assert visti == {"cdomain": CDOMAIN, "cproxy": CPROXY, "token": TOKEN, "ver": VER,
                     "gid": "7", "file": SQLITE}                  # GID dichiarato dall'impianto


def test_token_rifiutato(of, monkeypatch):
    hub = _Hub({"init_status": {"token": TOKEN}, "rubrica_ver": VER})

    def rifiuta(*a):
        raise cp.CloudAuthError("ipvdes.vimar.cloud ha rifiutato il token (HTTP 401)")

    monkeypatch.setattr(of.cloud_phonebook, "download", rifiuta)
    r = asyncio.run(_flow(of, hub).async_step_fetch_rubrica_cloud({"rubrica_gid": "101"}))
    assert r["errors"] == {"base": "cloud_auth_failed"}
    assert "401" in r["description_placeholders"]["rubrica_error"]


def test_download_with_a_missing_token_never_calls_the_cloud():
    s = _Session(_Resp(200, SQLITE))
    with pytest.raises(cp.CloudPhonebookError, match="token"):
        cp.download(CDOMAIN, CPROXY, "  ", VER, session=s)
    assert s.calls == []


@pytest.mark.parametrize("reply, expected", [
    (None, "no_cloud_status"),
    ({"dnd": "0", "rubrica_ver": "fixture-version"}, "no_cloud_token"),
])
def test_missing_reply_is_distinguished_from_a_reply_without_token(of, monkeypatch, reply, expected):
    real_sleep = asyncio.sleep

    async def no_wait(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(of.asyncio, "sleep", no_wait)
    hub = _Hub({"init_status": {"dnd": "1"}, "rubrica_ver": VER})
    hub._init_seq = 1

    async def request():
        hub.richieste += 1
        if reply is not None:
            hub.stats["init_status"] = reply
            hub._init_seq += 1

    hub.async_request_status = request
    result = asyncio.run(_flow(of, hub).async_step_fetch_rubrica_cloud())
    assert result["errors"] == {"base": expected}
    assert hub.richieste == 1
