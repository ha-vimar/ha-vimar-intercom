"""L'API HTTP locale del citofono.

I casi qui sotto sono tutti tarati su risposte **reali** raccolte dal citofono
di sviluppo (Tab 7S 40507, firmware con server `BaseHTTP/0.6 Python/3.5.2`) il
20 settembre 2026: il JSON dello stato, quello dei nickname, e le tre
stranezze del server — il `nonce` Digest con gli apici, il `Content-Encoding`
illegale e il 401 al posto del 404.
"""
from __future__ import annotations

import json

import pytest

from custom_components.vimar_intercom import rest_client as rc

# ─── parse_status ────────────────────────────────────────────────────────────

# Risposta reale di get_info.php?action=status (forma corta, 266 byte).
STATUS_REALE = json.dumps([
    {"VALUE": "2d4507706e969cea10731be56306e7c5", "PARAM": "rubrica_ver"},
    {"VALUE": "7deb3753beea6419437ae48e0b009b49", "PARAM": "vm_ver"},
    {"VALUE": "0/100", "PARAM": "vm_level"},
    {"VALUE": "0", "PARAM": "dnd"},
    {"VALUE": "0", "PARAM": "voicemail"},
])


def test_status_diventa_un_dizionario():
    st = rc.parse_status(STATUS_REALE)
    assert st["dnd"] == "0"
    assert st["voicemail"] == "0"
    assert st["vm_level"] == "0/100"
    assert st["rubrica_ver"] == "2d4507706e969cea10731be56306e7c5"


def test_status_accetta_i_byte_grezzi():
    assert rc.parse_status(STATUS_REALE.encode()) == rc.parse_status(STATUS_REALE)


def test_status_ignora_i_parametri_che_non_conosce():
    """La forma lunga porta parametri in più a seconda del modello.

    Il parser non deve inciampare su `apt_names` (un array) né sul `token`:
    chi chiama legge quello che gli serve.
    """
    lunga = json.dumps([
        {"PARAM": "dnd", "VALUE": "1"},
        {"PARAM": "vm_timeout", "VALUE": 10},
        {"PARAM": "vm_timeout_values", "VALUE": [1, 5, 10, 15, 20]},
        {"PARAM": "apt_names", "VALUE": ["Casa", "", ""]},
        {"PARAM": "media_enc", "VALUE": "srtp"},
        {"PARAM": "GID", "VALUE": "101"},
    ])
    st = rc.parse_status(lunga)
    assert st["dnd"] == "1"
    assert st["vm_timeout_values"] == [1, 5, 10, 15, 20]
    assert st["media_enc"] == "srtp"


def test_status_salta_le_voci_malformate_senza_fallire():
    st = rc.parse_status(json.dumps([
        {"PARAM": "dnd", "VALUE": "0"},
        {"VALUE": "orfano"},      # senza PARAM
        "non un oggetto",
        {"PARAM": "voicemail", "VALUE": "1"},
    ]))
    assert st == {"dnd": "0", "voicemail": "1"}


@pytest.mark.parametrize("spazzatura", [b"", b"<html>401</html>", b"{}", b"null"])
def test_status_non_json_o_non_array_alza_resterror(spazzatura):
    with pytest.raises(rc.RestError):
        rc.parse_status(spazzatura)


# ─── parse_nicknames e find_picg ─────────────────────────────────────────────

# Risposta reale di get_info.php?action=nickname, identica a quella che arriva
# via SIP con GET_NICKS_REPLY;
NICKNAME_REALE = json.dumps([
    {"ROLE": "PICG", "EXT": "55001", "NAME": "Casa CG"},
    {"ROLE": "PIM", "EXT": "60903", "NAME": "Phone A"},
    {"ROLE": "PIM", "EXT": "60902", "NAME": "test"},
    {"ROLE": "PIM", "EXT": "60901", "NAME": "Phone C"},
])


def test_nicknames_estrae_ruolo_interno_e_nome():
    nicks = rc.parse_nicknames(NICKNAME_REALE)
    assert len(nicks) == 4
    assert nicks[0] == {"role": "PICG", "ext": "55001", "name": "Casa CG"}


def test_il_citofono_dichiara_il_proprio_picg():
    """È questo che rende inutile la scansione a tentativi (issue #14)."""
    assert rc.find_picg(rc.parse_nicknames(NICKNAME_REALE)) == "55001"


def test_find_picg_ignora_maiuscole_e_minuscole():
    assert rc.find_picg([{"role": "picg", "ext": "60001", "name": "x"}]) == "60001"


def test_senza_capogruppo_find_picg_torna_none():
    """Va gestito: l'integrazione deve ricadere sul valore configurato a mano."""
    solo_pim = [{"role": "PIM", "ext": "60902", "name": "test"}]
    assert rc.find_picg(solo_pim) is None


def test_find_picg_non_si_fa_ingannare_da_un_ext_vuoto():
    assert rc.find_picg([{"role": "PICG", "ext": "", "name": "x"}]) is None


def test_nicknames_scarta_le_voci_senza_interno():
    nicks = rc.parse_nicknames(json.dumps([
        {"ROLE": "PICG", "NAME": "senza ext"},
        {"ROLE": "PIM", "EXT": "60902", "NAME": "buona"},
    ]))
    assert [n["ext"] for n in nicks] == ["60902"]


# ─── URL e nomi dei database ─────────────────────────────────────────────────

def test_base_url_e_http_semplice_sulla_porta_di_default():
    """Il citofono serve l'API in chiaro sulla 80; la 443 è chiusa."""
    assert rc.base_url("192.168.1.20") == "http://192.168.1.20/rest"


def test_i_nomi_dei_db_non_portano_estensione():
    """Con «rubrica.db» il citofono risponde 401 (verificato sul campo).

    L'app toglie il «.db» prima di comporre l'URL: se qualcuno rimettesse
    l'estensione qui, il download fallirebbe con un errore di autenticazione
    fuorviante.
    """
    assert rc.DB_RUBRICA == "rubrica"
    assert rc.DB_MAILBOX == "mailbox"


def test_download_db_rifiuta_un_nome_inventato():
    with pytest.raises(ValueError):
        rc.download_db("192.168.1.20", "u", "p", name="rubrica.db")


# ─── Le eccezioni raccontano cosa è successo ─────────────────────────────────

def test_l_errore_di_autenticazione_e_un_resterror():
    """Chi chiama può catturare solo RestError e gestire tutto insieme."""
    assert issubclass(rc.RestAuthError, rc.RestError)
    assert issubclass(rc.RestUnavailable, rc.RestError)


def test_il_401_dice_anche_che_la_risorsa_potrebbe_non_esistere():
    """Il citofono risponde 401 anche a un nome che non conosce, mai 404.

    Il messaggio deve dirlo, altrimenti chi legge il log va a cercare una
    password sbagliata che non c'è.
    """
    msg = rc.RestAuthError.__doc__ or ""
    assert "inesistente" in msg


# ─── Il flusso opzioni espone la nuova voce ──────────────────────────────────
#
# Questi controlli leggono i JSON, non Home Assistant: servono a impedire che
# la voce di menu o i segnaposto spariscano senza che nessuno se ne accorga
# (una descrizione con un segnaposto non dichiarato manda in errore il form).

import json as _json  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_COMPONENT = _Path(__file__).resolve().parents[1] / "custom_components" / "vimar_intercom"
_FILES = ["strings.json", "translations/it.json", "translations/en.json"]


@pytest.mark.parametrize("nome", _FILES)
def test_il_menu_offre_di_scaricare_dal_citofono(nome):
    data = _json.loads((_COMPONENT / nome).read_text(encoding="utf-8"))
    menu = data["options"]["step"]["init"]["menu_options"]
    assert "fetch_rubrica" in menu
    # La voce «da file» resta: è l'unica via per gli impianti solo-cloud.
    assert "import_rubrica" in menu


@pytest.mark.parametrize("nome", _FILES)
def test_i_segnaposto_del_nuovo_passo_sono_quelli_che_il_codice_passa(nome):
    data = _json.loads((_COMPONENT / nome).read_text(encoding="utf-8"))
    step = data["options"]["step"]["fetch_rubrica"]
    for segnaposto in ("{host}", "{rubrica_error}"):
        assert segnaposto in step["description"]
    assert "rubrica_gid" in step["data"]


@pytest.mark.parametrize("nome", _FILES)
def test_la_conferma_mostra_anche_il_picg(nome):
    data = _json.loads((_COMPONENT / nome).read_text(encoding="utf-8"))
    descr = data["options"]["step"]["import_confirm"]["description"]
    assert "{picg_info}" in descr
    assert "{sga_info}" in descr


@pytest.mark.parametrize("nome", _FILES)
def test_i_tre_errori_del_nuovo_passo_hanno_un_messaggio(nome):
    data = _json.loads((_COMPONENT / nome).read_text(encoding="utf-8"))
    errori = data["options"]["error"]
    for chiave in ("no_local_proxy", "rest_auth_failed", "rest_unavailable"):
        assert errori.get(chiave), f"manca il messaggio per {chiave} in {nome}"


# ─── HTTP calls, on a fake requests.get (no network) ─────────────────────────

HOST = "192.0.2.10"


class _Raw:
    def __init__(self, body, fail=False):
        self.body, self.fail, self.calls = body, fail, []

    def read(self, decode_content=True):
        self.calls.append(decode_content)
        if self.fail:
            raise ValueError("already consumed")
        return self.body


class _HttpResp:
    def __init__(self, status=200, body=b"", headers=None, raw_fails=False):
        self.status_code = status
        self.headers = headers or {}
        self.raw = _Raw(body, raw_fails)
        self.content = body


@pytest.fixture
def http(monkeypatch):
    """requests.get replaced: answers from `http.replies`, records the calls."""
    state = type("Http", (), {})()
    state.calls, state.replies = [], []

    def get(url, **kw):
        state.calls.append((url, kw))
        reply = state.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(rc.requests, "get", get)
    return state


def test_get_status_asks_digest_without_compression_and_reads_raw_bytes(http):
    resp = _HttpResp(200, STATUS_REALE.encode())
    http.replies.append(resp)
    status = rc.get_status(HOST, "u", "p")
    assert status["vm_level"] == "0/100"
    url, kw = http.calls[0]
    assert url == f"http://{HOST}/rest/get_info.php"
    assert kw["params"] == {"action": "status"}
    assert (kw["auth"].username, kw["auth"].password) == ("u", "p")
    assert kw["headers"]["Accept-Encoding"] == "identity"
    assert resp.raw.calls == [False]  # the illegal Content-Encoding is never decoded


def test_a_body_whose_raw_stream_is_gone_is_read_from_content(http):
    http.replies.append(_HttpResp(200, b'[{"ROLE":"PICG","EXT":"61000","NAME":"x"}]', raw_fails=True))
    assert rc.get_nicknames(HOST, "u", "p") == [{"role": "PICG", "ext": "61000", "name": "x"}]


def test_a_network_error_is_rest_unavailable(http):
    http.replies.append(rc.requests.exceptions.ConnectTimeout("slow"))
    with pytest.raises(rc.RestUnavailable, match=HOST):
        rc.get_status(HOST, "u", "p")


def test_a_401_is_rest_auth_error(http):
    http.replies.append(_HttpResp(401))
    with pytest.raises(rc.RestAuthError, match="401"):
        rc.get_nicknames(HOST, "u", "p")


def test_another_status_is_a_rest_error(http):
    http.replies.append(_HttpResp(500))
    with pytest.raises(rc.RestError, match="500"):
        rc.get_status(HOST, "u", "p")


def test_download_db_writes_the_database_and_returns_last_modified(http, tmp_path):
    body = b"SQLite format 3\x00rest"
    http.replies.append(_HttpResp(200, body, {"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"}))
    dest = tmp_path / "rubrica.db"
    data, lm = rc.download_db(HOST, "u", "p", dest=str(dest))
    assert data == body and dest.read_bytes() == body
    assert lm == "Mon, 01 Jan 2024 00:00:00 GMT"
    assert http.calls[0][1]["params"] == {"name": "rubrica"}
    assert "If-Modified-Since" not in http.calls[0][1]["headers"]


def test_download_db_unchanged_is_304_without_content(http):
    http.replies.append(_HttpResp(304, headers={"Last-Modified": "then"}))
    assert rc.download_db(HOST, "u", "p", rc.DB_MAILBOX, if_modified_since="then") == (None, "then")
    assert http.calls[0][1]["headers"]["If-Modified-Since"] == "then"


def test_download_db_refuses_a_body_that_is_not_sqlite(http):
    http.replies.append(_HttpResp(200, b"<html>"))
    with pytest.raises(rc.RestError, match="SQLite"):
        rc.download_db(HOST, "u", "p")


def test_nicks_reply_skips_broken_objects_in_a_truncated_body():
    body = 'GET_NICKS_REPLY;[{"ROLE":"PICG","EXT":"61000"},{"ROLE":"GA","EXT":1,"x":[}, {"ROLE":"GA'
    assert rc.parse_nicks_reply(body) == [{"role": "PICG", "ext": "61000", "name": ""}]


def test_nicknames_that_are_not_an_array_are_a_rest_error():
    with pytest.raises(rc.RestError, match="array"):
        rc.parse_nicknames(b'{"ROLE":"PICG"}')


def test_download_db_without_dest_writes_nothing(http, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    http.replies.append(_HttpResp(200, b"SQLite format 3\x00"))
    assert rc.download_db(HOST, "u", "p")[0] == b"SQLite format 3\x00"
    assert list(tmp_path.iterdir()) == []
