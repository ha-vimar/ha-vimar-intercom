"""Opzione `allowed_users`: squilli, foto e media live solo agli utenti scelti."""
import asyncio
import types

import pytest
from harness.web import Request, fake_ws, load_views, make_hass

from custom_components.vimar_intercom import runtime
from custom_components.vimar_intercom import views as views_mod


class _NotAllowed(Exception):
    pass


def _req(admin=False, uid="u1"):
    r = Request(admin=admin)
    r._user = types.SimpleNamespace(is_admin=admin, id=uid)
    return r


@pytest.mark.parametrize("allowed,admin,uid,ok", [
    ([], False, "u1", True),        # lista vuota: ogni utente autenticato
    (["u1"], False, "u1", True),
    (["u1"], False, "u2", False),
    (["u1"], True, "u2", True),     # admin sempre
])
def test_rings_solo_agli_utenti_ammessi(monkeypatch, hub, allowed, admin, uid, ok):
    views = load_views(monkeypatch)
    monkeypatch.setattr(views_mod, "Unauthorized", _NotAllowed)
    monkeypatch.setattr(runtime, "ALLOWED_USERS", allowed)
    monkeypatch.setattr(runtime, "SNAPSHOT_DIR", "")
    monkeypatch.setattr(views.web, "json_response", lambda d: d, raising=False)
    view = views.VimarRingsView(make_hass(types.SimpleNamespace(hub=hub)))

    async def run():
        return await view.get(_req(admin, uid))

    if ok:
        asyncio.run(run())
    else:
        with pytest.raises(_NotAllowed):
            asyncio.run(run())


@pytest.mark.slow  # 25 s: /av waits for a call that never comes
def test_audio_ws_e_av_chiusi_a_chi_non_e_ammesso(monkeypatch, hub):
    views = load_views(monkeypatch)
    monkeypatch.setattr(runtime, "ALLOWED_USERS", ["u1"])
    monkeypatch.setattr(runtime, "AV_KEY", "placeholder-av-key")
    rig = types.SimpleNamespace(hub=hub)
    hass = make_hass(rig)
    ws = fake_ws(monkeypatch, views)

    async def run():
        await views.VimarAudioWSView(hass).get(_req(uid="u2"))
        av = await views.VimarAVStreamView(hass).get(_req(uid="u2"))
        senza = Request(admin=False, query={runtime.AV_KEY_PARAM: runtime.AV_KEY})
        senza._user = None
        anon = await views.VimarAVStreamView(hass).get(senza)
        return av.status, anon.status

    av, anon = asyncio.run(run())
    assert ws.closed and not rig.audio_ws_clients  # mai entrato fra i client
    # Without a user but with the /av key (stream worker, go2rtc): as before (#63).
    assert av == 403 and anon != 403
