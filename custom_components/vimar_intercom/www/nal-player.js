// Live video over the WebSocket (WebCodecs), moved out of vimar-intercom-card.js as is.

const same = (a, b) => !!a && !!b && a.length === b.length && a.every((v, i) => v === b[i]);

const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  parts.reduce((o, p) => (out.set(p, o), o + p.length), 0);
  return out;
};

const HEADER_BYTES = 40;  // read by bits(): enough for the SPS fields and frame_num

// Bit reader over a NAL's RBSP (Annex B: header at [4]), emulation prevention (00 00 03)
// removed. `max`: bytes read (the slice header's frame_num needs only the first few).
const bits = (nal, max = nal.length) => {
  const b = [];
  for (let i = 5, z = 0; i < Math.min(nal.length, max); i++) {
    if (z >= 2 && nal[i] === 3) { z = 0; continue; }
    z = nal[i] ? 0 : z + 1;
    b.push(nal[i]);
  }
  let p = 0;
  const u = (n) => { let v = 0; for (; n > 0; n--, p++) v = v * 2 + ((b[p >> 3] >> (7 - (p & 7))) & 1); return v; };
  const ue = () => { let z = 0; while (!u(1)) if (++z > 31) return NaN; return 2 ** z - 1 + u(z); };
  return { u, ue, b, pos: () => p };
};

// Profiles whose SPS carries chroma format, bit depths and scaling lists.
const HIGH_PROFILES = [100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135];

// The SPS fields the player needs: profile, log2_max_frame_num, size, reference frames, and
// where the VUI and its bitstream_restriction are (bit positions in the RBSP). null: a High
// profile SPS with scaling lists. shortcut: scaling lists not parsed, such streams get no
// frame_num check and keep their SPS as is.
const parseSps = (nal) => {
  const r = bits(nal), s = { profile: r.u(8), bytes: r.b };
  r.u(16);  // constraint flags, level
  r.ue();   // sps id
  if (HIGH_PROFILES.includes(s.profile)) {
    if (r.ue() === 3) r.u(1);  // chroma_format_idc, separate_colour_plane
    r.ue(); r.ue(); r.u(1);    // bit depths, qpprime bypass
    if (r.u(1)) return null;   // seq_scaling_matrix_present
  }
  s.fnBits = r.ue() + 4;
  const poc = r.ue();
  if (poc === 0) r.ue();
  else if (poc === 1) {
    r.u(1); r.ue(); r.ue();
    for (let n = r.ue(); n > 0; n--) r.ue();
  }
  s.refs = r.ue();
  r.u(1);  // gaps_in_frame_num_allowed
  s.width = (r.ue() + 1) * 16;
  const mapUnits = r.ue() + 1, frameOnly = r.u(1);
  s.height = (2 - frameOnly) * mapUnits * 16;  // before cropping
  if (!frameOnly) r.u(1);  // mb_adaptive_frame_field
  r.u(1);  // direct_8x8_inference
  if (r.u(1)) { r.ue(); r.ue(); r.ue(); r.ue(); }  // cropping
  s.vuiAt = r.pos();
  s.vui = !!r.u(1);
  if (s.vui) {
    if (r.u(1) && r.u(8) === 255) r.u(32);          // aspect ratio (255: explicit SAR)
    if (r.u(1)) r.u(1);                             // overscan
    if (r.u(1)) { r.u(4); if (r.u(1)) r.u(24); }    // video signal, colour description
    if (r.u(1)) { r.ue(); r.ue(); }                 // chroma sample location
    if (r.u(1)) { r.u(32); r.u(32); r.u(1); }       // timing
    const hrd = () => {
      const n = r.ue() + 1;
      r.u(8);  // bit rate and cpb size scale
      for (let i = 0; i < n; i++) { r.ue(); r.ue(); r.u(1); }
      r.u(20);  // delay and length fields
    };
    const nalHrd = r.u(1);
    if (nalHrd) hrd();
    const vclHrd = r.u(1);
    if (vclHrd) hrd();
    if (nalHrd || vclHrd) r.u(1);  // low_delay_hrd
    r.u(1);                        // pic_struct_present
    s.restrictAt = r.pos();
    if (r.u(1)) {
      r.u(1); r.ue(); r.ue(); r.ue(); r.ue();  // mv over boundaries, bytes/bits limits, mv lengths
      s.reorder = r.ue();
      s.dpb = r.ue();
    }
  }
  s.end = r.pos();
  return s;
};

// log2_max_frame_num from the SPS (the panel's: 16). 0 = not known: no frame_num check then.
const frameNumBits = (sps) => parseSps(sps)?.fnBits || 0;

// The panel's SPS (Baseline, no B frames) has no VUI bitstream_restriction, so a decoder must
// assume pictures may need reordering and fills its whole buffer before giving any out: ~17
// frames (~1.1 s) held and released in a burst at each IDR, in Chrome and WebKit alike, and
// the draw queue dropped the burst. The SPS given to the decoder says it: no reordering
// (max_num_reorder_frames 0) and the smallest buffer (max_dec_frame_buffering = the reference
// frames, at least 1). A VUI without the restriction gets it appended; an SPS that has one, or
// one we can't parse, stays as is. The result is a valid RBSP (trailing bits, emulation prevention).
const lowDelaySps = (nal) => {
  const s = parseSps(nal);
  if (!s || s.reorder !== undefined || !(s.end <= s.bytes.length * 8)) return nal;
  const out = [];
  const put = (v, n) => { for (let i = n - 1; i >= 0; i--) out.push(Math.floor(v / 2 ** i) % 2); };
  const putUe = (v) => { const n = Math.floor(Math.log2(v + 1)); put(0, n); put(v + 1, n + 1); };
  const keep = s.vui ? s.restrictAt : s.vuiAt;
  for (let i = 0; i < keep; i++) out.push((s.bytes[i >> 3] >> (7 - (i & 7))) & 1);
  if (!s.vui) put(0b100000000, 9);  // VUI present; no aspect, overscan, signal, chroma, timing, HRD, pic_struct
  put(1, 1);                         // bitstream_restriction_flag
  put(1, 1); putUe(0); putUe(0);     // mv over pic boundaries; no bytes/bits per picture limits
  putUe(16); putUe(16);              // log2_max_mv_length (no limit)
  putUe(0); putUe(Math.max(1, s.refs));
  out.push(1);                       // rbsp_trailing_bits
  while (out.length % 8) out.push(0);
  const rbsp = [];
  for (let i = 0; i < out.length; i += 8) rbsp.push(out.slice(i, i + 8).reduce((v, x) => v * 2 + x, 0));
  const esc = [];
  for (let i = 0, z = 0; i < rbsp.length; i++) {
    if (z >= 2 && rbsp[i] <= 3) { esc.push(3); z = 0; }
    esc.push(rbsp[i]);
    z = rbsp[i] ? 0 : z + 1;
  }
  const res = Uint8Array.from([0, 0, 0, 1, nal[4], ...esc]);
  return parseSps(res)?.reorder === 0 ? res : nal;  // safety net: never hand the decoder a broken rewrite
};

// frame_num of a slice: first_mb, slice_type, pps id, then frame_num (n bits).
const frameNum = (nal, n) => {
  const r = bits(nal, HEADER_BYTES);
  r.ue(); r.ue(); r.ue();
  return r.u(n);
};

// Decoded frames waiting for the screen: the jitter buffer that absorbs the next 5G gap
// (~125 ms at 24 fps). Over it the oldest go at once: already decoded pictures, so dropping
// them breaks no reference and does not freeze until the next IDR; the picture jumps forward
// and latency stays bounded.
const QUEUE_MAX = 3;
const STALL_MS = 8000;         // no frame out of the decoder for this long, page visible: reconnect
const DECODE_QUEUE_MAX = 30;   // chunks waiting in the decoder: more, P dropped until the next IDR
const STEADY_MS = [15, 150];   // arrivals this far apart teach the frame interval (not bursts, not gaps)
const SMOOTHING = 16;          // the interval follows each steady arrival by 1/SMOOTHING
const HALF_VSYNC_MS = 8;       // a frame is due this early: half a 60 Hz vsync
const MAX_LAG_MS = 1500;       // video this far behind the server (video_ts): reconnect

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
    this.frames = 0;  // frames out of the decoder (drawn or dropped by the jitter buffer)
    this.resets = 0;  // decoder rifatti e WebSocket riaperti (nei test)
    this._n = 0;
    this._queue = [];           // decoded frames, drawn one per frame interval (_tick)
    this._interval = 0;         // frame interval, learnt from the decoder's output (0: not yet, draw at once)
    this._lastOutput = 0;       // when the decoder last gave a frame (the interval)
    this._afterGap = false;     // the last arrival came after a real gap: its burst does not teach
    this._lagFloor = Infinity;  // smallest (our clock - server clock) on this WebSocket
    this._shownAt = 0;          // when the last frame was drawn
    this._raf = 0;
    this._stallFrom = 0;        // the watchdog's clock: 0 until the first frame
    this._fnBits = 0;           // log2_max_frame_num of the configured SPS, 0 = no frame_num check
    this._fnMiss = 0;           // GOPs in a row whose frame_num check failed
    this._nalAt = 0;            // when the last NAL arrived on the current socket
    this._lastFrameNum = 0;     // frame_num of the last reference
    this._hass = hass;
    this._fail = (why) => {
      if (this._closed) return;
      console.warn("vimar-intercom-card: video WebCodecs non disponibile:", why);
      this.close();
      onFail(why);
    };
    this._open();
    this._watchdog = setInterval(this._checkStall, 1000);
  }

  // Watchdog: a dead 5G link may not close the WebSocket on WebKit for over 90 s (measured on
  // iOS), so the picture froze until the call ended. No frame for STALL_MS:
  // a new WebSocket (the server sends the current GOP again) and a new decoder (a stuck one
  // would not wake up on that IDR either). Armed by the first frame, so a "calling" with no
  // video yet does not reconnect in a loop; not while the page is hidden (a desktop tab:
  // the card keeps the player there, the decoder may be throttled).
  _checkStall = () => {
    if (!this._stallFrom) return;
    const now = performance.now();
    if (document.hidden) {
      this._stallFrom = now;
      return;
    }
    // Only once this socket brought data: a fresh one that stays silent is not reopened in a loop.
    if (now - this._stallFrom < STALL_MS || !this._ws || !this._nalAt) return;  // !_ws: already reopening
    this._stallFrom = now;
    this._ws.close();  // its late onclose is ignored: _ws is no longer it
    try { this._dec?.close(); } catch { /* already closed */ }
    this._dec = null;  // the next IDR configures a new one
    this._reopen(`nessun fotogramma da ${STALL_MS / 1000} s`);
  };

  async _open() {
    try {
      const { path } = await this._hass.callWS({ type: "auth/sign_path", path: "/api/vimar_intercom/audio_ws?only=video&ts=1" });
      if (this._closed) return;
      const ws = (this._ws = new WebSocket(location.origin.replace(/^http/, "ws") + path));
      ws.binaryType = "arraybuffer";
      ws.onopen = () => { this._wait = 0; if (this._stallFrom) this._stallFrom = performance.now(); };  // aperto: la prossima caduta riparte da 1 s; the watchdog counts from here
      this._lagFloor = Infinity;
      ws.onmessage = (ev) => {
        if (this._ws !== ws) return;
        if (typeof ev.data === "string") return this._text(ev.data);
        if (new Uint8Array(ev.data, 0, 1)[0] === 0x03) this._nal(new Uint8Array(ev.data, 1));
      };
      ws.onclose = () => this._ws === ws && this._reopen("WebSocket chiuso");
    } catch (e) {
      this._reopen(e.message || e);  // auth/sign_path fallita: connessione con HA in ripristino
    }
  }

  // Behind a proxy or tunnel (NGINX, Cloudflared) the server's sends never block, so a link
  // slower than the video lets a backlog grow in the proxy and the picture falls seconds behind.
  // The server sends {"type":"video_ts","t":<its monotonic ms>} before each live IDR. The two
  // clocks have unrelated origins, so the delay is relative: the smallest (now - t) seen on this
  // WebSocket is the offset, and more than MAX_LAG_MS over it reconnects (the server sends the
  // current GOP). Both clocks monotonic: a wall clock jump causes no spurious reconnect. Any
  // sample may come first: a late one only raises (now - t), so it never sets the offset too low.
  // Other text messages are ignored.
  _text(data) {
    let m;
    try { m = JSON.parse(data); } catch { return; }
    if (m?.type !== "video_ts" || typeof m.t !== "number") return;
    const lag = performance.now() - m.t;
    this._lagFloor = Math.min(this._lagFloor, lag);
    if (lag - this._lagFloor <= MAX_LAG_MS) return;
    this._ws.close();  // its late onclose is ignored: _ws is no longer it
    this._reopen(`video ${((lag - this._lagFloor) / 1000).toFixed(1)} s indietro`);
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
    this._nalAt = 0;  // the next socket must bring its own data before the watchdog can close it
    this._skip = true;
    this._t = setTimeout(() => this._open(), this._wait * 1000);
  }

  // Un NAL in Annex B (00 00 00 01 + NAL). SPS e PPS si tengono (vanno nella description
  // del decoder); l'IDR è il chunk chiave (configura il decoder la prima volta o a SPS/PPS
  // nuovi), i P seguono. Mai un P prima del primo IDR (_dec non c'è) o dopo un buco
  // (_skip): il decoder darebbe errore.
  _nal(nal) {
    this._nalAt = performance.now();
    const t = nal[4] & 0x1f;
    if (t === 7) this._sps = nal;
    else if (t === 8) this._pps = nal;
    else if (t === 5 && this._sps && this._pps) {
      try {
        // A new SPS/PPS (another panel, another resolution) needs a new description.
        if (!this._dec || !same(this._cfgSps, this._sps) || !same(this._cfgPps, this._pps)) this._configure();
        this._skip = false;
        this._lastFrameNum = 0;  // an IDR's frame_num
        this._decode("key", nal);
      } catch (e) {
        this._broken(e);
      }
    } else if (t === 1 && this._dec && !this._skip) {
      // A P whose reference never got here (lost upstream, a queue emptied, a new SSRC): fed to
      // VideoToolbox it smears until the next IDR. frame_num says so:
      // each picture is the last reference's + 1 (the same value: another slice or the second
      // field of that picture). Then the last good frame stays until the IDR.
      if (this._fnBits) {
        const fn = frameNum(nal, this._fnBits), last = this._lastFrameNum;
        if (fn !== last && fn !== (last + 1) % 2 ** this._fnBits) {
          console.debug(`vimar-intercom-card: video: frame_num ${fn} dopo ${last}, aspetto l'IDR`);
          // A stream this check misreads fails in every GOP and would freeze until the watchdog:
          // after 3 GOPs in a row with no good P-frame, stop checking it.
          if (++this._fnMiss >= 3) this._fnBits = 0;
          this._skip = true;
          return;
        }
        this._fnMiss = 0;
        if (nal[4] & 0x60) this._lastFrameNum = fn;  // nal_ref_idc: only references move it
      }
      // A decoder that really can't keep up: P dropped until the next IDR. 30, not 8: a 5G
      // burst (a second of video at once) or a cold decoder (#130) went over 8 and froze the
      // picture up to 3 s; extra latency is the draw queue's to drop (_paint).
      if (this._dec.decodeQueueSize > DECODE_QUEUE_MAX) this._skip = true;
      else this._decode("delta", nal);
    }
  }

  // The decoder gets AVC (avcC description + length-prefixed NALs), not Annex B: on iPhone
  // (iOS 27) the picture smeared between keyframes with Annex B input while a PC was clean
  // and Home Assistant had every packet (#53). Chrome takes both.
  _configure() {
    const s = lowDelaySps(this._sps).subarray(4), p = this._pps.subarray(4);  // without the start code
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
    this._fnBits = frameNumBits(this._sps);
    this._fnMiss = 0;
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

  // Decoder output: queued, drawn by _tick. Over 5G frames come in bursts after a gap;
  // drawn as they came out, a burst showed only its last frame (a jump) and then nothing
  // (the "saltella"). The interval is learnt from steady arrivals only.
  _paint(frame) {
    if (this._closed) return frame.close();
    const now = performance.now(), d = now - this._lastOutput;
    // Bunched arrivals (a late frame, then the next ones at once) count whole, or the interval
    // grows and the full queue drops a frame every few; only the burst after a real gap is not.
    if (d >= STEADY_MS[1]) this._afterGap = true;
    else if (d > STEADY_MS[0]) this._afterGap = false;
    if (this._lastOutput && d < STEADY_MS[1] && !(this._afterGap && d <= STEADY_MS[0])) {
      this._interval += (d - this._interval) / (this._interval ? SMOOTHING : 1);
    }
    this._lastOutput = this._stallFrom = now;
    this.frames++;
    this._queue.push(frame);
    while (this._queue.length > QUEUE_MAX) this._queue.shift().close();  // also a hidden page: no rAF
    this._raf ||= requestAnimationFrame(this._tick);
  }

  // One frame per interval, on the clock and not on the vsync it landed on: 24 fps on 60 Hz
  // alternates 2 and 3 vsyncs instead of settling on 3 (20 fps) and growing the queue.
  // After a pause, no catch-up rush.
  _tick = (now) => {
    this._raf = 0;
    if (this._closed || !this._queue.length) return;
    if (now - this._shownAt >= this._interval - HALF_VSYNC_MS) {
      this._shownAt = Math.max(this._shownAt + this._interval, now - this._interval / 2);
      this._draw(this._queue.shift());
    }
    if (this._queue.length) this._raf = requestAnimationFrame(this._tick);
  };

  _draw(frame) {
    const c = this.canvas;
    try {
      if (c.width !== frame.displayWidth || c.height !== frame.displayHeight) {
        c.width = frame.displayWidth;
        c.height = frame.displayHeight;
        this.onsize?.();
      }
      (this._ctx ||= c.getContext("2d")).drawImage(frame, 0, 0);
    } finally {
      frame.close();
    }
  }

  close() {
    this._closed = true;
    clearTimeout(this._t);
    clearInterval(this._watchdog);
    cancelAnimationFrame(this._raf);
    for (const f of this._queue.splice(0)) f.close();
    const ws = this._ws;
    this._ws = null;
    if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
    try { this._dec?.close(); } catch { /* già chiuso dopo un errore */ }
    this._dec = null;
  }
}

export { NalPlayer, same, frameNum, frameNumBits, parseSps, lowDelaySps };
