"""The config and options flow steps, driven on stub HA objects: which form
comes next, which errors show, and what ends up in the entry."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import types

import pytest


@pytest.fixture(scope="module")
def cf():
    # conftest stubs the HA modules config_flow imports.
    return pytest.importorskip("custom_components.vimar_intercom.config_flow")


@pytest.fixture(scope="module")
def of(cf):
    return pytest.importorskip("custom_components.vimar_intercom.options_flow")


def _form(**kw):
    return {"type": "form", **kw}


async def _job(fn, *args):
    return fn(*args)


# ─── _parse_actuators ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, message", [
    ("{not json", "JSON non valido"),
    ('{"name": "x"}', "lista"),
    ('["x"]', "non è un oggetto"),
    ('[{"name": "x", "msg": "OPEN"}]', "chiavi mancanti"),
    ('[{"name": "x", "msg": "OPEN", "target": "55001", "icon": "rocket"}]', "non valida"),
])
def test_a_malformed_actuator_list_is_refused_with_a_reason(of, raw, message):
    with pytest.raises(ValueError, match=message):
        of._parse_actuators(raw)


def test_an_empty_actuator_field_means_no_buttons(of):
    assert of._parse_actuators("  ") == []


def test_actuator_values_are_normalised_to_strings(of):
    assert of._parse_actuators('[{"name": 1, "msg": "OPEN", "target": " 55001 ", "icon": "door"}]') == [
        {"name": "1", "msg": "OPEN", "target": "55001", "icon": "door"}]


# ─── Setup flow ──────────────────────────────────────────────────────────────

def _setup_flow(cf):
    flow = cf.VimarIntercomConfigFlow()
    flow.context = {}
    flow.hass = types.SimpleNamespace(async_add_executor_job=_job)
    flow.async_show_form = _form
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}

    async def set_uid(uid):
        flow.unique_id = uid

    flow.async_set_unique_id = set_uid
    flow._abort_if_unique_id_configured = lambda: None
    return flow


def test_the_first_step_offers_qr_or_manual(cf):
    flow = _setup_flow(cf)
    assert asyncio.run(flow.async_step_user())["step_id"] == "user"
    assert asyncio.run(flow.async_step_user({"mode": "qr"}))["step_id"] == "qr"
    assert asyncio.run(flow.async_step_user({"mode": "manual"}))["step_id"] == "manual"


def test_confirming_a_discovered_intercom_goes_to_the_first_step(cf):
    flow = _setup_flow(cf)
    assert asyncio.run(flow.async_step_zeroconf_confirm({}))["step_id"] == "user"


def test_an_invalid_qr_stays_on_the_form_with_the_reason(cf):
    flow = _setup_flow(cf)
    result = asyncio.run(flow.async_step_qr({"qr_text": "  "}))
    assert result["step_id"] == "qr" and result["errors"] == {"qr_text": "qr_invalid"}
    assert result["description_placeholders"]["error_detail"] == "Testo QR vuoto"


def test_a_valid_qr_fills_the_credentials_and_asks_for_the_network(cf, monkeypatch):
    flow = _setup_flow(cf)
    fields = {"id": "60999", "pwd": "pw", "domain": "plant.example.test", "planttype": "2F"}
    monkeypatch.setattr(cf.qr_decoder, "decode", lambda text: fields if text == "QR" else None)
    result = asyncio.run(flow.async_step_qr({"qr_text": " QR "}))
    assert result["step_id"] == "network" and result["errors"] == {}
    assert flow._credentials["sip_user"] == "60999"
    assert result["description_placeholders"]["sip_domain"] == "plant.example.test"


def test_manual_credentials_are_all_required(cf):
    flow = _setup_flow(cf)
    result = asyncio.run(flow.async_step_manual({"sip_user": " ", "sip_password": "", "sip_domain": ""}))
    assert result["step_id"] == "manual"
    assert result["errors"] == {"sip_user": "required", "sip_password": "required", "sip_domain": "required"}


def test_manual_credentials_store_the_ha1_and_ask_for_the_network(cf):
    flow = _setup_flow(cf)
    result = asyncio.run(flow.async_step_manual({
        "sip_user": "60999", "sip_password": "pw", "sip_domain": "plant.example.test",
        "cloud_proxy": " relay.example.test "}))
    assert result["step_id"] == "network"
    import hashlib
    assert flow._credentials["sip_ha1"] == hashlib.md5(b"60999:plant.example.test:pw").hexdigest()
    assert flow._credentials["cloud_proxy"] == "relay.example.test"


def test_the_manual_form_prefills_a_discovered_cloud_domain_not_an_ip(cf):
    flow = _setup_flow(cf)
    flow._discovered = {"sip_domain": "plant.example.test"}
    assert asyncio.run(flow.async_step_manual())["step_id"] == "manual"
    flow._discovered = {"sip_domain": "192.0.2.20"}
    assert asyncio.run(flow.async_step_manual())["step_id"] == "manual"


def test_discovered_data_without_domain_keeps_the_qr_mac(cf):
    flow = _setup_flow(cf)
    flow._discovered = {"mac": "11:22:33:44:55:66"}
    flow._credentials = {"sip_domain": "d", "mac": "AA:BB:CC:DD:EE:FF"}
    flow._apply_discovered()
    assert flow._credentials == {"sip_domain": "d", "mac": "AA:BB:CC:DD:EE:FF"}


CREDENTIALS = {"sip_user": "60999", "sip_password": "pw", "sip_domain": "plant.example.test",
               "plant_type": "2F"}


@pytest.mark.parametrize("user_input, errors", [
    ({"local_proxy": "192.0.2.1", "device_name": "   "}, {"device_name": "required"}),
    ({"local_proxy": ""}, {"local_proxy": "required"}),
    ({"local_proxy": "not-an-ip"}, {"local_proxy": "invalid_ip"}),
])
def test_the_network_step_validates_before_any_registration(cf, monkeypatch, user_input, errors):
    async def never(**kw):
        raise AssertionError("no probe on invalid input")

    monkeypatch.setattr(cf, "_probe_transport", never)
    flow = _setup_flow(cf)
    flow._credentials = dict(CREDENTIALS)
    result = asyncio.run(flow.async_step_network(user_input))
    assert result["step_id"] == "network" and result["errors"] == errors


def test_a_second_try_keeps_the_identity_of_the_first(cf, monkeypatch):
    identities = []

    async def probe(credentials, *, identity, prefer_local, **kw):
        identities.append(dict(identity))
        return prefer_local, len(identities) > 1, "503"

    monkeypatch.setattr(cf, "_probe_transport", probe)
    flow = _setup_flow(cf)
    flow._credentials = dict(CREDENTIALS)
    first = asyncio.run(flow.async_step_network({"local_proxy": "192.0.2.1"}))
    assert first["errors"] == {"local_proxy": "sip_registration_failed"}
    second = asyncio.run(flow.async_step_network({"local_proxy": "192.0.2.1"}))
    assert second["type"] == "create_entry"
    assert identities[0]["device_uuid"] == identities[1]["device_uuid"]
    assert second["data"]["device_uuid"] == identities[0]["device_uuid"]


def test_the_network_form_shows_the_plant(cf):
    flow = _setup_flow(cf)
    flow._credentials = dict(CREDENTIALS)
    result = asyncio.run(flow.async_step_network())
    assert result["step_id"] == "network" and result["errors"] == {}
    assert result["description_placeholders"]["sip_user"] == "60999"


def test_the_setup_flow_hands_out_the_options_flow(cf, of):
    entry = types.SimpleNamespace(data={}, options={})
    handler = cf.VimarIntercomConfigFlow.async_get_options_flow(entry)
    assert isinstance(handler, of.OptionsFlowHandler) and handler._entry is entry


# ─── Zeroconf: entries that do not match ─────────────────────────────────────

def test_a_discovery_of_another_intercom_does_not_touch_existing_entries(cf):
    other = types.SimpleNamespace(entry_id="e0", data={"mac": "00:11:22:33:44:55",
                                                       "local_proxy": "192.0.2.50"}, options={})
    updates = []
    flow = _setup_flow(cf)
    flow.hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(
        async_update_entry=lambda entry, **kw: updates.append(kw),
        async_schedule_reload=lambda *_a: None))
    flow._async_current_entries = lambda include_ignore=True: [other]
    info = types.SimpleNamespace(host="192.0.2.20", properties={
        b"mac": b"11:22:33:44:55:66", b"proxy": b"192.0.2.20", b"domain": b"plant.example.test"})
    result = asyncio.run(flow.async_step_zeroconf(info))
    # One installation only (#38): another Tab is not offered, and the
    # existing entry keeps its address.
    assert result == {"type": "abort", "reason": "single_instance_allowed"}
    assert updates == [] and other.data["local_proxy"] == "192.0.2.50"


def test_a_new_ip_for_an_entry_without_proxy_option_updates_only_data(cf):
    updates, reloads = [], []
    flow = _setup_flow(cf)
    flow.hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(
        async_update_entry=lambda entry, **kw: updates.append(kw),
        async_schedule_reload=reloads.append))
    entry = types.SimpleNamespace(entry_id="e1", data={"local_proxy": "192.0.2.99"}, options={})
    flow._update_proxy(entry, "192.0.2.10")
    assert updates == [{"data": {"local_proxy": "192.0.2.10"}}] and reloads == ["e1"]


# ─── Options flow ────────────────────────────────────────────────────────────

ENTRY_DATA = {"sip_user": "60999", "sip_password": "pw", "sip_domain": "plant.example.test",
              "local_proxy": "192.0.2.1", "use_local_udp": False, "gid": "101"}


def _options_flow(of, data=None, options=None, *, hass_data=None, allowed=True, files=()):
    entry = types.SimpleNamespace(entry_id="e1", data=dict(data or ENTRY_DATA), options=dict(options or {}))
    flow = of.OptionsFlowHandler(entry)

    async def users():
        return [types.SimpleNamespace(id="u1", name="Anna", system_generated=False),
                types.SimpleNamespace(id="s", name=None, system_generated=True)]

    flow.hass = types.SimpleNamespace(
        data=hass_data or {},
        auth=types.SimpleNamespace(async_get_users=users),
        config=types.SimpleNamespace(is_allowed_path=lambda p: allowed,
                                     path=lambda *p: "/config/" + "/".join(p)),
        async_add_executor_job=_job)
    flow.async_show_form = _form
    flow.async_show_menu = lambda **kw: {"type": "menu", **kw}
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    return flow


def test_the_options_start_from_a_menu(of):
    """Settings first, the phonebook steps in order, and every entry leads to a
    step. Other pages may join the menu (the HomeKit one does in #32)."""
    flow = _options_flow(of)
    menu = asyncio.run(flow.async_step_init())
    assert menu["type"] == "menu"
    options = menu["menu_options"]
    assert options[0] == "settings"
    assert [o for o in options if "rubrica" in o] == ["fetch_rubrica", "fetch_rubrica_cloud", "import_rubrica"]
    assert all(callable(getattr(flow, f"async_step_{o}", None)) for o in options), "a dead menu entry"


def _settings(of, extra, **kw):
    return asyncio.run(_options_flow(of, **kw).async_step_settings({"local_proxy": "192.0.2.1",
                                                                     "use_local_udp": False, **extra}))


def test_an_away_file_that_does_not_exist_is_refused(of, tmp_path):
    result = _settings(of, {"away_message_file": str(tmp_path / "missing.mp3")})
    assert result["errors"] == {"away_message_file": "file_not_found"}


def test_an_away_file_outside_the_allowed_folders_is_refused(of, tmp_path):
    result = _settings(of, {"away_message_file": str(tmp_path / "x.mp3")}, allowed=False)
    assert result["errors"] == {"away_message_file": "file_not_allowed"}


def test_an_existing_away_file_is_saved(of, tmp_path):
    f = tmp_path / "away.mp3"
    f.write_bytes(b"x")
    result = _settings(of, {"away_message_file": str(f)})
    assert result["type"] == "create_entry" and result["data"]["away_message_file"] == str(f)


def _away_upload(of, monkeypatch, tmp_path, name, data=b"ID3 audio"):
    """The settings saved with an uploaded away file called `name`; the media folder is tmp_path/media."""
    (tmp_path / "upload" / "id1").mkdir(parents=True, exist_ok=True)
    src = tmp_path / "upload" / "id1" / name
    if data is not None:
        src.write_bytes(data)

    @contextlib.contextmanager
    def process_uploaded_file(hass, upload_id):
        assert upload_id == "id1"
        yield src

    monkeypatch.setattr(of, "process_uploaded_file", process_uploaded_file)
    flow = _options_flow(of)
    flow.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    result = asyncio.run(flow.async_step_settings({"local_proxy": "192.0.2.1", "use_local_udp": False,
                                                   "away_message_upload": "id1"}))
    return result, tmp_path / "media" / "citofono" / "messaggi"


def test_an_uploaded_away_file_is_saved_in_the_messages_folder_and_chosen(of, monkeypatch, tmp_path):
    result, folder = _away_upload(of, monkeypatch, tmp_path, "Benvenuti a casa.mp3")
    assert result["type"] == "create_entry"
    assert result["data"]["away_message_file"] == str(folder / "Benvenuti a casa.mp3")
    assert (folder / "Benvenuti a casa.mp3").read_bytes() == b"ID3 audio"
    assert "away_message_upload" not in result["data"]


def test_an_upload_with_the_name_of_a_different_file_does_not_overwrite_it(of, monkeypatch, tmp_path):
    folder = tmp_path / "media" / "citofono" / "messaggi"
    folder.mkdir(parents=True)
    (folder / "msg.wav").write_bytes(b"old")
    result, _ = _away_upload(of, monkeypatch, tmp_path, "msg.wav", b"new")
    assert (folder / "msg.wav").read_bytes() == b"old"
    assert result["data"]["away_message_file"] == str(folder / "msg-2.wav")
    assert (folder / "msg-2.wav").read_bytes() == b"new"


def test_an_upload_keeps_only_the_file_name(of, monkeypatch, tmp_path):
    """A name with ../ cannot write outside the messages folder."""
    (tmp_path / "evil.mp3").write_bytes(b"x")
    result, folder = _away_upload(of, monkeypatch, tmp_path, "../../evil.mp3", data=None)
    assert result["data"]["away_message_file"] == str(folder / "evil.mp3")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["evil.mp3", "media", "upload"]


@pytest.mark.parametrize("name", ["msg.exe", "msg.mp3.sh", ".hidden.mp3", "a\\..\\b:.mp3", "x;rm.mp3"])
def test_an_upload_of_the_wrong_kind_is_refused(of, monkeypatch, tmp_path, name):
    if ":" in name or "\\" in name:  # not a valid file name on Windows: check the name alone
        with pytest.raises(ValueError, match="upload_bad_type"):
            of.away_config.save_upload(name, str(tmp_path))
        return
    result, folder = _away_upload(of, monkeypatch, tmp_path, name)
    assert result["errors"] == {"away_message_upload": "upload_bad_type"}
    assert not folder.exists()


def test_a_too_big_upload_is_refused(of, monkeypatch, tmp_path):
    result, folder = _away_upload(of, monkeypatch, tmp_path, "big.m4a",
                                  b"x" * (of.away_config.UPLOAD_MAX + 1))
    assert result["errors"] == {"away_message_upload": "upload_too_big"}
    assert not folder.exists()


def test_an_empty_upload_is_refused(of, monkeypatch, tmp_path):
    result, folder = _away_upload(of, monkeypatch, tmp_path, "empty.mp3", b"")
    assert result["errors"] == {"away_message_upload": "upload_bad_type"}
    assert not folder.exists()


def test_an_upload_with_a_too_long_name_is_refused(of, tmp_path):
    """Over 200 bytes the name plus -n may not fit the 255-byte file name limit: the
    copy failed with an OSError instead of a form error."""
    name = "a" * 197 + ".mp3"  # 201 bytes, checked before the file is opened
    with pytest.raises(ValueError, match="upload_bad_type"):
        of.away_config.save_upload(str(tmp_path / name), str(tmp_path / "messaggi"))
    assert not (tmp_path / "messaggi").exists()


def test_an_expired_upload_shows_upload_failed(of, monkeypatch, tmp_path):
    """process_uploaded_file raises ValueError("File does not exist") for an expired
    upload: the form shows a translated error, not that raw text."""

    @contextlib.contextmanager
    def process_uploaded_file(hass, upload_id):
        raise ValueError("File does not exist")
        yield

    monkeypatch.setattr(of, "process_uploaded_file", process_uploaded_file)
    flow = _options_flow(of)
    flow.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    result = asyncio.run(flow.async_step_settings({"local_proxy": "192.0.2.1", "use_local_udp": False,
                                                   "away_message_upload": "id1"}))
    assert result["errors"] == {"away_message_upload": "upload_failed"}


def test_an_upload_never_writes_through_a_link_already_at_the_destination(of, monkeypatch, tmp_path):
    """A hard link to a file outside the folder that shows up after any existence check
    (os.path.exists patched to miss it): the upload goes to msg-2.wav, the outside file
    is untouched. Hard link, so it runs on Windows too."""
    folder = tmp_path / "media" / "citofono" / "messaggi"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"precious")
    os.link(outside, folder / "msg.wav")
    monkeypatch.setattr(of.away_config.os.path, "exists", lambda p: False)
    result, _ = _away_upload(of, monkeypatch, tmp_path, "msg.wav", b"new")
    assert outside.read_bytes() == b"precious"
    assert result["data"]["away_message_file"] == str(folder / "msg-2.wav")
    assert (folder / "msg-2.wav").read_bytes() == b"new"


def test_an_upload_never_writes_through_a_dangling_symlink(of, tmp_path):
    """os.path.exists is False for a dangling symlink: the old copy created its target."""
    src = tmp_path / "msg.wav"
    src.write_bytes(b"new")
    folder, target = tmp_path / "messaggi", tmp_path / "planted.wav"
    folder.mkdir()
    try:
        os.symlink(target, folder / "msg.wav")
    except OSError:
        pytest.skip("symlinks need privileges on this system")
    assert of.away_config.save_upload(str(src), str(folder)) == str(folder / "msg-2.wav")
    assert not target.exists()
    assert (folder / "msg-2.wav").read_bytes() == b"new"


def test_an_upload_skips_a_dangling_symlink_without_o_nofollow(of, monkeypatch, tmp_path):
    """Windows has no O_NOFOLLOW, and there O_CREAT|O_EXCL on a dangling symlink creates
    its target. Emulated here so it runs on Linux too: the link must be skipped before
    os.open is ever called on it."""
    src = tmp_path / "msg.wav"
    src.write_bytes(b"new")
    folder, target = tmp_path / "messaggi", tmp_path / "planted.wav"
    folder.mkdir()
    try:
        os.symlink(target, folder / "msg.wav")
    except OSError:
        pytest.skip("symlinks need privileges on this system")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    real_open = os.open

    def windows_open(path, flags, mode=0o777):
        # What Windows does: follow the link, then O_EXCL only checks the target.
        return real_open(os.path.realpath(path), flags, mode)

    monkeypatch.setattr(of.away_config.os, "open", windows_open)
    assert of.away_config.save_upload(str(src), str(folder)) == str(folder / "msg-2.wav")
    assert not target.exists()
    assert (folder / "msg-2.wav").read_bytes() == b"new"


def test_webhook_urls_must_be_http(of):
    result = _settings(of, {"ring_webhook_url": "ftp://192.0.2.5/x",
                            "ring_end_webhook_url": "https://example.test/end"})
    assert result["errors"] == {"ring_webhook_url": "invalid_url"}


def test_a_snapshot_folder_outside_the_allowed_ones_is_refused(of):
    result = _settings(of, {"snapshot_dir": "/elsewhere"}, allowed=False)
    assert result["errors"] == {"snapshot_dir": "path_not_allowed"}


def test_an_invalid_intercom_address_is_refused_in_the_options(of):
    result = _settings(of, {"local_proxy": "intercom.example.test"})
    assert result["errors"] == {"local_proxy": "invalid_ip"}


def test_the_settings_form_offers_only_real_users(of):
    flow = _options_flow(of, options={"allowed_users": ["u1", "gone"]})
    result = asyncio.run(flow.async_step_settings())
    assert result["step_id"] == "settings" and result["errors"] == {}


# ─── Options: phonebook from the intercom (local HTTP) ───────────────────────

ACTUATORS = [{"name": "Porta", "msg": "OPEN_2F", "target": "55001", "icon": "door"}]


def _fake_rest(of, monkeypatch, *, download=None, nicknames=None, parsed=None):
    calls = []

    def fake_download(host, user, password, name, dest):
        calls.append(("download", host, user, name))
        if isinstance(download, Exception):
            raise download
        with open(dest, "wb") as fh:
            fh.write(b"SQLite format 3\x00")

    def fake_nicks(host, user, password):
        if isinstance(nicknames, Exception):
            raise nicknames
        return nicknames or []

    def fake_parse(path, gid):
        calls.append(("parse", gid))
        return parsed if parsed is not None else {"actuators": ACTUATORS, "sga": "55001", "system": {}}

    monkeypatch.setattr(of.rest_client, "download_db", fake_download)
    monkeypatch.setattr(of.rest_client, "get_nicknames", fake_nicks)
    monkeypatch.setattr(of.rubrica_import, "parse_rubrica_file", fake_parse)
    return calls


def test_without_an_intercom_address_there_is_nothing_to_fetch(of):
    flow = _options_flow(of, data={**ENTRY_DATA, "local_proxy": ""})
    result = asyncio.run(flow.async_step_fetch_rubrica({"rubrica_gid": "101"}))
    assert result["errors"] == {"base": "no_local_proxy"}
    assert result["description_placeholders"]["host"] == "—"


def test_the_fetch_form_is_shown_first(of):
    result = asyncio.run(_options_flow(of).async_step_fetch_rubrica())
    assert result["step_id"] == "fetch_rubrica" and result["errors"] == {}


def test_the_phonebook_from_the_intercom_leads_to_the_confirmation_with_its_picg(of, monkeypatch):
    calls = _fake_rest(of, monkeypatch, nicknames=[{"role": "PICG", "ext": "61000", "name": ""}])
    result = asyncio.run(_options_flow(of).async_step_fetch_rubrica({"rubrica_gid": " "}))
    assert result["step_id"] == "import_confirm"
    assert calls == [("download", "192.0.2.1", "60999", "rubrica"), ("parse", "101")]
    assert "61000" in result["description_placeholders"]["picg_info"]


def test_missing_nicknames_do_not_spoil_the_import(of, monkeypatch):
    _fake_rest(of, monkeypatch, nicknames=of.rest_client.RestError("x"))
    flow = _options_flow(of)
    result = asyncio.run(flow.async_step_fetch_rubrica({"rubrica_gid": "7"}))
    assert result["step_id"] == "import_confirm" and flow._picg_from_rest is None
    assert flow._imported_gid == "7"


@pytest.mark.parametrize("error, base", [
    ("auth", "rest_auth_failed"),
    ("unavailable", "rest_unavailable"),
    ("other", "rubrica_import_failed"),
])
def test_fetch_errors_are_named(of, monkeypatch, error, base):
    exc = {"auth": of.rest_client.RestAuthError("401 from 192.0.2.1"),
           "unavailable": of.rest_client.RestUnavailable("192.0.2.1 unreachable"),
           "other": of.rest_client.RestError("500")}[error]
    _fake_rest(of, monkeypatch, download=exc)
    result = asyncio.run(_options_flow(of).async_step_fetch_rubrica({"rubrica_gid": "101"}))
    assert result["errors"] == {"base": base}
    assert result["description_placeholders"]["rubrica_error"] == str(exc)


def test_a_phonebook_without_actuators_for_the_flat_is_explained(of, monkeypatch):
    _fake_rest(of, monkeypatch, parsed={"actuators": [], "sga": None, "system": {}})
    result = asyncio.run(_options_flow(of).async_step_fetch_rubrica({"rubrica_gid": "9"}))
    assert result["errors"] == {"base": "rubrica_no_actuators"}
    assert "GID 9" in result["description_placeholders"]["rubrica_error"]


# ─── Options: phonebook from the Vimar cloud ─────────────────────────────────

CLOUD_DATA = {**ENTRY_DATA, "cloud_proxy": "relay.example.test",
              "cloud_domain": "plant.relay.example.test"}


class _Hub:
    def __init__(self, stats, registered=True, token_later=None):
        self.stats, self.registered, self.token_later = stats, registered, token_later
        self._init_seq = 0

    async def async_request_status(self):
        if self.token_later:
            self.stats["init_status"] = {"token": self.token_later}


def _cloud_flow(of, hub, data=CLOUD_DATA):
    return _options_flow(of, data=data, hass_data={"vimar_intercom": {"e1": {"hub": hub}}} if hub else None)


@pytest.fixture
def fast_sleep(of, monkeypatch):
    real_sleep = asyncio.sleep

    async def sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(of.asyncio, "sleep", sleep)


def test_without_the_integration_loaded_there_is_no_cloud_status(of):
    result = asyncio.run(_cloud_flow(of, None).async_step_fetch_rubrica_cloud())
    assert result["errors"] == {"base": "no_cloud_status"}


def test_a_token_arriving_after_the_status_request_is_used(of, fast_sleep):
    hub = _Hub({"rubrica_ver": "abc"}, token_later="tok")
    result = asyncio.run(_cloud_flow(of, hub).async_step_fetch_rubrica_cloud())
    assert result["errors"] == {}


def test_a_manual_entry_uses_its_sip_domain_as_the_cloud_domain(of, monkeypatch):
    seen = {}

    def download(cdomain, cproxy, token, ver):
        seen["cdomain"] = cdomain
        return b"SQLite format 3\x00"

    monkeypatch.setattr(of.cloud_phonebook, "download", download)
    monkeypatch.setattr(of.rubrica_import, "parse_rubrica_file",
                        lambda path, gid: {"actuators": ACTUATORS, "sga": "55001"})
    data = {**ENTRY_DATA, "cloud_proxy": "relay.example.test", "sip_domain": "plant.relay.example.test"}
    hub = _Hub({"init_status": {"token": "tok"}, "rubrica_ver": "abc"})
    result = asyncio.run(_cloud_flow(of, hub, data).async_step_fetch_rubrica_cloud({"rubrica_gid": "101"}))
    assert result["step_id"] == "import_confirm" and seen["cdomain"] == "plant.relay.example.test"


def test_missing_cloud_data_is_named(of):
    data = {**ENTRY_DATA, "cloud_proxy": "", "cloud_domain": "plant.relay.example.test"}
    hub = _Hub({"init_status": {"token": "tok"}, "rubrica_ver": "abc"})
    result = asyncio.run(_cloud_flow(of, hub, data).async_step_fetch_rubrica_cloud())
    assert result["errors"] == {"base": "cloud_failed"}
    assert result["description_placeholders"]["rubrica_error"].startswith("manca cproxy")


def test_a_cloud_download_error_is_shown(of, monkeypatch):
    def fail(*a):
        raise of.cloud_phonebook.CloudPhonebookError("relay.example.test ha risposto HTTP 500")

    monkeypatch.setattr(of.cloud_phonebook, "download", fail)
    hub = _Hub({"init_status": {"token": "tok"}, "rubrica_ver": "abc"})
    result = asyncio.run(_cloud_flow(of, hub).async_step_fetch_rubrica_cloud({"rubrica_gid": "101"}))
    assert result["errors"] == {"base": "cloud_failed"}
    assert "HTTP 500" in result["description_placeholders"]["rubrica_error"]


def test_a_cloud_phonebook_without_actuators_is_explained(of, monkeypatch):
    monkeypatch.setattr(of.cloud_phonebook, "download", lambda *a: b"SQLite format 3\x00")
    monkeypatch.setattr(of.rubrica_import, "parse_rubrica_file", lambda path, gid: {"actuators": []})
    hub = _Hub({"init_status": {"token": "tok"}, "rubrica_ver": "abc"})
    result = asyncio.run(_cloud_flow(of, hub).async_step_fetch_rubrica_cloud({"rubrica_gid": "5"}))
    assert result["errors"] == {"base": "rubrica_no_actuators"}
    assert "GID 5" in result["description_placeholders"]["rubrica_error"]


# ─── Options: phonebook from an uploaded file ────────────────────────────────

def _upload(of, monkeypatch, parse):
    seen = []

    @contextlib.contextmanager
    def process_uploaded_file(hass, upload_id):
        seen.append(upload_id)
        yield f"/tmp/uploads/{upload_id}.db"

    monkeypatch.setattr(of, "process_uploaded_file", process_uploaded_file)
    monkeypatch.setattr(of.rubrica_import, "parse_rubrica_file", parse)
    return seen


def test_the_upload_form_is_shown_first(of):
    result = asyncio.run(_options_flow(of).async_step_import_rubrica())
    assert result["step_id"] == "import_rubrica" and result["errors"] == {}


def test_an_uploaded_phonebook_leads_to_the_confirmation(of, monkeypatch):
    parsed_paths = []

    def parse(path, gid):
        parsed_paths.append((path, gid))
        return {"actuators": ACTUATORS, "sga": "55001", "system": {}}

    seen = _upload(of, monkeypatch, parse)
    result = asyncio.run(_options_flow(of).async_step_import_rubrica({"rubrica_file": "up1", "rubrica_gid": ""}))
    assert result["step_id"] == "import_confirm"
    assert seen == ["up1"] and parsed_paths == [("/tmp/uploads/up1.db", "101")]


def test_an_unreadable_upload_is_refused(of, monkeypatch):
    def parse(path, gid):
        raise of.rubrica_import.RubricaImportError("Non è un database SQLite valido")

    _upload(of, monkeypatch, parse)
    result = asyncio.run(_options_flow(of).async_step_import_rubrica({"rubrica_file": "up1"}))
    assert result["errors"] == {"rubrica_file": "rubrica_import_failed"}
    assert "SQLite" in result["description_placeholders"]["rubrica_error"]


def test_an_upload_without_actuators_for_the_flat_is_refused(of, monkeypatch):
    _upload(of, monkeypatch, lambda path, gid: {"actuators": []})
    result = asyncio.run(_options_flow(of).async_step_import_rubrica({"rubrica_file": "up1", "rubrica_gid": "3"}))
    assert result["errors"] == {"rubrica_file": "rubrica_no_actuators"}
    assert "GID appartamento 3" in result["description_placeholders"]["rubrica_error"]


# ─── Options: import confirmation ────────────────────────────────────────────

def _confirm_flow(of, imported, options=None, picg_from_rest=None):
    flow = _options_flow(of, options=options)
    flow._imported = imported
    flow._picg_from_rest = picg_from_rest
    return flow


@pytest.mark.parametrize("imported, options, key, expected", [
    ({"actuators": ACTUATORS, "sga": None}, {}, "sga_info", "non trovato"),
    ({"actuators": ACTUATORS, "sga": "55001"}, {"sga_target": "55001"}, "sga_info", "coincide"),
    ({"actuators": ACTUATORS, "sga": "55009"}, {"sga_target": "55001"}, "sga_info", "diverso"),
    ({"actuators": ACTUATORS, "camera": "55100"}, {"camera_target": "55100"}, "camera_info", "coincide"),
    ({"actuators": ACTUATORS, "door": "55002"}, {"door_target": "55002"}, "door_info", "coincide"),
    ({"actuators": ACTUATORS, "door": "55002"}, {"door_target": "55001"}, "door_info", "diversa"),
    ({"actuators": ACTUATORS, "door": "55002"}, {}, "door_info", "attuatore porta (GID_PE);"),
    ({"actuators": ACTUATORS}, {"door_target": "55001"}, "door_info", "resta quella già configurata"),
    ({"actuators": ACTUATORS, "sga": "55009"}, {}, "door_info", "resta all'SGA (55009)"),
])
def test_the_confirmation_explains_each_value(of, imported, options, key, expected):
    result = asyncio.run(_confirm_flow(of, imported, options).async_step_import_confirm())
    assert expected in result["description_placeholders"][key]


def test_a_picg_declared_by_the_intercom_equal_to_the_configured_one(of):
    flow = _confirm_flow(of, {"actuators": ACTUATORS, "sga": "55001"}, {"picg_target": "61000"}, "61000")
    result = asyncio.run(flow.async_step_import_confirm())
    assert "coincide" in result["description_placeholders"]["picg_info"]


def test_confirming_keeps_the_other_options_and_saves_the_imported_values(of):
    flow = _confirm_flow(of, {"actuators": ACTUATORS, "sga": "55009", "door": "55002", "camera": "55100"},
                         {"away_message_text": "back soon", "internal_panel_target": "55200"})
    result = asyncio.run(flow.async_step_import_confirm({}))
    data = result["data"]
    assert data["away_message_text"] == "back soon"
    assert (data["sga_target"], data["picg_target"], data["door_target"], data["camera_target"]) == (
        "55009", "55009", "55002", "55100")
    assert data["internal_panel_target"] == "55200" and data["actuators"] == ACTUATORS
    assert json.dumps(data)  # everything stored is plain JSON


# ─── /av key (#63) ───────────────────────────────────────────────────────────

def _key_flow(of, key="stored-av-key"):
    flow = _options_flow(of, data={**ENTRY_DATA, "av_key": key}, options={"camera_target": "55100"})
    flow.updates, flow.reloads = [], []

    def update(entry, **kw):
        flow.updates.append(kw)
        entry.data = kw.get("data", entry.data)
    flow.hass.config_entries = types.SimpleNamespace(
        async_update_entry=update, async_schedule_reload=flow.reloads.append)
    return flow


def test_the_av_key_page_is_in_the_menu_and_shows_the_key(of):
    flow = _key_flow(of)
    assert "av_key" in asyncio.run(flow.async_step_init())["menu_options"]
    form = asyncio.run(flow.async_step_av_key())
    assert form["step_id"] == "av_key"
    assert form["description_placeholders"] == {"param": "auth", "key": "stored-av-key"}


def test_saving_without_regenerating_changes_nothing(of):
    flow = _key_flow(of)
    result = asyncio.run(flow.async_step_av_key({"regenerate": False}))
    assert result["type"] == "create_entry" and result["data"] == {"camera_target": "55100"}
    assert flow.updates == [] and flow.reloads == []


def test_regenerating_stores_a_new_key_and_reloads(of, monkeypatch):
    runtime = of.runtime
    monkeypatch.setattr(runtime, "AV_KEY", "stored-av-key")
    flow = _key_flow(of)
    result = asyncio.run(flow.async_step_av_key({"regenerate": True}))
    new = flow._entry.data["av_key"]
    assert new != "stored-av-key" and len(new) == 32 and runtime.AV_KEY == new
    assert flow.updates == [{"data": {**ENTRY_DATA, "av_key": new}}]
    assert flow.reloads == ["e1"]
    # The options stay as they were: the reload is asked for explicitly.
    assert result["data"] == {"camera_target": "55100"}


def test_the_av_key_page_is_translated(of):
    import pathlib
    base = pathlib.Path(of.__file__).parent
    for name in ("strings.json", "translations/en.json", "translations/it.json"):
        step = json.loads((base / name).read_text(encoding="utf-8"))["options"]["step"]
        assert "av_key" in step["init"]["menu_options"], name
        page = step["av_key"]
        assert "{param}" in page["description"] and "{key}" in page["description"], name
        assert set(page["data"]) == {"regenerate"}, name
        # hassfest refuses anything that looks like HTML, `<key>` included.
        assert "<" not in json.dumps(page, ensure_ascii=False), name
