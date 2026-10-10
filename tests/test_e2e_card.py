"""La card vera nel browser (Chromium e WebKit = Safari/iPhone) contro l'impianto di
prova: hub, sip_client, /av, /audio_ws e /rings veri, targa finta (harness.card).

Il video dal vivo ha due strade: WebCodecs sui NAL del WebSocket (Chromium, Safari
16.4+) e lo stream di HA (/av) dove WebCodecs manca. I test sullo stream di HA aprono la
card con webcodecs=False; il bench e la prova di ripiego stanno in fondo.

    PYTHONPATH=<aiohttp> python -m pytest -m browser
"""
from __future__ import annotations

import asyncio
import base64
import re
import struct
import time

import pytest
from harness import media as hm
from harness.card import Card, engine  # noqa: F401  (fixture)
from harness.peer import answer_200, is_
from harness.rig import Rig, run, wait_until

from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import ring_log
from custom_components.vimar_intercom import runtime as R

pytestmark = pytest.mark.browser

IDLE = "info().pill === 'Pronto' && info().video === 'auto'"
# Il canvas riempie il riquadro 4:3 e, in "overlay", i tasti stanno sopra il video e ricevono il tocco.
GEOMETRY = """(() => { const r = card.shadowRoot, q = (s) => r.querySelector(s).getBoundingClientRect();
  const m = q('.media'), cv = q('#video > canvas'), o = q('#open');
  const top = r.elementFromPoint(o.x + o.width / 2, o.y + o.height / 2);
  return { canvas_fills_media: Math.abs(cv.width - m.width) < 1 && Math.abs(cv.height - m.height) < 1,
           ratio_4_3: Math.abs(m.width / m.height - 4 / 3) < 0.02,
           open_over_video: o.y >= m.y && o.y + o.height <= m.y + m.height,
           open_on_top: !!top?.closest('#open') }; })()"""


async def decline(peer, inv):
    peer.reply(inv, 100, "Trying")
    peer.reply(inv, 603, "Decline")


def test_dashboard_a_riposo_non_chiama_mai(monkeypatch, engine):  # noqa: F811
    """Requisito: aprire (o ricaricare) una dashboard con la card a riposo non chiama la
    targa e non apre /av. Né quando il citofono passa da offline a pronto, né con stati
    strani (riavvio di HA), né quando finisce uno squillo, né dopo una visione chiusa."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            rig.peer.on_invite = answer_200
            async with Card(rig, engine, webcodecs=False) as c:  # aperta PRIMA della registrazione
                await rig.register()                     # offline → idle
                await c.until(IDLE)
                for st in ("unavailable", "unknown", "offline", None):
                    rig.state_override = st
                    await asyncio.sleep(0.3)
                for _ in range(2):                       # cambio dashboard / ricarica
                    await c.open()
                    await asyncio.sleep(2)
                t = await c.T()
                assert not rig.peer.got(is_("INVITE")), "chiamata partita da sola"
                assert t["av"] == [] and t["ws"] == 0 and "live" not in t["created"], t
                assert rig.hub._stream_viewers == 0 and not rig.services
                rig.ring()                               # squillo: anteprima sì, chiamate no
                await rig.peer.wait_for(is_(code=183))
                await c.until("T.av.includes(200)")
                rig.peer.request("CANCEL", "ring-1", 1, "pnl")
                await c.until(IDLE)
                await asyncio.sleep(3)
                assert not [m for m in rig.peer.got(is_("INVITE")) if m.cid != "ring-1"]
                assert (await c.T())["live"] == 0 and rig.hub._stream_viewers == 0
                await c.tap("view")                      # visione chiusa con "Riaggancia"
                await c.until("info().pill === 'In chiamata'")
                await c.tap("hangup")
                await c.until(IDLE)
                n = len(rig.peer.got(is_("INVITE")))
                await c.open()
                await asyncio.sleep(3)
                assert len(rig.peer.got(is_("INVITE"))) == n
                assert rig.hub._stream_viewers == 0 and rig.hub.status == "idle"
                assert rig.services == ["vimar_intercom.call", "vimar_intercom.hangup"]
                assert not (await c.T())["errors"]
    run(s(), 120)


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_squillo_rispondi_parla_riaggancia(monkeypatch, engine):  # noqa: F811
    """Squillo → anteprima dal vivo → "Rispondi" (doppio tocco: una risposta sola) col
    microfono → voce nei due sensi a 20 ms → "Riaggancia": microfono e WS chiusi."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, webcodecs=False) as c:
                rig.ring("ring-card")
                await c.until("info().pill === 'Suonano alla porta' && T.av.includes(200) && T.avBytes > 0")
                assert (await c.info())["talk"] == "Rispondi"
                await c.page.evaluate("T.gumDelay = 500")
                await c.tap("talk")
                await c.tap("talk")                      # doppio tocco durante il permesso
                await c.until("info().pill === 'In chiamata' && T.ws === 1")
                ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-card"))
                rig.peer.request("ACK", "ring-card", 1, "pnl", to_tag=ok200.h("to").split("tag=")[1])
                for i in range(100):                     # la targa parla
                    rig.peer.rtp_audio.sendto(struct.pack("!BBHII", 0x80, 0, i, i * 160, 1234) + b"\x7f" * 160,
                                              ("127.0.0.1", media.RTP_AUDIO_PORT))
                    await asyncio.sleep(0.02)
                await c.until("T.rx > 20")               # la targa si sente
                await wait_until(lambda: len(rig.peer.audio_rx) > 20, 5, "voce alla targa")
                assert {len(p) - 12 for p in rig.peer.audio_rx} == {160}
                t = await c.T()
                assert rig.services == ["vimar_intercom.answer"], rig.services
                assert all(f[0] == 2 for f in t["frames"]), t["frames"]
                await c.tap("hangup")
                await rig.peer.wait_for(is_("BYE", cid="ring-card"))
                await c.until(IDLE + " && info().audio === 'off' && T.wsClosed === 1")
                n = len(rig.peer.audio_rx)
                await asyncio.sleep(0.5)
                assert len(rig.peer.audio_rx) == n, "voce ancora in uscita dopo il riaggancio"
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_muto_audio_targa(monkeypatch, engine):  # noqa: F811
    """Tondo "Audio" sul video: visibile per tutta la diretta (ringing/calling/in_call),
    non solo quando l'audio è già partito. Durante una chiamata vera (col microfono) il
    tocco muta/smuta solo la riproduzione locale (GainNode a 0/1) — la chiamata, il
    microfono e il WebSocket non se ne accorgono — e il muto scelto vale una sessione dal
    vivo sola (sparisce col riaggancio)."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, webcodecs=False) as c:
                rig.ring("ring-mute")
                await c.until("info().pill === 'Suonano alla porta' && T.av.includes(200) && T.avBytes > 0")
                m = (await c.info())["mute"]
                assert not m["hidden"] and not m["audible"], m  # già visibile, ma niente ancora da mutare
                await c.tap("talk")
                await c.until("info().pill === 'In chiamata' && T.ws === 1")
                ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-mute"))
                rig.peer.request("ACK", "ring-mute", 1, "pnl", to_tag=ok200.h("to").split("tag=")[1])
                for i in range(20):                      # la targa parla: arriva sul GainNode
                    rig.peer.rtp_audio.sendto(struct.pack("!BBHII", 0x80, 0, i, i * 160, 1234) + b"\x7f" * 160,
                                              ("127.0.0.1", media.RTP_AUDIO_PORT))
                    await asyncio.sleep(0.02)
                await c.until("T.rx > 5")
                m = (await c.info())["mute"]
                assert m["audible"] and m["gain"] == 1, m
                await c.tap("mute")
                await c.until("info().mute.muted")
                m = (await c.info())["mute"]
                assert m["gain"] == 0 and not m["audible"], m
                rx0 = (await c.T())["rx"]
                for i in range(20, 40):                   # la targa continua: la ricezione non si ferma da muti
                    rig.peer.rtp_audio.sendto(struct.pack("!BBHII", 0x80, 0, i, i * 160, 1234) + b"\x7f" * 160,
                                              ("127.0.0.1", media.RTP_AUDIO_PORT))
                    await asyncio.sleep(0.02)
                await c.until(f"T.rx > {rx0}")            # WS e ricezione intatti col muto
                await wait_until(lambda: len(rig.peer.audio_rx) > 5, 5, "voce alla targa")  # anche in uscita
                t = await c.T()
                assert t["gum"] == 1 and t["wsClosed"] == 0, t   # microfono e WS non toccati dal muto
                assert (await c.info())["pill"] == "In chiamata" and rig.services == ["vimar_intercom.answer"]
                await c.tap("mute")
                await c.until("!info().mute.muted")
                assert (await c.info())["mute"]["audible"]
                await c.tap("hangup")
                await rig.peer.wait_for(is_("BYE", cid="ring-mute"))
                await c.until(IDLE + " && info().audio === 'off'")
                assert (await c.info())["mute"]["hidden"]               # a riposo il tasto sparisce
                assert await c.page.evaluate("card._muted") is False    # il muto vale una sessione dal vivo sola
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_ascolta_durante_vedi_esterno_in_chiamata(monkeypatch, engine):  # noqa: F811
    """`listen_on_ring: true` fa sentire la targa anche durante "Vedi esterno" in corso
    (calling → in_call), non solo allo squillo in arrivo: l'RTP della targa, una volta
    stabilita la chiamata, arriva comunque — senza microfono né "answer"/"call"."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            async with Card(rig, engine, webcodecs=False, listen_on_ring=True) as c:
                await c.tap("view")
                await c.until("info().pill === 'In chiamata'")
                await c.until("info().listen", 3)               # l'ascolto si aggancia da solo anche qui
                assert (await c.T())["gum"] == 0, "il microfono non deve accendersi"
                n0 = (await c.T())["rx"]
                for i in range(15):                              # la targa parla durante la visione
                    rig.peer.rtp_audio.sendto(struct.pack("!BBHII", 0x80, 0, i, i * 160, 1234) + b"\x7f" * 160,
                                              ("127.0.0.1", media.RTP_AUDIO_PORT))
                    await asyncio.sleep(0.02)
                await c.until(f"T.rx > {n0}", 3)                 # arriva davvero, non solo allo squillo
                assert (await c.info())["mute"]["audible"]       # tasto "Audio" acceso, sempre visibile in diretta
                await c.tap("hangup")
                await c.until(IDLE)
                assert rig.services == ["vimar_intercom.call", "vimar_intercom.hangup"]
                assert not (await c.T())["errors"]
    run(s())


def test_vedi_esterno_finisce_col_bye_della_targa(monkeypatch, engine):  # noqa: F811
    """La targa chiude "Vedi esterno" dopo ~10 s: la card torna subito a riposo (video
    chiuso, nessun errore) e non si richiama. Per guardare ancora si ripreme il tasto."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            async with Card(rig, engine, webcodecs=False) as c:
                await c.tap("view")
                await c.until("info().pill === 'In chiamata' && T.av.includes(200)")
                await asyncio.sleep(0.5)
                rig.bye(rig.peer.got(is_("INVITE"))[0])
                await c.until(IDLE, 3)
                await asyncio.sleep(2)
                assert len(rig.peer.got(is_("INVITE"))) == 1, "richiamata dopo il BYE"
                assert not (await c.T())["errors"] and "non ha accettato" not in (await c.info())["err"]
                await c.tap("view")
                await c.until("info().pill === 'In chiamata'")
                assert len(rig.peer.got(is_("INVITE"))) == 2
    run(s())


def test_video_dal_vivo_solo_con_squillo_o_chiamata(monkeypatch, engine):  # noqa: F811
    """Stato → riquadro video: dal vivo in ringing/calling/in_call, altrimenti fermo;
    passaggi rapidissimi: vince l'ultimo. Nessun servizio chiamato da solo."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, webcodecs=False) as c:
                await c.until(IDLE)
                for st, view in (("calling", "live"), ("in_call", "live"), ("calling", "live"),
                                 ("idle", "auto"), ("ringing", "live"), ("offline", "auto")):
                    rig.state_override = st
                    await c.until(f"info().video === '{view}'", 3)
                await c.page.evaluate("['idle','in_call','idle','ringing'].forEach("
                                      "(s) => card.hass = {...card._hass, states: {...card._hass.states,"
                                      " 'sensor.vimar_intercom_intercom_stato': {state: s}}})")
                rig.state_override = "ringing"
                await asyncio.sleep(0.5)
                assert (await c.info())["video"] == "live"
                assert not rig.services and not (await c.T())["errors"]
                assert not rig.peer.got(is_("INVITE"))
    run(s())


def test_rispondi_ma_lo_squillo_finisce_durante_il_permesso(monkeypatch, engine):  # noqa: F811
    """iPhone, primo "Rispondi": il permesso del microfono resta a schermo e intanto ha
    risposto il Tab (CANCEL). Concesso il permesso, la card non chiama la targa."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine) as c:
                if not await c.page.evaluate("!!window.AudioContext"):
                    pytest.skip("questo motore headless non ha Web Audio")
                rig.ring()
                await c.until("info().talk === 'Rispondi'")
                await c.page.evaluate("T.gumDelay = 1500")
                await c.tap("talk")
                await asyncio.sleep(0.3)
                rig.peer.request("CANCEL", "ring-1", 1, "pnl")
                await c.until(IDLE)
                await asyncio.sleep(2)
                assert not rig.services, rig.services
                assert not [m for m in rig.peer.got(is_("INVITE")) if m.cid != "ring-1"]
                t = await c.T()  # niente audio; il WS video dello squillo (WebCodecs) si è chiuso
                assert (await c.info())["audio"] == "off" and t["ws"] == t["wsClosed"], t
    run(s())


def test_rispondi_senza_https_solo_video(monkeypatch, engine):  # noqa: F811
    """HA in HTTP: niente microfono, ma "Rispondi" risponde (solo video)."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, insecure=True, webcodecs=False) as c:
                rig.ring("ring-http")
                await c.until("info().talk === 'Rispondi' && T.av.includes(200)")
                await c.tap("talk")
                await c.until("info().pill === 'In chiamata' && info().err.includes('HTTPS')")  # perché niente voce
                assert (await c.T())["ws"] == 0 and (await c.info())["video"] == "live"
                ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-http"))
                rig.peer.request("BYE", "ring-http", 2, "pnl", to_tag=ok200.h("to").split("tag=")[1])
                await c.until(IDLE)
                assert not (await c.T())["errors"]
    run(s())


MIC_OFF = "info().audio === 'off' && card.shadowRoot.getElementById('talk').getAttribute('aria-pressed') === 'false'"


@pytest.mark.parametrize("insecure", [True, False])
def test_microfono_senza_api_lo_dice(monkeypatch, insecure):
    """Dal campo: HA aperta in HTTP (o una webview senza navigator.mediaDevices), tocco su
    "Parla" e non succedeva niente. Ora il tocco dice perché, e non chiama la targa."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, "chromium", insecure=insecure, webcodecs=False) as c:
                await c.until(IDLE)
                await c.page.evaluate("Object.defineProperty(navigator, 'mediaDevices', { value: undefined })")
                await c.tap("talk")
                await c.until("info().err.startsWith('Microfono non disponibile')")
                assert ("HTTPS" in (await c.info())["err"]) is insecure
                assert await c.page.evaluate(MIC_OFF)
                await asyncio.sleep(0.5)
                assert not (await c.T())["calls"] and not rig.peer.got(is_("INVITE"))
    run(s())


@pytest.mark.parametrize("ios", [False, True])
def test_microfono_permesso_negato_lo_dice(monkeypatch, ios):
    """Dal campo: permesso del microfono negato all'app (iOS non lo richiede più). La card
    dice dove riattivarlo, il tasto resta spento e la targa non viene chiamata."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, "chromium", webcodecs=False, query="&ios" if ios else "") as c:
                await c.until(IDLE)
                await c.page.evaluate("navigator.mediaDevices.getUserMedia = async () => {"
                                      " throw new DOMException('denied', 'NotAllowedError'); }; 0")
                await c.tap("talk")
                await c.until("info().err.startsWith('Permesso del microfono negato')")
                assert ("Impostazioni" in (await c.info())["err"]) is ios
                assert await c.page.evaluate(MIC_OFF)
                assert not (await c.T())["calls"] and not rig.peer.got(is_("INVITE"))
    run(s())


def test_apertura_fallita_mostra_l_errore_nella_lingua_dell_utente(monkeypatch, engine):  # noqa: F811
    """HA sends the lock's error in English with its key: the card shows it in the user's language (#128)."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()

            async def door(**_k):
                return False, "timeout", None

            monkeypatch.setattr(rig.hub, "async_door", door)
            async with Card(rig, engine) as c:
                await c.tap("open")
                await c.tap("open")
                await c.until("card.shadowRoot.querySelector('#open .lbl').textContent === 'Errore'")
                assert (await c.info())["err"] == "Apertura non riuscita: nessuna risposta dal citofono"
    run(s())


def test_apri_doppio_tocco(monkeypatch, engine):  # noqa: F811
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine) as c:
                await c.tap("open")
                await asyncio.sleep(0.5)
                assert not rig.peer.got(is_("MESSAGE")), "un tocco solo ha aperto"
                await c.tap("open")
                m = await rig.peer.wait_for(is_("MESSAGE"))
                assert m.body == "OPEN_2F" and m.h("panda") == "command"
                await c.until("card.shadowRoot.querySelector('#open .lbl').textContent === 'Aperto'")
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_rispondi_da_notifica_audio_da_solo(monkeypatch, engine):  # noqa: F811
    """Notifica "Rispondi": l'automazione ha già risposto (vimar_intercom.answer, quindi
    "in_call") prima che l'app apra .../camera#citofono. La card deve arrivare già sul
    vivo, con l'audio agganciato da sola (niente tocco su "Microfono"). Un secondo
    ricarico da fermo (mai squillato) non deve toccare né audio né video."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)  # come se avesse già risposto l'automazione
            async with Card(rig, engine, webcodecs=False) as c:
                await c.tap("view")
                await c.until("info().pill === 'In chiamata'")
                assert (await c.info())["audio"] == "off"
                await c.open(hash="citofono")              # ricarica come dalla notifica
                await c.until("info().video === 'live' && info().audio === 'on'")
                assert (await c.info())["talk"] == "Microfono"
                assert not (await c.T())["errors"]
                await c.tap("hangup")
                await c.until(IDLE)
                n = len(rig.services)

                # Da fermo, la stessa ancora non chiama né apre il microfono da sola.
                await c.open(hash="citofono")
                await asyncio.sleep(0.5)
                assert (await c.info())["audio"] == "off" and (await c.info())["video"] == "auto"
                assert len(rig.services) == n and not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_audio_bloccato_mostra_tasto_audio(monkeypatch, engine):  # noqa: F811
    """iOS senza un gesto vero: l'AudioContext dell'aggancio automatico resta sospeso
    (qui simulato). L'audio rinuncia in silenzio ma "Microfono" diventa "Audio", ben
    visibile — un secondo AudioContext (il tocco vero) lo aggancia comunque."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine, webcodecs=False) as c:
                await c.tap("view")
                await c.until("info().pill === 'In chiamata'")
                # Il primo AudioContext (l'aggancio automatico) resta "suspended": il
                # browser di prova non ha un vero gesto da riprodurre, ma qui lo forziamo
                # per simulare Safari/iOS senza sblocco. Il secondo (il tocco) è vero.
                await c.page.evaluate("""(() => {
                    const RealAC = window.AudioContext; let n = 0;
                    window.AudioContext = class extends RealAC {
                        constructor(...a) { super(...a); n++;
                          if (n === 1) Object.defineProperty(this, 'state', { get: () => 'suspended' }); }
                    };
                })()""")
                await c.open(hash="citofono")
                await c.until("card.shadowRoot.querySelector('#talk .lbl').textContent === 'Audio'")
                assert (await c.info())["audio"] == "off" and not (await c.T())["errors"]
                await c.tap("talk")
                await c.until("info().audio === 'on'")
                assert (await c.info())["talk"] == "Microfono"
                assert not (await c.T())["errors"]
    run(s())


def test_ascolta_durante_squillo_spento_di_default(monkeypatch, engine):  # noqa: F811
    """`listen_on_ring` di default è spento: durante lo squillo (anteprima video/anteprima
    audio già in early media) la card resta muta, nessun WebSocket audio si apre da sola
    e il microfono non si accende mai. La cronologia non risponde né chiama."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, webcodecs=False) as c:  # niente NalPlayer: solo il WS dell'ascolto conterebbe
                rig.ring()
                await rig.peer.wait_for(is_(code=183))
                await c.until("info().pill === 'Suonano alla porta'")
                for i in range(15):  # la targa: audio in early media, prima di ogni risposta
                    rig.peer.rtp_audio.sendto(struct.pack("!BBHII", 0x80, 0, i, i * 160, 1234) + b"\x7f" * 160,
                                              ("127.0.0.1", media.RTP_AUDIO_PORT))
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.3)
                assert (await c.info())["listen"] is False
                t = await c.T()
                assert t["ws"] == 0 and t["gum"] == 0, t
                rig.peer.request("CANCEL", "ring-1", 1, "pnl")
                await c.until(IDLE)
                assert not rig.services and not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_ascolta_durante_squillo_acceso(monkeypatch, engine):  # noqa: F811
    """`listen_on_ring: true`: appena squilla (anche solo in anteprima, prima di "Rispondi")
    si sente la targa da sola — stesso canale audio del parlato, ma senza microfono né
    "answer"/"call". Finito lo squillo l'ascolto si stacca da solo."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, webcodecs=False, listen_on_ring=True) as c:
                rig.ring()
                await rig.peer.wait_for(is_(code=183))
                await c.until("info().pill === 'Suonano alla porta'")
                await c.until("info().listen", 3)               # l'ascolto si aggancia da solo
                assert (await c.T())["gum"] == 0, "il microfono non deve accendersi"
                n0 = (await c.T())["rx"]
                for i in range(15):  # la targa parla già durante lo squillo (early media)
                    rig.peer.rtp_audio.sendto(struct.pack("!BBHII", 0x80, 0, i, i * 160, 1234) + b"\x7f" * 160,
                                              ("127.0.0.1", media.RTP_AUDIO_PORT))
                    await asyncio.sleep(0.02)
                await c.until(f"T.rx > {n0}", 3)                 # i pacchetti audio sono arrivati e sono stati suonati
                assert not rig.services, rig.services            # niente "answer"/"call" da sola
                rig.peer.request("CANCEL", "ring-1", 1, "pnl")
                await c.until(IDLE)
                await c.until("!info().listen")                  # fine squillo: l'ascolto si stacca
                assert not (await c.T())["errors"]
    run(s())


def test_chiamata_rifiutata_la_card_lo_dice_e_si_chiude(monkeypatch, engine):  # noqa: F811
    """Dal campo: la targa rifiuta (603) "Vedi esterno". La card non resta su
    "Collegamento…" col riquadro video vuoto: si richiude e lo dice."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.peer.on_invite = decline
            async with Card(rig, engine) as c:
                await c.tap("view")                           # "Vedi esterno" → 603
                await c.until(IDLE + " && info().err.includes('603')", 8)
                assert not (await c.page.evaluate("card.shadowRoot.getElementById('view').disabled"))
                await c.tap("view")
                await c.until("info().pill.startsWith('Collegamento') || info().pill === 'Pronto'")
                await c.until(IDLE, 8)
    run(s())


def test_socchiusa_e_ultimi_squilli_con_foto(monkeypatch, engine, tmp_path):  # noqa: F811
    """A riposo solo stato e tasti (riquadro video chiuso), si apre con lo squillo;
    nel cassetto, gli ultimi squilli dal registro, con la foto da un percorso firmato.
    Da fermo il cassetto si apre dalla foto dell'ultimo squillo (= tasto cronologia),
    sopra la foto grande, senza chiamare la targa."""
    ring_log.update_ring_log(str(tmp_path), lambda r: r.extend([
        {"time": "2026-09-27T09:00:00+02:00", "photo": None, "outcome": "missed"},
        {"time": "2026-09-27T10:15:00+02:00", "photo": "squillo_20260927_101500.jpg", "outcome": "away"}]))
    (tmp_path / "squillo_20260927_101500.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    js = """(() => { const r = card.shadowRoot;
      return { media: r.querySelector('.media').getBoundingClientRect().height,
               hist: [...r.querySelectorAll('.hist button')].map(b => [b.textContent, b.querySelector('img')?.getAttribute('src')]) }; })()"""

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
            await rig.register()
            async with Card(rig, engine) as c:
                await c.page.emulate_media(reduced_motion="reduce")
                await c.until("card.shadowRoot.querySelectorAll('.hist button').length === 2")
                st = await c.page.evaluate(js)
                assert st["media"] == 0, st
                src = st["hist"][0][1]
                assert re.fullmatch(r"/api/vimar_intercom/rings/squillo_20260927_101500\.jpg\?v=\d+&authSig=x", src), st
                assert "Messaggio di assenza" in st["hist"][0][0] and "Nessuna risposta" in st["hist"][1][0]
                assert await c.page.evaluate(f"fetch('{src}').then(r => r.status)") == 200
                assert await c.page.evaluate("fetch('/api/vimar_intercom/rings/ultimo_squillo.jpg')"
                                             ".then(r => r.status)") == 404
                for q, n in (("0", 1), ("1", 1), ("x", 2), ("999", 2)):   # ?limit= fra 1 e 50, 10 se strano
                    got = await c.page.evaluate(f"fetch('/api/vimar_intercom/rings?limit={q}').then(r => r.json())")
                    assert len(got) == n, (q, got)
                assert await c.page.evaluate("card.shadowRoot.querySelector('#photo img').getAttribute('src')") == src
                await c.tap("photo")                     # cassetto da fermo: foto, non video
                await c.until("card.shadowRoot.querySelector('.media').getBoundingClientRect().height > 100")
                assert await c.page.evaluate("card.shadowRoot.querySelector('.still').getAttribute('src')") == src
                assert (await c.T())["live"] == 0 and not rig.services and not rig.peer.got(is_("INVITE"))
                await c.tap("photo")
                await c.until("card.shadowRoot.querySelector('.media').getBoundingClientRect().height === 0")
                rig.ring()  # squillo vero: senza WebCodecs il riquadro apre /av, che a hub fermo chiamerebbe
                await rig.peer.wait_for(is_(code=183))
                await c.until("card.shadowRoot.querySelector('.media').getBoundingClientRect().height > 100")
                rig.peer.request("CANCEL", "ring-1", 1, "pnl")
                await c.until("card.shadowRoot.querySelector('.media').getBoundingClientRect().height === 0")
                assert not rig.services
    run(s())


def test_clip_nella_cronologia_play_e_video(monkeypatch, engine, tmp_path):  # noqa: F811
    """Squillo con clip: la miniatura ha il tasto play (anche senza foto) e il tocco apre il
    video (<video controls playsinline>, percorso firmato) al posto della foto; un tocco sui
    controlli del video non chiude la finestra, uno fuori sì e il video si ferma. Lo squillo
    con la sola foto apre la foto. Quando arrivano foto e clip di uno squillo nuovo (attributi
    del sensore) la lista si ricarica da sola."""
    ring_log.update_ring_log(str(tmp_path), lambda r: r.extend([
        {"time": "2026-09-27T09:00:00+02:00", "photo": "squillo_20260927_090000.jpg", "outcome": "missed"},
        {"time": "2026-09-27T10:15:00+02:00", "photo": None, "clip": "squillo_20260927_101500.mp4", "outcome": "missed"},
        {"time": "2026-09-27T10:20:00+02:00", "photo": "squillo_20260927_102000.jpg",
         "clip": "squillo_20260927_102000.mp4", "outcome": "answered"}]))
    for n in ("squillo_20260927_090000.jpg", "squillo_20260927_102000.jpg"):
        (tmp_path / n).write_bytes(b"\xff\xd8\xff\xd9")
    for n in ("squillo_20260927_101500.mp4", "squillo_20260927_102000.mp4"):
        (tmp_path / n).write_bytes(b"\x00\x00\x00\x18ftypmp42")
    js = """(() => { const r = card.shadowRoot, d = r.querySelector('dialog.photo'), v = d.querySelector('video');
      return { rows: [...r.querySelectorAll('.hist button')].map(b => [!!b.querySelector('.play'), !!b.querySelector('img'), b.disabled]),
               open: d.open, video: !v.hidden && !!v.getAttribute('src') ? v.getAttribute('src') : null, paused: v.paused,
               img: !d.querySelector('img').hidden, controls: v.controls && v.hasAttribute('playsinline') }; })()"""

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
            await rig.register()
            async with Card(rig, engine) as c:
                await c.until("card.shadowRoot.querySelectorAll('.hist button').length === 3")
                st = await c.page.evaluate(js)
                assert st["rows"] == [[True, True, False], [True, False, False], [False, True, False]], st
                await c.page.evaluate("card.shadowRoot.querySelectorAll('.hist button')[0].click()")
                await c.until("card.shadowRoot.querySelector('dialog.photo video').getAttribute('src')")
                st = await c.page.evaluate(js)
                assert st["open"] and st["controls"] and not st["img"], st
                assert st["video"] == "/api/vimar_intercom/rings/squillo_20260927_102000.mp4?authSig=x", st
                assert await c.page.evaluate(f"fetch('{st['video']}').then(r => r.status)") == 200
                await c.page.evaluate("card.shadowRoot.querySelector('dialog.photo video').click()")
                assert (await c.page.evaluate(js))["open"], "il tocco sul video ha chiuso la finestra"
                await c.page.evaluate("card.shadowRoot.querySelector('dialog.photo .cap').click()")
                await c.until("!card.shadowRoot.querySelector('dialog.photo').open")  # l'evento close arriva dopo
                await c.until("!card.shadowRoot.querySelector('dialog.photo video').getAttribute('src')")
                assert (await c.page.evaluate(js))["paused"]
                await c.page.evaluate("card.shadowRoot.querySelectorAll('.hist button')[2].click()")
                await c.until("card.shadowRoot.querySelector('dialog.photo').open")
                st = await c.page.evaluate(js)
                assert st["img"] and st["video"] is None, st
                await c.page.evaluate("card.shadowRoot.querySelector('dialog.photo').close()")
                # Squillo nuovo: la foto arriva ~1 s dopo (sensore), poi il clip: la lista si aggiorna da sola
                rig.hub.stats.update(last_photo="squillo_20260927_110000.jpg", last_photo_v=1)
                await asyncio.sleep(0.5)
                assert await c.page.evaluate("card.shadowRoot.querySelectorAll('.hist button').length") == 3
                ring_log.update_ring_log(str(tmp_path), lambda r: r.append(
                    {"time": "2026-09-27T11:00:00+02:00", "photo": "squillo_20260927_110000.jpg",
                     "clip": "squillo_20260927_110000.mp4", "outcome": "missed"}))
                (tmp_path / "squillo_20260927_110000.jpg").write_bytes(b"\xff\xd8\xff\xd9")
                rig.hub.stats.update(last_photo_v=2)
                await c.until("card.shadowRoot.querySelectorAll('.hist button').length === 4")
                assert (await c.page.evaluate(js))["rows"][0] == [False, True, False]
                (tmp_path / "squillo_20260927_110000.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
                rig.hub.stats.update(last_clip="squillo_20260927_110000.mp4")
                await c.until("!!card.shadowRoot.querySelector('.hist button').querySelector('.play')")
                assert (await c.page.evaluate(js))["rows"][0] == [True, True, False]
                assert not rig.services and not rig.peer.got(is_("INVITE")) and not (await c.T())["errors"]
    run(s())


PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


def test_history_photos_re_signed_after_expiry(monkeypatch, engine, tmp_path):  # noqa: F811
    """Signed paths expire ~30 s after auth/sign_path; a photo the phone fetched again later
    got a 401 ("invalid authentication" in HA's log) and stayed broken. Here the first fetch
    of every photo after the history reloads is refused as expired: thumbnails, the last
    ring's still and the photo opened from the list all end up loaded, each with a new
    signature."""
    for n in ("squillo_20260927_090000.jpg", "squillo_20260927_100000.jpg"):
        (tmp_path / n).write_bytes(PNG_1PX)
    ring_log.update_ring_log(str(tmp_path), lambda r: r.append(
        {"time": "2026-09-27T09:00:00+02:00", "photo": "squillo_20260927_090000.jpg", "outcome": "missed"}))
    loaded = """[...card.shadowRoot.querySelectorAll('.hist img, .still, #photo img')]
      .every((i) => i.complete && i.naturalWidth > 0)"""

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
            await rig.register()
            async with Card(rig, engine, query="&signs") as c:
                await c.until("card.shadowRoot.querySelectorAll('.hist img').length === 1 && " + loaded)
                refused, seen = [], set()

                async def expire_first(route):
                    path = route.request.url.split("?")[0]
                    if path in seen:
                        await route.continue_()
                    else:
                        seen.add(path)
                        refused.append(route.request.url)
                        await route.fulfill(status=401)
                await c.page.route(re.compile(r"/api/vimar_intercom/rings/[^?]+\.jpg\?"), expire_first)
                ring_log.update_ring_log(str(tmp_path), lambda r: r.append(
                    {"time": "2026-09-27T10:00:00+02:00", "photo": "squillo_20260927_100000.jpg", "outcome": "missed"}))
                rig.hub.stats.update(last_photo="squillo_20260927_100000.jpg", last_photo_v=2)
                await c.until("card.shadowRoot.querySelectorAll('.hist img').length === 2 && " + loaded)
                assert len(refused) == 2, refused
                srcs = await c.page.evaluate(
                    "[...card.shadowRoot.querySelectorAll('.hist img, .still, #photo img')].map((i) => i.src)")
                assert not set(srcs) & set(refused), (srcs, refused)
                await c.page.evaluate("card.shadowRoot.querySelectorAll('.hist button')[0].click()")
                await c.until("((i) => i.complete && i.naturalWidth > 0)(card.shadowRoot.querySelector('dialog.photo img'))")
                opened = await c.page.evaluate("card.shadowRoot.querySelector('dialog.photo img').src")
                assert "squillo_20260927_100000.jpg" in opened and opened not in srcs + refused, (opened, srcs)
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("idle", [None, "standby"])
def test_idle_picture_standby_keeps_ring_photo_out_of_the_scene(monkeypatch, engine, tmp_path, idle):  # noqa: F811
    """`idle_picture: standby`: at rest neither the scene's still nor the compact photo button
    shows the last ring (the doorbell icon does); the photo stays in the history drawer.
    Default (`last_ring`): both show it, as before."""
    (tmp_path / "squillo_20260927_090000.jpg").write_bytes(PNG_1PX)
    ring_log.update_ring_log(str(tmp_path), lambda r: r.append(
        {"time": "2026-09-27T09:00:00+02:00", "photo": "squillo_20260927_090000.jpg", "outcome": "missed"}))
    js = """(() => { const r = card.shadowRoot, src = (s) => r.querySelector(s).getAttribute('src');
      return { hist: src('.hist img'), still: src('.still'), pic: src('#photo img'),
               icon: getComputedStyle(r.querySelector('#photo ha-icon')).display }; })()"""

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
            await rig.register()
            async with Card(rig, engine, query=f"&idle_picture={idle}" if idle else "") as c:
                await c.until("card.shadowRoot.querySelector('.hist img')?.getAttribute('src')")
                st = await c.page.evaluate(js)
                assert "squillo_20260927_090000.jpg" in st["hist"], st
                if idle == "standby":
                    assert st["still"] is None and st["pic"] is None and st["icon"] != "none", st
                else:
                    assert st["still"] == st["pic"] == st["hist"] and st["icon"] == "none", st
                assert not rig.services and not rig.peer.got(is_("INVITE"))
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_video_webcodecs_primo_fotogramma_subito(monkeypatch, engine):  # noqa: F811
    """Dal campo (2026-09-27): INVITE, 200 OK a 1,1 s, primo IDR a ~2 s, ma il video in card
    a 4-6 s (stream di HA) e la targa chiude a ~10 s. Con WebCodecs il canvas dipinge il
    primo IDR entro 300 ms da quando lo manda la targa, senza aprire /av; allo squillo,
    con la card che si collega a GOP già iniziato, il server le rimanda il GOP corrente e
    il fotogramma arriva senza aspettare il prossimo IDR (3 s). Con -s stampa i tempi."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")
    aus = hm.access_units(45)  # IDR ogni 3 s, come la targa
    monkeypatch.setattr(hm, "access_units", lambda gop=15: aus)
    tl = {}

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()

            async def on_invite(peer, inv):
                tl["invite"] = time.time()
                peer.reply(inv, 100, "Trying")
                peer.reply(inv, 180, "Ringing")
                await asyncio.sleep(1.0)                   # il cloud: 200 OK dopo ~1 s
                peer.pending_invite = None
                peer.reply(inv, 200, "OK", body=peer.sdp())
                tl["ok200"] = time.time()
                rig.start_media(inv.body)                  # IDR subito dopo il 200 OK
            rig.peer.on_invite = on_invite

            async with Card(rig, engine) as c:
                await c.until(IDLE)
                tl["tap"] = time.time()
                await c.tap("view")                        # "Vedi esterno"
                await c.until("info().player?.frames > 0", 8)
                p = (await c.info())["player"]
                idr = rig.panel_media.idr_at
                ms = lambda t: f"{(t - tl['tap']) * 1000:6.0f} ms"  # noqa: E731
                print(f"\n[Vedi esterno, dal tocco]  INVITE {ms(tl['invite'])}  WS aperto {ms(p['ws'] / 1000)}"
                      f"  200 OK {ms(tl['ok200'])}  IDR targa {ms(idr)}  primo NAL {ms(p['nal'] / 1000)}"
                      f"  primo fotogramma {ms(p['frame'] / 1000)}  ->  IDR->canvas {p['frame'] - idr * 1000:.0f} ms")
                assert p["frame"] - idr * 1000 < 300, (p, await c.dump_diag())
                await asyncio.sleep(1)
                t = await c.T()
                assert (await c.info())["player"]["frames"] >= 10, await c.dump_diag()
                assert await c.page.evaluate(  # dipinto davvero: 320x240 e non nero
                    "(() => { const cv = card._player.canvas, d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;"
                    " return cv.width === 320 && cv.height === 240 && d.some((v) => v > 128); })()")
                assert await c.page.evaluate(GEOMETRY) == {"canvas_fills_media": True, "ratio_4_3": True,
                                                            "open_over_video": True, "open_on_top": True}
                assert t["av"] == [] and "live" not in t["created"] and t["ws"] == 1, t
                assert rig.hub._stream_viewers == 0
                await c.tap("hangup")
                await c.until(IDLE + " && T.wsClosed === 1 && !info().player")

                # Squillo: la card vede "ringing" (e apre il WS) 0,5 s dopo che la targa ha
                # mandato l'IDR; il prossimo sarebbe fra 2,5 s.
                rig.state_override = "idle"
                rig.ring()
                r183 = await rig.peer.wait_for(is_(code=183))
                rig.start_media(r183.body)
                await wait_until(lambda: rig.panel_media.idr_at, 3, "IDR della targa")
                await asyncio.sleep(0.5)
                rig.state_override = None
                await c.until("info().player?.frames > 0", 8)
                p = (await c.info())["player"]
                print(f"[Squillo, WS aperto {(p['ws'] / 1000 - rig.panel_media.idr_at) * 1000:.0f} ms dopo l'IDR]"
                      f"  WS->primo fotogramma {p['frame'] - p['ws']:.0f} ms")
                assert p["frame"] - p["ws"] < 300, (p, await c.dump_diag())
                rig.peer.request("CANCEL", "ring-1", 1, "pnl")
                await c.until(IDLE + " && T.wsClosed === 2 && !info().player")
                t = await c.T()
                assert t["av"] == [] and t["ws"] == 2 and not t["errors"], t
                assert len(rig.peer.got(is_("INVITE"))) == 1  # solo la nostra "Vedi esterno"
    run(s())


# The card's _pcmSink on a fake AudioContext at 48 kHz (WebKit of Playwright has no Web Audio): one second of a 440 Hz sine in 20 ms
# packets, then two packets after the playout clock ran dry.
PCM_SINK = """(() => {
  const starts = [], srcs = [], lp = {};
  let now = 0;
  const ctx = { sampleRate: 48000, get currentTime() { return now; },
    createBiquadFilter: () => Object.assign(lp, { frequency: {}, connect: (g) => (lp.to = g) }),
    createBuffer: (c, n, r) => { const d = new Float32Array(n); return { sampleRate: r, duration: n / r, getChannelData: () => d }; },
    createBufferSource: () => { const s = { connect: (d) => (s.to = d), start: (t) => starts.push(t) }; srcs.push(s); return s; } };
  const sink = card._pcmSink(ctx, "GAIN");
  const pkt = (k) => { const b = new ArrayBuffer(321), v = new DataView(b); v.setUint8(0, 1);
    for (let i = 0; i < 160; i++) v.setInt16(1 + 2 * i, Math.round(16000 * Math.sin(2 * Math.PI * 440 * (k * 160 + i) / 8000)), true);
    return b; };
  for (let k = 0; k < 50; k++) sink({ data: pkt(k) });
  const out = srcs.flatMap((s) => [...s.buffer.getChannelData(0)]);
  let jump = 0;
  for (let i = 1; i < out.length; i++) jump = Math.max(jump, Math.abs(out[i] - out[i - 1]));
  const gapless = starts.slice(1).every((t, i) => Math.abs(t - starts[i] - srcs[i].buffer.duration) < 1e-9);
  now = 10; sink({ data: pkt(50) }); const ahead1 = starts.at(-1) - now;
  now = 20; sink({ data: pkt(51) }); const ahead2 = starts.at(-1) - now;
  return { n: out.length, rates: [...new Set(srcs.map((s) => s.buffer.sampleRate))], jump, gapless,
           first: starts[0], ahead1, ahead2, lp: [lp.type, lp.frequency?.value],
           chain: srcs.every((s) => s.to === lp) && lp.to === "GAIN" };
})()"""


def test_voice_resampled_continuously_low_passed_growing_buffer(monkeypatch, engine):  # noqa: F811
    """#53: each 20 ms packet was an 8 kHz AudioBuffer resampled by the browser on its own,
    a click at every edge. The sink now resamples to the context's rate across packets (no
    jump bigger than the sine's own slope, exactly 6 samples per input sample), plays through
    a 3.6 kHz low-pass, starts 120 ms ahead and adds 40 ms at every underrun."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            async with Card(rig, engine) as c:
                r = await c.page.evaluate(PCM_SINK)
                assert r["rates"] == [48000] and abs(r["n"] - 48000) <= 1, r
                assert r["jump"] < 0.035, r  # 440 Hz at 0.49 full scale: max slope 0.028 per 48 kHz sample
                assert r["gapless"] and r["chain"] and r["lp"] == ["lowpass", 3600], r
                assert r["first"] == pytest.approx(0.12) and r["ahead1"] == pytest.approx(0.16), r
                assert r["ahead2"] == pytest.approx(0.2), r
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)  # WebKit di Playwright: niente WebCodecs
@pytest.mark.parametrize("ios", [False, True])
def test_decoder_gets_avc_description_and_length_prefixed_nals(monkeypatch, engine, ios):  # noqa: F811
    """#53: on iPhone the picture smeared between keyframes with Annex B input. The decoder
    is configured with an avcC description built from SPS/PPS, chunks carry a 4-byte length
    instead of a start code (the key chunk is the IDR alone), and low-latency mode is off
    on iOS only. Frames still decode."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine, query="&ios" if ios else "") as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 20", 8)  # past a second IDR
                t = await c.T()
                cfg = t["vdCfg"][0]
                d = cfg["desc"]
                assert len(t["vdCfg"]) == 1, t["vdCfg"]  # same SPS/PPS at every IDR: no reconfigure
                assert d[:1] == [1] and d[4:6] == [0xFF, 0xE1], cfg
                assert cfg["codec"] == "avc1." + "".join(f"{b:02X}" for b in d[1:4]), cfg
                sps_len = d[6] << 8 | d[7]
                assert d[8] & 0x1F == 7 and d[8 + sps_len] == 1 and d[11 + sps_len] & 0x1F == 8, cfg
                assert cfg["latency"] is (not ios), cfg
                # SD H.264 without VUI colour info: browsers would assume BT.709
                assert cfg["cs"] == {"primaries": "smpte170m", "transfer": "smpte170m", "matrix": "smpte170m", "fullRange": False}, cfg
                kind, *head = t["chunk"]
                assert kind == "key" and head[:4] != [0, 0, 0, 1] and head[4] & 0x1F == 5, t["chunk"]
                assert t["av"] == [] and not t["errors"], t
                await c.tap("hangup")
                await c.until(IDLE)
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)  # WebKit di Playwright: niente WebCodecs
def test_decoder_lento_a_partire_non_perde_il_primo_gop(monkeypatch, engine):  # noqa: F811
    """#130: in CI il decoder software parte lento e la coda passa 8 chunk prima del primo
    fotogramma; il player buttava i P fino al prossimo IDR e il video partiva un GOP dopo
    (3 s). Il decoder finto tiene la coda per 1 s: poi il video deve andare avanti, non
    fermarsi a IDR + 8 P fino all'IDR dopo."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")
    aus = hm.access_units(45)  # IDR ogni 3 s, come la targa
    monkeypatch.setattr(hm, "access_units", lambda gop=15: aus)

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine, query="&slowwc") as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                await c.until("info().player?.frames >= 20", 2)  # ben prima dell'IDR dopo (3 s)
                t, p = await c.T(), (await c.info())["player"]
                assert p["resets"] == 0 and t["av"] == [] and not t["errors"], (p, t, await c.dump_diag())
                await c.tap("hangup")
                await c.until(IDLE)
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)  # WebKit di Playwright: niente WebCodecs
def test_pacchetto_perso_a_meta_gop_niente_video_smerigliato(monkeypatch, engine):  # noqa: F811
    """Dal campo (40515 via cloud, 2026-09-28): un pacchetto RTP perso per strada dava un NAL
    col buco e il video smerigliato (righe nere, sbavate) fino all'IDR dopo, ~3 s. La targa
    finta perde un pacchetto a metà GOP: alla card non arriva nessun NAL col buco né P che
    riferiscano il fotogramma perso (il canvas resta fermo sull'ultimo buono), HA chiede
    subito un keyframe (INFO nel dialogo) e al prossimo IDR i fotogrammi riprendono."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")
    aus = hm.access_units(45)  # IDR ogni 3 s, come la targa
    monkeypatch.setattr(hm, "access_units", lambda gop=15: aus)
    ok_nals = {n for au in aus for n in au}
    seen: list = []  # NAL usciti dal server (frame_sink: gli stessi del WS) e le perdite, in ordine
    orig_lost = media.RTPVideoProtocol._lost
    monkeypatch.setattr(media.RTPVideoProtocol, "_lost",
                        lambda self, why: (seen.append(why), orig_lost(self, why)))

    async def s():
        async with Rig(monkeypatch, "tls", http=True) as rig:  # tls: INFO accettato solo con la Route giusta
            media.video_proto.frame_sink = seen.append  # il rig non avvia il frame grabber
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                pm = rig.panel_media
                pm.drop = {pm.sent + 20}                     # un pacchetto, fra poco, a metà GOP
                await wait_until(lambda: pm.sent > max(pm.drop), 5, "pacchetto perso")
                n_info = len(rig.peer.got(is_("INFO")))
                frames = (await c.info())["player"]["frames"]
                await wait_until(lambda: len(rig.peer.got(is_("INFO"))) == n_info + 1, 2, "INFO keyframe")
                info = rig.peer.got(is_("INFO"))[-1]
                assert "picture_fast_update" in info.body and info not in rig.peer.dropped
                await c.until(f"info().player?.frames > {frames} + 10", 6)  # riparte dall'IDR
                t, p = await c.T(), (await c.info())["player"]
                assert p["resets"] == 0 and t["av"] == [] and not t["errors"], (p, t)
                await c.tap("hangup")
                await c.until(IDLE)
    run(s())
    nals = [x for x in seen if isinstance(x, bytes)]
    assert nals and all(n in ok_nals for n in nals), "NAL col buco emesso"
    i = next(k for k, x in enumerate(seen) if isinstance(x, str))   # la perdita
    after = [x[0] & 0x1F for x in seen[i:] if isinstance(x, bytes)]
    assert 5 in after and 1 not in after[:after.index(5)], after[:20]  # niente P prima dell'IDR
    assert 1 in after[after.index(5):], "video fermo dopo l'IDR"


def test_layout_sotto_video_sopra_tasti_sotto(monkeypatch, engine, tmp_path):  # noqa: F811
    """`layout: sotto`: da fermo la cronologia è una lista sotto la riga (niente palco 4:3,
    niente foto grande); in diretta il video 4:3 sta sopra la riga e i tasti restano
    sotto, fuori dal video. Stessi posti fissi: "Vedi esterno" → "Riaggancia"."""
    ring_log.update_ring_log(str(tmp_path), lambda r: r.append(
        {"time": "2026-09-27T10:15:00+02:00", "photo": "squillo_20260927_101500.jpg", "outcome": "answered"}))
    (tmp_path / "squillo_20260927_101500.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    js = """(() => { const r = card.shadowRoot, q = (s) => r.querySelector(s).getBoundingClientRect();
      const m = q('.media'), t = q('#talk'), o = q('#open');
      return { media: m.height, ratio_4_3: Math.abs(m.width / m.height - 4 / 3) < 0.02, drawer: q('.drawer').height,
               still: getComputedStyle(r.querySelector('.still')).display,
               below: t.y >= m.y + m.height && o.y >= m.y + m.height,
               hangup: r.querySelector('#hangup').hidden, view: r.querySelector('#view').hidden }; })()"""

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
            await rig.register()
            rig.peer.on_invite = answer_200
            async with Card(rig, engine, layout="sotto") as c:
                await c.until(IDLE + " && card.shadowRoot.querySelectorAll('.hist button').length === 1")
                assert await c.page.evaluate("card.getAttribute('layout')") == "sotto"
                assert (await c.page.evaluate(js))["media"] == 0
                await c.tap("photo")                     # lista sotto la riga, senza foto grande
                await c.until("card.shadowRoot.querySelector('.drawer').getBoundingClientRect().height > 40")
                st = await c.page.evaluate(js)
                assert st["still"] == "none" and not st["ratio_4_3"] and st["media"] > 40, st
                await c.tap("photo")
                await c.until("card.shadowRoot.querySelector('.media').getBoundingClientRect().height === 0")
                await c.tap("view")
                await c.until("info().pill === 'In chiamata' && info().video !== 'auto'")
                st = await c.page.evaluate(js)
                assert st["ratio_4_3"] and st["below"] and not st["hangup"] and st["view"], st
                await c.tap("hangup")
                await c.until(IDLE)
                assert (await c.page.evaluate(js))["media"] == 0
                assert rig.services == ["vimar_intercom.call", "vimar_intercom.hangup"]
                assert not (await c.T())["errors"]
    run(s())


# Bottoni visibili nella riga, nell'ordine in cui stanno sullo schermo.
POP_BUTTONS = """(() => { const r = card.shadowRoot;
  return [...r.querySelectorAll('.row button')].filter((b) => b.getClientRects().length)
    .sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x)
    .map((b) => [b.id, Math.round(b.getBoundingClientRect().height)]); })()"""


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_tocco_apre_e_chiama_tre_tasti(monkeypatch, engine):  # noqa: F811
    """`layout: popup`: compatta finché non si tocca; il tocco apre il popup a tutto schermo
    (iPhone) e fa "Vedi esterno"; tasti Parla / Apri / Riaggancia da 44 px, senza "Vedi esterno";
    "Riaggancia" chiude anche il popup."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            async with Card(rig, engine, layout="popup") as c:
                await c.until(IDLE)
                assert not (await c.info())["pop"]
                assert await c.page.evaluate("card.shadowRoot.querySelector('.media').getClientRects().length") == 0
                await c.page.evaluate("card.shadowRoot.querySelector('.name').click()")
                await c.until("info().pill === 'In chiamata' && info().video !== 'auto' && info().pop")
                dlg = await c.page.evaluate("(() => { const r = card._pop.getBoundingClientRect(); return [r.width, r.height]; })()")
                assert dlg[0] == 366 and dlg[1] < 844, dlg  # pannello con margini, non a tutto schermo
                btns = await c.page.evaluate(POP_BUTTONS)
                assert [i for i, _ in btns] == ["talk", "open", "hangup"] and all(h >= 56 for _, h in btns), btns
                over = await c.page.evaluate("""(() => { const r = card.shadowRoot, q = (s) => r.querySelector(s).getBoundingClientRect();
                  const m = q('.media'), t = q('#talk'); return t.y + t.height <= m.y + m.height && Math.abs(m.width - 366) < 1; })()""")
                assert over, "vetro sul video: tasti dentro il video, video a tutta larghezza"
                await c.tap("hangup")
                await c.until(IDLE + " && !info().pop")
                assert rig.services == ["vimar_intercom.call", "vimar_intercom.hangup"], rig.services
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("size", [(640, 480), (1280, 720)])
@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_tema_chiaro_pannello_scuro_tasti_sotto(monkeypatch, engine, size):  # noqa: F811
    """Tema chiaro di HA e video di risoluzione vera: il popup resta un pannello scuro tondo centrato su fondo semitrasparente, il
    video 4:3 sta a tutta larghezza del pannello, i tasti (bianchi) in una barra SOTTO
    il video, mai sopra."""
    js = """(() => { const r = card.shadowRoot, q = (s) => r.querySelector(s).getBoundingClientRect();
      const m = q('.media'), row = q('.row'), d = q('dialog.pop'), lab = r.querySelector('#hangup .lbl');
      return { bg: getComputedStyle(r.querySelector('dialog.pop')).backgroundColor,
               backdrop: getComputedStyle(r.querySelector('dialog.pop'), '::backdrop').backgroundColor, label: getComputedStyle(lab).color,
               dlg: [d.width, d.height], media: [m.width, m.height], full: Math.abs(m.width / m.height - 4 / 3) < 0.02,
               tall: Math.abs(m.width - d.width) < 1, below: [...r.querySelectorAll('.row button')].filter((b) => b.getClientRects().length)
                 .every((b) => b.getBoundingClientRect().bottom <= m.y + m.height + 0.5),
               centered: Math.abs(d.y + d.height / 2 - innerHeight / 2) < 2 }; })()"""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup") as c:
                await c.page.add_style_tag(content=":root, body { --card-background-color:#fdfbf7; --primary-background-color:#fdfbf7;"
                                                    " --primary-text-color:#212121; --secondary-text-color:#727272; background:#fdfbf7 }")
                await c.until(IDLE)
                rig.ring("ring-light")
                await c.until("info().pop && info().video !== 'auto'")
                await c.page.evaluate(f"(() => {{ const cv = card.shadowRoot.querySelector('#video canvas');"
                                      f" cv.width = {size[0]}; cv.height = {size[1]}; }})()")
                await asyncio.sleep(0.3)
                st = await c.page.evaluate(js)
                assert st["bg"] == "rgb(17, 17, 17)" and st["label"] == "rgb(255, 255, 255)", st
                assert st["backdrop"] == "rgba(15, 15, 20, 0.5)", st  # semitrasparente: si intravede la dashboard
                assert st["dlg"][0] == 366 and st["tall"] and st["below"] and st["centered"], st
    run(s())



@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_scorciatoie_sulla_card_compatta(monkeypatch, engine):  # noqa: F811
    """Le scorciatoie (lock → unlock, button → press) stanno sulla card compatta, chiedono il
    secondo tocco (confirm_open) e non aprono il popup né chiamano la targa. Nel popup la prima è
    "Apri": restano visibili solo le altre, in fila sotto la barra."""
    tap = "(i) => card.shadowRoot.querySelectorAll('.sc button')[i].click()"
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup", shortcuts="lock.vimar_intercom_serratura,button.garage") as c:
                await c.until(IDLE)
                labels = await c.page.evaluate("[...card.shadowRoot.querySelectorAll('.sc button .lbl')].map((b) => b.textContent)")
                assert labels == ["Apri", "Garage"], labels
                for i in (0, 1):
                    await c.page.evaluate(f"({tap})({i})")
                    await c.until(f"card.shadowRoot.querySelectorAll('.sc button .lbl')[{i}].textContent === 'Tocca ancora'")
                    assert len(rig.services) == i  # il primo tocco arma soltanto
                    await c.page.evaluate(f"({tap})({i})")
                    await c.until(f"card.shadowRoot.querySelectorAll('.sc button .lbl')[{i}].textContent === 'Aperto'")
                assert rig.services == ["lock.unlock", "button.press"], rig.services
                assert not (await c.info())["pop"] and not rig.peer.got(is_("INVITE"))
                await c.page.evaluate("card.shadowRoot.querySelector('.name').click()")
                await c.until("info().pop")
                shown = await c.page.evaluate("[...card.shadowRoot.querySelectorAll('.sc button')].map((b) => b.getClientRects().length > 0)")
                assert shown == [False, True], shown
                assert not (await c.T())["errors"]
    run(s())



@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_squillo_rifiuta_e_ascolto_chiuso(monkeypatch, engine):  # noqa: F811
    """Allo squillo il tasto rosso del popup è "Rifiuta" e manda `decline` (603: smette di
    suonare tutta la casa) e chiude il popup. Chiuso il popup con la X, `listen_on_ring` non
    riapre l'ascolto finché suona."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup", listen_on_ring=True) as c:
                await c.until(IDLE)
                rig.ring("ring-ascolto")
                await c.until("info().pop && info().listen")
                await c.tap("x")
                await c.until("!info().pop && !info().listen")
                await asyncio.sleep(1)
                assert not (await c.info())["listen"] and not rig.services
                await c.page.evaluate("card._openPop()")
                await c.until("info().pop && card.shadowRoot.querySelector('#hangup .lbl').textContent === 'Rifiuta'")
                await c.tap("hangup")
                await c.until("!info().pop")
                await rig.peer.wait_for(is_(code=603, cid="ring-ascolto"))
                assert rig.services == ["vimar_intercom.decline"], rig.services
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_overlay_squillo_rifiuta(monkeypatch, engine):  # noqa: F811
    """Overlay allo squillo: la 4ª cella è "Rifiuta" e manda `decline` (la targa riceve 603)."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="overlay") as c:
                await c.until(IDLE)
                rig.ring("ring-overlay")
                await c.until("info().pill === 'Suonano alla porta' && card.shadowRoot.querySelector('#hangup .lbl').textContent === 'Rifiuta'")
                assert await c.page.evaluate("!card.shadowRoot.querySelector('#hangup').hidden")
                await c.tap("hangup")
                await rig.peer.wait_for(is_(code=603, cid="ring-overlay"))
                assert rig.services == ["vimar_intercom.decline"], rig.services
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_overlay_adatta_riempi_si_ricorda(monkeypatch, engine):  # noqa: F811
    """Adatta (contain, predefinito) / Riempi (cover): il tondo alterna object-fit di video e foto,
    la scelta sta in localStorage e sopravvive a un rerender."""
    fit = "getComputedStyle(card.shadowRoot.querySelector('#video canvas')).objectFit"
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="overlay") as c:
                await c.until(IDLE)
                rig.ring("ring-fit")
                await c.until("info().pill === 'Suonano alla porta' && info().video !== 'auto'")
                assert await c.page.evaluate(fit) == "contain"
                assert await c.page.evaluate("getComputedStyle(card.shadowRoot.querySelector('.still')).objectFit") == "contain"
                await c.tap("fit")
                assert await c.page.evaluate(fit) == "cover"
                assert await c.page.evaluate("card.shadowRoot.querySelector('#fit').getAttribute('aria-pressed')") == "true"
                assert await c.page.evaluate("card.shadowRoot.querySelector('#fit').title") == "Adatta video"
                await c.page.evaluate("card.hass = card._hass")  # rerender
                assert await c.page.evaluate(fit) == "cover"
                assert await c.page.evaluate("localStorage.getItem('vimar_intercom_card_fit')") == "cover"
                await c.tap("fit")
                assert await c.page.evaluate(fit) == "contain"
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("style", ["pillola", "tile"])
@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_card_compatta_stili(monkeypatch, engine, style):  # noqa: F811
    """`compact_style` pillola/tile: la card compatta si disegna, i tasti chiamano i servizi giusti
    (Apri: due tocchi → unlock; a riposo Vedi = popup + "Vedi esterno" solo nel tile) e il tocco su un tasto non apre il popup.
    Mentre suona: nome "Suonano alla porta", "Tocca per vedere · mm:ss", Rispondi; solo il tile ha Rifiuta (decline)."""
    vis = "(id) => card.shadowRoot.querySelector(id)?.getClientRects().length > 0"
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup", compact=style) as c:
                await c.until(IDLE)
                assert await c.page.evaluate("card.getAttribute('compact')") == style
                h = await c.page.evaluate("card._card.getBoundingClientRect().height")
                assert (h == 64) if style == "pillola" else (h > 90), h
                assert await c.page.evaluate(f"({vis})('.sc button')") and not await c.page.evaluate(f"({vis})('#talk')")
                await c.page.evaluate("card.shadowRoot.querySelector('.sc button').click()")  # arma
                await c.page.evaluate("card.shadowRoot.querySelector('.sc button').click()")  # conferma
                await c.until("card._card.querySelector('.sc button.ok')")
                assert rig.services == ["lock.unlock"] and not (await c.info())["pop"], rig.services
                if style == "tile":
                    assert await c.page.evaluate(f"({vis})('#view')") and await c.page.evaluate(f"({vis})('#hist')")
                rig.ring("ring-compatta")
                await c.until("info().pop")
                await c.tap("x")
                await c.until("!info().pop && info().pill.startsWith('Tocca per vedere · 00:')")
                assert await c.page.evaluate("card.shadowRoot.querySelector('.name').textContent") == "Suonano alla porta"
                assert await c.page.evaluate(f"({vis})('#talk')") and not await c.page.evaluate(f"({vis})('#view')")
                assert await c.page.evaluate(f"({vis})('#hangup')") == (style == "tile")
                if style == "tile":
                    await asyncio.sleep(1)  # past the close guard (CLOSE_GUARD_MS)
                    await c.tap("hangup")
                    await rig.peer.wait_for(is_(code=603, cid="ring-compatta"))
                    assert rig.services == ["lock.unlock", "vimar_intercom.decline"] and not (await c.info())["pop"], rig.services
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("style", ["pillola", "tile"])
@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_feedback_immediato_al_tocco(monkeypatch, engine, style):  # noqa: F811
    """Feedback senza aspettare HA: il primo tocco su Apri è "Tocca ancora" (warn), il secondo mette
    subito il tondo in "busy" e poi "ok" (Aperto), che dopo ~2 s torna normale; il tocco sulla card
    mette subito "Collegamento…" (data-pending)."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup", compact=style) as c:
                await c.until(IDLE)
                got = await c.page.evaluate("""(() => { const b = card.shadowRoot.querySelector('.sc button'), out = [];
                  b.click(); out.push([b.className, b.querySelector('.lbl').textContent]);
                  b.click(); out.push(b.className); return out; })()""")
                assert got == [["warn", "Tocca ancora"], "busy"], got
                await c.until("card.shadowRoot.querySelector('.sc button').className === 'ok'")
                await c.until("card.shadowRoot.querySelector('.sc button').className === ''", 4)
                assert rig.services == ["lock.unlock"], rig.services
                got = await c.page.evaluate("""(() => { card.shadowRoot.querySelector('.name').click();
                  return [info().pill, card._card.dataset.pending]; })()""")
                assert got == ["Collegamento…", "true"], got
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_impostazioni_citofono_righe_e_servizi(monkeypatch, engine):  # noqa: F811
    """L'ingranaggio apre "Impostazioni citofono": le righe delle entità presenti (stesso dispositivo della camera),
    l'interruttore chiama switch.turn_on/off, il ritardo select.select_option, il testo text.set_value;
    non-admin: solo Non disturbare, Segreteria e ritardo; entità mancante: riga assente."""
    rows = "[...card.shadowRoot.querySelectorAll('dialog.set .set-r')].map((r) => r.dataset.k)"
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup") as c:
                await c.until(IDLE)
                await c.until("!card.shadowRoot.querySelector('#cfgc').hidden")
                await c.page.evaluate("card.shadowRoot.querySelector('#cfgc').click()")
                assert await c.page.evaluate("card.shadowRoot.querySelector('dialog.set').open") and not (await c.info())["pop"]
                assert await c.page.evaluate(rows) == ["dnd", "vm", "delay", "text", "file"]
                await c.page.evaluate("card.shadowRoot.querySelector('dialog.set .set-c').click()")  # il padding non chiude
                assert await c.page.evaluate("card.shadowRoot.querySelector('dialog.set').open")
                assert await c.page.evaluate("card.shadowRoot.querySelector('[data-k=vm] small').textContent") == "Messaggio di Home Assistant"
                await c.page.evaluate("card.shadowRoot.querySelector('[data-k=dnd] button').click()")
                await c.page.evaluate("card.shadowRoot.querySelector('[data-k=vm] button').click()")
                await c.page.select_option("dialog.set [data-k=delay] select", "15")
                await c.page.fill("dialog.set [data-k=text] input", "Torniamo presto")
                await c.page.evaluate("card.shadowRoot.querySelector('[data-k=text] input').dispatchEvent(new Event('change'))")
                got = await c.page.evaluate("T.settings")
                assert got == [["switch", "turn_on", {"entity_id": "switch.vimar_intercom_non_disturbare"}],
                               ["switch", "turn_off", {"entity_id": "switch.vimar_intercom_segreteria"}],
                               ["select", "select_option", {"entity_id": "select.vimar_intercom_segreteria_ritardo", "option": "15"}],
                               ["text", "set_value", {"entity_id": "text.vimar_intercom_segreteria_testo_del_messaggio", "value": "Torniamo presto"}]], got
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_impostazioni_citofono_non_admin_e_righe_mancanti(monkeypatch, engine):  # noqa: F811
    rows = "[...card.shadowRoot.querySelectorAll('dialog.set .set-r')].map((r) => r.dataset.k)"
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="overlay", query="&noadmin&ents=dnd,delay,file,text") as c:
                await c.until(IDLE)
                await c.page.evaluate("card._openSettings()")
                assert await c.page.evaluate(rows) == ["dnd", "delay"], "senza admin: né testo né file; senza Segreteria: niente riga"
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
@pytest.mark.parametrize("layout,query,label", [("overlay", "&nofile", "Carica"), ("overlay", "", "Sostituisci"),
                                                ("popup", "&nofile", "Carica")])
def test_impostazioni_carica_il_file_audio(monkeypatch, engine, tmp_path, layout, query, label):  # noqa: F811
    """Sotto "File audio del messaggio": "Carica" se non c'è un file, "Sostituisci" se c'è. Il tasto apre la
    scelta del file, che va a /api/vimar_intercom/away_upload (la view vera), diventa il file del messaggio e il
    select si rilegge (homeassistant.update_entity). Un nome sbagliato dà l'errore e non salva niente.
    Il select mostra l'etichetta tradotta di "none", non il valore. Il click della scelta file non arriva alla
    card (in "popup" aprirebbe la diretta, e il suo `return false` annullava la scelta)."""
    import types
    file_row = "card.shadowRoot.querySelector('dialog.set [data-k=file]')"
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            entry = types.SimpleNamespace(entry_id="e1", options={})
            rig.hass.config = types.SimpleNamespace(media_dirs={"local": str(tmp_path)})
            # What views._loaded_entry asks Home Assistant for: the loaded entry of the domain.
            rig.hass.config_entries = types.SimpleNamespace(
                async_loaded_entries=lambda domain: [entry] if domain == "vimar_intercom" else [],
                async_update_entry=lambda e, options: setattr(e, "options", options))
            monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "")
            await rig.register()
            async with Card(rig, engine, layout=layout, query=query) as c:
                await c.until(IDLE)
                await c.page.evaluate("card._openSettings()")
                assert await c.page.evaluate(f"{file_row}.querySelector('.set-ub').textContent") == label
                assert await c.page.evaluate(f"{file_row}.querySelector('select').options[0].text") == "Nessuno (usa il testo)"
                assert await c.page.evaluate(f"{file_row}.querySelector('select').options[0].value") == "none"
                async with c.page.expect_file_chooser() as fc:
                    await c.page.locator("dialog.set [data-k=file] .set-ub").click()  # un tocco vero: senza, niente scelta del file
                await (await fc.value).set_files(files=[{"name": "ciao.txt", "mimeType": "text/plain", "buffer": b"x"}])
                await c.until("card.shadowRoot.querySelector('dialog.set .set-e').textContent !== ''")
                assert "mp3" in await c.page.evaluate("card.shadowRoot.querySelector('dialog.set .set-e').textContent")
                assert entry.options == {} and not (tmp_path / "citofono" / "messaggi").exists()
                await c.page.locator("dialog.set [data-k=file] input[type=file]").set_input_files(
                    files=[{"name": "Benvenuti.mp3", "mimeType": "audio/mpeg", "buffer": b"ID3audio"}])
                await c.until(f"{file_row}.querySelector('small')?.textContent === 'Caricato: Benvenuti.mp3'")
                saved = tmp_path / "citofono" / "messaggi" / "Benvenuti.mp3"
                assert saved.read_bytes() == b"ID3audio"
                assert entry.options == {"away_message_file": str(saved)} and R.AWAY_MESSAGE_FILE == str(saved)
                assert ["homeassistant", "update_entity", {"entity_id": "select.vimar_intercom_segreteria_file_audio"}] in await c.page.evaluate("T.settings")
                assert await c.page.evaluate("card.shadowRoot.querySelector('dialog.set .set-e').textContent") == ""
                assert not (await c.T())["errors"] and not (await c.info())["pop"]
                assert not [s for s in (await c.T())["calls"] if s.startswith("vimar_intercom.")]  # mai la targa
                await c.page.evaluate("""(() => { const st = {...card._hass.states};
                    st["select.vimar_intercom_segreteria_file_audio"] = {...st["select.vimar_intercom_segreteria_file_audio"], state: "unavailable"};
                    card.hass = {...card._hass, states: st}; })()""")
                assert await c.page.evaluate(f"{file_row}.querySelector('.set-ub').disabled")  # select non disponibile: niente carica
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_proporzioni_pc_e_telefono(monkeypatch, engine):  # noqa: F811
    """PC (1280x800): il pannello è il video 4:3 (al massimo 900 px, mai vuoto sotto); telefono (390x844): pannello alto, come prima.
    Riga in alto: pill di stato larga, ingranaggio, adatta/riempi, cronologia, X (da sinistra a destra, tondi da 44)."""
    js = """(() => { const r = card.shadowRoot, q = (s) => r.querySelector(s).getBoundingClientRect();
      const d = q('dialog.pop'), m = q('.media'), top = ['.badge', '#cfg', '#fit', '#log', '#x'].map((s) => q(s));
      return { dlg: [d.width, d.height], ratio: m.width / m.height, gap: d.bottom - m.bottom, fill: [m.width - d.width, m.height - d.height],
               order: top.every((b, i) => i === 0 || b.left >= top[i - 1].right - 0.5), sizes: top.slice(1).map((b) => [b.width, b.height]),
               inside: top.every((b) => b.top >= d.top && b.bottom <= d.bottom && b.right <= d.right) }; })()"""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup") as c:
                await c.until(IDLE)
                for w, h in ((1280, 800), (390, 844)):
                    await c.page.set_viewport_size({"width": w, "height": h})
                    rig.ring(f"ring-{w}")
                    await c.until("info().pop && info().video !== 'auto'")
                    await asyncio.sleep(0.3)
                    st = await c.page.evaluate(js)
                    assert st["order"] and st["inside"] and st["sizes"] == [[44, 44]] * 4, st
                    if w == 1280:
                        assert abs(st["ratio"] - 4 / 3) < 0.02 and st["gap"] <= 8 and 700 < st["dlg"][0] <= 900 and st["dlg"][1] < 760, st
                    else:
                        assert st["dlg"] == [366, 620] and st["fill"] == [0, 0], st
                    await c.tap("x")
                    await c.until("!info().pop")
                    rig.peer.request("CANCEL", f"ring-{w}", 1, "pnl")
                    await c.until(IDLE)
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_tastiera_e_anteprima_dell_editor(monkeypatch, engine):  # noqa: F811
    """La card compatta è un tasto (Invio la apre e fa "Vedi esterno"); nell'anteprima
    dell'editor apre il popup ma non chiama la targa."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            async with Card(rig, engine, layout="popup") as c:
                await c.until(IDLE)
                assert await c.page.evaluate("card._card.getAttribute('role')") == "button"
                await c.page.evaluate("card._card.focus()")
                await c.page.keyboard.press("Enter")
                await c.until("info().pop && info().pill === 'In chiamata'")
                await c.tap("x")
                await c.until(IDLE + " && !info().pop")
                assert rig.services == ["vimar_intercom.call", "vimar_intercom.hangup"], rig.services
                await asyncio.sleep(1)  # past the close guard (CLOSE_GUARD_MS)
                await c.page.evaluate("document.body.append(document.createElement('hui-card-preview').appendChild(card).parentNode)")
                await c.page.evaluate("card.shadowRoot.querySelector('.name').click()")
                await c.until("info().pop")
                await asyncio.sleep(1)
                assert rig.services == ["vimar_intercom.call", "vimar_intercom.hangup"], rig.services
                assert (await c.info())["pill"] == "Pronto"
    run(s())



@pytest.mark.parametrize("layout", ["overlay", "sotto"])
@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_scorciatoia_apri_e_cronologia_visibili_da_fermo(monkeypatch, engine, layout):  # noqa: F811
    """Anche negli altri layout, da fermo: la scorciatoia (la serratura) sta nella testata e apre
    con due tocchi senza chiamare la targa; il tasto cronologia è visibile, con nome."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout=layout) as c:
                await c.until(IDLE)
                st = await c.page.evaluate("""(() => { const r = card.shadowRoot, vis = (s) => r.querySelector(s).getClientRects().length > 0;
                  return { sc: vis('.sc button'), open_row: vis('#open'), hist: vis('#photo'), title: r.querySelector('#photo').title }; })()""")
                assert st == {"sc": True, "open_row": False, "hist": True, "title": "Cronologia squilli"}, st
                for _ in range(2):
                    await c.page.evaluate("card.shadowRoot.querySelector('.sc button').click()")
                await c.until("card.shadowRoot.querySelector('.sc button .lbl').textContent === 'Aperto'")
                assert rig.services == ["lock.unlock"] and not rig.peer.got(is_("INVITE"))
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_cronologia_non_chiama_e_chiusura_riaggancia(monkeypatch, engine, tmp_path):  # noqa: F811
    """Il tasto cronologia apre il popup con il cassetto, senza mai chiamare la targa; una
    chiamata avviata dalla card e poi chiusa con la X riaggancia."""
    ring_log.update_ring_log(str(tmp_path), lambda r: r.append(
        {"time": "2026-09-27T10:15:00+02:00", "photo": "squillo_20260927_101500.jpg", "outcome": "answered"}))
    (tmp_path / "squillo_20260927_101500.jpg").write_bytes(b"\xff\xd8\xff\xd9")

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
            await rig.register()
            rig.peer.on_invite = answer_200
            async with Card(rig, engine, layout="popup") as c:
                await c.until(IDLE + " && !card.shadowRoot.querySelector('#photo').disabled")
                await c.tap("photo")
                await c.until("info().pop && card._card.dataset.drawer === 'true'")
                await asyncio.sleep(1)
                assert not rig.services and not rig.peer.got(is_("INVITE")) and (await c.T())["av"] == []
                await c.page.evaluate("card._pop.close()")
                await c.until("!info().pop")
                await asyncio.sleep(1)  # past the close guard (CLOSE_GUARD_MS)
                await c.page.evaluate("card.shadowRoot.querySelector('.name').click()")  # chiamata dalla card
                await c.until("info().pill === 'In chiamata' && info().pop")
                await c.tap("x")
                await c.until(IDLE + " && !info().pop")
                assert rig.services == ["vimar_intercom.call", "vimar_intercom.hangup"], rig.services
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_si_apre_allo_squillo_una_volta(monkeypatch, engine):  # noqa: F811
    """Allo squillo il popup si apre da solo, già in diretta con "Rispondi"; chiuso a mano
    non si riapre (e non riaggancia: la chiamata non è della card) finché non arriva un
    nuovo squillo."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup") as c:
                await c.until(IDLE)
                rig.ring("ring-1")
                await c.until("info().pop && info().video !== 'auto' && info().talk === 'Rispondi'")
                await c.tap("x")
                await c.until("!info().pop")
                await asyncio.sleep(1)
                assert not (await c.info())["pop"] and not rig.services
                rig.peer.request("CANCEL", "ring-1", 1, "pnl")
                await c.until(IDLE)
                rig.ring("ring-2")
                await c.until("info().pop")
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_close_tap_does_not_reach_the_compact_card(monkeypatch, engine):  # noqa: F811
    """On the iPhone the tap on X landed again on the compact card that took the popup's place:
    during the ring it hit "Answer" (the popup came back and the card answered), after the
    hang-up it hit the card (popup again, black, and a call to the panel). Right after the popup
    closes, taps on the compact card are ignored; the ring goes on and a later tap works."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup") as c:
                await c.until(IDLE)
                rig.ring("ring-x")
                await c.until("info().pop && info().talk === 'Rispondi'")
                await c.tap("x")
                await asyncio.sleep(0.05)
                await c.page.evaluate("tap('talk'); card.shadowRoot.querySelector('ha-card').click()")
                await asyncio.sleep(1)
                assert not (await c.info())["pop"] and not rig.services, rig.services
                assert rig.hub.is_ringing  # X is "not now": the ring goes on for everyone else
                await c.page.evaluate("card.shadowRoot.querySelector('ha-card').click()")  # a real tap later
                await c.until("info().pop")
                assert not rig.services, rig.services
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_closed_on_a_ring_stays_closed_for_that_ring(monkeypatch, engine):  # noqa: F811
    """X during the ring: no service call, and the popup does not come back by itself for that
    ring, whatever follows (a state flap, answered elsewhere, the end). A new ring opens it."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup") as c:
                await c.until(IDLE)
                rig.ring("ring-a")
                await c.until("info().pop && info().talk === 'Rispondi'")
                await c.tap("x")
                await c.until("!info().pop")
                for state in ("idle", None, "in_call", "idle"):  # flap, back to ringing, answered elsewhere
                    rig.state_override = state
                    await asyncio.sleep(0.6)
                    assert not (await c.info())["pop"], state
                rig.state_override = None
                rig.peer.request("CANCEL", "ring-a", 1, "pnl")
                await c.until(IDLE)
                await asyncio.sleep(0.5)
                assert not (await c.info())["pop"] and not rig.services, rig.services
                rig.ring("ring-b")
                await c.until("info().pop")
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_waiting_for_video_is_not_a_black_box(monkeypatch, engine):  # noqa: F811
    """A live view whose first frame has not arrived (here the panel never answers) shows the
    doorbell icon and "In attesa del video…", not an empty black canvas."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup") as c:
                await c.until(IDLE)
                await c.page.evaluate("card.shadowRoot.querySelector('.name').click()")
                await c.until("info().pop && info().video === 'canvas'")
                ph = await c.page.evaluate("""(() => { const p = card.shadowRoot.querySelector('.ph');
                  return [getComputedStyle(p).display, getComputedStyle(p, '::after').content]; })()""")
                assert ph == ["grid", '"In attesa del video…"'], ph
                await c.tap("x")
                await c.until("!info().pop && !card.shadowRoot.querySelector('ha-card').classList.contains('wait')")
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_layout_popup_opens_when_card_starts_in_call(monkeypatch, engine):  # noqa: F811
    """Notification "Answer": the app wakes up and the card goes from idle to in_call without
    having seen the ring, and the #citofono anchor may not arrive. The popup opens anyway, once;
    closed by hand it does not reopen and does not hang up a call that is not the card's."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup", webcodecs=False) as c:
                await c.until(IDLE)
                rig.state_override = "in_call"  # as if the automation had answered
                await c.until("info().pop")
                await c.tap("x")
                await c.until("!info().pop")
                await asyncio.sleep(1)
                assert not (await c.info())["pop"] and not rig.services
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_shortcut_names_and_icons_cannot_inject_markup(monkeypatch, engine):  # noqa: F811
    """A button entity's friendly_name and icon go into the shortcut tile: a name with markup or
    a quote-breaking one must stay text, and a malformed icon falls back to the default."""
    hostile = [
        ("button.garage", '<img src=x onerror=window.__xss=1>', "mdi:evil\" onmouseover=\"window.__xss=2"),
        ("button.garage2", 'x" data-pwn="1" y="', "mdi:ok-icon"),
    ]
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, layout="popup", shortcuts="button.garage,button.garage2") as c:
                await c.until(IDLE)
                await c.page.evaluate("""(h) => {
                  const st = {...card._hass.states};
                  for (const [id, name, icon] of h) st[id] = { state: "unknown", attributes: { friendly_name: name, icon } };
                  card.hass = {...card._hass, states: st};
                }""", hostile)
                await c.until("card.shadowRoot.querySelectorAll('.sc button').length === 2 && "
                              "card.shadowRoot.querySelectorAll('.sc button .lbl')[0].textContent.startsWith('<img')")
                r = await c.page.evaluate("""() => {
                  const bs = [...card.shadowRoot.querySelectorAll('.sc button')];
                  return { xss: window.__xss ?? null,
                    imgs: card.shadowRoot.querySelectorAll('.sc img').length,
                    labels: bs.map((b) => b.querySelector('.lbl').textContent),
                    attrs: bs.map((b) => [...b.attributes].map((a) => a.name).sort().join()),
                    icons: bs.map((b) => b.querySelector('ha-icon').getAttribute('icon')),
                    iconAttrs: bs.map((b) => [...b.querySelector('ha-icon').attributes].map((a) => a.name).sort().join()) };
                }""")
                assert r["xss"] is None and r["imgs"] == 0, r
                assert r["labels"] == [hostile[0][1], hostile[1][1]], r
                assert r["attrs"] == ["data-icon,data-label"] * 2, r
                assert r["icons"] == ["mdi:gesture-tap-button", "mdi:ok-icon"], r
                assert r["iconAttrs"] == ["aria-hidden,icon"] * 2, r
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_editor_visuale(monkeypatch, engine):  # noqa: F811
    """L'editor (getConfigElement, ha-form finto) manda `config-changed` con le sole chiavi
    diverse dai default; la card viva riceve setConfig e cambia layout e nome subito."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.page.evaluate("""(() => {
                  const ed = customElements.get('vimar-intercom-card').getConfigElement();
                  window.changed = [];
                  ed.addEventListener('config-changed', (e) => {  // come HA: editor e card ricevono la config nuova
                    changed.push(e.detail.config); ed.setConfig(e.detail.config); card.setConfig(e.detail.config); });
                  document.body.append(ed); ed.hass = card._hass; ed.setConfig({ type: 'custom:vimar-intercom-card' });
                  window.ed = ed; })()""")
                assert await c.page.evaluate("ed.querySelector('select[name=layout]').value") == "overlay"
                await c.page.select_option("ha-form select[name=layout]", "sotto")
                await c.page.fill("ha-form input[name=name]", "Portone")
                await c.page.press("ha-form input[name=name]", "Tab")
                got = await c.page.evaluate("({ changed, layout: card.getAttribute('layout'),"
                                            " name: card.shadowRoot.querySelector('.name').textContent })")
                assert got["changed"][-1] == {"type": "custom:vimar-intercom-card", "layout": "sotto", "name": "Portone"}
                assert got["layout"] == "sotto" and got["name"] == "Portone", got
                await c.page.select_option("ha-form select[name=layout]", "overlay")  # default: chiave via dallo YAML
                got = await c.page.evaluate("({ last: changed.at(-1), layout: card.getAttribute('layout') })")
                assert got["last"] == {"type": "custom:vimar-intercom-card", "name": "Portone"} and got["layout"] == "overlay"
                stub = await c.page.evaluate("customElements.get('vimar-intercom-card').getStubConfig(card._hass, ['camera.x'])")
                assert stub == {"camera": "camera.vimar_intercom_intercom", "name": "Citofono", "layout": "overlay", "history": 8}
                assert not (await c.T())["errors"] and not rig.services
    run(s())


def test_video_senza_webcodecs_stream_di_ha(monkeypatch, engine):  # noqa: F811
    """Senza VideoDecoder (WebKit di Playwright, Safari vecchio, HA in HTTP) o con un
    decoder che si rompe (Chromium, VideoDecoder finto che fallisce la configurazione al
    primo IDR): il riquadro torna alla picture-entity di HA su /av, senza errori, e la
    chiamata continua; a riposo il WS è chiuso."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine, badwc=True) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().video === 'live' && T.av.includes(200) && T.avBytes > 0", 8)
                assert not (await c.info())["player"]
                await c.tap("hangup")
                await c.until(IDLE + " && T.live === 0")
                t = await c.T()
                assert t["ws"] == t["wsClosed"] and not t["errors"], t
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
@pytest.mark.parametrize("gap", [0.3, 2.5], ids=["durante_l_attesa", "dopo_l_attesa"])
def test_seconda_vedi_esterno_subito_dopo_resta_su_webcodecs(monkeypatch, engine, gap):  # noqa: F811
    """Dal campo (iPhone su 5G, 2026-09-27): tre "Vedi esterno" di fila, la seconda 3 s dopo
    la fine della prima → card bianca (stream di HA aperto: «Stream opened (1 viewers)»).
    La targa manda RTP anche dopo il nostro BYE (dal cloud ~100 ms; qui la finta non
    smette mai): il server lo scartava? No: «First video RTP» ricontato da zero,
    depacketizzato e mandato al WS della card in attesa. E una chiamata nuova durante
    l'attesa (1,5 s) riusava player, WebSocket e decoder della chiamata prima.
    Ora: dopo stop_media niente RTP ai WS; chiamata nuova = player nuovo, il cui primo
    NAL è della chiamata nuova (dopo il suo 200 OK); canvas con fotogrammi, /av mai."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")
    tl = {}

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            orig = rig.peer.on_invite

            async def on_invite(peer, inv):
                await orig(peer, inv)
                tl["ok200"] = time.time()
            rig.peer.on_invite = on_invite

            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                await asyncio.sleep(0.5)
                await c.tap("hangup")
                await c.until("info().pill === 'Pronto'")
                await asyncio.sleep(0.3)
                assert media.video_proto.remote_addr is None
                rx = (await c.T())["rx"]
                await asyncio.sleep(0.5)  # la targa manda ancora: al WS della card in attesa niente
                assert (await c.T())["rx"] == rx, "RTP dopo il BYE arrivato alla card"
                assert media.video_proto._gop_msgs == [] and media.video_proto.pkt_count == 0
                await asyncio.sleep(max(0.0, gap - 0.8))
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                p = (await c.info())["player"]
                assert p["nal"] >= tl["ok200"] * 1000 - 5, "NAL della chiamata prima al player nuovo"
                await asyncio.sleep(1)
                t = await c.T()
                info = await c.info()
                assert info["video"] == "canvas" and info["player"]["frames"] >= 15, (info, t)
                assert info["player"]["resets"] == 0
                assert t["av"] == [] and "live" not in t["created"] and not t["errors"], t
                assert t["ws"] == 2 and t["wsClosed"] == 1, t  # un player per chiamata
                assert rig.hub._stream_viewers == 0
                assert len(rig.peer.got(is_("INVITE"))) == 2
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_decoder_rotto_riparte_dal_prossimo_idr_senza_stream_di_ha(monkeypatch, engine):  # noqa: F811
    """VideoDecoder che dà errore a metà (dati corrotti, riferimento perso: su iPhone
    VideoToolbox lo fa): niente stream di HA (bianco su 5G), decoder nuovo al prossimo
    IDR e i fotogrammi continuano sul canvas."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine, flakywc=True) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("T.wcBroken === 1", 8)
                n = (await c.info())["player"]["frames"]
                await c.until(f"info().player?.frames > {n} + 10", 5)  # IDR ogni 1 s qui
                t = await c.T()
                info = await c.info()
                assert info["video"] == "canvas" and info["player"]["resets"] == 1, (info, t)
                assert t["av"] == [] and "live" not in t["created"] and not t["errors"], t
                await c.tap("hangup")
                await c.until(IDLE)
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_cambio_vista_e_ritorno_a_chiamata_in_corso(monkeypatch, engine):  # noqa: F811
    """Dal campo: a chiamata in corso si va su un'altra vista e si torna: card bianca.
    HA stacca la card (disconnectedCallback: player chiuso) e la riattacca; il video
    deve tornare subito sul canvas (GOP corrente rimandato dal server), senza /av.
    Provato sia col solo rientro in pagina sia con `hass` rimesso da HA."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")
    aus = hm.access_units(45)  # IDR ogni 3 s, come la targa: il rientro cade a metà GOP
    monkeypatch.setattr(hm, "access_units", lambda gop=15: aus)

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                for i, rehass in enumerate((False, True)):
                    await asyncio.sleep(1.2)
                    await c.page.evaluate("card.remove()")
                    await asyncio.sleep(1)
                    assert not (await c.info())["player"]
                    t0 = time.time()
                    await c.page.evaluate("document.body.appendChild(card)")
                    if rehass:
                        await c.page.evaluate("card.hass = { ...card._hass }")
                    await c.until("info().player?.frames > 0", 3)
                    p = (await c.info())["player"]
                    print(f"\n[rientro {i + 1}] primo fotogramma {p['frame'] / 1000 - t0:.3f} s dopo il rientro,"
                          f" {p['frame'] - p['ws']:.0f} ms dal WS")
                    assert p["frame"] - p["ws"] < 300, p
                    await asyncio.sleep(1)
                    t = await c.T()
                    info = await c.info()
                    print(f"[rientro {i + 1}] fotogrammi in 1 s: {info['player']['frames']}")
                    assert info["video"] == "canvas" and info["player"]["frames"] >= 10, (info, t)
                    assert t["av"] == [] and "live" not in t["created"] and not t["errors"], t
                    assert t["ws"] == 2 + i and t["wsClosed"] == 1 + i, t
                assert rig.hub._stream_viewers == 0 and rig.hub.status == "in_call"
                await c.tap("hangup")
                await c.until(IDLE)
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_websocket_video_caduto_si_riapre_senza_stream_di_ha(monkeypatch, engine):  # noqa: F811
    """Il WebSocket del video cade a chiamata in corso (rete, HA che riparte): la card
    non passa allo stream di HA, lo riapre entro ~1 s, il server le rimanda il GOP
    corrente e i fotogrammi riprendono sul canvas."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                for ws in list(rig.audio_ws_clients):  # il server chiude il WS della card
                    await ws.close()
                await c.until("info().player?.resets === 1")
                n = (await c.info())["player"]["frames"]
                await c.until(f"T.ws === 2 && info().player?.frames > {n} + 10", 5)
                t = await c.T()
                info = await c.info()
                assert info["video"] == "canvas" and t["av"] == [] and "live" not in t["created"], (info, t)
                assert not t["errors"] and rig.hub.status == "in_call"
                await c.tap("hangup")
                await c.until(IDLE)
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_websocket_video_caduto_di_continuo_riapre_con_attesa_crescente(monkeypatch, engine):  # noqa: F811
    """HA fermo: il WebSocket del video cade a ogni riapertura. La card non riprova ogni
    secondo per sempre: 1, 2, 4… s (massimo 10), e da capo dopo una riapertura riuscita."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                # HA giù: auth/sign_path fallisce a ogni tentativo di riapertura
                # (`; 0`: Playwright chiama da solo un'espressione che vale una funzione)
                await c.page.evaluate("window._cw = card._player._hass.callWS; "
                                      "card._player._hass.callWS = async () => { throw new Error('HA giù'); }; 0")
                for ws in list(rig.audio_ws_clients):
                    await ws.close()
                await c.until("info().player?.resets === 1 && info().player?.wait === 1")
                t0 = time.monotonic()
                await c.until("info().player?.wait === 4", 6)     # 1 s → 2 s → 4 s
                assert 2.5 < time.monotonic() - t0 < 5, "non ha aspettato 1 + 2 s"
                assert (await c.T())["ws"] == 1
                await c.page.evaluate("card._player._hass.callWS = window._cw; 0")
                await c.until("T.ws === 2 && info().player?.wait === 0", 6)  # riaperto: da capo
                n = (await c.info())["player"]["frames"]
                await c.until(f"info().player?.frames > {n} + 5", 5)
                await c.tap("hangup")
                await c.until(IDLE)
                ws_n = (await c.T())["ws"]
                await asyncio.sleep(1.5)
                assert (await c.T())["ws"] == ws_n, "il player chiuso non deve riaprire"
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_due_card_sullo_stesso_squillo_una_risponde_l_altra_segue(monkeypatch, engine):  # noqa: F811
    """Due telefoni con la card sullo stesso squillo: uno risponde, l'altro passa a "In chiamata"
    senza più "Rispondi" (non si risponde due volte) e il suo "Riaggancia" chiude per entrambi."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine) as a, Card(rig, engine) as b:
                rig.ring("ring-two")
                for c in (a, b):
                    await c.until("info().pill === 'Suonano alla porta' && info().video === 'canvas'")
                await a.tap("talk")
                await a.until("info().pill === 'In chiamata'")
                await b.until("info().pill === 'In chiamata' && info().talk === 'Microfono'")
                await b.tap("talk")  # è "Microfono": non una seconda risposta
                await asyncio.sleep(1)
                assert rig.services.count("vimar_intercom.answer") == 1, rig.services
                assert len(rig.peer.got(is_(code=200, cid="ring-two"))) == 1
                await b.tap("hangup")
                await rig.peer.wait_for(is_("BYE", cid="ring-two"))
                for c in (a, b):
                    await c.until(IDLE + " && info().audio === 'off'")
                assert not (await a.T())["errors"] and not (await b.T())["errors"]
    run(s())


def test_riaggancia_mentre_la_targa_chiude_lascia_la_card_tranquilla(monkeypatch, engine):  # noqa: F811
    """"Riaggancia" nell'istante in cui la targa chiude la visione: la card torna a riposo una
    volta sola, senza errori né seconde chiamate, e "Vedi esterno" funziona subito dopo."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer()
            async with Card(rig, engine, webcodecs=False) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().pill === 'In chiamata'")
                rig.bye(rig.peer.got(is_("INVITE"))[0])  # il BYE della targa parte un attimo prima del tocco
                await c.tap("hangup")
                await c.until(IDLE)
                await asyncio.sleep(2)
                assert (await c.info())["err"] == ""
                assert rig.hub.status == "idle" and len(rig.peer.got(is_("INVITE"))) == 1
                rig.answer()
                await c.tap("view")
                await c.until("info().pill === 'In chiamata'")
                await c.tap("hangup")
                await c.until(IDLE)
                assert not (await c.T())["errors"]
    run(s())


# ─── Fill / Fit (#42) ────────────────────────────────────────────────────────

FIT_MODE = "card._videoBox.firstElementChild?.cfg?.fit_mode"


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_fill_fit_reaches_home_assistants_picture_card(monkeypatch, engine):  # noqa: F811
    """#42: without WebCodecs the live picture is HA's picture-entity card, whose <video>
    sits in its own shadow DOM where our object-fit cannot reach. The choice goes in as
    its fit_mode, and a toggle rebuilds the card. The button stays: the video's shape is
    not known on this path."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, webcodecs=False) as c:
                await c.until(IDLE)
                rig.state_override = "ringing"
                await c.until("info().video === 'live'", 3)
                assert await c.page.evaluate(FIT_MODE) == "contain"
                await c.page.evaluate("card._fit.click()")
                await c.until(f"{FIT_MODE} === 'cover'", 3)
                assert await c.page.evaluate("card._videoBox.firstElementChild.cfg.camera_view") == "live"
                assert not await c.page.evaluate("card._card.hasAttribute('data-fit-same')")
                assert not rig.services and not (await c.T())["errors"]
                assert not rig.peer.got(is_("INVITE"))
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_fill_fit_is_hidden_while_video_and_box_have_the_same_shape(monkeypatch, engine):  # noqa: F811
    """#42: the panel's 320x240 video in the default 4:3 box looks the same filled or
    fitted, so the button would seem to do nothing: it is hidden. A box of another shape
    (popup, overlay, a rotated phone) brings it back."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()

            async def on_invite(peer, inv):
                peer.reply(inv, 100, "Trying")
                peer.pending_invite = None
                peer.reply(inv, 200, "OK", body=peer.sdp())
                rig.start_media(inv.body)
            rig.peer.on_invite = on_invite

            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                shown = "getComputedStyle(card.shadowRoot.querySelector('#fit')).display !== 'none'"
                await c.until("card._card.hasAttribute('data-fit-same')", 3)
                assert not await c.page.evaluate(shown)
                await c.page.evaluate("card._videoBox.style.height = '60px'")    # a wide, flat box
                await c.until("!card._card.hasAttribute('data-fit-same')", 3)
                assert await c.page.evaluate(shown)
                assert not (await c.T())["errors"]
    run(s())
