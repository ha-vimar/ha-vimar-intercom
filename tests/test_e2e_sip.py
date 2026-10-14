"""End-to-end SIP: sip_client + hub + media_handler + le view /av e /audio_ws vere
contro la targa finta (harness), solo 127.0.0.1, senza ffmpeg né browser: gira in CI.

Il campo: il campanello suona quando vuole, i BYE si incrociano, UDP perde pacchetti,
la connessione cloud cade, go2rtc riapre /av subito e lo stream worker di HA dopo 10 s.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import types

import pytest
from harness.peer import DOMAIN, PASSWORD, USER, Msg, answer_200, check_digest, is_, response
from harness.rig import Rig, run, wait_until
from harness.web import Request, fake_ws, load_views, make_hass, open_av

from custom_components.vimar_intercom import av_stream
from custom_components.vimar_intercom import const as C
from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import runtime as R
from custom_components.vimar_intercom import sip_client as sip

pytestmark = pytest.mark.slow


def lose_first(peer, method):
    """La rete (Wi-Fi del Tab) perde il primo datagramma `method` diretto alla targa."""
    orig, lost = peer._on_raw, []

    def on_raw(raw):
        if raw.startswith(method + " ") and not lost:
            lost.append(raw)
            return
        orig(raw)
    peer._on_raw = on_raw
    return lost


async def busy(peer, inv):
    peer.reply(inv, 486, "Busy Here")


# ─── registrazione ──────────────────────────────────────────────────────────

def test_registrazione_udp_con_digest(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            assert await sip.do_register()
            assert sip.registered
            regs = rig.peer.got(is_("REGISTER"))
            assert len(regs) == 2 and "authorization" in regs[1].hdrs
            # ri-registrazione (keepalive) e poi un rifiuto: il flag scende
            assert await sip.do_register()
            rig.peer.register_code = 403
            assert not await sip.do_register()
            assert not sip.registered
    run(s())


def test_registrazione_tls_e_riconnessione(monkeypatch):
    """Il proxy chiude la connessione: il reader si riconnette senza bloccarsi sulla
    REGISTER che solo lui può leggere, e la connessione nuova porta le chiamate."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            assert rig.peer.connections == 1
            n = len(rig.peer.log)
            rig.peer.drop()
            await wait_until(lambda: not sip.registered, 3)
            await rig.peer.wait_for(is_("REGISTER"), timeout=8, start=n)
            await wait_until(lambda: sip.registered, 5)
            assert rig.peer.connections == 2
            rig.peer.on_invite = answer_200
            ok, _ = await sip.do_call()
            assert ok and sip.in_call
    run(s())


def test_riconnessioni_parallele_una_sola_connessione(monkeypatch):
    """Mentre il reader si riconnette scatta il keepalive (reconnect()): due connect() in
    parallelo lasciavano una connessione orfana, e lo squillo che il proxy manda
    sull'ultima non veniva letto."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.peer.drop()
            await wait_until(lambda: not sip.registered, 3)
            await rig.hub._keepalive_tick()           # il giro dei 120 s cade qui
            await wait_until(lambda: sip.registered, 10)
            await asyncio.sleep(3)                     # tutti i tentativi si assestano
            assert rig.peer.connections == 2, f"{rig.peer.connections - 1} riconnessioni"
            rig.ring("ring-7")
            await rig.peer.wait_for(is_(code=183), timeout=3)
    run(s())


class _NotReady(Exception):
    pass


def test_cloud_irraggiungibile_all_avvio_si_riprova(monkeypatch):
    """Dopo un blackout HA riparte prima del router: l'entry va in «riprova»
    (ConfigEntryNotReady), non in errore fino al reload a mano; porte RTP libere."""
    monkeypatch.setattr(sys.modules["homeassistant.exceptions"], "ConfigEntryNotReady",
                        _NotReady, raising=False)
    views = load_views(monkeypatch)

    class _Store:  # storage degli SPS/PPS (stub di HA): vuoto
        def __init__(self, *a): ...

        async def async_load(self):
            return None
    monkeypatch.setattr(views, "Store", _Store)
    for k, v in vars(R).items():  # configure() riscrive il modulo: ripristino a fine test
        if k.isupper():
            monkeypatch.setattr(R, k, v)
    ports = []
    for _ in range(2):
        s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_.bind(("127.0.0.1", 0))
        ports.append(s_.getsockname()[1])
        s_.close()
    for mod in (C, media):
        monkeypatch.setattr(mod, "RTP_AUDIO_PORT", ports[0])
        monkeypatch.setattr(mod, "RTP_VIDEO_PORT", ports[1])
    for k in ("audio_proto", "video_proto", "_stun_task", "_audio_task", "ws_send_bytes"):
        monkeypatch.setattr(media, k, None)
    for k, val in dict(registered=False, reader=None, writer=None, _udp_sock=None,
                       incoming_requests=None, MY_IP=None).items():
        monkeypatch.setattr(sip, k, val)
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    monkeypatch.setattr(sip, "_resolve_sip_targets", lambda proxy, port: [("127.0.0.1", dead_port)])
    monkeypatch.setattr(C, "CA_PATH", os.path.join(tempfile.gettempdir(), "nessun-ca.pem"))
    entry = types.SimpleNamespace(
        entry_id="e1", options={},
        data=dict(sip_user=USER, sip_password=PASSWORD, sip_domain=DOMAIN,
                  cloud_proxy="localhost", use_local_udp=False,
                  device_imei="000", device_uuid="uuid-test", av_key="av-key-test"))
    hass = types.SimpleNamespace(data={})

    async def s():
        with pytest.raises(_NotReady):
            await views.async_setup_entry(hass, entry)
        assert not hass.data.get(C.DOMAIN), "hub morto lasciato in hass.data"
        await asyncio.sleep(0.1)  # transport.close() chiude al giro successivo del loop
        for port in ports:        # libere per il prossimo tentativo
            s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s_.bind(("0.0.0.0", port))
            s_.close()
    try:
        run(s(), 60)
    finally:
        media.close_transports()


# ─── chiamata in uscita ─────────────────────────────────────────────────────

def test_chiamata_200_keyframe_e_riaggancio(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            ok, msg = await rig.hub.async_call()
            assert ok, msg
            await rig.peer.wait_for(is_("ACK"))
            await rig.peer.wait_for(is_("INFO"))           # keyframe
            assert rig.hub.status == "in_call" and media.audio_proto.remote_addr
            await rig.hub.async_hangup()
            bye = await rig.peer.wait_for(is_("BYE"))
            assert f";tag={rig.peer.to_tag}" in bye.h("to")
            assert rig.hub.status == "idle" and media.audio_proto.remote_addr is None
            assert rig.events("call_ended")
    run(s())


@pytest.mark.parametrize("code, reason", [(486, "Busy Here"), (603, "Decline")])
def test_chiamata_rifiutata(monkeypatch, code, reason):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.retransmit_non2xx = True     # UAS UDP: rimanda il 4xx/6xx fino all'ACK

            async def rifiuta(peer, inv):
                peer.reply(inv, 100, "Trying")
                peer.reply(inv, code, reason)
            rig.peer.on_invite = rifiuta
            ok, msg = await sip.do_call()
            assert not ok and msg.startswith(str(code))
            await asyncio.sleep(1.0)
            assert not sip.calling and rig.hub.status == "idle"
            # L'ACK di un non-2xx appartiene alla transazione dell'INVITE: stesso branch.
            inv = rig.peer.got(is_("INVITE"))[0]
            acks = rig.peer.got(is_("ACK"))
            assert acks and all(a.branch == inv.branch for a in acks), "ACK fuori transazione"
            assert not rig.peer._unacked, "la targa continua a ritrasmettere il rifiuto"
    run(s())


def test_chiamata_timeout_manda_cancel(monkeypatch):
    fast = types.SimpleNamespace(time=lambda t0=time.time(): t0 + (time.time() - t0) * 20,  # noqa: B008 (t0 on purpose)
                                 monotonic=time.monotonic)
    monkeypatch.setattr(sip, "time", fast)

    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()

            async def muta(peer, inv):
                peer.reply(inv, 180, "Ringing")
            rig.peer.on_invite = muta
            ok, msg = await sip.do_call()
            assert not ok and "Timeout" in msg
            assert not sip.calling
            # Senza CANCEL la targa resta a squillare e, se poi risponde, tiene una
            # chiamata mezza aperta.
            await rig.peer.wait_for(is_("CANCEL"), timeout=2)
    run(s())


def test_a_call_with_an_answer_timeout_gives_up_early_and_cancels(monkeypatch):
    """#41: a view's call that the panel leaves unanswered returns NO_ANSWER
    after answer_timeout instead of 45 s, and the INVITE is cancelled."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()

            async def silent(peer, inv):
                peer.reply(inv, 180, "Ringing")
            rig.peer.on_invite = silent
            loop = asyncio.get_running_loop()
            started = loop.time()
            ok, msg = await sip.do_call(answer_timeout=0.5)
            assert not ok and msg.startswith(sip.NO_ANSWER)
            assert loop.time() - started < 0.5 + 1, "to the deadline, not up to 3 s past it"
            assert not sip.calling
            await rig.peer.wait_for(is_("CANCEL"), timeout=2)
    run(s())


def test_a_100_with_no_180_gives_up_after_the_ring_timeout(monkeypatch):
    """#44 on a 40507: the Tab's proxy says 100 Trying and the panel never sends its
    180 (in a good call it follows within ~50 ms). Given up after ring_timeout,
    not after answer_timeout, and cancelled."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()

            async def swallowed(peer, inv):
                peer.reply(inv, 100, "Trying")
            rig.peer.on_invite = swallowed
            loop = asyncio.get_running_loop()
            started = loop.time()
            ok, msg = await sip.do_call(answer_timeout=5, ring_timeout=0.5)
            assert not ok and msg.startswith(sip.NO_ANSWER) and "180" in msg
            assert loop.time() - started < 0.5 + 1
            await rig.peer.wait_for(is_("CANCEL"), timeout=2)
    run(s())


def test_a_ringing_panel_is_not_cut_by_the_ring_timeout(monkeypatch):
    """With a 180 the call waits for its answer: ring_timeout only catches a
    panel that never rang."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()

            async def rings(peer, inv):
                peer.reply(inv, 100, "Trying")
                peer.reply(inv, 180, "Ringing")
            rig.peer.on_invite = rings
            loop = asyncio.get_running_loop()
            started = loop.time()
            ok, msg = await sip.do_call(answer_timeout=1.2, ring_timeout=0.3)
            assert not ok and msg == f"{sip.NO_ANSWER} (1s)"
            assert loop.time() - started >= 1.0
    run(s())


def test_the_487_after_our_own_cancel_is_not_a_warning(monkeypatch, caplog):
    async def nothing(*_a):
        pass
    monkeypatch.setattr(sip, "_close_orphan_invite", nothing)
    raw = ("SIP/2.0 487 Request Terminated\r\nCall-ID: gone-487\r\nCSeq: 7 INVITE\r\n"
           "Content-Length: 0\r\n\r\n")
    with caplog.at_level(logging.DEBUG, logger=sip._LOGGER.name):
        run(sip._dispatch_message(raw))
    recs = [r for r in caplog.records if "già chiuso" in r.getMessage()]
    assert recs and all(r.levelno == logging.DEBUG for r in recs)


@pytest.mark.parametrize("ritrasmesso", [False, True])
def test_chiamata_con_407(monkeypatch, ritrasmesso):
    """Sfida del proxy: un solo INVITE con auth, l'ACK del 407 nella sua transazione;
    un 407 ritrasmesso (ACK perso) riceve solo un altro ACK, non un terzo INVITE."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()

            async def sfida(peer, inv):
                if "proxy-authorization" not in inv.hdrs:
                    raw = response(inv, 407, "Proxy Authentication Required", to_tag="prx",
                                   extra=f'Proxy-Authenticate: Digest realm="{DOMAIN}", nonce="abc"\r\n')
                    peer.send(raw)
                    if ritrasmesso:
                        await asyncio.sleep(0.2)
                        peer.send(raw)
                    return
                assert check_digest(inv, "INVITE", inv.h("proxy-authorization"))
                peer.reply(inv, 180, "Ringing")
                await asyncio.sleep(0.8)            # la targa squilla un po'
                peer.reply(inv, 200, "OK", body=peer.sdp())
            rig.peer.on_invite = sfida
            ok, msg = await sip.do_call()
            assert ok, msg
            await asyncio.sleep(0.3)
            invites = rig.peer.got(is_("INVITE"))
            assert len(invites) == 2, f"INVITE duplicati: {invites}"
            ack407 = rig.peer.got(lambda m: m.kind == "ACK" and m.h("cseq").startswith(
                invites[0].h("cseq").split()[0] + " "))
            assert ack407 and all(a.branch == invites[0].branch for a in ack407)
            await rig.hub.async_hangup()
    run(s())


def test_invite_perso_viene_ritrasmesso(monkeypatch):
    """"Vedi esterno" col primo INVITE perso: senza Timer A 45 s e poi «Timeout»."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            lost = lose_first(rig.peer, "INVITE")
            t0 = time.monotonic()
            ok, msg = await asyncio.wait_for(sip.do_call(), 50)
            assert lost and ok, msg
            assert time.monotonic() - t0 < 5, f"video dopo {time.monotonic() - t0:.0f} s"
            await sip.do_hangup()
    run(s(), 120)


def test_bye_perso_viene_ritrasmesso(monkeypatch):
    """Il BYE si perde: la targa resta occupata e il "Vedi esterno" dopo riceve 486."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]
            lost = lose_first(rig.peer, "BYE")
            await rig.hub.async_hangup()
            assert lost
            await rig.peer.wait_for(is_("BYE"), timeout=3)
            assert rig.hub.status == "idle"
    run(s())


def test_bye_della_targa_ferma_i_keyframe(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]
            await asyncio.sleep(6)
            infos = len(rig.peer.got(is_("INFO")))
            assert infos >= 2                          # burst + refresh a 5 s
            n = len(rig.peer.log)
            rig.bye(rig.peer.got(is_("INVITE"))[0])
            await rig.peer.wait_for(is_(code=200, cseq="BYE"), start=n)
            await wait_until(lambda: rig.events("call_ended"))
            assert rig.hub.status == "idle" and rig.hub._keyframe_task is None
            await asyncio.sleep(5.5)
            assert len(rig.peer.got(is_("INFO"))) == infos, "INFO dopo la fine della chiamata"
    run(s())


def test_annulla_durante_calling_e_200_tardivo(monkeypatch):
    """Riaggancio mentre la targa squilla, poi il 200 OK arriva lo stesso."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            go = asyncio.Event()

            async def lenta(peer, inv):
                peer.reply(inv, 100, "Trying")
                peer.reply(inv, 180, "Ringing")
                await go.wait()
                peer.reply(inv, 200, "OK", body=peer.sdp())   # incrocia il CANCEL
            rig.peer.on_invite = lenta
            call = asyncio.create_task(rig.hub.async_call())
            await rig.peer.wait_for(is_("INVITE"))
            await asyncio.sleep(0.2)
            await rig.hub.async_hangup()                # "Annulla" della card
            go.set()
            ok, _ = await call
            await asyncio.sleep(0.5)
            assert not ok, "la chiamata annullata è partita lo stesso"
            assert not sip.in_call and rig.hub.status == "idle"
            inv = rig.peer.got(is_("INVITE"))[0]
            assert rig.peer.got(is_("CANCEL", cid=inv.cid))
            # la targa non resta con una chiamata mezza aperta: ACK + BYE del 200
            assert rig.peer.got(is_("ACK", cid=inv.cid)) and rig.peer.got(is_("BYE", cid=inv.cid))
    run(s())


def test_annulla_mentre_si_lavora_il_200_ok_non_fa_risorgere_la_chiamata(monkeypatch):
    """«Annulla» nell'istante fra il 200 OK e il media aperto (ACK partito, setup_media in
    corso): do_hangup ha già chiuso tutto (call_ended) e la chiamata ripartiva lo stesso,
    in_call e call_started, senza nessuno che la chiudesse."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            orig = media.setup_media

            async def setup(*a, **k):
                await orig(*a, **k)
                await rig.hub.async_hangup()          # l'utente riaggancia proprio ora
            monkeypatch.setattr(media, "setup_media", setup)
            assert await rig.hub.async_call() == (False, "Annullata")
            await asyncio.sleep(0.3)
            assert not sip.in_call and not sip.calling and rig.hub.status == "idle"
            assert rig.events("call_started") == [] and len(rig.events("call_ended")) == 1
            inv = rig.peer.got(is_("INVITE"))[0]
            assert len(rig.peer.got(is_("ACK", cid=inv.cid))) == 1
            assert rig.peer.got(is_("BYE", cid=inv.cid)), "la targa resta con la chiamata aperta"
            assert media.audio_proto.remote_addr is None and sip.call_state["call_id"] is None
    run(s())


def test_annulla_con_tls_caduto_non_solleva(monkeypatch):
    """«Annulla» mentre la targa squilla e la connessione cloud è appena caduta: il CANCEL
    non parte, ma do_call finisce pulito («Annullata»), non con l'eccezione del send."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()

            async def muta(peer, inv):
                peer.reply(inv, 180, "Ringing")
            rig.peer.on_invite = muta
            call = asyncio.create_task(rig.hub.async_call())
            await rig.peer.wait_for(is_("INVITE"))
            rig.peer.drop()
            await asyncio.sleep(0.2)
            await rig.hub.async_hangup()
            assert await call == (False, "Annullata")
            assert not sip.calling and not sip.in_call
    run(s())


def test_200_orfano_dopo_timeout_viene_chiuso(monkeypatch):
    """Il log di campo: «Stale response 200 for cid=call-… (known: [])»."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            cid = "call-orfano"
            inv_raw = (f"INVITE sip:55001@{DOMAIN} SIP/2.0\r\n"
                       f"Via: SIP/2.0/UDP 127.0.0.1:{sip._my_port()};branch=z9hG4bKorf;rport\r\n"
                       f"From: <sip:60901@{DOMAIN}>;tag=mio\r\nTo: <sip:55001@{DOMAIN}>\r\n"
                       f"Call-ID: {cid}\r\nCSeq: 7 INVITE\r\nContent-Length: 0\r\n\r\n")
            rig.peer.reply(Msg(inv_raw), 200, "OK", body=rig.peer.sdp())
            await rig.peer.wait_for(is_("ACK", cid=cid), timeout=2)
            bye = await rig.peer.wait_for(is_("BYE", cid=cid), timeout=2)
            assert f"tag={rig.peer.to_tag}" in bye.h("to") and "tag=mio" in bye.h("from")
            assert not sip.in_call
    run(s())


def test_200_ritrasmesso_della_chiamata_in_corso_solo_ack(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await sip.do_call())[0]
            inv = rig.peer.got(is_("INVITE"))[0]
            n = len(rig.peer.log)
            rig.peer.reply(inv, 200, "OK", body=rig.peer.sdp())   # il nostro ACK si è perso
            await rig.peer.wait_for(is_("ACK"), start=n)
            await asyncio.sleep(0.3)
            assert not rig.peer.got(is_("BYE")) and sip.in_call
            await sip.do_hangup()
    run(s())


def test_reinvite_su_chiamata_in_uscita(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]
            inv = rig.peer.got(is_("INVITE"))[0]
            our_tag = inv.h("from").split("tag=")[1]
            n = len(rig.peer.log)
            rig.peer.request("INVITE", inv.cid, 1, rig.peer.to_tag, to_tag=our_tag, body=rig.peer.sdp())
            r = await rig.peer.wait_for(lambda m: m.code and m.cid == inv.cid and m.code >= 200, start=n)
            assert r.code == 200, f"re-INVITE della targa risposto con {r.code}"
            await asyncio.sleep(0.3)
            assert rig.hub.status == "in_call" and not rig.peer.got(is_(code=603))
            await rig.hub.async_hangup()
    run(s())


def test_bye_con_altro_call_id(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]
            rig.peer.request("BYE", "estraneo", 1, "x", to_tag="y")
            await rig.peer.wait_for(is_(code=481))
            assert rig.hub.status == "in_call"
            await rig.hub.async_hangup()
    run(s())


def test_bye_incrociati_poi_nuova_visione(monkeypatch):
    """"Riaggancia" nello stesso istante in cui la targa chiude (10 s), poi subito
    "Vedi esterno": il vecchio riaggancio finisce subito e non smonta la chiamata nuova."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]
            first = rig.peer.got(is_("INVITE"))[0]
            rig.bye(first)                                      # la targa chiude...
            t0 = time.monotonic()
            hang = asyncio.create_task(rig.hub.async_hangup())  # ...e l'utente riaggancia
            await wait_until(lambda: rig.hub.status == "idle", 3)
            ok, msg = await rig.hub.async_call()                # "Vedi esterno" di nuovo
            assert ok, msg
            second = rig.peer.got(is_("INVITE"))[-1]
            assert second.cid != first.cid
            await asyncio.wait_for(hang, 10)
            assert time.monotonic() - t0 < 2, "Riaggancia ha aspettato la risposta al BYE"
            await asyncio.sleep(0.2)
            assert sip.in_call and sip.call_state["call_id"] == second.cid, \
                f"chiamata nuova smontata dal vecchio riaggancio: {sip.call_state['call_id']}"
            assert media.audio_proto.remote_addr, "media della chiamata nuova chiuso"
            n = len(rig.peer.log)
            rig.bye(second)
            r = await rig.peer.wait_for(lambda m: m.code and m.cid == second.cid and "BYE" in m.h("cseq"),
                                        start=n)
            assert r.code == 200, f"BYE della targa sulla chiamata nuova: {r.code}"
            await wait_until(lambda: rig.hub.status == "idle", 3)
    run(s())


def test_bye_incrociati_non_spengono_lo_squillo_dopo(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]
            rig.bye(rig.peer.got(is_("INVITE"))[0])
            hang = asyncio.create_task(rig.hub.async_hangup())     # stesso istante
            await asyncio.sleep(1)
            rig.ring("ring-2", "q")
            await rig.peer.wait_for(is_(code=183))
            await hang
            assert rig.hub.status == "ringing" and media.video_proto.remote_addr, "anteprima spenta"
            assert len(rig.events("call_ended")) == 1
    run(s())


# ─── chiamata in arrivo ─────────────────────────────────────────────────────

def test_squillo_early_media_cancel(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            raw = rig.ring()
            r183 = await rig.peer.wait_for(is_(code=183))
            assert r183.body and "m=audio" in r183.body
            await wait_until(lambda: rig.rings == 1)
            assert rig.hub.status == "ringing" and rig.hub.video_active
            # ritrasmissione dell'INVITE: stessa risposta, nessun secondo squillo
            rig.peer.send(raw)
            await wait_until(lambda: len(rig.peer.got(is_(code=183))) == 2)
            assert rig.rings == 1 and rig.hub.stats["ring_count"] == 1
            rig.peer.request("CANCEL", "ring-1", 1, "pnl")
            await rig.peer.wait_for(is_(code=487))
            await wait_until(lambda: rig.hub.status == "idle")
            assert not rig.hub.video_active and media.video_proto.remote_addr is None
            assert rig.hub.stats["missed_count"] == 1
            # il 487 si è perso (UDP): all'INVITE ritrasmesso di nuovo il 487, non il silenzio
            n = len(rig.peer.log)
            rig.peer.send(raw)
            r = await rig.peer.wait_for(lambda m: m.code and m.cid == "ring-1", start=n, timeout=2)
            assert r.code == 487 and rig.rings == 1 and rig.hub.status == "idle"
    run(s())


def test_invite_ritrasmesso_dopo_il_rifiuto_riceve_di_nuovo_il_rifiuto(monkeypatch):
    """Il 603 si perde via UDP: la targa ritrasmette l'INVITE e deve riavere la stessa
    risposta finale. Prima riceveva niente, e il PBX teneva lo squillo aperto."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            raw = rig.ring()
            await rig.peer.wait_for(is_(code=183))
            await rig.hub.async_decline()
            await rig.peer.wait_for(is_(code=603))
            n = len(rig.peer.log)
            rig.peer.send(raw)
            r = await rig.peer.wait_for(lambda m: m.code and m.cid == "ring-1", start=n, timeout=2)
            assert r.code == 603 and rig.rings == 1 and rig.hub.status == "idle"
            assert not media.video_proto.remote_addr
    run(s())


def test_squillo_risposta_e_reinvite(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            raw = rig.ring()
            await rig.peer.wait_for(is_(code=183))
            ok, _ = await rig.hub.async_answer()
            assert ok and rig.hub.status == "in_call"
            ok200 = await rig.peer.wait_for(is_(code=200, cseq="INVITE"))
            my_tag = ok200.h("to").split("tag=")[1]
            rig.peer.send(raw)                  # il 200 si è perso (UDP): la targa ritrasmette
            await wait_until(lambda: len(rig.peer.got(is_(code=200, cseq="1 INVITE"))) == 2, 2,
                             "200 OK di nuovo all'INVITE ritrasmesso")
            rig.peer.request("ACK", "ring-1", 1, "pnl", to_tag=my_tag)
            n = len(rig.peer.log)
            # re-INVITE nel dialogo (refresh di sessione / cambio media)
            rig.peer.request("INVITE", "ring-1", 2, "pnl", to_tag=my_tag, body=rig.peer.sdp())
            r = await rig.peer.wait_for(lambda m: m.code and m.cid == "ring-1", start=n)
            assert r.code == 200 and r.h("cseq") == "2 INVITE", f"re-INVITE risposto con {r}"
            assert r.body, "il 200 del re-INVITE deve portare l'SDP"
            await asyncio.sleep(0.3)
            assert rig.hub.status == "in_call" and rig.rings == 1
            n = len(rig.peer.log)
            rig.peer.request("UPDATE", "ring-1", 3, "pnl", to_tag=my_tag)
            r = await rig.peer.wait_for(lambda m: m.code and m.cid == "ring-1", start=n, timeout=2)
            assert r.code == 200 and r.h("cseq") == "3 UPDATE"
            await rig.hub.async_hangup()
    run(s())


def test_rifiuto_e_secondo_invite_durante_squillo(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.ring("ring-A", "a")
            await rig.peer.wait_for(is_(code=183))
            n = len(rig.peer.log)
            rig.ring("ring-B", "b")          # seconda targa mentre la prima squilla
            r = await rig.peer.wait_for(lambda m: m.code and m.cid == "ring-B" and m.code >= 180, start=n)
            await asyncio.sleep(0.2)
            assert sip.ringing("ring-A"), "il secondo INVITE ha rubato lo squillo in corso"
            assert r.code == 486
            await rig.hub.async_decline()
            dec = await rig.peer.wait_for(is_(code=603))
            assert dec.cid == "ring-A" and rig.hub.status == "idle"
    run(s())


def test_squillo_scade_dopo_ring_max(monkeypatch):
    monkeypatch.setattr(sip, "RING_MAX_S", 0.5)

    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.ring()
            await rig.peer.wait_for(is_(code=183))
            await wait_until(lambda: rig.events("ring_ended"), 3)
            assert rig.hub.status == "idle" and not rig.hub.video_active
            # la targa deve sapere che qui non si risponde più
            r = await rig.peer.wait_for(lambda m: m.code and m.code >= 300 and m.cid == "ring-1", timeout=1)
            assert r.h("cseq") == "1 INVITE"
    run(s())


@pytest.mark.parametrize("utente_prima", [True, False])
def test_risposta_in_gara_col_messaggio_di_assenza(monkeypatch, utente_prima):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "/x/msg.mp3")
            monkeypatch.setattr(R, "AWAY_MESSAGE_DELAY", 1)

            async def load(path):
                return b"\0" * 320 * 50              # 1 s di silenzio
            monkeypatch.setattr(media, "load_pcm", load)
            rig.ring()
            await rig.peer.wait_for(is_(code=183))
            await asyncio.sleep(0.98 if utente_prima else 1.05)
            ok, msg = await rig.hub.async_answer()
            assert ok, msg
            await asyncio.sleep(1.5)
            assert len(rig.peer.got(is_(code=200, cseq="INVITE"))) == 1, "due 200 OK allo stesso INVITE"
            # chi ha risposto (utente) tiene la linea: il messaggio non riaggancia
            assert rig.hub.status == "in_call" and not rig.peer.got(is_("BYE"))
            await rig.hub.async_hangup()
    run(s())


def test_squillo_senza_sdp_offerta_nell_ack(monkeypatch):
    """Offerta ritardata: INVITE senza SDP, il nostro 200 offre, la targa risponde
    nell'ACK. Il media deve partire dall'SDP dell'ACK."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.ring(sdp=False)
            await rig.peer.wait_for(is_(code=180))
            await wait_until(lambda: rig.rings == 1)
            assert (await rig.hub.async_answer())[0]
            ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-1"))
            assert "m=audio" in ok200.body
            rig.peer.request("ACK", "ring-1", 1, "pnl", to_tag=ok200.h("to").split("tag=")[1],
                             body=rig.peer.sdp())
            await wait_until(lambda: media.audio_proto.remote_addr is not None, 2)
            assert media.video_proto.remote_addr == ("127.0.0.1", rig.peer.video_port)
            await rig.hub.async_hangup()
    run(s())


def test_squillo_solo_audio_e_porta_video_zero(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.ring(body=("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                           f"m=audio {rig.peer.audio_port} RTP/AVP 8 0\r\na=rtpmap:8 PCMA/8000\r\n"
                           "m=video 0 RTP/AVP 96\r\n"))
            await rig.peer.wait_for(is_(code=183))
            await wait_until(lambda: rig.rings == 1)
            assert (await rig.hub.async_answer())[0]
            assert media.video_proto.remote_addr is None
            await rig.hub.async_hangup()
    run(s())


def test_apri_porta_durante_la_chiamata(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.ring()
            await rig.peer.wait_for(is_(code=183))
            assert (await rig.hub.async_answer())[0]
            await asyncio.sleep(0.3)                           # burst di INFO in corso
            ok, msg, _ = await rig.hub.async_door()
            assert ok, msg
            assert rig.peer.got(lambda m: m.kind == "MESSAGE" and m.body == "OPEN_2F")
            assert (await rig.hub.async_door(command="OPEN_3F"))[0]   # senza target: command vale
            assert rig.peer.got(lambda m: m.kind == "MESSAGE" and m.body == "OPEN_3F")
            assert rig.hub.status == "in_call"
            await rig.hub.async_hangup()
    run(s())


def test_rinnovo_register_durante_lo_squillo_cloud_non_tocca_lo_squillo(monkeypatch):
    """Il keepalive rinnova il REGISTER mentre la targa suona (TLS, early media): lo squillo
    resta, e il 200 OK parte sullo stesso dialogo del 183."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.ring("ring-reg")
            r183 = await rig.peer.wait_for(is_(code=183))
            await wait_until(lambda: rig.rings == 1)
            n = len(rig.peer.log)
            await rig.hub._keepalive_tick()  # il giro dei 120 s, senza aspettarlo
            await rig.peer.wait_for(lambda m: m.kind == "REGISTER" and "authorization" in m.hdrs, start=n)
            assert sip.registered and not rig.hub.stats["register_failures"]  # il tick aspetta il 200
            assert rig.hub.status == "ringing" and rig.hub.video_active
            assert (await rig.hub.async_answer())[0]
            ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-reg"), start=n)
            assert ok200.h("to").split("tag=")[1] == r183.h("to").split("tag=")[1]
            await asyncio.sleep(0.3)
            assert rig.hub.status == "in_call"
            await rig.hub.async_hangup()
            await rig.peer.wait_for(is_("BYE", cid="ring-reg"))
    run(s())


def test_cancel_del_ramo_doppio_prima_della_risposta_non_chiude_lo_squillo(monkeypatch):
    """Il relay biforca lo squillo e annulla il ramo doppio prima che rispondiamo (il Tab ha
    risposto e mollato, un retry del relay): quel CANCEL prende 481, lo squillo prosegue
    e la risposta funziona. Gemello di test_squillo_biforcato_dal_relay_e_cancel_del_ramo_doppio, dove il CANCEL arriva dopo."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            real = rig.peer.fork_ring("ring-fk", "pnl", rig.peer.sdp())
            await rig.peer.wait_for(is_(code=482))
            await wait_until(lambda: rig.rings == 1)
            n = len(rig.peer.log)
            dup = next(m.branch for m in rig.peer.got(is_(code=482)))
            rig.peer.request("CANCEL", "ring-fk", 1, "pnl", branch=dup)
            r = await rig.peer.wait_for(is_(cseq="CANCEL"), start=n)
            assert r.code == 481 and rig.hub.status == "ringing" and not rig.peer.got(is_(code=487))
            assert (await rig.hub.async_answer())[0]
            ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-fk"), start=n)
            assert ok200.branch == real
            await asyncio.sleep(0.3)
            assert rig.hub.status == "in_call" and rig.hub.stats["missed_count"] == 0
            rig.bye(ok200)
            await wait_until(lambda: rig.hub.status == "idle")
    run(s())


def test_cancel_incrociato_col_nostro_200_ok_poi_bye_del_relay(monkeypatch):
    """Gara: il Tab risponde nello stesso istante. Il nostro 200 OK e il CANCEL del ramo vero
    si incrociano (RFC 3261 9.2): 200 al CANCEL e nessun 487, la chiamata resta su finché il
    relay non manda il BYE. Né ring_ended né chiamate perse, un solo call_ended."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.ring("ring-glare")
            await rig.peer.wait_for(is_(code=183))
            await wait_until(lambda: rig.rings == 1)
            assert (await rig.hub.async_answer())[0]
            ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-glare"))
            n = len(rig.peer.log)
            rig.peer.request("CANCEL", "ring-glare", 1, "pnl")
            assert (await rig.peer.wait_for(is_(cseq="CANCEL"), start=n)).code == 200
            await asyncio.sleep(0.3)
            assert not rig.peer.got(is_(code=487)) and rig.hub.status == "in_call"
            assert not rig.events("ring_ended")
            # CSeq 2: il CANCEL aveva l'1
            rig.peer.request("BYE", "ring-glare", 2, "pnl", to_tag=ok200.h("to").split("tag=")[1])
            await wait_until(lambda: rig.hub.status == "idle", 3)
            assert len(rig.events("call_ended")) == 1 and rig.hub.stats["missed_count"] == 0
            assert media.audio_proto.remote_addr is None
    run(s())


def test_invite_ritrasmesso_durante_anteprima_lenta_riceve_183(monkeypatch):
    """UDP locale: la targa ritrasmette l'INVITE mentre l'anteprima si sta ancora aprendo (la
    ritrasmissione è mandata a mano). Un solo evento di squillo, una sola anteprima, e lo
    stesso 183 un'altra volta."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            setup = media.setup_media

            async def slow(*a, **k):
                await asyncio.sleep(0.6)
                await setup(*a, **k)
            monkeypatch.setattr(media, "setup_media", slow)
            raw = rig.ring("ring-rtx")
            await rig.peer.wait_for(is_(code=183))
            rig.peer.send(raw)
            await wait_until(lambda: len(rig.peer.got(is_(code=183))) == 2, 2)
            await wait_until(lambda: rig.rings == 1, 2)
            await asyncio.sleep(0.5)
            assert rig.rings == 1 and rig.hub.stats["ring_count"] == 1
            assert rig.hub.status == "ringing" and media.video_proto.remote_addr
            rig.peer.request("CANCEL", "ring-rtx", 1, "pnl")
            await wait_until(lambda: rig.hub.status == "idle")
    run(s())


# ─── connessione cloud (TLS) che cade, input strani ─────────────────────────

def test_tls_cade_durante_la_chiamata(monkeypatch):
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]
            n = len(rig.peer.log)
            rig.peer.drop()
            await rig.peer.wait_for(is_("REGISTER"), timeout=8, start=n)
            await wait_until(lambda: sip.registered, 5)
            rig.bye(rig.peer.got(is_("INVITE"))[0])
            await wait_until(lambda: rig.hub.status == "idle", 3)
    run(s())


def test_riaggancio_con_tls_caduto_chiude_la_chiamata(monkeypatch):
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]
            rig.peer.drop()
            await asyncio.sleep(0.2)
            await rig.hub.async_hangup()            # prima: ConnectionResetError e in_call per sempre
            assert not sip.in_call and media.audio_proto.remote_addr is None
            assert rig.events("call_ended")
            await wait_until(lambda: sip.registered, 10)
            ok, msg = await rig.hub.async_call()
            assert ok, f"dopo la riconnessione non si chiama più: {msg}"
            await rig.hub.async_hangup()
    run(s())


def test_rifiuto_con_tls_caduto_chiude_lo_squillo(monkeypatch):
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.ring()
            await rig.peer.wait_for(is_(code=183))
            rig.peer.drop()
            await asyncio.sleep(0.2)
            await rig.hub.async_decline()           # prima: eccezione e squillo appeso
            assert not sip.ringing() and not rig.hub.video_active
            await wait_until(lambda: sip.registered, 10)
            n = len(rig.peer.log)
            rig.ring("ring-2", "q")
            r = await rig.peer.wait_for(lambda m: m.code and m.cid == "ring-2" and m.code >= 180, start=n)
            assert r.code == 183, f"nuovo squillo rifiutato: {r.code}"
    run(s())


def test_tls_cade_durante_lo_squillo_e_si_risponde(monkeypatch):
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.ring("ring-tls")
            await rig.peer.wait_for(is_(code=183))
            await wait_until(lambda: rig.rings == 1)
            n = len(rig.peer.log)
            rig.peer.drop()
            await rig.peer.wait_for(is_("REGISTER"), timeout=8, start=n)
            await wait_until(lambda: sip.registered, 5)
            assert rig.hub.status == "ringing"
            ok, msg = await rig.hub.async_answer()
            assert ok, msg
            await rig.peer.wait_for(is_(code=200, cid="ring-tls"), start=n)
            await rig.hub.async_hangup()
            await rig.peer.wait_for(is_("BYE", cid="ring-tls"), start=n)
    run(s())


def test_input_malformati_non_fermano_il_reader(monkeypatch):
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            for junk in ("\x00\xff garbage", "SIP/2.0 abc\r\n\r\n", "INVITE\r\n\r\n",
                         "SIP/2.0 200 OK\r\nCall-ID: x\r\nCSeq: INVITE\r\n\r\n",
                         "SIP/2.0 486 Busy\r\nCall-ID: y\r\nCSeq: 9 INVITE\r\n\r\n"):
                rig.peer.send(junk)
            await asyncio.sleep(0.3)
            assert await sip.do_register()
    run(s())


def test_content_length_negativo_su_tls(monkeypatch):
    """Il ciclo di framing non deve girare all'infinito (event loop bloccato)."""
    esito = {}

    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.peer.send("OPTIONS sip:x SIP/2.0\r\nCall-ID: neg\r\nContent-Length: -1000\r\n\r\n")
            await asyncio.sleep(0.3)
            esito["ok"] = await sip.do_register()

    t = threading.Thread(target=lambda: run(s()), daemon=True)
    t.start()
    t.join(20)
    assert not t.is_alive(), "event loop bloccato dal Content-Length negativo"
    assert esito.get("ok")


# ─── /av (go2rtc, stream worker, iPhone) ────────────────────────────────────

def test_av_a_riposo_fa_l_autocall_e_in_chiamata_no(monkeypatch):
    views = load_views(monkeypatch)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.peer.on_invite = answer_200
            t1 = open_av(views, hass)                 # riposo → auto-call
            await wait_until(lambda: rig.hub.status == "in_call")
            t2 = open_av(views, hass)                 # in chiamata → nessun INVITE
            await asyncio.sleep(0.5)
            assert len(rig.peer.got(is_("INVITE"))) == 1
            assert rig.hub._stream_viewers == 2 and len(av_stream._av_clients) == 2
            await rig.hub.async_hangup()
            for t in (t1, t2):
                await asyncio.wait_for(t, 5)
    run(s())


def test_le_view_seguono_l_entry_attiva(monkeypatch):
    """Integrazione tolta e riaggiunta: le view restano quelle registrate la prima volta,
    ma l'entry è un'altra. Legate al vecchio entry_id rispondevano 503 fino al riavvio."""
    views = load_views(monkeypatch)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.peer.on_invite = answer_200
            view = views.VimarAVStreamView(hass)
            hass.data[C.DOMAIN] = {"entry-nuova": hass.data[C.DOMAIN].pop("e1")}
            t = asyncio.create_task(view.get(Request()))
            await wait_until(lambda: rig.hub.status == "in_call", 5, "auto-call dalla view")
            await asyncio.sleep(1.0)
            await rig.hub.async_hangup()
            assert (await asyncio.wait_for(t, 5)).status == 200
    run(s())


def test_miniatura_della_camera_a_riposo_non_chiama(monkeypatch):
    """Requisito: la dashboard a citofono fermo non chiama mai la targa. La miniatura
    (camera_proxy, ogni 10 s) è None e nessun INVITE, per quante volte la si chieda."""
    import importlib
    cam_mod = sys.modules["homeassistant.components.camera"]
    monkeypatch.setattr(cam_mod, "Camera", type("Camera", (), {"__init__": lambda self: None}),
                        raising=False)
    monkeypatch.setattr(cam_mod, "CameraEntityFeature", types.SimpleNamespace(STREAM=2), raising=False)
    monkeypatch.delitem(sys.modules, "custom_components.vimar_intercom.camera", raising=False)
    camera = importlib.import_module("custom_components.vimar_intercom.camera")

    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            cam = camera.VimarIntercomCamera(rig.hub, "e1", types.SimpleNamespace())
            for _ in range(5):
                assert await cam.async_camera_image() is None
            assert not cam.is_streaming
            await asyncio.sleep(0.5)
            assert not rig.peer.got(is_("INVITE")) and rig.hub.status == "idle"
            assert rig.hub._stream_viewers == 0 and av_stream.av_ffmpeg_proc is None
    run(s())


def test_av_durante_squillo_non_chiama(monkeypatch):
    views = load_views(monkeypatch)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.ring()
            await rig.peer.wait_for(is_(code=183))
            t = open_av(views, hass)
            await asyncio.sleep(0.8)
            assert not [m for m in rig.peer.log if m.kind == "INVITE" and m.cid != "ring-1"]
            assert rig.hub._stream_viewers == 1 and av_stream._av_clients
            rig.peer.request("CANCEL", "ring-1", 1, "pnl")
            resp = await asyncio.wait_for(t, 5)
            assert resp.status == 200 and rig.hub._stream_viewers == 0
    run(s())


def test_av_riaperto_dopo_fine_chiamata_non_richiama(monkeypatch):
    """Chiusa la chiamata, go2rtc riapre /av: l'hub richiamava da solo (→ 486 dalla
    targa ancora occupata → un altro auto-call più tardi)."""
    views = load_views(monkeypatch)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]      # "Vedi esterno" dalla card
            t = open_av(views, hass)                    # la card va in live → go2rtc
            await asyncio.sleep(0.5)
            rig.bye(rig.peer.got(is_("INVITE"))[0])
            await asyncio.wait_for(t, 5)                # lo stream finisce...
            rig.peer.on_invite = busy
            r = await asyncio.wait_for(open_av(views, hass), 3)   # ...e go2rtc lo riapre
            assert r.status == 503
            assert len(rig.peer.got(is_("INVITE"))) == 1, "auto-call partito da solo"
    run(s())


def test_autocall_rifiutato_503_subito_e_niente_retry(monkeypatch):
    """La targa è occupata (486): /av non tiene l'iPhone 25 s sulla rotella, e il retry
    di go2rtc non fa un altro auto-call."""
    views = load_views(monkeypatch)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.peer.on_invite = busy
            t0 = time.monotonic()
            r = await asyncio.wait_for(open_av(views, hass), 30)
            assert r.status == 503 and time.monotonic() - t0 < 3, f"503 dopo {time.monotonic() - t0:.1f} s"
            assert rig.hub._stream_viewers == 0
            r = await asyncio.wait_for(open_av(views, hass), 5)   # retry di go2rtc
            assert r.status == 503
            assert len(rig.peer.got(is_("INVITE"))) == 1, "retry dopo 486: altro auto-call"
    run(s())


def test_av_non_aspetta_una_chiamata_annullata(monkeypatch):
    """"Vedi esterno" e subito "Annulla": /av aperto durante il collegamento non resta
    appeso 25 s contando come spettatore."""
    views = load_views(monkeypatch)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()

            async def squilla(peer, inv):
                peer.reply(inv, 180, "Ringing")
            rig.peer.on_invite = squilla
            call = asyncio.create_task(rig.hub.async_call())
            await wait_until(lambda: sip.calling)
            av = open_av(views, hass)
            await asyncio.sleep(0.2)
            await rig.hub.async_hangup()                        # "Annulla"
            assert not (await call)[0]
            resp = await asyncio.wait_for(av, 2)
            assert resp.status == 503 and rig.hub._stream_viewers == 0
    run(s())


def test_av_chiuso_mentre_collega_non_lascia_la_chiamata_aperta(monkeypatch):
    """HA annulla l'handler di /av quando il client se ne va (handler_cancellation):
    l'iPhone chiude la camera mentre il cloud collega. Nessuno guarda: si chiude."""
    views = load_views(monkeypatch)
    monkeypatch.setattr(hub_mod, "STREAM_HANGUP_DELAY", 0.5)

    async def lenta(peer, inv):                     # il cloud collega dopo 1,5 s
        peer.reply(inv, 180, "Ringing")
        await asyncio.sleep(1.5)
        if peer.pending_invite is inv:
            peer.pending_invite = None
            peer.reply(inv, 200, "OK", body=peer.sdp())

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.peer.on_invite = lenta
            t = open_av(views, hass)
            await wait_until(lambda: sip.calling)
            t.cancel()                               # l'iPhone chiude la camera
            await asyncio.gather(t, return_exceptions=True)
            await asyncio.sleep(2.5)
            assert rig.peer.got(is_("CANCEL")) or rig.peer.got(is_("BYE")), "nessuno chiude"
            assert rig.hub.status == "idle"
    run(s())


def test_spettatore_se_ne_va_la_chiamata_si_chiude(monkeypatch):
    """L'iPhone va in background a metà vista (auto-call dalla camera): dopo la pausa di
    riaggancio la chiamata si chiude da noi, e non si richiama."""
    views = load_views(monkeypatch)
    monkeypatch.setattr(hub_mod, "STREAM_HANGUP_DELAY", 0.5)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.peer.on_invite = answer_200
            t = open_av(views, hass)
            await wait_until(lambda: rig.hub.status == "in_call")
            await asyncio.sleep(0.3)
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
            await rig.peer.wait_for(is_("BYE"), timeout=3)
            await wait_until(lambda: rig.hub.status == "idle", 3)
            await asyncio.sleep(3)
            assert len(rig.peer.got(is_("INVITE"))) == 1, "richiamata senza spettatori"
    run(s())


# ─── /av?autocall=0 (Scrypted, go2rtc, Frigate: docs/EXTERNAL.md) ────────────

def test_av_passivo_a_riposo_503_subito_e_niente_invite(monkeypatch):
    """Un NVR che riapre /av in ciclo non deve mai chiamare la targa: a riposo 503
    subito (niente attesa, niente spettatore, niente ffmpeg), per quante volte riprovi."""
    views = load_views(monkeypatch)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.peer.on_invite = answer_200
            for _ in range(3):
                t0 = time.monotonic()
                r = await asyncio.wait_for(open_av(views, hass, passive=True), 2)
                assert r.status == 503 and time.monotonic() - t0 < 0.5
            await asyncio.sleep(0.5)
            assert not rig.peer.got(is_("INVITE")) and rig.hub.status == "idle"
            assert rig.hub._stream_viewers == 0 and av_stream.av_ffmpeg_proc is None
    run(s())


def test_av_passivo_durante_lo_squillo_riceve_l_anteprima_e_chiude_alla_fine(monkeypatch):
    views = load_views(monkeypatch)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.ring()
            await rig.peer.wait_for(is_(code=183))
            t = open_av(views, hass, passive=True)
            await asyncio.sleep(0.8)
            assert not t.done() and len(av_stream._av_clients) == 1
            assert rig.hub._stream_viewers == 0, "il passivo non conta come spettatore"
            assert not [m for m in rig.peer.log if m.kind == "INVITE" and m.cid != "ring-1"]
            rig.peer.request("CANCEL", "ring-1", 1, "pnl")
            resp = await asyncio.wait_for(t, 5)
            assert resp.status == 200 and resp.chunks, "nessun byte dell'anteprima"
            assert not av_stream._av_clients
    run(s())


def test_av_passivo_in_chiamata_si_aggancia_ma_non_tiene_la_linea(monkeypatch):
    """Auto-call dalla camera di HA con Scrypted agganciato in passivo: quando lo
    spettatore vero se ne va la chiamata si chiude lo stesso (Scrypted non è uno
    spettatore) e lo stream passivo finisce con lei."""
    views = load_views(monkeypatch)
    monkeypatch.setattr(hub_mod, "STREAM_HANGUP_DELAY", 0.5)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.peer.on_invite = answer_200
            t = open_av(views, hass)                       # camera di HA → auto-call
            await wait_until(lambda: rig.hub.status == "in_call")
            p = open_av(views, hass, passive=True)         # Scrypted si aggancia
            await asyncio.sleep(0.5)
            assert not p.done() and len(av_stream._av_clients) == 2
            assert rig.hub._stream_viewers == 1 and len(rig.peer.got(is_("INVITE"))) == 1
            t.cancel()                                     # lo spettatore vero se ne va
            await asyncio.gather(t, return_exceptions=True)
            await rig.peer.wait_for(is_("BYE"), timeout=3)
            resp = await asyncio.wait_for(p, 5)
            assert resp.status == 200 and resp.chunks and not av_stream._av_clients
    run(s())


# ─── il campanello suona mentre si guarda la telecamera ──────────────────────

def test_squillo_subito_dopo_il_bye_della_visione(monkeypatch):
    """Il visitatore preme mentre qualcuno guarda: la targa chiude la visione (BYE) e
    subito squilla. È uno squillo vero: evento per le automazioni, e mai un 6xx (il PBX
    lo propagherebbe, annullando lo squillo anche sul Tab)."""
    views = load_views(monkeypatch)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            fast = av_stream._stop_av_ffmpeg_locked

            async def slow():                    # ffmpeg vero: qualche centinaio di ms
                if av_stream.av_ffmpeg_proc:
                    await asyncio.sleep(0.3)
                await fast()
            monkeypatch.setattr(av_stream, "_stop_av_ffmpeg_locked", slow)
            await rig.register()
            rig.peer.on_invite = answer_200
            av = open_av(views, hass)                           # auto-call
            await wait_until(lambda: rig.hub.status == "in_call")
            await asyncio.sleep(0.5)
            rig.bye(rig.peer.got(is_("INVITE"))[0])
            rig.ring("ring-dopo-bye")
            await rig.peer.wait_for(is_(code=183, cid="ring-dopo-bye"))
            await asyncio.sleep(1.0)
            assert not rig.peer.got(lambda m: m.cid == "ring-dopo-bye" and (m.code or 0) >= 300), \
                "squillo rifiutato"
            assert rig.rings == 1 and rig.hub.stats["ring_count"] == 1, "evento campanello perso"
            assert rig.hub.status == "ringing"
            assert rig.hub.video_active and media.video_proto.remote_addr, "anteprima spenta dal BYE"
            rig.peer.request("CANCEL", "ring-dopo-bye", 1, "pnl")
            await asyncio.wait_for(av, 5)
    run(s())


def test_squillo_durante_la_visione_limite_noto(monkeypatch):
    """Un INVITE nuovo con la visione aperta è, per il codice, l'eco della nostra
    chiamata. Comportamento attuale, documentato: 486 (non 6xx, così il Tab continua a
    squillare), la visione resta su, nessun evento doorbell in HA."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]
            rig.ring("ring-in-vista")
            r = await rig.peer.wait_for(lambda m: m.code and m.cid == "ring-in-vista" and m.code >= 180)
            await asyncio.sleep(0.3)
            assert r.code == 486 and rig.hub.status == "in_call" and rig.rings == 0
            await rig.hub.async_hangup()
    run(s())


# ─── card: "Microfono" (/audio_ws) ──────────────────────────────────────────

def test_microfono_durante_chiamata(monkeypatch):
    views = load_views(monkeypatch)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]      # card: "Vedi esterno"
            av = open_av(views, hass)                   # video live
            await asyncio.sleep(0.3)
            view = views.VimarAudioWSView(hass)
            ws = fake_ws(monkeypatch, views)
            wst = asyncio.create_task(view.get(Request(admin=False)))
            await asyncio.sleep(0.1)
            # 2048 campioni @48 kHz → 341 @8 kHz ogni ~43 ms, come la card
            frame = b"\x02" + b"\x10\x00" * 341
            for _ in range(120):                        # ~5 s di voce
                ws.inbox.put_nowait(types.SimpleNamespace(type="binary", data=frame))
                await asyncio.sleep(0.043)
            assert rig.hub.status == "in_call", "la chiamata è caduta col microfono"
            assert not rig.peer.got(is_("BYE")) and not rig.peer.got(is_("CANCEL"))
            assert len(rig.peer.audio_rx) >= 100, "la voce non arriva alla targa"
            sizes = {len(p) - 12 for p in rig.peer.audio_rx}
            assert sizes == {160}, f"pacchetti RTP non da 20 ms (ptime 20): {sizes}"
            ws.inbox.put_nowait(None)
            await wst
            await rig.hub.async_hangup()
            await asyncio.wait_for(av, 5)
    run(s())


# ─── Revisione della PR #22 (28/09): relay cloud che biforca e non manda l'ACK, 407 ────

def test_squillo_biforcato_dal_relay_e_cancel_del_ramo_doppio(monkeypatch):
    """Sul cloud lo squillo arriva come due INVITE dello stesso Call-ID a pochi ms (branch
    diversi: 40515 e 40517), e ~70 ms dopo il nostro 200 OK il relay annulla il ramo doppio
    (m4r1k). Il secondo ramo riceve 482; quel CANCEL non è la fine della chiamata."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            real = rig.peer.fork_ring("ring-f", "pnl", rig.peer.sdp())
            r482 = await rig.peer.wait_for(is_(code=482))
            assert r482.branch != real and "ring-f" == r482.cid
            await wait_until(lambda: rig.rings == 1)
            assert len(rig.peer.got(is_(code=183))) == 1
            ok, msg = await rig.hub.async_answer()
            assert ok, msg
            r481 = await rig.peer.wait_for(is_(code=481, cseq="CANCEL"), timeout=2)  # il CANCEL del ramo doppio
            assert r481.branch != real
            await asyncio.sleep(0.3)
            assert sip.in_call and rig.hub.status == "in_call" and not rig.peer.got(is_(code=487))
            assert rig.hub.stats["missed_count"] == 0
            rig.bye(rig.peer.got(is_(code=200, cseq="INVITE"))[0])
            await wait_until(lambda: rig.hub.status == "idle")
    run(s())


def test_relay_che_non_manda_mai_l_ack_la_chiamata_resta_su(monkeypatch):
    """Sul relay cloud l'ACK del nostro 200 OK non arriva mai, anche a chiamata perfetta
    (m4r1k, 40517). Nessun «senza ACK → BYE» (ogni chiamata cloud cadrebbe dopo ~32 s) e
    nessuna ritrasmissione infinita del 200: la chiamata finisce col BYE della targa."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.ring("ring-noack")
            await rig.peer.wait_for(is_(code=183))
            assert (await rig.hub.async_answer())[0]
            await asyncio.sleep(2.0)
            assert sip.in_call and rig.hub.status == "in_call"
            assert not rig.peer.got(is_("ACK")) and not rig.peer.got(is_("BYE"))
            assert len(rig.peer.got(is_(code=200, cseq="INVITE"))) == 1
            rig.bye(rig.peer.got(is_(code=200, cseq="INVITE"))[0])
            await wait_until(lambda: rig.hub.status == "idle")
    run(s())


def test_407_ripetuto_con_lo_stesso_nonce_non_fa_la_tempesta_di_invite(monkeypatch):
    """Dal campo (2F): ~40 INVITE in 3,5 s, uno per ogni 407 ritrasmesso. Con lo stesso
    nonce e senza stale=true ci si ferma dopo il tentativo autenticato."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()

            async def sfida(peer, inv):
                peer.send(response(inv, 407, "Proxy Authentication Required", to_tag="prx",
                                   extra=f'Proxy-Authenticate: Digest realm="{DOMAIN}", nonce="fisso"\r\n'))
            rig.peer.on_invite = sfida
            ok, msg = await sip.do_call()
            assert not ok and "407" in msg
            await asyncio.sleep(0.5)
            assert len(rig.peer.got(is_("INVITE"))) == 2 and not sip.calling
    run(s())


def test_info_di_keyframe_gia_autenticati_con_la_sfida_del_proxy(monkeypatch):
    """Il proxy sfida ogni INFO: il burst iniziale (8 INFO a 150 ms) faceva 8 volte 407 +
    INFO ripetuto, e la copia autenticata partiva senza aspettarne la risposta. Con la
    sfida dell'INVITE in cache il primo INFO è già autenticato e riceve 200."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.peer.info_auth = True

            async def sfida(peer, inv):
                if "proxy-authorization" not in inv.hdrs:
                    peer.send(response(inv, 407, "Proxy Authentication Required", to_tag="prx",
                                       extra=f'Proxy-Authenticate: Digest realm="{DOMAIN}", nonce="abc"\r\n'))
                    return
                peer.reply(inv, 200, "OK", body=peer.sdp())
            rig.peer.on_invite = sfida
            assert (await sip.do_call())[0]
            await asyncio.sleep(1.5)                       # il burst dei keyframe
            infos = rig.peer.got(is_("INFO"))
            assert infos and all("proxy-authorization" in i.hdrs for i in infos), "INFO senza auth"
            await rig.hub.async_hangup()
    run(s())


def test_info_sfidato_rimanda_una_volta_e_aspetta_la_risposta(monkeypatch):
    """Senza sfida in cache (nessun 407 all'INVITE): il primo INFO prende 407, la copia
    autenticata parte una volta sola e il suo 200 viene atteso; da lì tutti autenticati."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.info_auth = True
            rig.peer.on_invite = answer_200
            assert (await sip.do_call())[0]
            await asyncio.sleep(1.5)
            # A inizio chiamata partono due INFO insieme (do_call e il giro dei keyframe):
            # al più quei due prendono il 407; da lì in poi tutti autenticati, mai un altro senza.
            infos = rig.peer.got(is_("INFO"))
            senza = [i for i, m in enumerate(infos) if "proxy-authorization" not in m.hdrs]
            con = [i for i in range(len(infos)) if i not in senza]
            assert senza and con and len(senza) <= 2 and max(senza) < min(con), [m.h("cseq") for m in infos]
            await rig.hub.async_hangup()
    run(s())


def test_av_aperto_dall_utente_dopo_la_chiamata_richiama(monkeypatch):
    """Rispondi dalla card, riaggancia, poi la camera dalla dashboard entro il minuto: nessuno
    guardava /av alla fine, non è una riconnessione di go2rtc, si chiama. Prima: 503 secco
    per 60 s. Chi invece guardava /av alla fine riprova dopo il minuto (#57)."""
    views = load_views(monkeypatch)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]      # dalla card, senza /av
            await asyncio.sleep(0.5)
            rig.bye(rig.peer.got(is_("INVITE"))[0])
            await wait_until(lambda: not rig.hub._busy_now, 3, "fine chiamata")
            await asyncio.sleep(0.6)                     # l'utente apre la camera dopo
            t = open_av(views, hass)
            await wait_until(lambda: len(rig.peer.got(is_("INVITE"))) == 2, 3, "auto-call dell'utente")
            await asyncio.sleep(0.3)
            rig.bye(rig.peer.got(is_("INVITE"))[1])
            await asyncio.wait_for(t, 5)
    run(s())
