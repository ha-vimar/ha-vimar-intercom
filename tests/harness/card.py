"""La card vera (www/vimar-intercom-card.js) in Chromium o WebKit (= Safari/iPhone), su
una pagina con un hass finto servita dal server vero di web.start: stato dall'hub
(/state, o rig.state_override), servizi verso l'hub (/svc), /av, /audio_ws e /rings veri.

Il riquadro video è un elemento finto che fa quello che fanno frontend + go2rtc: in
"live" apre /av e lo riapre da solo quando finisce, finché è in pagina. Con WebCodecs
(Chromium; WebKit di Playwright non ce l'ha) la card non lo usa dal vivo: decodifica i
NAL del WebSocket su un canvas (`info().video === "canvas"`). `?nowc` toglie
VideoDecoder (browser senza WebCodecs), `?badwc` ne mette uno che fallisce la
configurazione (codec non supportato): in entrambi i casi la card deve tornare a /av.
`?flakywc` ne mette uno che si rompe al 10° chunk (dati corrotti): la card resta sul
canvas e riparte dal prossimo IDR. `&swwc` forza hardwareAcceleration "prefer-software". `&slowwc` (via `query`)
ne mette uno che per il primo secondo tiene i chunk in coda (decoder software a freddo, come sul runner della CI, #130).
`?ios` dà alla pagina lo user agent di un iPhone. `?layout=sotto` (o popup) passa `layout` in setConfig. `ha-form` è un finto minimo (label +
input/select nativi, `value-changed` come quello vero) per provare l'editor visuale.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

CARD_JS = Path(__file__).resolve().parents[2] / "custom_components" / "vimar_intercom" / "www" / "vimar-intercom-card.js"

PAGE = """<!doctype html><html><head><meta name="viewport" content="width=390"></head>
<body><home-assistant></home-assistant><script>
window.T = { av: [], avBytes: 0, live: 0, created: [], rx: 0, ws: 0, sent: 0, frames: [],
             wsClosed: 0, calls: [], errors: [], gumDelay: 0, wcBroken: 0, gum: 0,
             wsOpenAt: 0, firstNalAt: 0, firstFrameAt: 0 };  // epoca in ms, del player corrente: latenza
// Diagnostica del player (stampata dai test che falliscono, #130): decode [t, key, coda], fotogrammi [t, coda],
// transizioni di _skip [t, skip, coda, _out], NAL ricevuti {t, nal, sps, pps, dec} (sps/pps/dec: lo stato del
// player PRIMA di quel NAL); t in ms dall'apertura del WebSocket (diag()).
const D = window.D = { dec: [], out: [], msg: [], skip: [], lastSkip: false };
const MSGS = 60;  // NAL ricevuti tenuti
const lim = (a, x, n = 40) => { if (a.length < n) a.push(x); };
// Perché il primo IDR non è stato decodificato: lo stato del player nel momento in cui è arrivato.
const why = () => { const i = D.msg.find((m) => m.nal === 5);
  return !i ? `nessun IDR nei primi ${MSGS} messaggi` : !(i.sps && i.pps) ? "il primo IDR è arrivato senza SPS/PPS nel player" : "il player aveva SPS+PPS al primo IDR"; };
window.diag = () => { const o = T.wsOpenAt, r = (t) => t && t - o, pl = window.card?._player, d = pl?._dec;
  return JSON.stringify({ cores: navigator.hardwareConcurrency, sw: location.search.includes("swwc"), state: d?.state,
    q: d?.decodeQueueSize, out: pl?._out, nal: r(T.firstNalAt), frame: r(T.firstFrameAt),
    why: why(), msg: D.msg.map((m) => ({ ...m, t: r(m.t) })), dec: D.dec.map(([t, k, q]) => [r(t), k, q]),
    frm: D.out.map(([t, q]) => [r(t), q]), skip: D.skip.map(([t, ...x]) => [r(t), ...x]) }); };
window.onerror = (m) => T.errors.push(String(m));
window.addEventListener("unhandledrejection", (e) => T.errors.push("REJ " + e.reason));
if (location.search.includes("insecure")) Object.defineProperty(window, "isSecureContext", { value: false });
if (location.search.includes("ios")) Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X)" });
if (window.VideoDecoder) {  // sonda di latenza (il primo fotogramma dipinto), configurazioni e primo chunk
  const VD = window.VideoDecoder;
  window.VideoDecoder = class extends VD {
    constructor(init) { super({ ...init, output: (f) => { T.firstFrameAt ||= Date.now(); lim(D.out, [Date.now(), this.decodeQueueSize]); init.output(f); } }); }
    configure(c) { if (location.search.includes("swwc")) c = { ...c, hardwareAcceleration: "prefer-software" };  // senza GPU, come in CI
      (T.vdCfg ||= []).push({ codec: c.codec, desc: [...new Uint8Array(c.description || [])], latency: c.optimizeForLatency, cs: c.colorSpace }); super.configure(c); }
    decode(c) { lim(D.dec, [Date.now(), c.type[0], this.decodeQueueSize]);
      if (!T.chunk) { const b = new Uint8Array(c.byteLength); c.copyTo(b); T.chunk = [c.type, ...b.slice(0, 5)]; } super.decode(c); }
  };
}
if (location.search.includes("nowc")) window.VideoDecoder = undefined;
if (location.search.includes("badwc") && window.VideoDecoder) {
  const VD = window.VideoDecoder;
  window.VideoDecoder = class extends VD {
    constructor(init) { super(init); this._err = init.error; }
    configure() { this._err(new DOMException("decoder finto", "NotSupportedError")); }
  };
}
if (location.search.includes("flakywc") && window.VideoDecoder) {  // si rompe al 10° chunk, una volta
  const VD = window.VideoDecoder;
  window.VideoDecoder = class extends VD {
    constructor(init) { super(init); this._err = init.error; this._n = 0; }
    decode(c) {
      if (++this._n === 10 && !T.wcBroken++) { super.close(); this._err(new DOMException("dati corrotti", "EncodingError")); return; }
      super.decode(c);
    }
  };
}
if (location.search.includes("slowwc") && window.VideoDecoder) {  // parte lento: 1 s di chunk in coda
  const VD = window.VideoDecoder;
  window.VideoDecoder = class extends VD {
    configure(c) {
      super.configure(c);
      this._held = [];
      setTimeout(() => { const h = this._held; this._held = null; if (h && this.state === "configured") h.forEach((k) => super.decode(k)); }, 1000);
    }
    decode(c) { this._held ? this._held.push(c) : super.decode(c); }
    get decodeQueueSize() { return (this._held?.length || 0) + super.decodeQueueSize; }
  };
}
if (window.AudioContext && navigator.mediaDevices) {  // microfono finto, con il tempo del permesso
  navigator.mediaDevices.getUserMedia = async () => {
    T.gum++;  // conta le richieste vere di microfono (l'ascolto allo squillo non ne fa)
    await new Promise((r) => setTimeout(r, T.gumDelay));
    const ac = new AudioContext(), osc = ac.createOscillator(), dst = ac.createMediaStreamDestination();
    osc.connect(dst); osc.start();
    return dst.stream;
  };
}
customElements.define("fake-picture-entity", class extends HTMLElement {
  connectedCallback() { if (this.cfg.camera_view === "live") { this.alive = true; T.live++; this.loop(); } }
  disconnectedCallback() { if (this.alive) T.live--; this.alive = false; this.ctrl?.abort(); }
  async loop() {
    while (this.alive) {
      this.ctrl = new AbortController();
      try {
        const r = await fetch("__AV_URL__", { signal: this.ctrl.signal });  // the key, as HA's camera (#63)
        T.av.push(r.status);
        const rd = r.body.getReader();
        for (;;) { const { done, value } = await rd.read(); if (done) break; T.avBytes += value.length; }
      } catch (e) { T.av.push("abort"); }
      await new Promise((r) => setTimeout(r, 300));
    }
  }
});
window.loadCardHelpers = async () => ({ createCardElement: (cfg) => {
  T.created.push(cfg.camera_view);
  const el = document.createElement("fake-picture-entity"); el.cfg = cfg; return el; } });
customElements.define("ha-form", class extends HTMLElement {
  set schema(s) { this._s = s; }
  set data(d) {
    this._d = d; this.innerHTML = "";
    for (const f of this._s) {
      const sel = f.selector, v = d[f.name] ?? "";
      let inp;
      if (sel.select) { inp = document.createElement("select");
        for (const o of sel.select.options) inp.add(new Option(o.label, o.value, false, o.value === v)); }
      else { inp = document.createElement("input"); inp.type = sel.number ? "number" : "text"; inp.value = v; }
      inp.name = f.name;
      inp.onchange = () => { const value = { ...this._d, [f.name]: sel.number ? +inp.value : inp.value };
        this.dispatchEvent(new CustomEvent("value-changed", { detail: { value }, bubbles: true })); };
      const lab = document.createElement("label"); lab.textContent = this.computeLabel(f); lab.append(inp); this.append(lab);
    }
  }
});
const WS = window.WebSocket;
window.WebSocket = class extends WS {
  constructor(u) { super(u); T.ws++;
    T.wsOpenAt = T.firstNalAt = T.firstFrameAt = 0;  // WebSocket nuovo = player nuovo
    D.dec = []; D.out = []; D.msg = []; D.skip = []; D.lastSkip = false;
    this.addEventListener("open", () => (T.wsOpenAt = Date.now()));
    this.addEventListener("message", (e) => { if (typeof e.data === "string") return; T.rx++;
      const h = new Uint8Array(e.data, 0, Math.min(e.data.byteLength, 6));  // sotto i 6 byte niente tipo NAL: niente RangeError
      if (h[0] !== 3) return;
      T.firstNalAt ||= Date.now();
      const pl = window.card?._player;  // gira prima dell'onmessage del player: lo stato è di prima di questo NAL
      lim(D.msg, { t: Date.now(), nal: h[5] & 0x1f, sps: +!!pl?._sps, pps: +!!pl?._pps, dec: +!!pl?._dec }, MSGS);
      setTimeout(() => { const p = window.card?._player, s = !!p?._skip;  // dopo che il player ha letto il NAL (a campione, due transizioni nello stesso tick si perdono)
        if (p && s !== D.lastSkip) { D.lastSkip = s; D.skip.push([Date.now(), +s, p._dec?.decodeQueueSize, p._out]); } }, 0); }); }
  send(b) { T.sent++; if (T.frames.length < 5 && b.byteLength) T.frames.push([new Uint8Array(b)[0], b.byteLength]); super.send(b); }
  close() { T.wsClosed++; super.close(); }
};
// Le impostazioni del citofono (stesso dispositivo della camera): `?ents=dnd,vm` ne tiene solo alcune, `?noadmin` toglie is_admin,
// `?nofile` lascia il file audio su "none" (nessuno).
const QS = new URLSearchParams(location.search), WANT = QS.get("ents")?.split(",");
const SET = {
  "switch.vimar_intercom_non_disturbare": { k: "dnd", state: "off", attributes: { friendly_name: "Non disturbare" } },
  "switch.vimar_intercom_segreteria": { k: "vm", state: "on", attributes: { friendly_name: "Segreteria", modo: "Home Assistant" } },
  "select.vimar_intercom_segreteria_ritardo": { k: "delay", state: "10", attributes: { friendly_name: "Segreteria · ritardo", options: ["5", "10", "15"] } },
  "text.vimar_intercom_segreteria_testo_del_messaggio": { k: "text", state: "Non siamo in casa", attributes: { friendly_name: "Segreteria · testo del messaggio" } },
  "select.vimar_intercom_segreteria_file_audio": { k: "file", state: QS.has("nofile") ? "none" : "a.wav", attributes: { friendly_name: "Segreteria · file audio", options: ["none", "a.wav", "b.wav"] } },
};
const setEnts = Object.entries(SET).filter(([, v]) => !WANT || WANT.includes(v.k));
const mkHass = (status, lastRing = {}, ringTime = null) => ({
  user: { is_admin: !QS.has("noadmin") },
  formatEntityState: (s, v) => (v === "none" ? "Nessuno (usa il testo)" : v),  // come la traduzione dello stato del select
  fetchWithAuth: (p, init) => fetch(p, init),
  entities: Object.fromEntries([["camera.vimar_intercom_intercom", 0], ...setEnts].map(([id]) => [id, { entity_id: id, device_id: "dev1", platform: "vimar_intercom" }])),
  states: {
    ...Object.fromEntries(setEnts.map(([id, v]) => [id, { state: v.state, attributes: v.attributes }])),
    "camera.vimar_intercom_intercom": { state: "idle", attributes: { card_entities: Object.fromEntries(setEnts.map(([id, v]) => [v.k === "vm" ? "segreteria" : v.k, id])) } },
    "sensor.vimar_intercom_intercom_stato": { state: status },
    "sensor.vimar_intercom_intercom_ultimo_squillo": { state: ringTime || "unknown", attributes: lastRing },
    "lock.vimar_intercom_serratura": { state: "locked" },
    "button.garage": { state: "unknown", attributes: { friendly_name: "Garage" } },
  },
  callService: async (d, sv, data) => {
    T.calls.push(d + "." + sv);
    if (["switch", "select", "text", "homeassistant"].includes(d)) { (T.settings ||= []).push([d, sv, data]); return { context: {} }; }
    const j = await (await fetch(`/svc/${d}/${sv}`, { method: "POST" })).json();
    if (d === "lock" && !j.ok) throw j.error;  // as HA: {message (English), translation_key, ...}
    return { context: {}, response: j };
  },
  // as HA: the integration's strings in the user's language (it), looked up by full key
  loadBackendTranslation: async () => {
    const t = await (await fetch("/translations/it")).json();
    return (k, p = {}) => k.split(".").slice(2).reduce((o, x) => o?.[x], t)?.replace(/{([a-z_]+)}/g, (_, n) => p[n]);
  },
  // come HA: firma anche la query; `?signs` numera le firme (x1, x2…) per distinguerle
  callWS: async (m) => ({ path: m.path + (m.path.includes("?") ? "&" : "?") + "authSig=x" + (QS.has("signs") ? (T.signs = (T.signs || 0) + 1) : "") }),
  callApi: async (method, path) => (await fetch("/api/" + path)).json(),
});
let last = "";
setInterval(async () => {
  try {
    const j = await (await fetch("/state")).json(), s = JSON.stringify(j);
    if (s !== last && window.card) { last = s; card.hass = mkHass(j.status, j.last_ring, j.last_ring_time); }
  } catch (e) {}
}, 100);
document.querySelector("home-assistant").hass = mkHass("unknown");
</script><script type="module">
await import("/vimar_intercom/vimar-intercom-card.js");
await customElements.whenDefined("vimar-intercom-card");
const c = document.createElement("vimar-intercom-card");
const qs = new URLSearchParams(location.search), layout = qs.get("layout");
c.setConfig({ type: "custom:vimar-intercom-card", ...(layout && { layout }), ...(qs.get("compact") && { compact_style: qs.get("compact") }),
              ...(qs.get("idle_picture") && { idle_picture: qs.get("idle_picture") }),
              ...(qs.has("listen_on_ring") && { listen_on_ring: true }),
              ...(qs.get("shortcuts") && { shortcuts: qs.get("shortcuts").split(",") }) });
document.body.appendChild(c);
window.card = c;
window.tap = (id) => c.shadowRoot.getElementById(id).click();
window.info = () => ({ pill: c.shadowRoot.querySelector(".pill").textContent,
  talk: c.shadowRoot.querySelector("#talk .lbl").textContent,
  err: c.shadowRoot.querySelector(".err").textContent,
  video: ((el) => el?.cfg ? el.cfg.camera_view : el?.tagName === "CANVAS" ? "canvas" : undefined)(
    c.shadowRoot.getElementById("video").firstElementChild),
  player: c._player && { frames: c._player.frames, resets: c._player.resets, wait: c._player._wait || 0,
                         ws: T.wsOpenAt, nal: T.firstNalAt, frame: T.firstFrameAt },
  audio: !c._audio && !c._ws ? "off" : "on",
  listen: !!c._listenWs,
  pop: !!c._pop.open,
  mute: { hidden: c.shadowRoot.getElementById("mute").hidden, muted: !!c._muted,
          audible: (!!c._ws || !!c._listenWs) && !c._muted, gain: (c._talkGain || c._listenGain)?.gain.value } });
</script></body></html>"""


@pytest.fixture(params=["chromium", "webkit"])
def engine(request):
    """Motore del browser: Chromium, e WebKit come Safari sull'iPhone."""
    return request.param


class Card:
    """La pagina della card aperta in `engine` sul server di `rig` (Rig(http=True))."""

    def __init__(self, rig, engine: str, insecure=False, webcodecs=True, badwc=False, flakywc=False, layout=None,
                 listen_on_ring=False, shortcuts=None, compact=None, query=""):
        self.rig, self.engine = rig, engine
        self.query = "?" + "&".join(f for f, on in (("insecure", insecure), ("nowc", not webcodecs),
                                                    ("badwc", badwc), ("flakywc", flakywc),
                                                    (f"layout={layout}", layout),
                                                    ("listen_on_ring", listen_on_ring),
                                                    (f"shortcuts={shortcuts}", shortcuts),
                                                    (f"compact={compact}", compact)) if on) + query

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required"] if self.engine == "chromium" else []
        try:
            self.browser = await getattr(self.pw, self.engine).launch(args=args)
        except Exception as e:  # noqa: BLE001 — motore non installato
            await self.pw.stop()
            pytest.skip(f"{self.engine} non disponibile: {e}")
        self.page = await self.browser.new_page(viewport={"width": 390, "height": 844})  # un iPhone
        await self.open()
        return self

    async def open(self, hash=None):
        """Apre (o riapre: cambio dashboard, ricarica) la pagina. `hash`: come il link
        .../camera#citofono di una notifica."""
        url = self.rig.base + "/" + self.query + (f"#{hash}" if hash else "")
        await self.page.goto(url)
        await self.page.wait_for_function("window.card && card.shadowRoot && window.info")

    async def __aexit__(self, *exc):
        await self.browser.close()
        await self.pw.stop()

    async def T(self):
        return await self.page.evaluate("T")

    async def dump_diag(self) -> str:
        """Stampa la diagnostica del player per intero, una volta (la CI lancia pytest con -s); ritorna un
        rimando corto per il messaggio dell'assert."""
        try:
            print("DIAG", await self.page.evaluate("diag()"))
        except Exception as e:  # noqa: BLE001 - una pagina chiusa non deve nascondere il fallimento originale
            print(f"DIAG non disponibile: {e}")
        return "(DIAG stampata sopra)"

    async def info(self):
        return await self.page.evaluate("info()")

    async def tap(self, bid):
        await self.page.evaluate(f"tap('{bid}')")

    async def until(self, js: str, timeout=5.0):
        end = asyncio.get_running_loop().time() + timeout
        while not await self.page.evaluate(js):
            if asyncio.get_running_loop().time() > end:
                raise AssertionError(f"mai vero: {js} T={await self.T()} info={await self.info()} {await self.dump_diag()}")
            await asyncio.sleep(0.05)
