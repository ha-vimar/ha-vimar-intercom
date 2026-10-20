"""Richieste nel dialogo attraverso il proxy cloud (RFC 3261 §12.2.1.1).

Campo, 2026-09-27: INVITE → 200 OK con Contact sip:60002@127.0.0.1:6095 (l'interno del
proxy). I nostri INFO e BYE andavano a quel Contact con il Route fisso del proxy invece
del suo Record-Route: Flexisip li scartava senza rispondere. Keyframe mai richiesti,
«Riaggancia» che restava in chiamata finché non chiudeva la targa (10 s).

Il peer finto in TLS fa il proxy severo: mette il Record-Route e butta via (senza
risposta, come Flexisip) le richieste nel dialogo che non lo riportano come Route.
"""
from __future__ import annotations

import asyncio
import logging
import time

from harness.peer import answer_200, is_
from harness.rig import Rig, run, wait_until

from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import runtime as R
from custom_components.vimar_intercom import sip_client as sip

RR1, RR2 = "<sip:p1.test;transport=tls;lr>", "<sip:p2.test;lr>"


# ─── unità ──────────────────────────────────────────────────────────────────

def test_route_set_dai_record_route(monkeypatch):
    """Più header e più URI nello stesso header; per il chiamante al contrario."""
    raw = (f"SIP/2.0 200 OK\r\nRecord-Route: {RR1}\r\nRecord-Route: {RR2}, <sip:p3;lr>\r\n"
           "Contact: <sip:60002@127.0.0.1:6095>\r\nCall-ID: c\r\nCSeq: 2 INVITE\r\n\r\n")
    _, hdrs, *_ = sip._parse(raw)
    assert sip._route_set(hdrs) == [RR1, RR2, "<sip:p3;lr>"]
    assert sip._route_set(hdrs, reverse=True) == ["<sip:p3;lr>", RR2, RR1]
    assert sip._contact_uri(hdrs) == "sip:60002@127.0.0.1:6095"
    assert sip._route_set({}) == []

    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(R, "SIP_PROXY", "proxy.test")
    # con il route set: Request-URI = Contact, Route = i Record-Route
    assert sip._dialog_target("sip:60002@127.0.0.1:6095", "sip:55001@d", [RR2, RR1]) == (
        "sip:60002@127.0.0.1:6095", f"Route: {RR2}, {RR1}\r\n")
    # senza, sul cloud: per AOR dal proxy fisso (un 127.0.0.1 non porta da nessuna parte)
    assert sip._dialog_target("sip:60002@127.0.0.1:6095", "sip:55001@d", []) == (
        "sip:55001@d", "Route: <sip:proxy.test;transport=tls;lr>\r\n")
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    assert sip._dialog_target("sip:55001@192.168.1.5", "sip:55001@d", None) == ("sip:55001@192.168.1.5", "")


def test_dump_di_debug_oscura_le_credenziali(caplog):
    msg = ("INFO sip:60002@127.0.0.1:6095 SIP/2.0\r\nTo: <sip:55001@d>;tag=t\r\n"
           'Proxy-Authorization: Digest username="60902", response="abcdef0123456789"\r\n'
           "Content-Length: 300\r\n\r\n" + "x" * 300)
    out = sip._dump(msg)
    assert "abcdef0123456789" not in out and "Proxy-Authorization: ***" in out
    assert out.endswith("x" * 200 + "…")
    with caplog.at_level(logging.DEBUG, logger=sip.__name__):
        sip._log_full(">>>", msg)
        sip._log_full(">>>", "REGISTER sip:d SIP/2.0\r\nTo: <sip:60902@d>\r\n\r\n")   # fuori dialogo
        sip._log_full("<<<", "SIP/2.0 200 OK\r\nCSeq: 3 BYE\r\n\r\n")
    dumps = [r for r in caplog.records if r.getMessage().startswith("[SIP ")]
    assert len(dumps) == 2 and "abcdef" not in caplog.text


def test_info_ignora_la_risposta_di_un_info_precedente(monkeypatch):
    """Nella coda del dialogo c'è ancora il 200 dell'INFO prima (es. quello rispedito
    con l'auth dopo un 407): non è la risposta a questo INFO, va saltato."""
    cid = "dlg-2"
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setattr(sip, "pending_responses", {})
    for k, v in dict(call_id=cid, from_tag="lt", to_tag="rt", remote_contact="sip:55001@192.0.2.1",
                     original_target="sip:55001@x", route_set=None).items():
        monkeypatch.setitem(sip.call_state, k, v)

    async def _send(msg):
        seq = sip._parse(msg)[1]["cseq"].split()[0]
        await sip.pending_responses[cid].put(f"SIP/2.0 200 OK\r\nCall-ID: {cid}\r\nCSeq: {seq} INFO\r\n\r\n")
    monkeypatch.setattr(sip, "send", _send)

    async def scenario():
        sip.pending_responses[cid] = asyncio.Queue()
        await sip.pending_responses[cid].put(f"SIP/2.0 200 OK\r\nCall-ID: {cid}\r\nCSeq: 3 INFO\r\n\r\n")
        t0 = time.monotonic()
        await sip.send_keyframe_request()
        return time.monotonic() - t0, sip.pending_responses[cid].qsize()

    elapsed, left = asyncio.run(scenario())
    assert elapsed < 1 and left == 0, "il 200 vecchio è stato preso per il nostro"


# ─── e2e: proxy cloud severo ────────────────────────────────────────────────

def _routed(m, peer):
    return m.h("route") == peer.record_route and m.first.split()[1] == peer.contact_uri


def test_cloud_info_e_bye_passano_dal_record_route(monkeypatch):
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            ok, msg = await rig.hub.async_call()
            assert ok, msg
            ack = await rig.peer.wait_for(is_("ACK"))
            info = await rig.peer.wait_for(is_("INFO"))
            assert _routed(ack, rig.peer) and _routed(info, rig.peer), (ack.hdrs, info.hdrs)
            assert sip.call_state["route_set"] == [rig.peer.record_route]
            t0 = time.monotonic()
            await rig.hub.async_hangup()                   # returns once the BYE has left
            bye = await rig.peer.wait_for(is_("BYE"))
            assert _routed(bye, rig.peer) and f";tag={rig.peer.to_tag}" in bye.h("to")
            await wait_until(lambda: sip.call_state["call_id"] is None, 2, "BYE answered")
            assert time.monotonic() - t0 < 2, "il BYE non ha avuto risposta"
            assert rig.peer.dropped == [], f"scartati dal proxy: {rig.peer.dropped}"
            assert rig.hub.status == "idle" and len(rig.events("call_ended")) == 1
            assert sip.call_state["route_set"] is None
    run(s())


def test_cloud_bye_della_chiamata_ricevuta_segue_il_record_route(monkeypatch):
    """Chiamata in arrivo (UAS): remote target = Contact dell'INVITE, route set nell'ordine."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.ring("ring-rr")
            await rig.peer.wait_for(is_(code=183))
            assert (await rig.hub.async_answer())[0]
            info = await rig.peer.wait_for(is_("INFO"))
            assert _routed(info, rig.peer)
            n = len(rig.peer.log)
            await rig.hub.async_hangup()
            bye = await rig.peer.wait_for(is_("BYE", cid="ring-rr"), start=n)
            assert _routed(bye, rig.peer) and rig.peer.dropped == []
            assert rig.hub.status == "idle"
    run(s())


def test_cloud_200_tardivo_chiuso_dal_record_route(monkeypatch):
    """«Annulla» durante lo squillo, poi il 200 OK arriva lo stesso: l'ACK e il BYE
    che lo chiudono seguono il Record-Route di quel 200."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            go = asyncio.Event()

            async def lenta(peer, inv):
                peer.reply(inv, 100, "Trying")
                peer.reply(inv, 180, "Ringing")
                await go.wait()
                peer.reply(inv, 200, "OK", body=peer.sdp())
            rig.peer.on_invite = lenta
            call = asyncio.create_task(rig.hub.async_call())
            await rig.peer.wait_for(is_("INVITE"))
            await asyncio.sleep(0.2)
            await rig.hub.async_hangup()
            await rig.peer.wait_for(is_("CANCEL"))         # the 200 crosses the CANCEL
            go.set()
            assert not (await call)[0]
            inv = rig.peer.got(is_("INVITE"))[0]
            bye = await rig.peer.wait_for(is_("BYE", cid=inv.cid))
            ack = rig.peer.got(is_("ACK", cid=inv.cid))[-1]
            assert _routed(ack, rig.peer) and _routed(bye, rig.peer) and rig.peer.dropped == []
    run(s())


def test_riaggancia_va_idle_subito_anche_se_il_bye_resta_senza_risposta(monkeypatch):
    """Il campo di oggi: il cloud non risponde al BYE. La chiamata è finita quando il BYE
    parte (RFC 3261 §15.1.1): card idle e media spento subito, non dopo 5 s. Il BYE
    della targa che arriva nel frattempo trova ancora il dialogo (200, non 481)."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]
            inv = rig.peer.got(is_("INVITE"))[0]
            rig.peer.silent = {"BYE"}
            hang = asyncio.create_task(rig.hub.async_hangup())
            await wait_until(lambda: rig.hub.status == "idle", 0.5, "idle subito dopo il BYE")
            # The hang-up returns once the BYE has left, without its answer.
            await asyncio.wait_for(hang, 0.5)
            assert media.audio_proto.remote_addr is None
            assert rig.events("call_ended") and not sip.in_call
            assert sip.call_state["call_id"] == inv.cid, "dialogo dimenticato prima della risposta"
            n = len(rig.peer.log)
            rig.bye(inv)                                   # la targa chiude a modo suo (10 s)
            r = await rig.peer.wait_for(lambda m: m.code and "BYE" in m.h("cseq"), start=n)
            assert r.code == 200
            # la coda tolta sblocca il BYE rimasto in background
            await wait_until(lambda: sip.call_state["call_id"] is None, 2, "dialogo chiuso")
            assert len(rig.events("call_ended")) == 1
            # e non richiama la targa da solo: il prossimo "Vedi esterno" parte pulito
            rig.peer.silent = set()
            assert (await rig.hub.async_call())[0]
            await rig.hub.async_hangup()
    run(s())


# ─── UAS: the answers carry the INVITE's Record-Route (#186) ───────────────

def test_the_record_route_header_keeps_every_uri_in_order():
    """RFC 3261 §12.1.1: the UAS copies the INVITE's Record-Route into its 1xx/2xx, in order."""
    assert sip._record_route_lines([RR1, RR2, "<sip:p3;lr>"]) == (
        f"Record-Route: {RR1}\r\nRecord-Route: {RR2}\r\nRecord-Route: <sip:p3;lr>\r\n")
    assert sip._record_route_lines([]) == "" and sip._record_route_lines(None) == ""


def test_cloud_ring_answers_copy_the_record_route(monkeypatch):
    """The VIEW app copies the six Record-Route of a cloud ring into its 180 (40515, #186);
    without them the proxies cannot route the ACK, INFO and BYE of the dialog back to us."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.ring("ring-rr-copy")
            early = await rig.peer.wait_for(is_(code=183))
            assert early.h("record-route") == rig.peer.record_route, early.raw
            assert (await rig.hub.async_answer())[0]
            ok = await rig.peer.wait_for(is_(code=200, cid="ring-rr-copy"))
            assert ok.h("record-route") == rig.peer.record_route, ok.raw
            await rig.hub.async_hangup()
    run(s())


def test_a_ring_without_sdp_gets_a_180_with_the_record_route(monkeypatch):
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.ring("ring-rr-180", sdp=False)
            ringing = await rig.peer.wait_for(is_(code=180))
            assert ringing.h("record-route") == rig.peer.record_route, ringing.raw
            await rig.hub.async_decline()
    run(s())


def test_several_record_routes_are_copied_one_per_header_in_order(monkeypatch):
    """A comma list in one header becomes one header per URI, in the INVITE's order,
    and our own BYE still follows them (the strict fake proxy drops it otherwise)."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            first = rig.peer.record_route
            rig.peer.record_route = f"{first}, <sip:p2.test;lr>"
            rig.ring("ring-rr-two")
            await rig.peer.wait_for(is_(code=183))
            assert (await rig.hub.async_answer())[0]
            ok = await rig.peer.wait_for(is_(code=200, cid="ring-rr-two"))
            rr = [line for line in ok.raw.split("\r\n") if line.lower().startswith("record-route:")]
            assert rr == [f"Record-Route: {first}", "Record-Route: <sip:p2.test;lr>"], ok.raw
            n = len(rig.peer.log)
            await rig.hub.async_hangup()
            await rig.peer.wait_for(is_("BYE", cid="ring-rr-two"), start=n)
            assert rig.peer.dropped == []
    run(s())


def test_local_udp_ring_with_a_record_route_copies_it_and_hangs_up(monkeypatch):
    """On local UDP the Tab's proxy may record-route too (2F, ADR-19 local case)."""
    async def s():
        async with Rig(monkeypatch, "udp") as rig:
            await rig.register()
            rig.peer.record_route = f"<sip:127.0.0.1:{rig.peer.port};lr>"
            rig.ring("ring-rr-udp")
            early = await rig.peer.wait_for(is_(code=183))
            assert early.h("record-route") == rig.peer.record_route, early.raw
            assert (await rig.hub.async_answer())[0]
            ok = await rig.peer.wait_for(is_(code=200, cid="ring-rr-udp"))
            assert ok.h("record-route") == rig.peer.record_route
            n = len(rig.peer.log)
            await rig.hub.async_hangup()
            bye = await rig.peer.wait_for(is_("BYE", cid="ring-rr-udp"), start=n)
            assert bye.h("route") == rig.peer.record_route
            await wait_until(lambda: rig.hub.status == "idle", 2, "call closed")
    run(s())


def test_a_record_route_with_a_line_break_is_not_echoed():
    """It comes from the network: a bare CR or LF would split our answer into a new header."""
    assert sip._record_route_lines(["<sip:a;lr>", "<sip:b;lr>\nX-Evil: 1", "<sip:c;lr>\r"]) == (
        "Record-Route: <sip:a;lr>\r\n")
