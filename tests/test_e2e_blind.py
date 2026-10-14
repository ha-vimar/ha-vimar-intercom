"""End-to-end «alla cieca»: scenari scelti da un tester indipendente, sul banco di prova
(harness) con hub, sip_client, media_handler e le view vere contro la targa finta.

Il filo conduttore: una famiglia che usa il citofono come un Ring (chi ha suonato, foto,
messaggio di assenza, apri-porta al volo) e un cloud/una targa che fanno le bizze (sfide
407 sul MESSAGE, BYE mai risposti, connessione che cade proprio mentre si apre la porta,
cartella foto non scrivibile, targa che cifra da sola).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
import types

import pytest
from harness import peer as peer_mod
from harness.peer import DOMAIN, Msg, answer_200, check_digest, is_, response
from harness.rig import Rig, run, wait_until
from harness.web import Request, fake_ws, load_views, make_hass, open_av

from custom_components.vimar_intercom import away_tts, frame_grabber, ring_log
from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import runtime as R
from custom_components.vimar_intercom import sip_client as sip

pytestmark = pytest.mark.slow

JPEG = b"\xff\xd8FOTO\xff\xd9"


def snapshots(monkeypatch, tmp_path, delay=0, jpeg=JPEG):
    """Cartella foto nelle opzioni; la foto dell'anteprima arriva da un grabber finto.
    Va chiamata DENTRO il Rig: il suo __aenter__ azzera SNAPSHOT_DIR e AWAY_MESSAGE_*."""
    monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(R, "SNAPSHOT_DELAY", delay)

    async def wait_frame(timeout=6, after=0):
        return jpeg
    monkeypatch.setattr(frame_grabber, "wait_frame", wait_frame)


def away_message(monkeypatch, delay, seconds=1.0):
    """Messaggio di assenza: file «caricato» come PCM 8 kHz di `seconds` s."""
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "/x/messaggio.mp3")
    monkeypatch.setattr(R, "AWAY_MESSAGE_DELAY", delay)

    async def load(path):
        return b"\x10\x00" * int(8000 * seconds)
    monkeypatch.setattr(media, "load_pcm", load)


async def next_second():
    """Le voci del registro hanno la risoluzione del secondo: due squilli distinti."""
    await asyncio.sleep(1 - time.time() % 1 + 0.05)


def rings_log(folder):
    return ring_log.read_ring_log(folder)


def mic_ws(monkeypatch, rig, admin=False):
    """La card apre /audio_ws (WS finto): restituisce (ws, task della view)."""
    views = load_views(monkeypatch)
    hass = make_hass(rig)
    view = views.VimarAudioWSView(hass)
    ws = fake_ws(monkeypatch, views)
    return ws, asyncio.create_task(view.get(Request(admin=admin))), views, hass


MIC_FRAME = b"\x02" + b"\x10\x00" * 341   # come la card: ~43 ms a 8 kHz


# ─── la giornata tipo: chi ha suonato, foto, risposta, porta ─────────────────

def test_giornata_tipo_squillo_perso_poi_risposto_registro_e_stats(monkeypatch, tmp_path):
    """Mattina: suona il corriere, nessuno risponde (CANCEL). Pomeriggio: suona un amico,
    si risponde dalla card, si apre la porta in chiamata, si riaggancia. Il registro
    (quello della card) deve dire chi/quando/esito e avere la foto, le statistiche
    (sensori) devono tornare."""
    async def s():
        async with Rig(monkeypatch) as rig:
            snapshots(monkeypatch, tmp_path)
            await rig.register()
            monkeypatch.setattr(R, "DOOR_ESTERNO", f"sip:55001@{DOMAIN}")
            rig.ring("ring-corriere")
            await rig.peer.wait_for(is_(code=183))
            await wait_until(lambda: rig.rings == 1)
            await asyncio.sleep(0.3)
            rig.peer.request("CANCEL", "ring-corriere", 1, "pnl")
            await wait_until(lambda: rig.hub.status == "idle")
            await asyncio.sleep(0.3)
            [r1] = rings_log(str(tmp_path))
            assert r1["outcome"] == "missed" and r1["caller"] == "55001"
            assert (tmp_path / r1["photo"]).read_bytes() == JPEG
            assert (tmp_path / "ultimo_squillo.jpg").read_bytes() == JPEG
            assert rig.hub.stats["missed_count"] == 1 and rig.hub.stats["ring_count"] == 1

            await next_second()
            rig.ring("ring-amico", "q")
            await rig.peer.wait_for(is_(code=183, cid="ring-amico"))
            await wait_until(lambda: rig.rings == 2)
            ok, msg = await rig.hub.async_answer()
            assert ok, msg
            ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-amico"))
            rig.peer.request("ACK", "ring-amico", 1, "q", to_tag=ok200.h("to").split("tag=")[1])
            ok, msg, _ = await rig.hub.async_door()                  # target di default
            assert ok, msg
            door = rig.peer.got(lambda m: m.kind == "MESSAGE" and m.body == "OPEN_2F")
            assert door and door[0].first.split()[1] == f"sip:55001@{DOMAIN}"
            await asyncio.sleep(0.5)
            await rig.hub.async_hangup()
            await rig.peer.wait_for(is_("BYE", cid="ring-amico"))
            await wait_until(lambda: rig.hub.status == "idle")
            await asyncio.sleep(0.3)

            log = rings_log(str(tmp_path))
            assert [r["outcome"] for r in log] == ["missed", "answered"]
            assert log[0]["time"] != log[1]["time"]
            recent = ring_log.recent_rings(str(tmp_path), 10)       # quello che vede la card
            assert [r["outcome"] for r in recent] == ["answered", "missed"]
            assert all(r["photo"] and (tmp_path / r["photo"]).exists() for r in recent)
            st = rig.hub.stats
            assert st["ring_count"] == 2 and st["missed_count"] == 1
            assert st["call_count"] == 1 and st["last_call_direction"] == "in"
            assert st["door_count"] == 1 and st["last_call_duration"] is not None
            assert st["last_caller_id"] == "55001"
    run(s())


def test_visitatore_va_via_prima_della_foto(monkeypatch, tmp_path):
    """Squillo di 0,2 s (il Tab risponde subito, o il visitatore molla): la foto sarebbe
    arrivata dopo 1 s. Niente foto inventata, ma la voce nel registro c'è e la lista per
    la card dice «senza foto» invece di puntare a un file che non esiste."""

    async def s():
        async with Rig(monkeypatch) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
            monkeypatch.setattr(R, "SNAPSHOT_DELAY", 1)      # wait_frame vero: grabber spento → None
            await rig.register()
            rig.ring()
            await rig.peer.wait_for(is_(code=183))
            await asyncio.sleep(0.2)
            rig.peer.request("CANCEL", "ring-1", 1, "pnl")
            await wait_until(lambda: rig.hub.status == "idle")
            await asyncio.sleep(1.5)
            [r] = ring_log.recent_rings(str(tmp_path), 10)
            assert r["outcome"] == "missed" and r["photo"] is None
            assert sorted(os.listdir(tmp_path)) == [ring_log.RING_LOG]
    run(s())


def test_due_squilli_nello_stesso_secondo_hanno_voci_distinte(monkeypatch, tmp_path):
    """La targa suona, il PBX annulla e risuona nello stesso secondo (doppia pressione,
    riavvio del PBX): si risponde al secondo. Nel registro il primo deve restare
    «Nessuna risposta», solo il secondo «Risposto»."""

    async def s():
        async with Rig(monkeypatch) as rig:
            snapshots(monkeypatch, tmp_path)
            await rig.register()
            await next_second()
            rig.ring("ring-a", "a")
            await rig.peer.wait_for(is_(code=183, cid="ring-a"))
            rig.peer.request("CANCEL", "ring-a", 1, "a")
            await wait_until(lambda: rig.hub.status == "idle")
            rig.ring("ring-b", "b")
            await rig.peer.wait_for(is_(code=183, cid="ring-b"))
            await wait_until(lambda: rig.rings == 2)
            assert (await rig.hub.async_answer())[0]
            await asyncio.sleep(0.4)
            log = rings_log(str(tmp_path))
            assert len(log) == 2
            assert [r["outcome"] for r in log] == ["missed", "answered"], log
            await rig.hub.async_hangup()
    run(s())


def test_cartella_foto_non_scrivibile_lo_squillo_suona_lo_stesso(monkeypatch, tmp_path):
    """Nelle opzioni c'è un percorso che è un file (o una share staccata): niente registro
    né foto, ma il campanello deve suonare, si risponde e si riaggancia senza errori."""
    bad = tmp_path / "non_una_cartella"
    bad.write_bytes(b"x")

    async def s():
        async with Rig(monkeypatch) as rig:
            snapshots(monkeypatch, bad)
            await rig.register()
            rig.ring()
            await rig.peer.wait_for(is_(code=183))
            await wait_until(lambda: rig.rings == 1)
            await asyncio.sleep(0.5)
            assert (await rig.hub.async_answer())[0]
            await asyncio.sleep(0.5)
            assert rig.hub.status == "in_call"
            await rig.hub.async_hangup()
            await wait_until(lambda: rig.hub.status == "idle")
            assert bad.read_bytes() == b"x"
            assert rig.hub.stats["ring_count"] == 1 and rig.hub.stats["call_count"] == 1
    run(s())


def test_squillo_da_altra_targa_stats_registro_e_bye_al_chiamante(monkeypatch, tmp_path):
    """Suona la targa interna (55002): sensori e registro dicono 55002, e il nostro BYE
    a fine chiamata è indirizzato a chi ha chiamato, non alla targa di default."""
    monkeypatch.setattr(peer_mod, "PANEL", "55002")

    async def s():
        async with Rig(monkeypatch) as rig:
            snapshots(monkeypatch, tmp_path)
            await rig.register()
            rig.ring("ring-interna")
            await rig.peer.wait_for(is_(code=183))
            await wait_until(lambda: rig.rings == 1)
            assert rig.hub.stats["last_caller_id"] == "55002"
            assert (await rig.hub.async_answer())[0]
            await asyncio.sleep(0.3)
            assert rings_log(str(tmp_path))[0]["caller"] == "55002"
            await rig.hub.async_hangup()
            bye = await rig.peer.wait_for(is_("BYE", cid="ring-interna"))
            assert f"sip:55002@{DOMAIN}" in bye.h("to"), bye.h("to")
            await wait_until(lambda: rig.hub.status == "idle")
    run(s())


# ─── messaggio di assenza ─────────────────────────────────────────────────────

def test_messaggio_di_assenza_suona_alla_targa_e_riaggancia(monkeypatch, tmp_path):
    """Nessuno in casa: dopo 1 s HA risponde, la targa riceve la voce a pacchetti da
    20 ms, poi BYE. Registro: «Messaggio di assenza». E il campanello dopo suona ancora."""

    async def s():
        async with Rig(monkeypatch) as rig:
            snapshots(monkeypatch, tmp_path)
            away_message(monkeypatch, delay=1, seconds=1.0)
            await rig.register()
            rig.ring()
            await rig.peer.wait_for(is_(code=183))
            ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-1"), timeout=4)
            rig.peer.request("ACK", "ring-1", 1, "pnl", to_tag=ok200.h("to").split("tag=")[1])
            await rig.peer.wait_for(is_("BYE", cid="ring-1"), timeout=4)
            await wait_until(lambda: rig.hub.status == "idle")
            assert len(rig.peer.audio_rx) >= 40, f"solo {len(rig.peer.audio_rx)} pacchetti di voce"
            assert {len(p) - 12 for p in rig.peer.audio_rx} == {160}
            await asyncio.sleep(0.3)
            assert rings_log(str(tmp_path))[0]["outcome"] == "away"
            assert rig.hub.stats["missed_count"] == 0
            n = len(rig.peer.log)
            rig.ring("ring-2", "q")
            r = await rig.peer.wait_for(lambda m: m.code and m.cid == "ring-2" and m.code >= 180, start=n)
            assert r.code == 183 and rig.rings == 2
            rig.peer.request("CANCEL", "ring-2", 1, "q")
            await wait_until(lambda: rig.hub.status == "idle")
    run(s())


def test_parlare_dalla_card_durante_il_messaggio_lo_ferma_e_tiene_la_linea(monkeypatch, tmp_path):
    """Il messaggio sta suonando (3 s), uno di casa arriva e preme «Microfono»: la voce
    passa, il messaggio non riaggancia a fine file, e chiude solo chi parla."""

    async def s():
        async with Rig(monkeypatch) as rig:
            snapshots(monkeypatch, tmp_path)
            away_message(monkeypatch, delay=0.3, seconds=3.0)
            await rig.register()
            ws, wst, _, _ = mic_ws(monkeypatch, rig)
            rig.ring()
            ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-1"), timeout=4)
            rig.peer.request("ACK", "ring-1", 1, "pnl", to_tag=ok200.h("to").split("tag=")[1])
            await asyncio.sleep(0.5)
            for _ in range(20):
                ws.inbox.put_nowait(types.SimpleNamespace(type="binary", data=MIC_FRAME))
                await asyncio.sleep(0.043)
            assert rig.hub._away_task is None, "il messaggio continua dopo il microfono"
            await asyncio.sleep(3.5)                       # oltre la fine del file
            assert rig.hub.status == "in_call" and not rig.peer.got(is_("BYE"))
            await asyncio.sleep(0.3)
            assert rings_log(str(tmp_path))[0]["outcome"] == "away"
            await rig.hub.async_hangup()
            await rig.peer.wait_for(is_("BYE"))
            ws.inbox.put_nowait(None)
            await wst
    run(s())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg non installato")
def test_messaggio_di_assenza_da_testo_letto_dal_tts(monkeypatch, tmp_path):
    """Niente file, solo il testo nelle opzioni: il TTS di HA (finto: un wav di 1 s
    fatto qui) e l'ffmpeg vero; nessuno risponde, la targa riceve la voce a pacchetti
    PCMU da 20 ms, poi BYE. Registro: «Messaggio di assenza»."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(bytes(x % 64 for x in range(2 * 16000)))  # 1 s di dente di sega
    sintesi = []

    async def audio(hass, media_id):
        sintesi.append(media_id)
        return "wav", buf.getvalue()

    async def s():
        async with Rig(monkeypatch) as rig:
            snapshots(monkeypatch, tmp_path)
            monkeypatch.setattr(R, "AWAY_MESSAGE_TEXT", "Non siamo in casa")
            monkeypatch.setattr(R, "AWAY_MESSAGE_TTS", "tts.google_translate_it_com")
            monkeypatch.setattr(R, "AWAY_MESSAGE_DELAY", 1)
            monkeypatch.setattr(away_tts, "_hass", types.SimpleNamespace(
                config=types.SimpleNamespace(language="it")))
            monkeypatch.setattr(away_tts, "tts", types.SimpleNamespace(
                generate_media_source_id=lambda hass, msg, engine, language: f"{engine}/{language}/{msg}",
                async_get_media_source_audio=audio))
            await rig.register()
            rig.ring()
            await rig.peer.wait_for(is_(code=183))
            ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-1"), timeout=4)
            rig.peer.request("ACK", "ring-1", 1, "pnl", to_tag=ok200.h("to").split("tag=")[1])
            await rig.peer.wait_for(is_("BYE", cid="ring-1"), timeout=4)
            await wait_until(lambda: rig.hub.status == "idle")
            assert sintesi == ["tts.google_translate_it_com/it/Non siamo in casa"]
            assert len(rig.peer.audio_rx) >= 40, f"solo {len(rig.peer.audio_rx)} pacchetti di voce"
            assert {(p[1] & 0x7F, len(p) - 12) for p in rig.peer.audio_rx} == {(0, 160)}  # PCMU, 20 ms
            assert len({p[12:] for p in rig.peer.audio_rx}) > 1, "voce piatta: non è il wav"
            await asyncio.sleep(0.3)
            assert rings_log(str(tmp_path))[0]["outcome"] == "away"
            assert away_tts._cache and away_tts._cache[0] == ("Non siamo in casa", "tts.google_translate_it_com", "it")
    run(s())


def test_rispondi_durante_il_messaggio_registro_dice_risposto(monkeypatch, tmp_path):

    async def s():
        async with Rig(monkeypatch) as rig:
            snapshots(monkeypatch, tmp_path)
            away_message(monkeypatch, delay=0.3, seconds=3.0)
            await rig.register()
            rig.ring()
            await rig.peer.wait_for(is_(code=200, cid="ring-1"), timeout=4)
            await asyncio.sleep(0.5)
            ok, msg = await rig.hub.async_answer()
            assert ok and "assenza" in msg
            await asyncio.sleep(3.5)
            assert rig.hub.status == "in_call" and not rig.peer.got(is_("BYE"))
            assert rings_log(str(tmp_path))[0]["outcome"] == "answered"
            assert len(rig.peer.got(is_(code=200, cseq="INVITE"))) == 1
            await rig.hub.async_hangup()
    run(s())


# ─── il cloud fa le bizze ─────────────────────────────────────────────────────

def test_porta_con_sfida_407_del_cloud(monkeypatch):
    """Il proxy cloud sfida il MESSAGE dell'apri-porta: un solo MESSAGE con digest
    valido in più, esito OK, contatore porta a 1."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            peer, orig = rig.peer, rig.peer._on_raw

            def on_raw(raw):
                m = Msg(raw)
                if m.kind == "MESSAGE" and "proxy-authorization" not in m.hdrs:
                    peer.log.append(m)
                    peer._new.set()
                    peer.send(response(m, 407, "Proxy Authentication Required", extra=(
                        f'Proxy-Authenticate: Digest realm="{DOMAIN}", nonce="n407", qop="auth"\r\n')))
                    return
                orig(raw)
            peer._on_raw = on_raw
            ok, msg, _ = await rig.hub.async_door(target="55001")
            assert ok, msg
            msgs = rig.peer.got(is_("MESSAGE"))
            assert len(msgs) == 2 and msgs[1].body == "OPEN_2F"
            assert check_digest(msgs[1], "MESSAGE", msgs[1].h("proxy-authorization"))
            assert msgs[1].h("panda") == "command"
            assert rig.hub.stats["door_count"] == 1
    run(s())


def test_porta_col_cloud_appena_caduto_si_riregistra_e_apre(monkeypatch):
    """Il proxy chiude la connessione e mezzo secondo dopo si preme «Apri»: niente
    «Non registrato» in faccia, si riregistra e la porta si apre."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.peer.drop()
            await wait_until(lambda: not sip.registered, 3)
            await asyncio.sleep(0.5)
            t0 = time.monotonic()
            ok, msg, _ = await asyncio.wait_for(rig.hub.async_door(target="55001"), 40)
            assert ok, msg
            assert rig.peer.got(lambda m: m.kind == "MESSAGE" and m.body == "OPEN_2F")
            assert time.monotonic() - t0 < 20, "porta aperta troppo tardi"
            await asyncio.sleep(3)                         # la riconnessione si assesta
            assert sip.registered
    run(s(), 120)


def test_cloud_sordo_al_bye_squillo_e_risposta_mentre_il_bye_aspetta(monkeypatch):
    """Il cloud non risponde mai al nostro BYE (5 s di attesa). Intanto suonano e si
    risponde: lo stato torna «idle» subito, lo squillo suona, la risposta tiene, e la
    fine del BYE vecchio non smonta la chiamata nuova."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]
            rig.peer.silent = {"BYE"}
            t0 = time.monotonic()
            hang = asyncio.create_task(rig.hub.async_hangup())
            await wait_until(lambda: rig.hub.status == "idle", 1.5, "idle subito dopo il BYE")
            assert rig.events("call_ended") and rig.hub.stats["last_call_duration"] is not None
            rig.ring("ring-dopo")
            await rig.peer.wait_for(is_(code=183, cid="ring-dopo"))
            await wait_until(lambda: rig.rings == 1)
            assert (await rig.hub.async_answer())[0]
            await rig.peer.wait_for(is_(code=200, cid="ring-dopo"))
            await hang                                     # il BYE scade (5 s)
            assert time.monotonic() - t0 < 8
            assert rig.hub.status == "in_call" and sip.call_state["call_id"] == "ring-dopo"
            assert media.audio_proto.remote_addr is not None
            rig.peer.silent = set()
            await rig.hub.async_hangup()
            await wait_until(lambda: rig.hub.status == "idle")
    run(s())


def test_squillo_cloud_record_route_rispondi_keyframe_riaggancia_niente_scartato(monkeypatch):
    """Cloud (proxy con Record-Route e routing rigido): squillo → nessun INFO di keyframe
    nell'anteprima (dialogo early: la 40515 risponde sempre 491/501) → risposta → INFO →
    BYE nostro. Il proxy non deve scartare niente (Route e Contact giusti)."""
    async def s():
        async with Rig(monkeypatch, "tls") as rig:
            await rig.register()
            rig.ring("ring-rr")
            await rig.peer.wait_for(is_(code=183))
            await asyncio.sleep(0.3)
            assert not rig.peer.got(is_("INFO", cid="ring-rr")) and not sip.in_call
            n = len(rig.peer.log)
            assert (await rig.hub.async_answer())[0]
            ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-rr"))
            rig.peer.request("ACK", "ring-rr", 1, "pnl", to_tag=ok200.h("to").split("tag=")[1])
            await rig.peer.wait_for(is_("INFO", cid="ring-rr"), start=n)
            await asyncio.sleep(0.3)
            await rig.hub.async_hangup()
            bye = await rig.peer.wait_for(is_("BYE", cid="ring-rr"))
            await wait_until(lambda: rig.hub.status == "idle")
            await asyncio.sleep(0.3)
            assert rig.peer.dropped == [], [m.first for m in rig.peer.dropped]
            assert bye.h("route") == rig.peer.record_route
            assert bye.first.split()[1] == rig.peer.contact_uri
            assert len(rig.peer.got(is_("INFO", cid="ring-rr"))) >= 1
            assert sip.call_state["call_id"] is None, "dialogo non ripulito dopo il 200 al BYE"
    run(s())


def test_targa_cifra_ma_noi_no_mai_voce_in_chiaro(monkeypatch):
    """La targa offre a=crypto e «Cifra il media» è spento: la risposta rispecchia
    l'offerta, quindi si sente lei (RX decifrato) e la nostra voce parte cifrata,
    mai in chiaro dentro una sessione SRTP."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.peer.key = "d0RmdmcmVCspeEc3QGZiNWpVLFJhQX1cfHAwJSoj"
            rig.ring()
            r183 = await rig.peer.wait_for(is_(code=183))
            assert "RTP/SAVP" in r183.body and "a=crypto" in r183.body
            assert (await rig.hub.async_answer())[0]
            await asyncio.sleep(0.2)
            assert media.audio_proto.srtp_rx is not None and media.audio_proto.srtp_tx is not None
            for _ in range(20):
                media.send_audio(b"\x10\x00" * 160)
                await asyncio.sleep(0.02)
            await wait_until(lambda: rig.peer.audio_rx, 2, "voce alla targa")
            # 12 header + 160 PCMU + 10 of SRTP auth tag: nothing left in the clear.
            assert {len(p) - 12 for p in rig.peer.audio_rx} == {170}, "RTP in chiaro verso una targa che cifra"
            await rig.hub.async_hangup()
    run(s())


def test_squillo_solo_audio_poi_reinvite_aggiunge_il_video(monkeypatch):
    """La targa suona senza video (m=video 0), si risponde, poi manda un re-INVITE con il
    video: il media video si apre verso la sua porta, la chiamata resta in piedi."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            rig.ring(body=("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                           f"m=audio {rig.peer.audio_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
                           "m=video 0 RTP/AVP 96\r\n"))
            await rig.peer.wait_for(is_(code=183))
            assert (await rig.hub.async_answer())[0]
            ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-1"))
            my_tag = ok200.h("to").split("tag=")[1]
            rig.peer.request("ACK", "ring-1", 1, "pnl", to_tag=my_tag)
            assert media.video_proto.remote_addr is None
            n = len(rig.peer.log)
            rig.peer.request("INVITE", "ring-1", 2, "pnl", to_tag=my_tag, body=rig.peer.sdp())
            r = await rig.peer.wait_for(lambda m: m.code and m.cid == "ring-1", start=n)
            assert r.code == 200 and r.body
            await wait_until(lambda: media.video_proto.remote_addr == ("127.0.0.1", rig.peer.video_port), 2)
            assert rig.hub.status == "in_call" and rig.rings == 1
            await rig.hub.async_hangup()
    run(s())


def test_cloud_srtp_squillo_risposta_chiude_la_targa(monkeypatch, tmp_path):
    """Cloud TLS + SRTP: suonano, si risponde, dopo un po' la targa chiude (BYE). Media
    chiuso, evento call_ended, registro «Risposto», direzione «in» e durata."""

    async def s():
        async with Rig(monkeypatch, "tls", srtp=True) as rig:
            snapshots(monkeypatch, tmp_path)
            await rig.register()
            rig.ring("ring-srtp")
            r183 = await rig.peer.wait_for(is_(code=183))
            assert "a=crypto" in r183.body
            assert (await rig.hub.async_answer())[0]
            ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-srtp"))
            assert media.audio_proto.srtp_rx and media.audio_proto.srtp_tx and media.video_proto.srtp_rx
            my_tag = ok200.h("to").split("tag=")[1]
            rig.peer.request("ACK", "ring-srtp", 1, "pnl", to_tag=my_tag)
            await asyncio.sleep(0.6)
            n = len(rig.peer.log)
            rig.peer.request("BYE", "ring-srtp", 2, "pnl", to_tag=my_tag)
            r = await rig.peer.wait_for(lambda m: m.code and m.cid == "ring-srtp" and "BYE" in m.h("cseq"), start=n)
            assert r.code == 200
            await wait_until(lambda: rig.hub.status == "idle")
            assert rig.events("call_ended") and media.audio_proto.remote_addr is None
            assert media.video_proto.remote_addr is None and rig.hub._keyframe_task is None
            await asyncio.sleep(0.3)
            st = rig.hub.stats
            assert rings_log(str(tmp_path))[0]["outcome"] == "answered"
            assert st["last_call_direction"] == "in" and st["last_call_duration"] >= 0.5
            assert st["missed_count"] == 0
    run(s())


# ─── /av: la pausa dopo la chiamata non blocca l'anteprima dello squillo ─────

def test_av_in_pausa_dopo_la_chiamata_ma_lo_squillo_riapre_l_anteprima(monkeypatch):
    """«Vedi esterno», riaggancio: go2rtc riapre /av → 503 (pausa, niente auto-call).
    Un minuto non è passato e suonano: /av riaperto dà l'anteprima (200), senza INVITE."""
    views = load_views(monkeypatch)

    async def s():
        async with Rig(monkeypatch) as rig:
            hass = make_hass(rig)
            await rig.register()
            rig.peer.on_invite = answer_200
            assert (await rig.hub.async_call())[0]
            av = open_av(views, hass)
            await asyncio.sleep(0.5)
            await rig.hub.async_hangup()
            assert (await asyncio.wait_for(av, 5)).status == 200
            r = await asyncio.wait_for(open_av(views, hass), 3)   # riconnessione di go2rtc
            assert r.status == 503
            rig.ring("ring-in-pausa")
            await rig.peer.wait_for(is_(code=183))
            av2 = open_av(views, hass)
            await asyncio.sleep(0.8)
            assert not av2.done(), "l'anteprima dello squillo non passa da /av"
            assert rig.hub._stream_viewers == 1
            rig.peer.request("CANCEL", "ring-in-pausa", 1, "pnl")
            assert (await asyncio.wait_for(av2, 5)).status == 200
            assert len(rig.peer.got(is_("INVITE"))) == 1, "auto-call partito"
            assert rig.rings == 1 and rig.hub.status == "idle"
    run(s())


# ─── riavvio di Home Assistant nel momento peggiore ──────────────────────────

def test_riavvio_ha_durante_lo_squillo_con_messaggio_e_foto_in_attesa(monkeypatch, tmp_path):
    """Unload/riavvio mentre suona, col timer del messaggio e quello della foto in corso:
    tutto annullato, nessuna eccezione, e un secondo async_stop non fa danni."""

    async def s():
        async with Rig(monkeypatch) as rig:
            snapshots(monkeypatch, tmp_path, delay=5)
            away_message(monkeypatch, delay=5)
            await rig.register()
            rig.ring()
            await rig.peer.wait_for(is_(code=183))
            await wait_until(lambda: rig.rings == 1)
            away, photo = rig.hub._away_task, rig.hub._photo_task
            assert away and photo and not away.done() and not photo.done()
            await rig.hub.async_stop()
            await asyncio.sleep(0.1)
            assert away.cancelled() and photo.cancelled()
            assert media.audio_proto is None and media.video_proto is None
            await rig.hub.async_stop()                     # idempotente
    run(s())


# ─── foto vera dallo squillo (frame grabber + ffmpeg) ─────────────────────────

@pytest.mark.media
def test_foto_vera_dello_squillo_dal_frame_grabber(monkeypatch, tmp_path):
    """Suonano, la targa manda H.264 vero nell'anteprima: la foto JPEG di chi ha suonato
    finisce nella cartella col nome dello squillo e in ultimo_squillo.jpg, e la card la
    trova nel registro. Nessuno risponde (CANCEL): «Nessuna risposta» con la foto."""

    async def s():
        async with Rig(monkeypatch, real_av=True) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
            monkeypatch.setattr(R, "SNAPSHOT_DELAY", 0)
            await rig.register()
            rig.ring()
            r183 = await rig.peer.wait_for(is_(code=183))
            rig.start_media(r183.body)
            await wait_until(lambda: any(ring_log.RING_FILE.fullmatch(n) for n in os.listdir(tmp_path)),
                             10, "foto dello squillo")
            await asyncio.sleep(0.2)
            rig.peer.request("CANCEL", "ring-1", 1, "pnl")
            await wait_until(lambda: rig.hub.status == "idle")
            [r] = ring_log.recent_rings(str(tmp_path), 10)
            assert r["outcome"] == "missed" and r["photo"]
            jpeg = (tmp_path / r["photo"]).read_bytes()
            assert jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9" and len(jpeg) > 2000
            assert (tmp_path / "ultimo_squillo.jpg").read_bytes() == jpeg
            assert ring_log.ring_photo_path(str(tmp_path), r["photo"])
    run(s())


@pytest.mark.media
def test_clip_dello_squillo_e_foto_subito_poi_migliore(monkeypatch, tmp_path):
    """Come un Ring. Suonano, H.264 vero nell'anteprima: la foto di chi ha suonato c'è entro
    1,5 s dal primo IDR (senza aspettare snapshot_delay), i sensori la indicano, e dopo
    snapshot_delay la sostituisce quella con l'esposizione regolata (stesso nome, versione
    nuova). Nessuno risponde (CANCEL): il video dello squillo è in squillo_<ora>.mp4 (H.264
    copiato, durata reale), nel registro e nei sensori, servito anche a pezzi (Range) alla
    card; il file a metà (.part) non è mai raggiungibile."""
    import aiohttp
    from harness.media import clip_info

    async def s():
        async with Rig(monkeypatch, real_av=True, http=True) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
            monkeypatch.setattr(R, "SNAPSHOT_DELAY", 2)
            await rig.register()
            rig.ring()
            r183 = await rig.peer.wait_for(is_(code=183))
            rig.start_media(r183.body)
            await wait_until(lambda: rig.panel_media.idr_at, 3, "IDR della targa")
            await wait_until(lambda: any(ring_log.RING_FILE.fullmatch(n) for n in os.listdir(tmp_path)),
                             5, "foto dello squillo")
            t_photo = time.time()
            [r] = ring_log.recent_rings(str(tmp_path), 10)
            first = (tmp_path / r["photo"]).read_bytes()
            assert t_photo - rig.panel_media.idr_at < 1.5, "prima foto in ritardo"
            assert r["clip"] is None, "clip elencato mentre è ancora in scrittura"
            media_attrs = rig.hub.ring_media()
            assert media_attrs["foto"] == str(tmp_path / r["photo"]) and media_attrs["clip"] is None
            assert media_attrs["foto_url"] == f"/api/vimar_intercom/rings/{r['photo']}?v={r['photo_v']}"
            assert not rig.peer.got(is_("INFO", cid="ring-1")), "INFO nell'anteprima: 491/501 dalla 40515"
            await asyncio.sleep(3.5)                       # snapshot_delay 2 + un IDR (ogni 1 s)
            [r2] = ring_log.recent_rings(str(tmp_path), 10)
            better = (tmp_path / r2["photo"]).read_bytes()
            assert r2["photo"] == r["photo"] and better != first and r2["photo_v"] > r["photo_v"]
            assert (tmp_path / "ultimo_squillo.jpg").read_bytes() == better
            assert rig.hub.ring_media()["foto_url"].endswith(f"?v={r2['photo_v']}")
            clip = r["photo"][:-4] + ".mp4"
            assert (tmp_path / (clip + ".part")).exists()
            async with aiohttp.ClientSession() as http:
                async with http.get(f"{rig.base}/api/vimar_intercom/rings/{clip}.part") as resp:
                    assert resp.status == 404
            rig.peer.request("CANCEL", "ring-1", 1, "pnl")
            await wait_until(lambda: rig.hub.status == "idle")
            await wait_until(lambda: (tmp_path / clip).exists(), 15, "clip chiuso")
            assert not (tmp_path / (clip + ".part")).exists()
            codec, dur, n = clip_info(str(tmp_path / clip))
            assert codec == "h264" and dur > 3 and n > 40, (codec, dur, n)
            [r3] = ring_log.recent_rings(str(tmp_path), 10)
            assert r3["clip"] == clip and r3["outcome"] == "missed"
            await wait_until(lambda: rig.hub.stats["last_clip"] == clip, 2, "clip nei sensori")
            assert rig.hub.ring_media()["clip_url"] == f"/api/vimar_intercom/rings/{clip}"
            async with aiohttp.ClientSession() as http:
                async with http.get(f"{rig.base}/api/vimar_intercom/rings/{clip}",
                                    headers={"Range": "bytes=0-99"}) as resp:
                    assert resp.status == 206 and resp.headers["Content-Type"] == "video/mp4"
                    part = await resp.read()
                    assert len(part) == 100 and part[4:8] == b"ftyp"
    run(s())


# ─── risposta a voce (/audio_ws): solo se dichiarata, mai con un mic rimasto aperto ──

LOUD_FRAME = b"\x02" + b"\x00\x40" * 341   # RMS ~16000, ben sopra VOICE_RMS


def _voice_answer(monkeypatch, query, before=None, ring="ring-v", frames=10):
    """Squilla, il WS manda voce forte: True se l'hub ha risposto da solo."""
    async def s():
        async with Rig(monkeypatch) as rig:
            await rig.register()
            views = load_views(monkeypatch)
            view = views.VimarAudioWSView(make_hass(rig))
            ws = fake_ws(monkeypatch, views)
            wst = asyncio.create_task(view.get(Request(admin=False, query=query)))
            if before:
                await before(rig, ws)
            rig.ring(ring)
            await wait_until(lambda: rig.hub.is_ringing)
            for _ in range(frames):
                ws.inbox.put_nowait(types.SimpleNamespace(type="binary", data=LOUD_FRAME))
                await asyncio.sleep(0.02)
            answered = rig.hub.in_call
            ws.inbox.put_nowait(None)
            await wst
            return answered
    return run(s())


def test_voce_non_risponde_senza_flag(monkeypatch):
    assert _voice_answer(monkeypatch, {}) is False


def test_voce_risponde_con_flag(monkeypatch):
    assert _voice_answer(monkeypatch, {"voice_answer": "1"}) is True


def test_voce_da_connessione_gia_in_chiamata_non_risponde_al_giro_dopo(monkeypatch):
    async def before(rig, ws):
        # la connessione ha parlato in una chiamata precedente, poi l'hub e' tornato a riposo
        rig.ring("ring-0")
        await wait_until(lambda: rig.hub.is_ringing)
        assert (await rig.hub.async_answer())[0]
        ws.inbox.put_nowait(types.SimpleNamespace(type="binary", data=MIC_FRAME))
        await asyncio.sleep(0.1)
        await rig.hub.async_hangup()
        await wait_until(lambda: rig.hub.status == "idle")
    # mai un frame a riposo: e' ancora "in chiamata" per il server, quindi tace
    assert _voice_answer(monkeypatch, {"voice_answer": "1"}, before, ring="ring-v") is False


def test_voce_a_riposo_non_si_accumula_per_lo_squillo_dopo(monkeypatch):
    async def before(rig, ws):  # 3 frame forti a riposo (~127 ms)
        for _ in range(3):
            ws.inbox.put_nowait(types.SimpleNamespace(type="binary", data=LOUD_FRAME))
        await asyncio.sleep(0.2)
    # altri 3 allo squillo: da soli (127 ms) sotto VOICE_ANSWER_MS, insieme (254) no
    assert _voice_answer(monkeypatch, {"voice_answer": "1"}, before, frames=3) is False
