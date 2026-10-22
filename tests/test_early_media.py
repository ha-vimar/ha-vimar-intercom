"""Anteprima allo squillo (early media) e BYE di altri dialoghi."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from custom_components.vimar_intercom import media_handler as mh  # noqa: E402
from custom_components.vimar_intercom import sip_client as sip

INVITE = (
    "INVITE sip:60902@x SIP/2.0\r\n"
    "Via: SIP/2.0/TLS 1.2.3.4;branch=z9hG4bKa\r\n"
    "From: <sip:55001@x>;tag=abc\r\n"
    "To: <sip:60902@x>\r\n"
    "Call-ID: ring-1\r\n"
    "CSeq: 1 INVITE\r\n"
    "Content-Type: application/sdp\r\n"
    "\r\n"
    "v=0\r\nc=IN IP4 5.6.7.8\r\nm=audio 4000 RTP/AVP 0\r\nm=video 4002 RTP/AVP 96\r\n"
)


@pytest.fixture
def rete(monkeypatch):
    inviati, media = [], []

    async def _send(msg):
        inviati.append(msg)

    async def _broadcast(*a):
        pass

    async def _setup(*a, **k):
        media.append("setup")

    async def _stop():
        media.append("stop")

    async def _keyframe():
        pass

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(sip, "broadcast", _broadcast)
    monkeypatch.setattr(sip, "send_keyframe_request", _keyframe)
    monkeypatch.setattr(mh, "setup_media", _setup)
    monkeypatch.setattr(mh, "stop_media", _stop)
    monkeypatch.setattr(sip, "build_sdp", lambda offer=None, reuse_keys=False: "v=0\r\nSDP-NOSTRO\r\n")
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", False)  # cloud: media dai relay pubblici
    monkeypatch.setitem(sip.call_state, "call_id", None)
    monkeypatch.setitem(sip.pending_incoming, "active", False)
    monkeypatch.setitem(sip.pending_incoming, "cid", None)
    return inviati, media


def test_squillo_risponde_183_con_sdp_e_apre_il_video(rete):
    inviati, media = rete
    asyncio.run(sip.handle_incoming_invite(INVITE))
    assert inviati[0].startswith("SIP/2.0 183 Session Progress")
    assert "SDP-NOSTRO" in inviati[0]
    assert media == ["setup"]
    assert sip.pending_incoming["early"]


def test_risposta_riusa_lo_stesso_sdp_senza_riaprire_il_video(rete, monkeypatch):
    inviati, media = rete
    monkeypatch.setattr(mh, "video_proto", SimpleNamespace(remote_addr=("5.6.7.8", 4002)))
    asyncio.run(sip.handle_incoming_invite(INVITE))
    ok, _ = asyncio.run(sip.do_answer_incoming())
    assert ok
    assert inviati[-1].startswith("SIP/2.0 200 OK") and "SDP-NOSTRO" in inviati[-1]
    assert media == ["setup"]  # niente secondo setup_media
    sip._set_in_call(False)


def test_audio_only_early_media_is_not_reopened_on_answer(rete, monkeypatch):
    """An audio-only ring opens only the audio line: answering must not see
    "no video open" and restart the media the preview already runs."""
    inviati, media = rete
    tx = []
    monkeypatch.setattr(mh, "audio_proto", SimpleNamespace(remote_addr=("5.6.7.8", 4000)))
    monkeypatch.setattr(mh, "video_proto", SimpleNamespace(remote_addr=None))
    monkeypatch.setattr(mh, "enable_tx", lambda: tx.append(True))
    asyncio.run(sip.handle_incoming_invite(INVITE.split("m=video")[0]))
    ok, _ = asyncio.run(sip.do_answer_incoming())
    assert ok
    assert media == ["setup"] and tx == [True]
    sip._set_in_call(False)


def test_answer_does_not_wait_for_the_keyframe_info(rete, monkeypatch):
    """The keyframe INFO is a round trip through the relay: answering returns
    at once, and the request still goes out."""
    inviati, media = rete
    monkeypatch.setattr(mh, "video_proto", SimpleNamespace(remote_addr=("5.6.7.8", 4002)))
    asked = []

    async def _slow_keyframe():
        asked.append("sent")
        await asyncio.sleep(5)
        asked.append("answered")
    monkeypatch.setattr(sip, "send_keyframe_request", _slow_keyframe)

    async def go():
        await sip.handle_incoming_invite(INVITE)
        res = await asyncio.wait_for(sip.do_answer_incoming(), 1)
        await asyncio.sleep(0)
        return res
    ok, _ = asyncio.run(go())
    assert ok and asked == ["sent"]
    sip._set_in_call(False)


def test_a_stale_response_is_not_a_warning(caplog):
    """A late answer to a request nobody waits for any more is logged at DEBUG."""
    raw = ("SIP/2.0 200 OK\r\nCall-ID: gone-1\r\nCSeq: 5 INFO\r\n"
           "Content-Length: 0\r\n\r\n")
    with caplog.at_level(logging.DEBUG, logger=sip._LOGGER.name):
        asyncio.run(sip._dispatch_message(raw))
    stale = [r for r in caplog.records if "Stale response" in r.getMessage()]
    assert stale and all(r.levelno == logging.DEBUG for r in stale)


def test_durante_una_nostra_chiamata_niente_early_media(rete, monkeypatch):
    inviati, media = rete
    monkeypatch.setattr(sip, "calling", True)  # l'eco della nostra chiamata
    asyncio.run(sip.handle_incoming_invite(INVITE))
    # occupato: 486 (un 603 farebbe annullare al PBX anche gli altri rami)
    assert inviati[0].startswith("SIP/2.0 486 Busy Here")
    assert media == [] and not sip.pending_incoming["active"]


def test_rifiuto_chiude_il_video_dello_squillo(rete):
    inviati, media = rete
    asyncio.run(sip.handle_incoming_invite(INVITE))
    asyncio.run(sip.do_decline_incoming())
    assert media == ["setup", "stop"]
    assert not sip.pending_incoming["active"]


def test_bye_di_un_altro_dialogo_non_chiude_la_chiamata(rete, monkeypatch):
    inviati, media = rete
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setitem(sip.call_state, "call_id", "nostra")
    bye = ("BYE sip:60902@x SIP/2.0\r\nVia: SIP/2.0/TLS 1.2.3.4;branch=z9hG4bKb\r\n"
           "From: <sip:55001@x>;tag=abc\r\nTo: <sip:60902@x>;tag=def\r\n"
           "Call-ID: altra\r\nCSeq: 2 BYE\r\n\r\n")
    asyncio.run(sip.handle_incoming_bye(bye))
    assert inviati[0].startswith("SIP/2.0 481")
    assert sip.in_call and media == []


def test_invite_ritrasmesso_non_e_un_nuovo_squillo(rete):
    inviati, media = rete
    asyncio.run(sip.handle_incoming_invite(INVITE))
    asyncio.run(sip.handle_incoming_invite(INVITE))  # stesso Call-ID (UDP)
    assert inviati[0] == inviati[1]  # stessa risposta, stesse chiavi
    assert media == ["setup"]


def test_niente_chiamate_durante_uno_squillo(rete, monkeypatch):
    monkeypatch.setattr(sip, "registered", True)
    monkeypatch.setitem(sip.pending_incoming, "active", True)
    ok, msg = asyncio.run(sip.do_call())
    assert not ok and "Squillo" in msg


def test_ritrasmissione_tardiva_di_uno_squillo_finito_riceve_il_rifiuto(rete):
    """Non è un nuovo squillo: riceve di nuovo la risposta finale (il 603 si era perso)."""
    inviati, media = rete
    asyncio.run(sip.handle_incoming_invite(INVITE))
    asyncio.run(sip.do_decline_incoming())
    n = len(inviati)
    asyncio.run(sip.handle_incoming_invite(INVITE))  # arriva dopo la fine
    assert len(inviati) == n + 1 and inviati[-1] == inviati[-2] and inviati[-1].startswith("SIP/2.0 603")
    assert not sip.pending_incoming["active"] and media == ["setup", "stop"]


def test_riaggancio_durante_lo_squillo_non_spegne_l_anteprima(rete, monkeypatch):
    inviati, media = rete
    asyncio.run(sip.handle_incoming_invite(INVITE))
    asyncio.run(sip.do_hangup())
    assert "stop" not in media and sip.pending_incoming["early"]


def test_sdp_anomalo_non_silenzia_il_campanello(rete, monkeypatch):
    inviati, media = rete
    squilli = []

    async def _rotto(*a):
        raise ValueError("chiave SRTP corta")

    async def _broadcast(tipo, *a):
        squilli.append(tipo)

    monkeypatch.setattr(mh, "setup_media", _rotto)
    monkeypatch.setattr(sip, "broadcast", _broadcast)
    asyncio.run(sip.handle_incoming_invite(INVITE))
    assert "ring" in squilli and not sip.pending_incoming["early"]


def test_udp_locale_accetta_solo_il_citofono(monkeypatch):
    """In UDP locale un INVITE da un altro host della LAN non deve far squillare. Lo si
    dice una volta (WARNING: un IP sbagliato nelle opzioni si deve leggere), poi DEBUG."""
    ricevuti = []
    pacchetti = [(b"da-intruso", ("192.168.0.66", 5060)), (b"da-intruso", ("192.168.0.66", 5060)),
                 (b"dal-citofono", ("192.168.0.129", 5060))]

    async def _recv(_sock, _n):
        if not pacchetti:
            raise asyncio.CancelledError
        return pacchetti.pop(0)

    async def _dispatch(raw):
        ricevuti.append(raw)

    monkeypatch.setattr(sip.R, "LOCAL_PROXY", "192.168.0.129")
    monkeypatch.setattr(sip, "_dispatch_message", _dispatch)

    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), "sock_recvfrom", _recv)
        with pytest.raises(asyncio.CancelledError):
            await sip._udp_reader_task()

    # Handler sul logger del modulo, non caplog: dopo i test e2e il logger del pacchetto
    # (log_buffer.install) non propaga e rimanda copie alla radice.
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    sip._LOGGER.addHandler(handler)
    level = sip._LOGGER.level
    sip._LOGGER.setLevel(logging.DEBUG)
    try:
        asyncio.run(run())
    finally:
        sip._LOGGER.removeHandler(handler)
        sip._LOGGER.setLevel(level)
    assert ricevuti == ["dal-citofono"]
    ignorati = [r for r in records if "192.168.0.66" in r.getMessage()]
    assert [r.levelno for r in ignorati] == [logging.WARNING, logging.DEBUG]
    assert "192.168.0.129" in ignorati[0].getMessage()  # dice chi ci si aspettava


def test_udp_locale_risposta_con_sdp_fuori_lan_da_488(rete, monkeypatch):
    """Lo squillo suona (senza anteprima), ma rispondere aprirebbe il media verso l'indirizzo
    pubblico dell'SDP: 488 e niente chiamata. Prima partiva il 200 OK e la chiamata restava
    in piedi senza media."""
    inviati, media = rete
    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", True)
    asyncio.run(sip.handle_incoming_invite(INVITE))       # c=IN IP4 5.6.7.8
    ok, _ = asyncio.run(sip.do_answer_incoming())
    assert not ok and inviati[-1].startswith("SIP/2.0 488 ") and inviati[-1].endswith("Content-Length: 0\r\n\r\n")
    assert not any(m.startswith("SIP/2.0 200") for m in inviati)
    assert not sip.in_call and not sip.ringing() and "setup" not in media


def test_reinvite_con_sdp_fuori_lan_riceve_488(rete, monkeypatch):
    inviati, media = rete
    _in_dialogo(monkeypatch)
    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", True)
    reinvite = INVITE.replace("ring-1", "nostra").replace("4002", "4010")
    reinvite = reinvite.replace("To: <sip:60902@x>", "To: <sip:60902@x>;tag=t")
    asyncio.run(sip.handle_incoming_invite(reinvite))
    assert len(inviati) == 1 and inviati[0].startswith("SIP/2.0 488 ") and "CSeq: 1 INVITE" in inviati[0]
    assert media == [] and sip.in_call  # la chiamata resta com'è


def test_udp_locale_niente_media_verso_indirizzi_fuori_lan(rete, monkeypatch):
    """In UDP locale un INVITE si falsifica: il media (anche il microfono) non deve
    andare verso l'indirizzo pubblico scritto nell'SDP. Il campanello suona comunque."""
    inviati, media = rete
    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", True)
    asyncio.run(sip.handle_incoming_invite(INVITE))  # c=IN IP4 5.6.7.8
    assert "setup" not in media
    assert sip.ringing()


def _in_dialogo(monkeypatch, cid="nostra"):
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setitem(sip.call_state, "call_id", cid)
    monkeypatch.setitem(sip.call_state, "local_sdp",
                        "v=0\r\nSDP-DELLA-CHIAMATA\r\nm=audio 9100 RTP/AVP 0\r\nm=video 9200 RTP/AVP 96\r\n")
    monkeypatch.setitem(sip.call_state, "remote_sdp", sip.parse_sdp(INVITE.split("\r\n\r\n", 1)[1]))


def test_reinvite_nella_nostra_chiamata_non_e_uno_squillo(rete, monkeypatch):
    """Prima: 180 Ringing + 603 Decline (via "Suppressing ring") al re-INVITE."""
    inviati, media = rete
    _in_dialogo(monkeypatch)
    squilli = []
    monkeypatch.setattr(sip, "broadcast", lambda *a: squilli.append(a) or asyncio.sleep(0))
    reinvite = INVITE.replace("ring-1", "nostra").replace("To: <sip:60902@x>", "To: <sip:60902@x>;tag=t")
    reinvite = reinvite.replace("CSeq: 1 INVITE", "CSeq: 7 INVITE")
    asyncio.run(sip.handle_incoming_invite(reinvite))
    assert len(inviati) == 1 and inviati[0].startswith("SIP/2.0 200 OK")
    assert "SDP-DELLA-CHIAMATA" in inviati[0] and "CSeq: 7 INVITE" in inviati[0]
    assert squilli == [] and not sip.pending_incoming["active"]
    assert media == []  # stesso SDP: il media resta com'è


def test_reinvite_con_media_nuovo_riapre_il_media(rete, monkeypatch):
    inviati, media = rete
    _in_dialogo(monkeypatch)
    reinvite = INVITE.replace("ring-1", "nostra").replace("4002", "4010")
    reinvite = reinvite.replace("To: <sip:60902@x>", "To: <sip:60902@x>;tag=t")
    asyncio.run(sip.handle_incoming_invite(reinvite))
    assert media == ["setup"]


def test_200_ok_ritrasmesso_riceve_di_nuovo_l_ack(rete, monkeypatch):
    inviati, _ = rete
    _in_dialogo(monkeypatch)
    # la coda del dialogo aperta dagli INFO di keyframe: prima ingoiava il 200
    monkeypatch.setitem(sip.pending_responses, "nostra", asyncio.Queue())
    ok200 = ("SIP/2.0 200 OK\r\nVia: SIP/2.0/TLS 1.2.3.4;branch=z9hG4bKc\r\n"
             "From: <sip:60902@x>;tag=f\r\nTo: <sip:55001@x>;tag=t\r\n"
             "Call-ID: nostra\r\nCSeq: 2 INVITE\r\n\r\n")
    asyncio.run(sip._dispatch_message(ok200))
    assert len(inviati) == 1 and inviati[0].startswith("ACK ") and "CSeq: 2 ACK" in inviati[0]


def test_parse_sdp_direzione_e_formati():
    r = sip.parse_sdp("v=0\r\nc=IN IP4 1.2.3.4\r\na=recvonly\r\nm=audio 4000 RTP/SAVP 8 101\r\n"
                      "a=sendonly\r\nm=video 4002 RTP/SAVP 99\r\n")
    assert r["audio"]["fmts"] == ["8", "101"] and r["audio"]["dir"] == "sendonly"
    assert r["video"]["fmts"] == ["99"] and r["video"]["dir"] == "recvonly"  # di sessione


# ─── Revisione della PR #22: filtro UDP, squillo biforcato, CANCEL per branch ─────

@pytest.mark.parametrize("testo, host", [
    ("sip:55001@192.168.0.5:5060;transport=udp", "192.168.0.5"),
    ("sip:55001@192.168.0.5", "192.168.0.5"),
    ("SIP/2.0/UDP 192.168.0.7:5060;branch=z9hG4bKx;rport", "192.168.0.7"),
])
def test_host_of(testo, host):
    assert sip._host_of(testo) == host


def test_udp_locale_accetta_gli_host_del_dialogo(monkeypatch):
    """Impianto senza Record-Route: BYE, re-INVITE e INFO arrivano dalla targa (Contact/Via
    dell'INVITE), non dal proxy. Il filtro li scartava: HA restava «in chiamata» e la targa
    ritrasmetteva il BYE. Gli host del dialogo passano; un altro host della LAN no."""
    ricevuti = []
    pacchetti = [(b"bye-dalla-targa", ("192.168.0.77", 5060)), (b"intruso", ("192.168.0.66", 5060)),
                 (b"da-proxy", ("192.168.0.129", 5060))]

    async def _recv(_sock, _n):
        if not pacchetti:
            raise asyncio.CancelledError
        return pacchetti.pop(0)

    async def _dispatch(raw):
        ricevuti.append(raw)

    monkeypatch.setattr(sip.R, "LOCAL_PROXY", "192.168.0.129")
    monkeypatch.setattr(sip, "_dispatch_message", _dispatch)
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setitem(sip.pending_incoming, "active", False)
    monkeypatch.setitem(sip.pending_incoming, "via_block", "Via: SIP/2.0/UDP 192.168.0.129:5060;branch=z9hG4bKp\r\n")
    monkeypatch.setitem(sip.pending_incoming, "contact", "sip:55001@192.168.0.77:5060")
    monkeypatch.setitem(sip.call_state, "remote_contact", "sip:55001@192.168.0.77:5060")
    assert sip._dialog_hosts() == {"192.168.0.129", "192.168.0.77"}

    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), "sock_recvfrom", _recv)
        with pytest.raises(asyncio.CancelledError):
            await sip._udp_reader_task()

    asyncio.run(run())
    assert ricevuti == ["bye-dalla-targa", "da-proxy"]
    monkeypatch.setattr(sip, "in_call", False)  # a riposo il dialogo non c'è più
    monkeypatch.setattr(sip, "calling", False)
    assert sip._dialog_hosts() == set()


CANCEL = ("CANCEL sip:60902@x SIP/2.0\r\nVia: SIP/2.0/TLS 1.2.3.4;branch={b}\r\n"
          "From: <sip:55001@x>;tag=abc\r\nTo: <sip:60902@x>\r\nCall-ID: ring-1\r\nCSeq: 1 CANCEL\r\n\r\n")


def test_squillo_biforcato_secondo_ramo_482_e_cancel_per_branch(rete):
    """Il relay cloud manda due INVITE dello stesso Call-ID con branch diversi a pochi ms
    (40515 e 40517), e ~70 ms dopo il nostro 200 OK annulla il ramo doppio (m4r1k). Preso
    per una ritrasmissione, il secondo ramo restava senza risposta finale; il CANCEL
    abbinato solo al Call-ID chiudeva la chiamata appena risposta."""
    inviati, _ = rete

    async def run():
        await sip.handle_incoming_invite(INVITE)
        await sip.handle_incoming_invite(INVITE.replace("branch=z9hG4bKa", "branch=z9hG4bKb"))
        assert sip.ringing("ring-1") and sip.pending_incoming["branch"] == "z9hG4bKa"
        assert inviati[-1].startswith("SIP/2.0 482 ") and "branch=z9hG4bKb" in inviati[-1]
        assert len([m for m in inviati if m.startswith("SIP/2.0 183")]) == 1
        # CANCEL del ramo doppio: non è lo squillo che finisce
        await sip.handle_incoming_cancel(CANCEL.format(b="z9hG4bKb"))
        assert inviati[-1].startswith("SIP/2.0 481 ") and sip.ringing("ring-1")
        # CANCEL del ramo vero: 200 al CANCEL e 487 all'INVITE
        await sip.handle_incoming_cancel(CANCEL.format(b="z9hG4bKa"))
        assert [m.split("\r\n")[0] for m in inviati[-2:]] == ["SIP/2.0 200 OK", "SIP/2.0 487 Request Terminated"]
        assert not sip.ringing()

    asyncio.run(run())


@pytest.mark.parametrize("reason, msg", [
    ('Reason: SIP;cause=200;text="Call completed elsewhere"\r\n', "Risposto altrove"),
    ('Reason: SIP ;cause = 200\r\n', "Risposto altrove"),
    ('Reason: SIP;cause=487;text="Request terminated"\r\n', "Chiamata cancellata"),
    ('Reason: Q.850;cause=200\r\n', "Chiamata cancellata"),
    ("", "Chiamata cancellata"),
    # RFC 3326: one Reason header can list several reason-values, and there can be several headers
    ('Reason: SIP;cause=487, Q.850;cause=200\r\n', "Chiamata cancellata"),
    ('Reason: Q.850;cause=16, SIP;cause=200\r\n', "Risposto altrove"),
    ('Reason: SIP;cause=200\r\nReason: Q.850;cause=16\r\n', "Risposto altrove"),
    ('Reason: SIP;cause=487;text="no, SIP;cause=200"\r\n', "Chiamata cancellata"),
    ('Reason: SIP;cause=487;text="a \\", SIP;cause=200;b="\r\n', "Chiamata cancellata"),  # escaped quote
])
def test_cancel_with_cause_200_says_someone_else_answered(rete, monkeypatch, reason, msg):
    """#164 (40517, local UDP): a ring answered from the indoor monitor or the Vimar
    app ends with a CANCEL carrying `Reason: SIP;cause=200` (RFC 3326, "Call completed
    elsewhere"). It was read as an unanswered ring."""
    eventi = []

    async def _bc(t, m):
        eventi.append((t, m))

    monkeypatch.setattr(sip, "broadcast", _bc)

    async def run():
        await sip.handle_incoming_invite(INVITE)
        cancel = CANCEL.format(b="z9hG4bKa").replace("CSeq: 1 CANCEL\r\n", "CSeq: 1 CANCEL\r\n" + reason)
        await sip.handle_incoming_cancel(cancel)

    asyncio.run(run())
    assert eventi[-1] == ("ring_ended", msg)


@pytest.mark.parametrize("sfide, attesi", [
    (['nonce="a"', 'nonce="a"'], [True, False]),                         # stesso nonce ripetuto: basta
    (['nonce="a"', 'nonce="b"'], [True, True]),                          # nonce nuovo: si riprova
    (['nonce="a"', 'nonce="a", stale=true'], [True, True]),              # stale: si riprova
    (['nonce="a"', 'nonce="b"', 'nonce="c"'], [True, True, False]),      # mai oltre AUTH_RETRIES
    ([""], [False]),
])
def test_tetto_ai_tentativi_di_autenticazione(sfide, attesi):
    """Dal campo (2F): ~40 INVITE in 3,5 s, uno per ogni 407 ritrasmesso dal proxy."""
    esiti, last = [], None
    for n, ch in enumerate(sfide):
        ch = f'Digest realm="x", {ch}' if ch else ""
        esiti.append(sip._retry_auth(ch, n, last))
        last = ch
    assert esiti == attesi


def test_rifiuto_diffonde_ring_ended_cosi_il_prossimo_squillo_non_resta_muto(rete, monkeypatch):
    eventi = []

    async def _bc(t, m):
        eventi.append(t)

    monkeypatch.setattr(sip, "broadcast", _bc)
    asyncio.run(sip.handle_incoming_invite(INVITE))
    asyncio.run(sip.do_decline_incoming())
    assert eventi[-1:] == ["ring_ended"] and "ring_ended" not in eventi[:-1]


def test_200_non_inviato_diffonde_ring_ended(rete, monkeypatch):
    eventi = []

    async def _bc(t, m):
        eventi.append(t)

    async def _boom(msg):
        raise OSError("caduto")

    monkeypatch.setattr(sip, "broadcast", _bc)
    asyncio.run(sip.handle_incoming_invite(INVITE))
    monkeypatch.setattr(sip, "send", _boom)
    ok, _ = asyncio.run(sip.do_answer_incoming())
    assert not ok and eventi[-1:] == ["ring_ended"]
