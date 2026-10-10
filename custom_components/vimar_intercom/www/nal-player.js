// Live video over the WebSocket (WebCodecs), moved out of vimar-intercom-card.js as is.

const same = (a, b) => !!a && !!b && a.length === b.length && a.every((v, i) => v === b[i]);

const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  parts.reduce((o, p) => (out.set(p, o), o + p.length), 0);
  return out;
};

// Video dal vivo a bassa latenza: i NAL H.264 che il server manda sul WebSocket (0x03 +
// Annex B, gli stessi dell'app iOS) decodificati con WebCodecs e disegnati su un
// <canvas>. Niente stream di HA (go2rtc/HLS: 2-4 s di ritardo, e la targa chiude dopo
// ~10 s). Autonomo: prende un canvas qualunque, chi lo crea lo chiude con close().
// Parte dal primo IDR (il server manda SPS→PPS→IDR, e a chi si collega a video in
// corso rimanda il GOP corrente). Chiama onFail (la card rimette lo stream di HA) solo
// se manca VideoDecoder (HTTP non sicuro, Safari vecchio, WebKit di Playwright) o il
// codec non è supportato: lo stream di HA fuori dalla LAN (5G, niente TURN) è bianco,
// quindi a un WebSocket caduto si riapre (il server rimanda il GOP corrente) e a un
// decoder rotto (dati corrotti, riferimento perso: VideoToolbox su iPhone è severo)
// se ne fa uno nuovo al prossimo IDR, ogni ~3 s dalla targa. Dal campo: seconda
// "Vedi esterno" 3 s dopo la prima, card bianca su 5G.
class NalPlayer {
  static ok = () => !!window.VideoDecoder;

  constructor(hass, canvas, onFail) {
    this.canvas = canvas;
    this.onsize = null;  // the video's size changed: the card checks whether Fill/Fit matters
    this.frames = 0;
    this.resets = 0;  // decoder rifatti e WebSocket riaperti (nei test)
    this._n = 0;
    this._hass = hass;
    this._fail = (why) => {
      if (this._closed) return;
      console.warn("vimar-intercom-card: video WebCodecs non disponibile:", why);
      this.close();
      onFail(why);
    };
    this._open();
  }

  async _open() {
    try {
      const { path } = await this._hass.callWS({ type: "auth/sign_path", path: "/api/vimar_intercom/audio_ws?only=video" });
      if (this._closed) return;
      const ws = (this._ws = new WebSocket(location.origin.replace(/^http/, "ws") + path));
      ws.binaryType = "arraybuffer";
      ws.onopen = () => (this._wait = 0);  // aperto: la prossima caduta riparte da 1 s
      ws.onmessage = (ev) => {
        if (typeof ev.data === "string" || new Uint8Array(ev.data, 0, 1)[0] !== 0x03) return;
        this._nal(new Uint8Array(ev.data, 1));
      };
      ws.onclose = () => this._ws === ws && this._reopen("WebSocket chiuso");
    } catch (e) {
      this._reopen(e.message || e);  // auth/sign_path fallita: connessione con HA in ripristino
    }
  }

  // WebSocket caduto (rete, HA che riparte): si riapre dopo 1, 2, 4… s (massimo 10, così
  // HA fermo non riceve un tentativo al secondo), finché il player non viene chiuso
  // (close(): la card è uscita dal vivo). I P arrivati prima del GOP che il server
  // rimanda non si decodificano (riferimenti persi).
  _reopen(why) {
    if (this._closed) return;
    this._wait = Math.min((this._wait || 0.5) * 2, 10);
    console.warn("vimar-intercom-card: video WebSocket:", why, `— riapro fra ${this._wait} s`);
    this.resets++;
    this._ws = null;
    this._skip = true;
    this._t = setTimeout(() => this._open(), this._wait * 1000);
  }

  // Un NAL in Annex B (00 00 00 01 + NAL). SPS e PPS si tengono (vanno nella description
  // del decoder); l'IDR è il chunk chiave (configura il decoder la prima volta o a SPS/PPS
  // nuovi), i P seguono. Mai un P prima del primo IDR (_dec non c'è) o dopo un buco
  // (_skip): il decoder darebbe errore.
  _nal(nal) {
    const t = nal[4] & 0x1f;
    if (t === 7) this._sps = nal;
    else if (t === 8) this._pps = nal;
    else if (t === 5 && this._sps && this._pps) {
      try {
        // A new SPS/PPS (another panel, another resolution) needs a new description.
        if (!this._dec || !same(this._cfgSps, this._sps) || !same(this._cfgPps, this._pps)) this._configure();
        this._skip = false;
        this._decode("key", nal);
      } catch (e) {
        this._broken(e);
      }
    } else if (t === 1 && this._dec && !this._skip) {
      // Il decoder non tiene il passo: via i P fino al prossimo IDR, non si accumula ritardo.
      // Until this decoder's first frame allow a longer queue: one that starts slowly
      // (software, cold) would otherwise lose the whole first GOP and the picture would
      // show up 3 s late (#130). 60 = 4 s, a decoder that never answers still gets capped.
      if (this._dec.decodeQueueSize > (this._out ? 8 : 60)) this._skip = true;
      else this._decode("delta", nal);
    }
  }

  // The decoder gets AVC (avcC description + length-prefixed NALs), not Annex B: on iPhone
  // (iOS 27) the picture smeared between keyframes with Annex B input while a PC was clean
  // and Home Assistant had every packet (#53). Chrome takes both.
  _configure() {
    const s = this._sps.subarray(4), p = this._pps.subarray(4);  // without the start code
    // profile_idc, constraint_set, level_idc → "avc1.42C01E"
    const codec = "avc1." + [s[1], s[2], s[3]].map((b) => b.toString(16).padStart(2, "0")).join("").toUpperCase();
    const description = concat(
      Uint8Array.of(1, s[1], s[2], s[3], 0xff, 0xe1, s.length >> 8, s.length & 0xff), s,
      Uint8Array.of(1, p.length >> 8, p.length & 0xff), p);
    try { this._dec?.close(); } catch { /* già chiuso */ }
    this._dec = new VideoDecoder({ output: (f) => this._paint(f), error: (e) => this._broken(e) });
    // iOS: VideoToolbox in real-time mode may drop frames under load, and with every P frame
    // a reference the picture smears until the next keyframe. Elsewhere latency wins.
    // Codec non supportato: arriva da `error`.
    const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    // The panel's SD (PAL) H.264 has no colour info in its VUI and browsers then assume BT.709,
    // which tints skin and sky. Say BT.601 explicitly.
    const colorSpace = { primaries: "smpte170m", transfer: "smpte170m", matrix: "smpte170m", fullRange: false };
    this._dec.configure({ codec, description, colorSpace, optimizeForLatency: !ios });
    this._cfgSps = this._sps;
    this._cfgPps = this._pps;
    this._out = 0;  // frames out of this decoder
  }

  _decode(type, nal) {
    const body = nal.subarray(4), data = new Uint8Array(body.length + 4);  // 4-byte length + NAL
    new DataView(data.buffer).setUint32(0, body.length);
    data.set(body, 4);
    try {
      this._dec.decode(new EncodedVideoChunk({ type, timestamp: this._n++ * 66667, data }));
    } catch (e) {
      this._broken(e);
    }
  }

  // Decoder rotto: si butta e se ne fa uno nuovo al prossimo IDR. Solo un codec non
  // supportato manda la card allo stream di HA.
  _broken(e) {
    if (this._closed) return;
    if (e.name === "NotSupportedError") return this._fail(e.message);
    console.warn("vimar-intercom-card: decoder video:", e.message || e, "— riparto dal prossimo IDR");
    this.resets++;
    try { this._dec?.close(); } catch { /* già chiuso dall'errore */ }
    this._dec = null;
  }

  _paint(frame) {
    const c = this.canvas;
    if (c.width !== frame.displayWidth || c.height !== frame.displayHeight) {
      c.width = frame.displayWidth;
      c.height = frame.displayHeight;
      this.onsize?.();
    }
    (this._ctx ||= c.getContext("2d")).drawImage(frame, 0, 0);
    frame.close();
    this.frames++;
    this._out++;
  }

  close() {
    this._closed = true;
    clearTimeout(this._t);
    const ws = this._ws;
    this._ws = null;
    if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
    try { this._dec?.close(); } catch { /* già chiuso dopo un errore */ }
    this._dec = null;
  }
}

export { NalPlayer, same };
