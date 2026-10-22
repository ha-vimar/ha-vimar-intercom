"""Registro degli squilli per la card: squillo.json accanto alle foto, esito
aggiornato alla risposta, foto servite solo se sono davvero foto squillo."""
from __future__ import annotations

import asyncio
import os
import stat

import pytest

from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import ring_log as rl  # noqa: E402

sip = hub_mod.sip
R = hub_mod.R


def test_registro_limitato_e_dal_piu_recente(tmp_path):
    folder = str(tmp_path / "foto")
    for i in range(rl.RING_LOG_MAX + 5):
        rl.update_ring_log(folder, lambda r, i=i: r.append({"time": str(i), "photo": None}))
    rings = rl.read_ring_log(folder)
    assert len(rings) == rl.RING_LOG_MAX and rings[-1]["time"] == str(rl.RING_LOG_MAX + 4)
    assert [r["time"] for r in rl.recent_rings(folder, 2)] == ["204", "203"]
    assert os.listdir(folder) == [rl.RING_LOG], "file temporanei rimasti"


def test_le_foto_degli_squilli_usciti_dal_registro_si_cancellano(tmp_path):
    """Oltre RING_LOG_MAX la voce sparisce e con lei foto e clip; restano ultimo_squillo.jpg,
    le foto delle voci ancora in registro e i file che non sono foto squillo registrate."""
    folder = str(tmp_path)
    vecchia, ultima = "squillo_20260101_000000.jpg", "squillo_20260927_101500.jpg"
    clip = "squillo_20260101_000000.mp4"
    for n in (vecchia, clip, ultima, "ultimo_squillo.jpg", "squillo_20260102_000000.jpg"):
        (tmp_path / n).write_bytes(b"jpg")
    rl.update_ring_log(folder, lambda r: r.append({"time": "0", "photo": vecchia, "clip": clip}))
    rl.update_ring_log(folder, lambda r: r.extend(
        {"time": str(i), "photo": ultima if i == rl.RING_LOG_MAX else None}
        for i in range(1, rl.RING_LOG_MAX + 1)))
    assert not (tmp_path / vecchia).exists() and not (tmp_path / clip).exists()
    assert sorted(os.listdir(folder)) == [rl.RING_LOG, "squillo_20260102_000000.jpg", ultima, "ultimo_squillo.jpg"]


def test_registro_rotto_riparte_vuoto(tmp_path):
    (tmp_path / rl.RING_LOG).write_text("{non json")
    assert rl.read_ring_log(str(tmp_path)) == []


def test_registro_con_voci_non_oggetti(tmp_path):
    """squillo.json scritto a mano o da altro: niente 500 all'endpoint né squilli persi."""
    (tmp_path / rl.RING_LOG).write_text('[1, "x", null, {"time": "a", "photo": 5}]')
    assert rl.recent_rings(str(tmp_path), 10) == [{"time": "a", "photo": None}]
    rl.update_ring_log(str(tmp_path), lambda r: r.append({"time": "b"}))
    assert [r["time"] for r in rl.read_ring_log(str(tmp_path))] == ["a", "b"]


def test_il_temporaneo_e_sempre_un_file_nuovo(tmp_path):
    """Il temporaneo non ha un nome fisso: qualcosa messo al posto di «squillo.json.tmp»
    (un symlink verso un altro file, qui una cartella) non viene né seguito né usato."""
    (tmp_path / (rl.RING_LOG + ".tmp")).mkdir()
    rl.update_ring_log(str(tmp_path), lambda r: r.append({"time": "a"}))
    assert rl.read_ring_log(str(tmp_path)) == [{"time": "a"}]


def test_foto_solo_squillo_dentro_la_cartella(tmp_path):
    folder = tmp_path / "foto"
    folder.mkdir()
    (folder / "squillo_20260927_101500.jpg").write_bytes(b"jpg")
    (folder / "ultimo_squillo.jpg").write_bytes(b"jpg")
    (tmp_path / "squillo_20260927_101501.jpg").write_bytes(b"fuori")
    ok = rl.ring_photo_path(str(folder), "squillo_20260927_101500.jpg")
    assert ok and os.path.samefile(ok, folder / "squillo_20260927_101500.jpg")
    (folder / "squillo_20260927_101500_123.mp4").write_bytes(b"mp4")
    (folder / "squillo_20260927_101500_123.mp4.part").write_bytes(b"mp4 a meta")
    assert rl.ring_photo_path(str(folder), "squillo_20260927_101500_123.mp4")
    for bad in ("ultimo_squillo.jpg", "squillo.json", "../squillo_20260927_101501.jpg",
                "squillo_20260927_101500.jpg/..", "squillo_20260927_999999.jpg",
                "squillo_20260927_101500.jpgx", "..%2Fsquillo_20260927_101501.jpg",
                "squillo_20260927_101500_123.mp4.part", "squillo_20260927_101500.mkv"):
        assert rl.ring_photo_path(str(folder), bad) is None, bad
    assert rl.ring_photo_path("", "squillo_20260927_101500.jpg") is None


def test_lista_toglie_le_foto_mancanti(tmp_path):
    (tmp_path / "squillo_20260927_101500.jpg").write_bytes(b"jpg")
    rl.update_ring_log(str(tmp_path), lambda r: r.extend([
        {"time": "a", "photo": "squillo_20260927_101500.jpg"},
        {"time": "b", "photo": "squillo_20260927_101600.jpg"}]))  # foto mai arrivata
    assert [r["photo"] for r in rl.recent_rings(str(tmp_path), 10)] == [
        None, "squillo_20260927_101500.jpg"]


def test_lista_clip_solo_se_chiuso_e_versione_della_foto(tmp_path):
    """Il clip in scrittura (.part) non c'è ancora; photo_v cambia quando la foto migliore
    riscrive il file (la card non tiene in cache la prima)."""
    photo, clip = "squillo_20260927_101500_001.jpg", "squillo_20260927_101500_001.mp4"
    (tmp_path / (clip + ".part")).write_bytes(b"mp4 a meta")
    v1 = rl.write_photo(str(tmp_path), photo, b"prima")
    rl.update_ring_log(str(tmp_path), lambda r: r.append({"time": "a", "photo": photo, "clip": clip}))
    [r] = rl.recent_rings(str(tmp_path), 10)
    assert r["clip"] is None and r["photo"] == photo and r["photo_v"] == v1
    os.utime(tmp_path / photo, (0, os.path.getmtime(tmp_path / photo) + 3))  # come riscritta 3 s dopo
    (tmp_path / clip).write_bytes(b"mp4")
    [r] = rl.recent_rings(str(tmp_path), 10)
    assert r["clip"] == clip and r["photo_v"] > v1


def test_squillo_registrato_poi_risposto(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "")
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    monkeypatch.setattr(sip, "pending_incoming", {"caller_uri": "sip:55001@dom", "cid": "c1",
                                                  "active": True, "early": False})

    async def answer():
        return True, "ok"

    monkeypatch.setattr(sip, "do_answer_incoming", answer)

    async def scenario():
        hub = hub_mod.VimarIntercomHub()
        hub._save_ring_photo = lambda name: asyncio.sleep(0)
        await hub._handle_broadcast("ring", "")
        await asyncio.sleep(0.2)
        [ring] = rl.read_ring_log(str(tmp_path))
        assert ring["outcome"] == "missed" and ring["caller"] == "55001"
        assert rl.RING_FILE.fullmatch(ring["photo"])
        assert (await hub.async_answer())[0]
        await asyncio.sleep(0.2)
        assert rl.read_ring_log(str(tmp_path))[0]["outcome"] == "answered"

    asyncio.run(scenario())


def test_squillo_registrato_poi_rifiutato(tmp_path, monkeypatch):
    """Rifiuta (603) scrive «declined» nel registro: prima restava «missed» (prova sul 40507, 30/09)."""
    monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "")
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    monkeypatch.setattr(sip, "pending_incoming", {"caller_uri": "sip:55001@dom", "cid": "c1",
                                                  "active": True, "early": False})
    esito = [True]

    async def decline():
        return esito[0]

    monkeypatch.setattr(sip, "do_decline_incoming", decline)

    async def scenario():
        hub = hub_mod.VimarIntercomHub()
        hub._save_ring_photo = lambda name: asyncio.sleep(0)
        await hub._handle_broadcast("ring", "")
        await asyncio.sleep(0.2)
        assert rl.read_ring_log(str(tmp_path))[0]["outcome"] == "missed"
        esito[0] = False                      # nessuno squillo da rifiutare: il registro non cambia
        assert not (await hub.async_decline())[0]
        await asyncio.sleep(0.2)
        assert rl.read_ring_log(str(tmp_path))[0]["outcome"] == "missed"
        esito[0] = True
        assert (await hub.async_decline())[0]
        await asyncio.sleep(0.2)
        assert rl.read_ring_log(str(tmp_path))[0]["outcome"] == "declined"

    asyncio.run(scenario())


def test_a_link_planted_at_the_photo_name_is_replaced_not_written_through(tmp_path):
    """A hard link (or a symlink) at ultimo_squillo.jpg pointing at another file:
    the photo goes to a new file, the other file keeps its content."""
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep me")
    folder = tmp_path / "foto"
    folder.mkdir()
    try:
        os.link(victim, folder / rl.LAST_PHOTO)
    except OSError:
        pytest.skip("cannot create a link here")
    rl.write_photo(str(folder), "squillo_20260927_101500.jpg", b"NEW")
    assert victim.read_bytes() == b"keep me"
    assert (folder / rl.LAST_PHOTO).read_bytes() == b"NEW"


def test_a_symlink_as_the_last_photo_is_not_served(tmp_path):
    victim = tmp_path / "secret.txt"
    victim.write_bytes(b"not a photo")
    folder = tmp_path / "foto"
    folder.mkdir()
    try:
        os.symlink(victim, folder / rl.LAST_PHOTO)
    except OSError:
        pytest.skip("cannot create a symlink here")
    assert rl.read_last_photo(None, str(folder)) is None
    assert rl.read_last_photo(str(folder / rl.LAST_PHOTO), None) is None
    (tmp_path / "real.jpg").write_bytes(b"jpg")
    assert rl.read_last_photo(str(tmp_path / "real.jpg"), None) == b"jpg"


def test_a_hard_link_in_the_folder_is_not_served(tmp_path):
    """#46: a hard link to another file, left at the last photo or at a ring file name,
    is not a photo or a clip of ours (they are always renamed into place, one name)."""
    victim = tmp_path / "secret.txt"
    victim.write_bytes(b"not a photo")
    folder = tmp_path / "foto"
    folder.mkdir()
    try:
        os.link(victim, folder / rl.LAST_PHOTO)
        os.link(victim, folder / "squillo_20260927_101500.mp4")
    except OSError:
        pytest.skip("cannot create a hard link here")
    assert rl.read_last_photo(None, str(folder)) is None
    assert rl.ring_photo_path(str(folder), "squillo_20260927_101500.mp4") is None
    rl.write_photo(str(folder), "squillo_20260927_101500.jpg", b"NEW")
    assert rl.read_last_photo(None, str(folder)) == b"NEW"
    assert rl.ring_photo_path(str(folder), "squillo_20260927_101500.jpg")


def test_a_share_reporting_zero_links_is_still_served():
    from types import SimpleNamespace

    reg = stat.S_IFREG | 0o644
    assert not rl._linked(SimpleNamespace(st_mode=reg, st_nlink=0))
    assert not rl._linked(SimpleNamespace(st_mode=reg, st_nlink=1))
    assert rl._linked(SimpleNamespace(st_mode=reg, st_nlink=2))


def test_rotation_removes_a_ring_file_even_if_it_was_hard_linked(tmp_path):
    victim = tmp_path / "other.txt"
    victim.write_bytes(b"x")
    folder = tmp_path / "foto"
    folder.mkdir()
    try:
        os.link(victim, folder / "squillo_20260927_101500.jpg")
    except OSError:
        pytest.skip("cannot create a hard link here")
    rl.update_ring_log(str(folder), lambda r: r.append({"time": "0", "photo": "squillo_20260927_101500.jpg"}))
    rl.update_ring_log(str(folder), lambda r: r.extend({"time": str(i)} for i in range(1, rl.RING_LOG_MAX + 1)))
    assert not (folder / "squillo_20260927_101500.jpg").exists() and victim.read_bytes() == b"x"


def test_a_symlink_to_a_file_in_the_folder_is_not_served_and_rotation_keeps_the_target(tmp_path):
    folder = tmp_path / "foto"
    folder.mkdir()
    (folder / "squillo.json").write_text("[]")
    try:
        os.symlink(folder / "squillo.json", folder / "squillo_20260927_101500.jpg")
    except OSError:
        pytest.skip("cannot create a symlink here")
    assert rl.ring_photo_path(str(folder), "squillo_20260927_101500.jpg") is None
    rl.update_ring_log(str(folder), lambda r: r.append({"time": "0", "photo": "squillo_20260927_101500.jpg"}))
    rl.update_ring_log(str(folder), lambda r: r.extend({"time": str(i)} for i in range(1, rl.RING_LOG_MAX + 1)))
    assert (folder / "squillo.json").exists()
