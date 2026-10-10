// Vimar Intercom — card citofono: video (anche durante lo squillo), parla/ascolta, apri,
// cronologia degli squilli. Caricata dall'integrazione, non serve aggiungerla alle risorse.
//
//   type: custom:vimar-intercom-card
//   name: Citofono                                 (opzionali: questi sono i default)
//   camera: camera.vimar_intercom_intercom
//   status: sensor.vimar_intercom_intercom_stato
//   lock: lock.vimar_intercom_serratura
//   last_ring: sensor.vimar_intercom_intercom_ultimo_squillo
//
// Le entità non vanno scritte: un entity_id che non esiste (area del dispositivo,
// rinomina) viene sostituito da quello vero. La camera dell'integrazione si trova nel
// registro del frontend; stato, ultimo squillo, serratura e impostazioni dall'attributo
// `card_entities` della camera. Scritte e esistenti, vincono quelle della config.
//   anchor: citofono      (URL con #citofono: la card si porta in vista; se lo stato è già
//                         "in_call" — es. "Rispondi" premuto sulla notifica, che risponde
//                         dall'automazione — anche l'audio riparte da sola; "" = no)
//   history: 8            (ultimi squilli con foto e clip, se c'è la cartella foto; 0 = no)
//   confirm_open: true    (Apri chiede un secondo tocco; false = apre al primo)
//   shortcuts: [lock.x]   (tasti tondi "Apri" sulla card a riposo, in tutti i layout, senza aprire il popup né
//                         chiamare la targa; entità lock (unlock) o button (press), anche {entity, name, icon};
//                         default = la serratura. Rispettano confirm_open. In diretta stanno i tasti veri;
//                         nel popup "Apri" apre la prima e le altre stanno in fila sotto la barra)
//   listen_on_ring: false (si sente il visitatore già allo squillo, senza rispondere:
//                         solo ricezione, il microfono resta spento; anche dall'editor)
//   layout: overlay       (o "sotto" o "popup"; anche dall'editor visuale)
//   compact_style: pillola (solo layout popup: la card compatta in dashboard, "pillola" o "tile")
//   idle_picture: last_ring (da fermo la scena e la foto della card compatta mostrano l'ultimo squillo;
//                         "standby" = niente foto, l'icona del citofono; le foto restano in cronologia)
//
//   layout: overlay   (default) "Video a tutta card": da fermo riga da 72 px (foto dell'ultimo
//                     squillo = tasto cronologia | nome · stato · ultimo / tre pill). In diretta
//                     la card È il video 4:3: stato in alto a sinistra, cronologia in alto a
//                     destra, tasti da 44 px etichettati su uno scrim sfumato in basso. Da fermo
//                     con cronologia: palco 4:3 con la foto grande e il cassetto, sopra la riga.
//   layout: sotto     "Tasti sotto il video": stessa riga; in diretta il video 4:3 si infila
//                     SOPRA la riga e i tasti restano nella riga, niente sopra al video. Da fermo
//                     con cronologia: lista a piena larghezza sotto la riga (3 righe, poi scorre).
//
//   layout: popup     In dashboard la card è compatta (foto dell'ultimo squillo + stato). Un tocco, o il
//                     tasto cronologia, apre un <dialog> (pannello scuro tondo centrato, margini 12 px, max 720) dove va la
//                     card intera, video con sotto i tasti [Parla|Rispondi] [Apri] [Riaggancia]. Il tocco
//                     sulla card = "Vedi esterno"; la cronologia non chiama mai la targa. Allo squillo (o
//                     in_call/calling dall'ancora) si apre da sola, una volta per squillo. Chiuderlo
//                     (X, Esc, tocco fuori) riaggancia se la chiamata l'ha avviata la card, e chiude l'audio.
//
// Stesso DOM per i layout: cambia il CSS, agganciato all'attributo `layout` sull'host (blocchi
// :host([layout="overlay"]) / "sotto" / "popup" in fondo a STYLE); il popup in più sposta la card nel <dialog>.
//
// Regole comuni: aprire la pagina non chiama la targa; posti fissi [vedi|annulla|riaggancia]
// [parla|rispondi|microfono] [apri]; "Apri" in due tocchi; slot 1 spento (non nascosto)
// durante lo squillo; il cassetto si chiude da solo allo squillo; dopo il riaggancio il
// video resta 1,5 s con i tasti spenti; un avviso sostituisce la riga di stato per 4 s.
//
// Audio e video sul WebSocket /api/vimar_intercom/audio_ws (lo stesso dell'app iOS):
//   targa → browser: 0x01 + PCM16LE 8 kHz mono, 0x03 + NAL H.264 (Annex B);
//   browser → targa: 0x02 + PCM16LE.
// Il video dal vivo passa da lì (WebCodecs → canvas: primo fotogramma in ~0,1 s) e, dove
// WebCodecs manca, dallo stream di HA (go2rtc/HLS, 2-4 s). Microfono e WebCodecs
// funzionano solo in HTTPS (o su localhost).

import { CardAudio } from "./card-audio.js";
import { DEFAULTS, EDITOR_TAG, VimarIntercomCardEditor } from "./card-editor.js";
import { STYLE } from "./card-style.js";
import { NalPlayer, same } from "./nal-player.js";

const FIT_KEY = "vimar_intercom_card_fit";
const NO_ANSWER = "La targa non risponde, riprova.";
const REFUSED = "La targa non ha accettato, riprova.";
const SETUP_TIMEOUT_S = 20;  // oltre, "la targa non risponde" (il cloud a volte ci mette 15 s)
const HOLD_MS = 1500;        // dopo il riaggancio: video fermo, tasti spenti, poi la card si richiude
const OPEN_FLASH_MS = 2000;  // "Aperto" / "Errore" sul tasto
const CLOSE_GUARD_MS = 800;   // after the popup closes, taps on the compact card are ignored (iOS click-through)
const PENDING_MS = 10000;   // "Collegamento…" subito al tocco, senza aspettare lo stato di HA; oltre, si lascia stare
const SAY_MS = 4000;         // un avviso resta 4 s al posto della riga di stato
const LIVE = ["ringing", "calling", "in_call"];
const OUTCOME = { answered: "Risposto", declined: "Rifiutato", away: "Messaggio di assenza",
                  missed: "Nessuna risposta", answered_elsewhere: "Risposto altrove",
                  opened: "Aperto" };
const LABEL = {
  idle: "Pronto", ringing: "Suonano alla porta", calling: "Collegamento…",
  in_call: "In chiamata", offline: "Non raggiungibile",
};
const hm = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const dayLabel = (t) => {  // "" oggi, "Ieri", altrimenti "26 set"
  const d = new Date(t), now = new Date(), y = new Date(now);
  y.setDate(now.getDate() - 1);
  return d.toDateString() === now.toDateString() ? "" : d.toDateString() === y.toDateString() ? "Ieri"
    : d.toLocaleDateString([], { day: "numeric", month: "short" });
};
const when = (t) => [dayLabel(t), hm(t)].filter(Boolean).join(" ");  // "18:42", "Ieri 21:30"
const whenFull = (t) => new Date(t).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

// Entity names and icons come from the states of other entities (friendly_name, icon): escape them
// before they reach innerHTML, and accept only a "prefix:name" icon.
const esc = (v) => String(v).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const btn = (id, icon, label) => {
  icon = /^[a-z0-9_-]+:[a-z0-9_-]+$/.test(icon) ? icon : "mdi:gesture-tap-button";
  label = esc(label);
  return `<button${id ? ` id="${id}"` : ""} data-icon="${icon}" data-label="${label}"><span class="ic"><ha-icon icon="${icon}" aria-hidden="true"></ha-icon></span>` +
    `<span class="lbl">${label}</span></button>`;
};
const DRAWER = `<aside class="drawer" id="drawer" aria-label="Ultimi squilli"><div class="hist"></div>
  <div class="empty"><ha-icon icon="mdi:bell-off-outline" aria-hidden="true"></ha-icon><span></span></div></aside>`;
const SCENE = `<div id="video"></div><img class="still" alt="">
  <span class="ph"><ha-icon icon="mdi:doorbell-video" aria-hidden="true"></ha-icon></span>`;
const PHOTO = `<button id="photo" aria-label="Cronologia squilli" title="Cronologia squilli" aria-expanded="false" aria-controls="drawer" disabled>
  <img alt=""><ha-icon icon="mdi:doorbell-video" aria-hidden="true"></ha-icon><ha-icon class="hb" icon="mdi:history" aria-hidden="true"></ha-icon></button>`;
const X = `<button id="x" aria-label="Chiudi"><ha-icon icon="mdi:close" aria-hidden="true"></ha-icon></button>`;
const LOG = `<button id="log" aria-label="Cronologia squilli" title="Cronologia squilli" aria-expanded="false" aria-controls="drawer">
  <ha-icon icon="mdi:history" aria-hidden="true"></ha-icon></button>`;
// Sempre in vista per tutta la diretta: se l'audio non sta ancora suonando (bloccato su
// iOS o mai partito) il tocco avvia l'ascolto — è il gesto vero che sblocca l'AudioContext;
// se sta suonando (ascolto allo squillo o parlato) lo muta/smuta soltanto. Mai la chiamata,
// mai il microfono, mai il WebSocket. Icona sola, senza etichetta.
const MUTE = `<button id="mute" aria-label="Audio" aria-pressed="true">
  <ha-icon icon="mdi:volume-off" aria-hidden="true"></ha-icon></button>`;
// Adatta (video intero, bande scure: predefinito) / Riempi (cover, zoom). Si ricorda per dispositivo.
const FIT = `<button id="fit" aria-label="Riempi schermo" title="Riempi schermo" aria-pressed="false">
  <ha-icon icon="mdi:arrow-expand-all" aria-hidden="true"></ha-icon></button>`;
const HIST = `<button id="hist" aria-label="Cronologia squilli" title="Cronologia squilli" disabled><span class="ic"><ha-icon icon="mdi:history" aria-hidden="true"></ha-icon></span><span class="lbl">Storico</span></button>`;
const BELL = `<span class="bell" aria-hidden="true"><ha-icon icon="mdi:bell"></ha-icon></span>`;
// Impostazioni del citofono (Non disturbare, Segreteria…): tondo con l'ingranaggio, uno sul video e uno nella card compatta.
// Due copie dello stesso tasto (id diversi) perché vivono in contenitori diversi: .media (video) e .head (card compatta).
const cfgBtn = (id) => `<button id="${id}" class="cfg" aria-label="Impostazioni citofono" title="Impostazioni citofono" hidden>
  <ha-icon icon="mdi:cog" aria-hidden="true"></ha-icon></button>`;
const CFG = cfgBtn("cfg"), CFGC = cfgBtn("cfgc");
const SUB = `<span class="sub"><span class="pill" role="status" aria-live="polite"></span><span class="last"></span><span class="err" role="alert"></span></span>`;
const ROW = `<div class="row">${btn("view", "mdi:cctv", "Vedi esterno")}${btn("talk", "mdi:microphone", "Parla")}` +
  `${btn("hangup", "mdi:phone-hangup", "Riaggancia")}${btn("open", "mdi:door-open", "Apri")}</div>`;
// Foto dello squillo in grande, o il suo clip (<video>) se c'è.
const PHOTO_DLG = `<dialog class="photo" aria-label="Squillo"><img alt="Foto dello squillo">` +
  `<video controls playsinline preload="metadata" hidden></video><p class="cap"></p></dialog>`;

const TEMPLATE = `<ha-card>
  <div class="media">${SCENE}<span class="badge dyn" aria-hidden="true"></span>${MUTE}${FIT}${LOG}${CFG}${X}${DRAWER}</div>
  <div class="head">${BELL}${PHOTO}<div class="ttl"><span class="name"></span>${SUB}</div>${ROW}<div class="sc"></div>${HIST}${CFGC}</div>
  ${PHOTO_DLG}</ha-card><dialog class="pop" aria-label="Citofono"></dialog>
  <dialog class="set" aria-label="Impostazioni citofono"></dialog>`;

const SC = { lock: ["unlock", "mdi:door-open"], button: ["press", "mdi:gesture-tap-button"] };  // dominio → servizio, icona
// The ring (last_ring state) whose popup was closed by hand on this page: it does not open by
// itself again for that ring, whatever follows (answered elsewhere, call ended, card rebuilt).
let dismissedRing = null;

// Popup aperto: la dashboard sotto non scorre (su iOS il dito sulla cronologia la trascinava).
const lockPageScroll = (on) => {
  for (const el of [document.documentElement, document.body]) el.style.overflow = on ? "hidden" : "";
};

class VimarIntercomCard extends CardAudio(HTMLElement) {
  static getConfigElement() {
    return document.createElement(EDITOR_TAG);
  }

  static getStubConfig(_hass, entities = []) {
    const reg = _hass?.entities || {};
    const camera = Object.keys(reg).find((id) => id.startsWith("camera.") && reg[id].platform === "vimar_intercom")
      || entities.find((e) => e.startsWith("camera.vimar_intercom")) || DEFAULTS.camera;
    return { camera, name: DEFAULTS.name, layout: DEFAULTS.layout, history: DEFAULTS.history };
  }

  setConfig(config) {
    this._cfg = { ...DEFAULTS, ...config };
    this.setAttribute("layout", ["sotto", "popup"].includes(this._cfg.layout) ? this._cfg.layout : "overlay");  // il CSS si aggancia qui
    this.setAttribute("compact", this._cfg.compact_style === "tile" ? "tile" : "pillola");
    if (this._root) {  // l'editor richiama setConfig sulla card viva: nome, cronologia e layout cambiano subito
      this._applyCfg();
      this._histKey = null;
      if (this._hass) this._render();
    }
  }

  get _popup() {
    return this._cfg.layout === "popup";
  }

  // entity_id da usare per camera / status / lock / last_ring (vedi l'intestazione).
  _ent(key) {
    const hass = this._hass, want = this._cfg[key];
    if (!hass || hass.states[want]) return want;
    if (key === "camera") {
      const reg = hass.entities || {};
      return Object.keys(reg).find((id) => id.startsWith("camera.") && reg[id].platform === "vimar_intercom")
        || want;
    }
    return hass.states[this._ent("camera")]?.attributes?.card_entities?.[key] || want;
  }

  get _preview() {
    return !!this.closest("hui-card-preview, hui-dialog-edit-card");
  }

  // Compatta = un tasto (anche da tastiera); nel popup no.
  _armCard() {
    const on = this._popup && !this._pop.open;
    for (const [k, v] of [["role", "button"], ["tabindex", "0"]]) on ? this._card.setAttribute(k, v) : this._card.removeAttribute(k);
  }

  _applyCfg() {
    this._root.querySelector(".name").textContent = this._cfg.name;
    this._log.hidden = !(this._cfg.history > 0);
    this._armCard();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._root) {
      this._build();
      this._toAnchor();
    }
    if (this._video) this._video.hass = hass;
    this._render();
  }

  _render() {
    const hass = this._hass;
    const raw = hass.states[this._ent("status")]?.state;
    const state = LABEL[raw] ? raw : "offline";  // unknown/unavailable: come non raggiungibile
    const on = !!this._ws;
    const was = this._state;
    this._state = state;
    const live = LIVE.includes(state);
    // Riaggancio: il video resta HOLD_MS con i tasti spenti, così il secondo tocco di "Apri"
    // non cade sulla card sotto quando la dashboard rifluisce. Niente animazione di altezza.
    if (was === "in_call" && state === "idle") {
      clearTimeout(this._holdT);
      this._card.classList.add("hold");
      this._holdT = setTimeout(() => { this._holdT = null; this._card.classList.remove("hold"); this._render(); }, HOLD_MS);
    } else if (live && this._holdT) {
      // Chiamata nuova durante l'attesa: player nuovo (WebSocket e decoder), non
      // quello della chiamata prima, che avrebbe in pancia i suoi ultimi fotogrammi.
      clearTimeout(this._holdT);
      this._holdT = null;
      this._card.classList.remove("hold");
      this._live = undefined;
    }
    const show = (live || !!this._holdT) && (!this._popup || this._pop.open);  // compatta: niente video
    this._setVideo(show);
    this._card.dataset.state = state;
    this._card.classList.toggle("live", show);
    if (state === "ringing" && was !== "ringing") this._setDrawer(false);  // lo squillo non va coperto
    this._tickSetup();
    if (was === "calling" && state === "idle" && !this._cancelled && this._err.textContent === this._hint) {
      this._err.textContent = REFUSED;
    } else if (state !== "idle" && state !== was && this._err.textContent === REFUSED) {
      this._err.textContent = this._hint;
    }
    if (state !== "calling") this._cancelled = false;
    const lr = hass.states[this._ent("last_ring")], last = lr?.state, a = lr?.attributes || {};
    this._last.textContent = isNaN(Date.parse(last)) ? "" : `ultimo ${when(last)}`;
    // La lista si ricarica anche quando arrivano la foto (subito, poi quella migliore) e il clip.
    const key = `${last}|${this._live}|${a.foto_url}|${a.clip_url}`;
    if (this._cfg.history > 0 && key !== this._histKey) {
      this._histKey = key;
      this._loadHistory();
    }

    // Posti fissi: [vedi / annulla / riaggancia] [parla / rispondi / microfono] [apri].
    // Allo squillo "Vedi esterno" resta al suo posto, spento: lo slot non si svuota.
    this._view.hidden = ["calling", "in_call"].includes(state);
    this._view.disabled = state !== "idle" || this._view.classList.contains("busy");
    const ring = state === "ringing", inCall = state === "in_call" || state === "calling";
    this._hangup.hidden = !LIVE.includes(state) && !this._pop.open;  // squillo: "Rifiuta" in ogni layout
    this._label(this._hangup, state === "calling" ? "Annulla" : ring ? "Rifiuta" : "Riaggancia");
    // L'audio automatico è riuscito (_autoAudioTried) ma il gesto vero mancava (iOS):
    // "Microfono" diventa "Audio", ben visibile, finché non si tocca — un microfono
    // spento si legge come "muto", non come invito a toccare.
    const audioHint = inCall && !on && this._audioBlocked;
    this._talk.className = ring || audioHint ? "ok answer" : on ? "fill" : "";
    this._talk.setAttribute("aria-pressed", on);
    this._icon(this._talk, ring ? "mdi:phone" : audioHint ? "mdi:volume-off"
      : on || !inCall ? "mdi:microphone" : "mdi:microphone-off");
    this._label(this._talk, ring ? "Rispondi" : audioHint ? "Audio" : inCall ? "Microfono" : "Parla");
    this._talk.disabled = state === "offline" || (state === "calling" && !on);
    this._shortcuts();
    this._syncSettings();
    this._histBtn.disabled = this._photo.disabled;
    this._open.disabled = state === "offline" || hass.states[this._ent("lock")]?.state === "unavailable";
    if ((state === "idle" || state === "offline") && this._ws) this._stopAudio();

    // Muto (tondo sul video): sempre visibile per tutta la diretta (ringing/calling/
    // in_call), non solo quando l'audio è già partito — su iOS l'ascolto automatico può
    // restare bloccato senza un tocco vero, e il tasto è anche il modo per darglielo
    // (vedi onclick, sotto: se non sta ancora suonando avvia l'ascolto invece di mutare).
    const audible = (!!this._ws || !!this._listenWs) && !this._muted;
    this._mute.hidden = !live;
    this._applyFit();
    this._icon(this._mute, audible ? "mdi:volume-high" : "mdi:volume-off");
    this._mute.setAttribute("aria-pressed", !audible);

    // Arrivo dall'ancora (link della notifica) già "in_call" (l'automazione ha risposto
    // lei, con vimar_intercom.answer): l'audio si aggancia da sola, un tentativo per
    // chiamata — se l'utente stacca il microfono a mano non si riattacca da sola, e se
    // iOS tiene l'audio sospeso senza un tocco vero si rinuncia in silenzio, ma il tasto
    // diventa "Audio" (sopra): resta un tocco solo, ben visibile. Mai per
    // "ringing"/"calling": aprire la pagina non risponde né chiama da sola.
    if (state === "in_call") {
      if (!this._autoAudioTried && !this._ws && !this._starting && window.isSecureContext
          && this._cfg.anchor && location.hash === `#${this._cfg.anchor}`) {
        this._autoAudioTried = true;
        this._startTalk(true).catch(() => {});
      }
    } else {
      this._autoAudioTried = false;
    }
    // "Audio" (sopra) serve tanto all'aggancio automatico quanto all'ascolto allo squillo
    // (sotto): si azzera solo lasciando gli stati dal vivo, non ad ogni giro di `_render`
    // durante ringing/calling — altrimenti un blocco vero (iOS) sparirebbe e riproverebbe
    // ad ogni aggiornamento di `hass`, anche senza alcun cambio di stato.
    if (!live) { this._audioBlocked = false; this._muted = false; }
    if (LIVE.includes(was) && !live) this._mine = false;  // chiamata finita: non più "della card"  // il muto vale una sessione dal vivo sola

    // `listen_on_ring`: si sente il visitatore già a video (ringing/calling/in_call in
    // anteprima), senza rispondere né aprire il microfono — smette da sola a fine
    // squillo/preview o quando parte l'audio vero (_ws, mic compreso: si passa a quello,
    // niente doppio canale). "Rispondi" resta al suo posto durante lo squillo: un tocco
    // solo, già pronto, anche se l'ascolto automatico non parte (iOS senza gesto).
    // Chiusura solo per fine diretta o audio vero: un ascolto avviato a mano dal tasto
    // Audio (anche a `listen_on_ring` spento) resta finché dura la diretta.
    if (!live || this._ws || this._starting) this._stopListen();
    else if (this._cfg.listen_on_ring && (!this._popup || this._pop.open) && !this._listenWs && !this._listenStarting) this._startListen(true);

    // Popup: opens by itself when a live period begins that did not start from us: at the ring, or
    // with an already answered call (notification "Answer", app reopened after missing the ring: the
    // card sees idle -> in_call), once per period. It does not rely on the `#citofono` anchor, which
    // may never reach the card. A period beginning with "calling" is an outgoing call (a tap on the
    // card, HomeKit, Alexa): the popup only opens if the anchor is present.
    if (!live) this._liveFrom = null;
    else if (!this._liveFrom) this._liveFrom = state;
    if (!live) this._popTried = false;
    else if (this._pop.open) this._popTried = true;
    else if (this._popup && !this._popTried && this.isConnected && !this._preview
        && last !== dismissedRing
        && (this._liveFrom !== "calling" || (this._cfg.anchor && location.hash === `#${this._cfg.anchor}`))) {
      this._popTried = true;
      this._openPop();
    }
  }

  // Sposta la card nel dialog; `view`: il tocco sulla card fa "Vedi esterno".
  _openPop(view) {
    if (this._pop.open) return;
    this._card.classList.add("pop");
    this._pop.append(this._card);
    this._pop.showModal();
    lockPageScroll(true);
    this._armCard();
    this._render();
    if (view && !this._view.disabled) {
      this._mine = true;
      this._call("call", this._view).catch(() => {});
    }
  }

  _label(button, text) {
    button.querySelector(".lbl").textContent = text;
  }

  _icon(button, icon) {
    button.querySelector("ha-icon").icon = icon;
  }

  _build() {
    this._root = this.attachShadow({ mode: "open" });
    this._root.innerHTML = `<style>${STYLE}</style>${TEMPLATE}`;
    const $ = (s) => this._root.querySelector(s);
    this._card = $("ha-card");
    this._pill = $(".pill");
    this._last = $(".last");
    this._badge = $(".badge.dyn");   // badge sul video col testo dello stato (dove c'è)
    this._log = $("#log");
    this._fit = $("#fit");
    this._photo = $("#photo");
    this._pic = $("#photo img");
    this._still = $(".still");
    this._hist = $(".hist");
    this._set = $("dialog.set");
    this._cfgs = [$("#cfg"), $("#cfgc")];
    this._empty = $(".empty span");
    this._dlg = $("dialog.photo");
    this._pop = $("dialog.pop");
    this._clipEl = $("dialog.photo video");
    // Tocco ovunque (o Esc) chiude, tranne sui controlli del video.
    this._dlg.onclick = (e) => e.target !== this._clipEl && this._dlg.close();
    this._dlg.onclose = () => { this._clipEl.pause(); this._clipEl.removeAttribute("src"); this._clipEl.load(); };
    // A signed path expires ~30 s after auth/sign_path. A photo or clip the browser fetches
    // again later (a purged image shown again on the phone, a clip resumed) gets a 401, which
    // HA logs as a failed login: the element is signed again, once until it loads.
    this._root.addEventListener("error", async (e) => {
      const el = e.target, m = el.src?.match(/\/api\/vimar_intercom\/rings\/(.+?)[?&]authSig=/);
      if (!m || el._resigned) return;
      el._resigned = true;
      const t = el.currentTime;
      try { el.src = await this._sign(m[1]); } catch { return; }  // HA scollegato: resta com'è
      if (t) el.currentTime = t;
    }, true);
    for (const ev of ["load", "loadeddata"]) this._root.addEventListener(ev, (e) => (e.target._resigned = false), true);
    // Popup chiuso (X, Esc, fuori): la card torna al suo posto, audio chiuso, riaggancio solo se la chiamata è della card.
    this._pop.onclick = (e) => e.target === this._pop && this._pop.close();
    this._pop.onclose = () => {
      this._closedAt = performance.now();
      if (this.isConnected && LIVE.includes(this._state)) {  // not when the card leaves the page
        const ring = this._hass.states[this._ent("last_ring")]?.state;
        dismissedRing = Date.parse(ring) ? ring : null;  // "unknown": no ring yet
      }
      lockPageScroll(false);
      this._card.classList.remove("pop");
      this._root.insertBefore(this._card, this._pop);
      this._armCard();
      this._stopAudio();
      this._stopListen();
      const mine = this._mine;
      this._mine = false;
      if (mine && ["calling", "in_call"].includes(this._state)) this._call("hangup", this._hangup).catch(() => {});
      this._render();
    };
    this._err = $(".err");
    this._hint = "";  // niente avviso permanente: in HTTP lo dice il tocco sul microfono
    this._view = $("#view");
    this._view.setAttribute("aria-label", "Vedi esterno");
    this._talk = $("#talk");
    this._hangup = $("#hangup");
    this._open = $("#open");
    this._mute = $("#mute");
    this._videoBox = $("#video");
    // The box changes shape with the layout, the popup and the phone's rotation.
    if (window.ResizeObserver) new ResizeObserver(() => this._fitShape()).observe(this._videoBox);
    this._applyCfg();
    this._view.className = "fill";
    this._open.setAttribute("aria-label", "Apri portone, tocca due volte");
    this._sc = $(".sc");
    for (const b of this._cfgs) b.onclick = (e) => { e.stopPropagation(); this._openSettings(); };
    this._set.onclick = (e) => e.target === this._set && this._set.close();
    this._histBtn = $("#hist");
    if (!window.isSecureContext) this._talk.title = "Per parlare serve Home Assistant in HTTPS.";
    for (const b of [this._log, this._photo, this._histBtn]) b.onclick = (e) => {
      if (this._popup && !this._pop.open) { e.stopPropagation(); this._setDrawer(true); return this._openPop(); }  // cronologia: mai la targa
      this._setDrawer(this._card.dataset.drawer !== "true");
    };
    // The tap that closed the popup (X, Hang up) must not land on the compact card that takes its
    // place: on the iPhone it did, and answered the ring, called the panel or opened the door.
    this._card.addEventListener("click", (e) => {
      if (!this._pop.open && performance.now() - this._closedAt < CLOSE_GUARD_MS) {
        e.stopPropagation();
        e.preventDefault();
      }
    }, true);
    this._card.onclick = (e) => this._popup && !this._pop.open && !e.target.closest("button") && this._openPop(!this._preview);  // anteprima dell'editor: niente Vedi esterno
    this._card.onkeydown = (e) => e.target === this._card && (e.key === "Enter" || e.key === " ") && (e.preventDefault(), this._card.click());
    this._root.getElementById("x").onclick = () => this._pop.close();
    this._view.onclick = () => {
      if (this._popup && !this._pop.open) return this._openPop(!this._preview);  // card compatta: apre il popup e fa "Vedi esterno"
      this._mine = true;
      this._call("call", this._view).catch(() => {});
    };
    this._talk.onclick = () => {
      if (this._popup && !this._pop.open) this._openPop();  // "Rispondi" sulla card compatta: prima il popup
      this._ws ? this._stopAudio() : this._starting || this._startTalk();
    };
    this._hangup.onclick = () => {  // allo squillo "Rifiuta" (smette di suonare in tutta la casa), poi "Riaggancia"
      if (this._state === "ringing") this._call("decline", this._hangup).catch(() => {});
      else if (LIVE.includes(this._state)) this._call("hangup", this._hangup).catch(() => {});
      if (this._pop.open) {
        this._mine = false;  // già fatto qui: la chiusura non riaggancia di nuovo
        this._pop.close();
      }
    };
    this._open.onclick = () => this._openDoor();
    // Non ancora in ascolto (bloccato su iOS o mai partito): il tocco stesso è il gesto
    // vero che sblocca l'AudioContext, quindi avvia l'ascolto invece di mutare un canale
    // che non c'è ancora. Se il parlato vero è già in corso, non lo tocca: solo il muto.
    this._mute.onclick = () => {
      if (this._ws || this._listenWs) this._toggleMute();
      else if (!this._listenStarting) this._startListen();
    };
    // Adatta (contain, predefinito) / Riempi (cover): la scelta resta sul dispositivo.
    try { this._cover = localStorage.getItem(FIT_KEY) === "cover"; } catch { this._cover = false; }
    this._fit.onclick = () => {
      this._cover = !this._cover;
      try { localStorage.setItem(FIT_KEY, this._cover ? "cover" : "contain"); } catch { /* storage bloccato: vale per questa sessione */ }
      this._applyFit();
      // HA's picture-entity card (no WebCodecs, or the player failed) keeps its <video> in its
      // own shadow DOM, out of reach of our object-fit: it takes fit_mode, so it is rebuilt (#42).
      if (this._video && !this._player) this._setPicture(this._live);
    };
    this._applyFit();
    // Un avviso vive SAY_MS al posto della riga di stato, poi sparisce (NO_ANSWER resta finché si collega).
    new MutationObserver(() => {
      clearTimeout(this._sayT);
      const t = this._err.textContent;
      if (t && t !== NO_ANSWER) this._sayT = setTimeout(() => { if (this._err.textContent === t) this._err.textContent = ""; }, SAY_MS);
    }).observe(this._err, { childList: true, characterData: true, subtree: true });
  }

  // Le entità delle impostazioni, dall'attributo `card_entities` della camera (chiavi dnd, segreteria, delay, file, text);
  // se una manca, la sua riga non compare.
  _setIds() {
    const ids = {};
    for (const [k, key] of [["dnd", "dnd"], ["vm", "segreteria"], ["delay", "delay"], ["file", "file"], ["text", "text"]]) {
      const id = this._ent(key);
      if (id && this._hass.states[id]) ids[k] = id;
    }
    if (!this._hass.user?.is_admin) delete ids.file, delete ids.text;  // testo e file audio: solo admin
    return ids;
  }

  _syncSettings() {
    const ids = this._setIds(), any = Object.keys(ids).length > 0;
    for (const b of this._cfgs) b.hidden = !any;
    if (this._set.open) this._fillSettings(ids);
  }

  _openSettings() {
    const ids = this._setIds();
    if (!Object.keys(ids).length || this._set.open) return;
    const h = document.createElement("template");
    h.innerHTML = `<div class="set-c"><div class="set-h"><h2>Impostazioni citofono</h2><button class="set-x" aria-label="Chiudi"><ha-icon icon="mdi:close" aria-hidden="true"></ha-icon></button></div>
      <div class="set-body"></div><p class="set-e" role="alert"></p></div>`;
    this._set.replaceChildren(h.content);
    this._set.querySelector(".set-x").onclick = () => this._set.close();
    this._setBuilt = null;
    this._fillSettings(ids);
    this._set.showModal();
  }

  // Le righe si costruiscono una volta per elenco di entità; poi si aggiornano soltanto i valori (mai sotto le dita di chi scrive).
  _fillSettings(ids) {
    const st = (id) => this._hass.states[id], body = this._set.querySelector(".set-body");
    const key = Object.values(ids).join();
    const call = (dom, sv, data) => this._hass.callService(dom, sv, data).then(() => { this._set.querySelector(".set-e").textContent = ""; },
      (e) => { this._set.querySelector(".set-e").textContent = `Non riuscito: ${e.message || e}`; });
    const row = (k, label, control, col) => {
      const r = document.createElement("div");
      r.className = "set-r" + (col ? " col" : "");
      r.dataset.k = k;
      const l = document.createElement("span");
      l.className = "set-l";
      l.append(label);
      r.append(l, control);
      return r;
    };
    if (this._setBuilt !== key) {
      this._setBuilt = key;
      const rows = [];
      const toggle = (k, label) => {
        const b = document.createElement("button");
        b.className = "set-tg";
        b.setAttribute("role", "switch");
        b.setAttribute("aria-label", label);
        b.onclick = () => call("switch", b.getAttribute("aria-checked") === "true" ? "turn_off" : "turn_on", { entity_id: ids[k] });
        rows.push(row(k, label, b));
      };
      const select = (k, label, col) => {
        const el = document.createElement("select");
        el.className = "set-in";
        el.setAttribute("aria-label", label);
        el.onchange = () => call("select", "select_option", { entity_id: ids[k], option: el.value });
        rows.push(row(k, label, el, col));
      };
      if (ids.dnd) toggle("dnd", "Non disturbare");
      if (ids.vm) toggle("vm", "Segreteria");
      if (ids.delay) select("delay", "Ritardo segreteria");
      if (ids.text) {
        const el = document.createElement("input");
        el.className = "set-in";
        el.type = "text";
        el.setAttribute("aria-label", "Testo del messaggio");
        el.onchange = () => call("text", "set_value", { entity_id: ids.text, value: el.value });
        rows.push(row("text", "Testo del messaggio", el, true));
      }
      if (ids.file) {  // il select, e sotto "Carica"/"Sostituisci" (solo admin: ids.file c'è solo per loro)
        select("file", "File audio del messaggio", true);
        const sel = rows.at(-1).querySelector("select"), wrap = document.createElement("div");
        const up = document.createElement("button"), inp = document.createElement("input");
        wrap.className = "set-up";
        up.className = "set-ub";
        inp.type = "file";
        inp.accept = ".mp3,.wav,.m4a,audio/*";
        inp.hidden = true;
        up.onclick = () => inp.click();
        // Il click della scelta file sale fino a .card, il cui onclick dà false (= preventDefault) e la chiuderebbe.
        inp.onclick = (e) => e.stopPropagation();
        inp.onchange = () => { const f = inp.files[0]; inp.value = ""; if (f) this._uploadAway(f, ids.file, up); };
        sel.replaceWith(wrap);
        wrap.append(sel, up, inp);
      }
      body.replaceChildren(...rows);
    }
    for (const r of body.children) {
      const k = r.dataset.k, s = st(ids[k]), ctl = r.querySelector("button, select, input"), off = !s || s.state === "unavailable";
      ctl.disabled = off;
      if (ctl.matches("button")) ctl.setAttribute("aria-checked", s?.state === "on");
      else if (ctl.matches("select")) {
        const opts = s?.attributes?.options || [];
        // L'etichetta tradotta dall'entità ("none" → "Nessuno (usa il testo)"), il valore resta l'opzione.
        if (ctl.options.length !== opts.length || opts.some((o, i) => ctl.options[i].value !== o))
          ctl.replaceChildren(...opts.map((o) => new Option(this._hass.formatEntityState?.(s, o) ?? o, o)));
        if (this._root.activeElement !== ctl) ctl.value = s?.state;
        const up = r.querySelector(".set-ub");
        if (up) {
          up.textContent = s?.state && s.state !== "none" ? "Sostituisci" : "Carica";
          up.disabled = off || !!up._busy;  // _busy: upload in progress
        }
      } else if (this._root.activeElement !== ctl) ctl.value = s && s.state !== "unknown" ? s.state : "";
      if (k === "vm") {  // da dove viene il messaggio: dall'attributo `modo` dello switch, se c'è
        const modo = s?.attributes?.modo, small = r.querySelector("small") || r.querySelector(".set-l").appendChild(document.createElement("small"));
        small.textContent = modo === "Home Assistant" ? "Messaggio di Home Assistant" : modo === "Tab" ? "Segreteria del Tab" : "";
      }
    }
  }

  // Il file scelto va a /api/vimar_intercom/away_upload (solo admin) con l'utente di HA; salvato,
  // diventa il messaggio e il select si rilegge subito invece che al prossimo giro (ogni minuto).
  async _uploadAway(f, id, b) {
    const e = this._set.querySelector(".set-e"), small = b.closest(".set-r").querySelector(".set-l small")
      || b.closest(".set-r").querySelector(".set-l").appendChild(document.createElement("small"));
    const ERR = {
      upload_bad_type: "serve un file .mp3, .wav o .m4a con un nome semplice",
      upload_too_big: "file troppo grande, massimo 5 MB",
    };
    e.textContent = small.textContent = "";
    b.disabled = b._busy = true;
    try {
      if (f.size > 5 * 1024 * 1024) throw new Error(ERR.upload_too_big);
      const r = await this._hass.fetchWithAuth(`/api/vimar_intercom/away_upload?name=${encodeURIComponent(f.name)}`, { method: "POST", body: f });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(ERR[j.error] || "file non salvato, guarda il log di Home Assistant");
      await this._hass.callService("homeassistant", "update_entity", { entity_id: id });
      small.textContent = `Caricato: ${j.file}`;
    } catch (err) {
      e.textContent = `Non riuscito: ${err.message || err}`;
    } finally {
      b._busy = false;
      b.disabled = false;
    }
  }

  // Fill and fit differ only when the video and its box have different shapes: a 4:3 panel in the
  // default 4:3 box looks the same either way, and the button would seem to do nothing (#42).
  // Known only for our own canvas; with HA's card the button stays.
  _fitShape() {
    const c = this._player?.canvas, box = this._videoBox;
    const w = c?.width, h = c?.height, bw = box?.clientWidth, bh = box?.clientHeight;
    const same = !!(w && h && bw && bh) && Math.abs(w / h - bw / bh) < 0.02 * (w / h);
    this._card?.toggleAttribute("data-fit-same", same);
  }

  _applyFit() {
    this._card.dataset.fit = this._cover ? "cover" : "contain";
    const label = this._cover ? "Adatta video" : "Riempi schermo";  // il tasto dice cosa farà
    this._fit.setAttribute("aria-label", label);
    this._fit.title = label;
    this._fit.setAttribute("aria-pressed", this._cover);
    this._icon(this._fit, this._cover ? "mdi:fit-to-screen-outline" : "mdi:arrow-expand-all");
  }

  _setDrawer(open) {
    this._card.dataset.drawer = open;
    for (const b of [this._log, this._photo]) b.setAttribute("aria-expanded", open);
    this._icon(this._log, "mdi:history");  // aperto o chiuso: lo dice lo sfondo del tasto
  }

  // Link diretto (es. dalla notifica): con l'URL .../camera#citofono la card si porta in
  // vista. HA non lo fa per le card; "location-changed" è la navigazione interna di HA.
  // Si ricontrolla anche l'audio automatico (_render, in fondo): l'hash può arrivare
  // (hashchange, navigazione HA) senza che lo stato sia appena cambiato — se non si
  // richiama _render qui, un "in_call" già in corso non aggancerebbe mai l'audio da solo.
  _toAnchor = () => requestAnimationFrame(() => {
    const a = this._cfg?.anchor;
    if (a && this._root && this.isConnected && location.hash === `#${a}`) {
      this.scrollIntoView({ block: "start" });
      if (this._hass) this._render();
    }
  });

  connectedCallback() {
    for (const e of ["hashchange", "location-changed"]) window.addEventListener(e, this._toAnchor);
    this._toAnchor();
    // Rimessa in pagina (cambio di vista e ritorno, a chiamata in corso): il riquadro
    // video si rifà subito, senza aspettare un `hass` nuovo da HA.
    if (this._root && this._hass) this._render();
  }

  // Ultimi squilli: righe da 52 px con separatore quando cambia il giorno. La foto più
  // recente fa da tasto cronologia (e da sfondo dove la scena è visibile da fermo).
  async _loadHistory() {
    const n = (this._histN = (this._histN || 0) + 1);
    try {
      const rings = await this._hass.callApi("GET", `vimar_intercom/rings?limit=${this._cfg.history}`);
      // ?v=: la foto migliore sostituisce la prima sullo stesso nome; il browser non tiene la vecchia.
      const srcs = await Promise.all(rings.map((r) => r.photo && this._sign(`${r.photo}?v=${r.photo_v}`)));
      if (n !== this._histN) return;  // arrivata dopo una più recente
      const nodes = [];
      let day;
      rings.forEach((r, i) => {
        const d = new Date(r.time).toDateString();
        if (d !== day) {
          day = d;
          const l = dayLabel(r.time);
          if (l) { const s = document.createElement("div"); s.className = "day"; s.textContent = l; nodes.push(s); }
        }
        nodes.push(this._ringItem(r, srcs[i]));
      });
      this._hist.replaceChildren(...nodes);
      this._empty.textContent = "Nessuno squillo registrato";
      const still = this._cfg.idle_picture !== "standby" && srcs.find(Boolean);
      for (const img of [this._still, this._pic]) if (still) img.src = still; else img.removeAttribute("src");
      this._photo.disabled = this._histBtn.disabled = !rings.length;
    } catch {
      if (n !== this._histN) return;
      this._hist.replaceChildren();
      this._empty.textContent = "Cronologia non disponibile";
    }
  }

  async _sign(file) {
    return (await this._hass.callWS({ type: "auth/sign_path", path: `/api/vimar_intercom/rings/${file}` })).path;
  }

  _ringItem(r, src) {
    const b = document.createElement("button");
    b.className = "ring";
    b.dataset.outcome = r.outcome;
    b.innerHTML = `<span class="th"><ha-icon icon="mdi:image-off-outline" aria-hidden="true"></ha-icon></span>` +
      `<span class="txt"><span class="at"></span><span class="out"></span></span>`;
    const [th, txt] = b.children;
    const [at, out] = txt.children;
    at.textContent = hm(r.time);
    out.textContent = OUTCOME[r.outcome] || "";
    const cap = `${whenFull(r.time)} · ${out.textContent}`;
    b.setAttribute("aria-label", `Squillo ${cap}${r.clip ? " · video" : ""}`);
    if (src) {
      const img = document.createElement("img");
      img.src = src;
      img.alt = "";
      th.replaceChildren(img);
    }
    if (r.clip) th.insertAdjacentHTML("beforeend", `<span class="play"><ha-icon icon="mdi:play-circle" aria-hidden="true"></ha-icon></span>`);
    if (!src && !r.clip) b.disabled = true;
    else {
      b.onclick = async () => {  // firmata di nuovo: la prima firma scade dopo poco
        try {
          const img = this._dlg.querySelector("img");
          img.hidden = !!r.clip;
          this._clipEl.hidden = !r.clip;
          if (r.clip) this._clipEl.src = await this._sign(r.clip);
          else img.src = await this._sign(`${r.photo}?v=${r.photo_v}`);
          this._dlg.querySelector(".cap").textContent = cap;
          this._dlg.showModal();
          if (r.clip) this._clipEl.play().catch(() => {});  // dove non parte da solo (iOS, dopo l'await) ci sono i controlli
        } catch (e) {  // HA scollegato, file sparito: detto sulla card, non in console
          this._err.textContent = `Media non disponibile: ${e.message || e}`;
        }
      };
    }
    return b;
  }

  // Stato con i secondi di collegamento; oltre SETUP_TIMEOUT_S lo dice in chiaro.
  _tickSetup() {
    const calling = this._state === "calling";
    if (calling && !this._setupAt) {
      this._setupAt = Date.now();
      this._setupTimer = setInterval(() => this._tickSetup(), 1000);
    } else if (!calling && this._setupAt) {
      clearInterval(this._setupTimer);
      this._setupAt = null;
      if (this._err.textContent === NO_ANSWER) this._err.textContent = this._hint;
    }
    const s = this._setupAt ? Math.round((Date.now() - this._setupAt) / 1000) : 0;
    // Card compatta che suona: "Suonano alla porta" nel nome e "Tocca per vedere · mm:ss" nello stato.
    if (this._state !== "idle") this._pendingAt = 0;
    const pending = this._state === "idle" && Date.now() - (this._pendingAt || 0) < PENDING_MS;
    this._card.dataset.pending = pending;
    const ring = this._state === "ringing", compactRing = ring && this._popup && !this._pop.open;
    if (ring && !this._ringAt) {
      this._ringAt = Date.now();
      this._ringTimer = setInterval(() => this._tickSetup(), 1000);
    } else if (!ring && this._ringAt) {
      clearInterval(this._ringTimer);
      this._ringAt = null;
    }
    const r = Math.floor((Date.now() - (this._ringAt || Date.now())) / 1000), two = (n) => String(n).padStart(2, "0");
    this._root.querySelector(".name").textContent = compactRing ? LABEL.ringing : this._cfg.name;
    this._pill.textContent = calling ? `${LABEL.calling} ${s} s`
      : pending ? LABEL.calling
      : compactRing ? `Tocca per vedere · ${two(Math.floor(r / 60))}:${two(r % 60)}` : LABEL[this._state];
    this._badge.textContent = this._pill.textContent;
    if (s >= SETUP_TIMEOUT_S && this._err.textContent === this._hint) this._err.textContent = NO_ANSWER;
  }

  // Il riquadro video. Dal vivo, con WebCodecs: i NAL del WebSocket su un canvas
  // (NalPlayer), senza aprire lo stream di HA; se il player non può, o si rompe,
  // la card standard di HA. Da fermo (o senza WebCodecs) sempre quella.
  _setVideo(live) {
    if (this._live === live) return;
    this._live = live;
    this._player?.close();
    this._player = null;
    this._fitShape();
    this._card.classList.remove("wait");
    if (!live || !NalPlayer.ok()) return this._setPicture(live);
    this._card.classList.add("wait");  // until the first frame: "waiting for video", not a black box
    const canvas = document.createElement("canvas");
    this._player = new NalPlayer(this._hass, canvas, () => {
      this._player = null;
      this._card.classList.remove("wait");
      this._fitShape();
      if (this._live) this._setPicture(true);
    });
    this._player.onsize = () => {  // first frame (and any size change)
      this._card.classList.remove("wait");
      this._fitShape();
    };
    this._videoBox.replaceChildren(canvas);
    this._video = null;
  }

  // La card standard di HA (picture-entity). "live" apre lo stream, che da fermo
  // farebbe chiamare la targa: lo si usa solo a chiamata o squillo in corso.
  async _setPicture(live) {
    this._helpers ||= window.loadCardHelpers();
    const el = (await this._helpers).createCardElement({
      type: "picture-entity", entity: this._ent("camera"), camera_view: live ? "live" : "auto",
      fit_mode: this._cover ? "cover" : "contain",
      show_name: false, show_state: false, tap_action: { action: "none" }, hold_action: { action: "none" },
    });
    if (this._live !== live) return;  // stato cambiato nel frattempo
    el.hass = this._hass;
    this._videoBox.replaceChildren(el);
    this._video = el;
  }

  // Doppio tocco (confirm_open, default): il primo arma per 3 s, il secondo apre. La pressione lunga su iOS
  // litiga con VoiceOver e col menu contestuale; confirm() si conferma di riflesso.
  // Scorciatoie (config `shortcuts`, default la serratura): [{ entity, name, icon }], con dominio lock o button.
  _list() {
    const raw = this._cfg.shortcuts ?? [this._ent("lock")];
    return raw.map((s) => (typeof s === "string" ? { entity: s } : s)).filter((s) => SC[s.entity?.split(".")[0]]);
  }

  // Tasti tondi nella card compatta; si rifanno solo se cambia l'elenco (nome/icona dallo stato).
  _shortcuts() {
    const list = this._list().map((s) => {
      const st = this._hass.states[s.entity], dom = s.entity.split(".")[0];
      return { ...s, name: s.name || (dom === "lock" ? "Apri" : st?.attributes?.friendly_name || s.entity),
        icon: s.icon || st?.attributes?.icon || SC[dom][1], off: st?.state === "unavailable" };
    });
    const key = JSON.stringify(list);
    if (key === this._scKey) return;
    this._scKey = key;
    this._sc.replaceChildren(...list.map((s) => {
      const t = document.createElement("template");
      t.innerHTML = btn("", s.icon, s.name);
      const b = t.content.firstElementChild;
      b.disabled = s.off;
      b.onclick = (e) => { e.stopPropagation(); this._openDoor(b, s.entity); };  // mai il popup né la targa
      return b;
    }));
  }

  // `b`: il tasto che si arma / si conferma (Apri o una scorciatoia), `entity`: la prima scorciatoia se non detto.
  async _openDoor(b = this._open, entity = this._list()[0]?.entity || this._ent("lock")) {
    if (this._cfg.confirm_open && this._armed !== b) {
      this._resetOpen();
      this._armed = this._flash = b;
      this._openTimer = setTimeout(() => this._resetOpen(), 3000);
      b.className = "warn";
      this._label(b, "Tocca ancora");
      return;
    }
    this._resetOpen();
    this._err.textContent = this._hint;
    this._flash = b;
    b.className = "busy";  // subito: il tondo gira mentre il servizio lavora
    try {
      await this._hass.callService(entity.split(".")[0], SC[entity.split(".")[0]][0], { entity_id: entity });
      b.className = "ok";
      this._icon(b, "mdi:check");
      this._label(b, "Aperto");
    } catch (e) {
      let m = e.translation_key ? e.message : `Apertura non riuscita: ${e.message || e}`;
      if (e.translation_key) {  // HA's message is in English: translate it as the frontend does (#128)
        const k = `component.${e.translation_domain}.exceptions.${e.translation_key}.message`;
        const late = new Promise((_, no) => setTimeout(no, 2000));  // a dropped WS must not keep the button spinning
        try {
          m = (await Promise.race([this._hass.loadBackendTranslation("exceptions", e.translation_domain), late]))(
            k, e.translation_placeholders) || m;
        } catch {}
      }
      this._err.textContent = m;
      b.className = "bad";
      this._icon(b, "mdi:alert-circle-outline");
      this._label(b, "Errore");
    }
    this._openTimer = setTimeout(() => this._resetOpen(), OPEN_FLASH_MS);
  }

  _resetOpen() {
    clearTimeout(this._openTimer);
    this._armed = null;
    const b = this._flash;
    this._flash = null;
    if (!b) return;
    b.className = "";
    this._icon(b, b.dataset.icon);
    this._label(b, b.dataset.label);
  }

  async _call(service, button) {
    if (service === "hangup") {
      this._stopAudio();
      this._cancelled = this._state === "calling";  // "Annulla": non è un rifiuto della targa
    }
    this._err.textContent = this._hint;
    button.classList.add("busy");
    button.disabled = true;
    if (service === "call") {  // feedback al tocco: "Collegamento…" finché lo stato non cambia (o fallisce)
      this._pendingAt = Date.now();
      this._tickSetup();
      setTimeout(() => this._tickSetup(), PENDING_MS + 50);
    }
    try {
      const r = await this._hass.callService("vimar_intercom", service, {}, undefined, true, true);
      if (r?.response?.ok === false) throw new Error(r.response.result);
    } catch (e) {
      this._err.textContent = `Non riuscito: ${e.message || e}`;
      this._pendingAt = 0;
      this._tickSetup();
      throw e;
    } finally {
      button.classList.remove("busy");
      button.disabled = false;
    }
  }

  disconnectedCallback() {
    for (const e of ["hashchange", "location-changed"]) window.removeEventListener(e, this._toAnchor);
    if (this._pop.open) this._pop.close();
    if (this._set.open) this._set.close();
    this._stopAudio();
    this._stopListen();
    if (this._player) {  // card tolta dalla pagina: il WS video non resta aperto
      this._player.close();
      this._player = null;
      this._live = undefined;  // al prossimo hass si ricrea
    }
    clearInterval(this._setupTimer);
    clearInterval(this._ringTimer);
    this._ringAt = null;
    clearTimeout(this._holdT);
    this._holdT = null;  // altrimenti al rientro il riquadro resterebbe "dal vivo" da fermo
    this._card.classList.remove("hold");
    this._setupAt = null;
  }

  getCardSize() {
    return this._live || this._card?.dataset.drawer === 'true' ? 4 : 1;
  }
}

// Il frontend di HA, dove il browser non ha i registri "scoped" (Safari/iOS), installa un
// polyfill che sostituisce customElements: definita prima, la card si perde ("Custom element
// doesn't exist"). Si definisce quando HA è partito; le card in attesa si ricostruiscono da sole.
const TAG = "vimar-intercom-card";
const ready = () => document.querySelector("home-assistant")?.hass;
const define = () => {
  customElements.get(TAG) || customElements.define(TAG, VimarIntercomCard);
  customElements.get(EDITOR_TAG) || customElements.define(EDITOR_TAG, VimarIntercomCardEditor);
};
if (ready()) define();
else {
  const wait = setInterval(() => ready() && (clearInterval(wait), define()), 200);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "vimar-intercom-card",
  name: "Citofono Vimar",
  description: "Video (anche allo squillo), parla/ascolta, apri il portone e cronologia degli squilli.",
  preview: true,
});
