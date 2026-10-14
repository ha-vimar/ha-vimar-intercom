// The card's audio (listen, talk, mute), moved out of vimar-intercom-card.js as is:
// the same methods, mixed into the card class.

const RATE = 8000;

// Why the microphone failed, in words the user can act on. A denied permission is
// usually the app's own (iOS asks once per app and never again), not the page's.
const micError = (e) => {
  if (["NotAllowedError", "SecurityError"].includes(e.name)) {
    const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    return ios ? "Permesso del microfono negato: Impostazioni → Home Assistant (o Safari) → Microfono."
      : "Permesso del microfono negato: consentilo a questo sito o app.";
  }
  return `Audio non disponibile: ${e instanceof DOMException ? `${e.name}: ${e.message}` : e.message || e}`;
};

const CardAudio = (Base) => class extends Base {
  // Card fuori dalla pagina o app in background (_hidden, card-page.js): niente audio nuovo.
  get _gone() {
    return !this.isConnected || this._hidden;
  }

  // Crea un AudioContext e prova a sbloccarlo (resume): se iOS lo tiene sospeso per
  // mancanza di un gesto vero, lo chiude, segnala _audioBlocked ("Microfono"/"Ascolta"
  // → "Audio") e torna null. Usato da _startTalk (solo per `auto`, senza un tocco
  // davanti) e da _startListen (mai un tocco davanti).
  async _unlockedContext() {
    const ctx = new AudioContext();
    await ctx.resume().catch(() => {});
    if (ctx.state === "running") return ctx;
    ctx.close();
    this._audioBlocked = true;
    this._render();
    return null;
  }

  // `auto`: chiamata da sola all'arrivo sull'ancora già "in_call" (vedi _render), non da
  // un tocco. Niente "Rispondi" in HTTP (aprire la pagina non risponde da sola) e, se
  // Safari/iOS tiene l'AudioContext sospeso senza un gesto vero, si rinuncia in silenzio
  // prima ancora di chiedere il microfono: resta il tocco su "Microfono".
  async _startTalk(auto = false) {
    this._stopListen();  // l'ascolto allo squillo lascia il posto all'audio vero (stesso canale)
    this._err.textContent = this._hint;
    // No microphone API (HTTP, or a webview without it): "Rispondi" still answers with
    // video only, and every tap says why there is no voice instead of doing nothing.
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      if (auto) return;
      if (this._state === "ringing") {
        this._mine = true;
        await this._call("answer", this._talk).catch(() => {});
      }
      if (this._err.textContent === this._hint) {
        this._err.textContent = window.isSecureContext ? "Microfono non disponibile in questa app o browser."
          : "Microfono non disponibile: serve HTTPS (usa l'indirizzo https di Home Assistant).";
      }
      return;
    }
    this._starting = true;  // doppio tocco durante il permesso: una chiamata sola
    // Il tocco era "Rispondi": se lo squillo finisce mentre iOS chiede il permesso
    // del microfono (ha risposto il Tab), non si chiama la targa al suo posto.
    const answering = this._state === "ringing";
    // Nel gesto, prima di ogni await: creato dopo, Safari/iOS lo lascia sospeso
    // (niente voce del visitatore e onaudioprocess fermo, quindi niente microfono).
    const ctx = auto ? await this._unlockedContext() : new AudioContext();
    if (auto && !ctx) {  // niente gesto vero: non si chiede nemmeno il microfono
      this._starting = false;
      return;
    }
    let mic;
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      });
      if (this._state === "ringing") {
        this._mine = true;
        await this._call("answer", this._talk);
      } else if (answering && this._state !== "in_call") throw new Error("lo squillo è finito");
      else if (this._state !== "in_call") {
        this._mine = true;
        await this._call("call", this._talk);
      }
      this._audioBlocked = false;
      await this._openAudio(mic, ctx);
    } catch (e) {
      // Uscita dalla card o dall'app nel frattempo: nessun "card chiusa" da mostrare al ritorno.
      if (!auto && !this._gone && this._err.textContent === this._hint) this._err.textContent = micError(e);
      mic?.getTracks().forEach((t) => t.stop());  // il microfono non resta acceso
      if (!this._audio) ctx.close();
      this._stopAudio();
    } finally {
      this._starting = false;
    }
  }

  // Riproduce la voce del visitatore (0x01 + PCM16LE) su un AudioContext: usato sia dal
  // parlato vero (_openAudio, col microfono) sia dal solo ascolto allo squillo
  // (_startListen, senza microfono). Un chiusura sola per chiamata: `playAt` vive qui.
  // Passa da un GainNode (_gain) così il tasto "Audio" può azzerare solo questa
  // riproduzione: niente riaggancio, niente microfono, WebSocket sempre aperto.
  // The 8 kHz voice is resampled here to the context's rate, continuously across packets
  // (the last sample and the fractional position carry over): an 8 kHz AudioBuffer per
  // 20 ms packet was resampled by the browser one packet at a time, with a click at every
  // edge, 50 times a second (#53). 120 ms ahead of the clock absorbs the relay's jitter
  // (up to ~150 ms measured); each underrun adds 40 ms, up to 300 ms. A 3.6 kHz low-pass (the
  // G.711 band ends at 3.4 kHz) removes the images that linear interpolation leaves above
  // the voice, heard as a metallic hiss.
  _pcmSink(ctx, gain) {
    const step = RATE / ctx.sampleRate;
    const lp = ctx.createBiquadFilter();
    lp.type = "lowpass";
    lp.frequency.value = 3600;
    lp.connect(gain);
    let playAt = 0, last = 0, pos = 0, ahead = 0.12, started = false;
    return (ev) => {
      if (typeof ev.data === "string" || new Uint8Array(ev.data, 0, 1)[0] !== 0x01) return;
      const pcm = new Int16Array(ev.data.slice(1)), n = pcm.length;
      if (!n) return;
      const at = (i) => (i < 0 ? last : pcm[i] / 32768);  // index -1 is the previous packet's last sample
      const count = Math.ceil((n - pos) / step);
      const buf = ctx.createBuffer(1, count, ctx.sampleRate);
      const ch = buf.getChannelData(0);
      let p = pos;
      for (let k = 0; k < count; k++, p += step) {
        const i = Math.floor(p), f = p - i;
        ch[k] = at(i - 1) + (at(i) - at(i - 1)) * f;
      }
      pos = p - n;
      last = pcm[n - 1] / 32768;
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(lp);
      if (playAt < ctx.currentTime + 0.01) {
        if (started) ahead = Math.min(ahead + 0.04, 0.3);  // ran dry: keep more in hand
        started = true;
        playAt = ctx.currentTime + ahead;
      }
      src.start(playAt);
      playAt += buf.duration;
    };
  }

  // Un guadagno per sessione (`key`: _listenGain o _talkGain): lo tocca solo il tasto "Audio",
  // mai il microfono o il WebSocket. Il muto dura finché dura la diretta.
  _gain(ctx, key) {
    const g = (this[key] = ctx.createGain());
    g.gain.value = this._muted ? 0 : 1;
    g.connect(ctx.destination);
    return g;
  }

  _toggleMute() {
    this._muted = !this._muted;
    for (const g of [this._listenGain, this._talkGain]) if (g) g.gain.value = this._muted ? 0 : 1;
    this._render();
  }

  // Ascolto senza rispondere: solo ricezione, niente microfono né "answer"/"call" — un
  // WebSocket audio a sé, separato da quello video (NalPlayer scarta i pacchetti 0x01) e
  // da quello del parlato vero (_openAudio, che lo scavalca: vedi _startTalk). `auto`:
  // richiamato da solo da `_render` quando `listen_on_ring` è acceso (stesso limite iOS
  // del parlato: senza un gesto vero l'AudioContext resta sospeso, si rinuncia in
  // silenzio e il tasto "Audio" spento resta lì pronto al tocco). Senza `auto`: il tasto
  // "Audio" stesso, tocco vero — parte comunque, anche a `listen_on_ring` spento: è
  // l'utente a chiederlo, non l'anteprima automatica.
  async _startListen(auto = false) {
    if (!window.isSecureContext || this._gone) return;
    this._listenStarting = true;
    let ctx;
    try {
      ctx = await this._unlockedContext();
      if (!ctx) return;
      const { path } = await this._hass.callWS({ type: "auth/sign_path", path: "/api/vimar_intercom/audio_ws?only=audio" });
      // Stato cambiato nell'attesa: uscito dalla card o dall'app, o (solo per l'automatico) l'anteprima
      // non serve più (config spenta dall'editor, oppure il parlato vero l'ha scavalcata).
      if (this._gone || this._ws || (auto && !this._cfg.listen_on_ring)) return ctx.close();
      const ws = new WebSocket(location.origin.replace(/^http/, "ws") + path);
      ws.binaryType = "arraybuffer";
      ws.onmessage = this._pcmSink(ctx, this._gain(ctx, "_listenGain"));
      ws.onclose = () => { if (this._listenWs === ws) this._stopListen(); };
      this._listenCtx = ctx;
      this._listenWs = ws;
      this._audioBlocked = false;
      this._render();
    } catch (e) {
      if (ctx && this._listenCtx !== ctx) ctx.close();  // firma o WebSocket falliti: l'AudioContext non resta aperto
      throw e;
    } finally {
      this._listenStarting = false;
    }
  }

  // `_render` la richiama a ogni giro: senza nulla da fermare è un no-op.
  _stopListen() {
    this._listenWs?.close();
    this._listenWs = null;
    this._listenCtx?.close();
    this._listenCtx = null;
    this._listenGain = null;
  }

  async _openAudio(mic, ctx) {
    // Card tolta dalla pagina mentre iOS chiedeva il permesso del microfono: niente
    // WebSocket orfano (chi la chiama spegne il microfono).
    if (this._gone) throw new Error("card chiusa");
    // WebSocket con percorso firmato: il browser non può mandare il token negli header.
    const { path } = await this._hass.callWS({ type: "auth/sign_path", path: "/api/vimar_intercom/audio_ws?only=audio" });
    // La card può uscire (cambio vista, app in background) durante la firma: senza questo
    // controllo nascerebbero WebSocket e microfono orfani, che nessuno più spegne.
    if (this._gone) throw new Error("card chiusa");
    const ws = new WebSocket(location.origin.replace(/^http/, "ws") + path);
    this._ws = ws;  // da qui _stopAudio lo chiude anche se qualcosa sotto fallisce
    ws.binaryType = "arraybuffer";
    ctx.resume();
    ws.onmessage = this._pcmSink(ctx, this._gain(ctx, "_talkGain"));  // voce del visitatore
    ws.onclose = () => {
      // Solo la sessione corrente: chiuso da noi, o un WS vecchio (microfono spento
      // e riacceso in fretta) che si chiude in ritardo e spegnerebbe quello nuovo.
      if (this._ws !== ws) return;
      this._err.textContent = "Audio interrotto.";
      this._stopAudio();
    };

    // Microfono → 8 kHz PCM16. ponytail: ScriptProcessor e media semplice per
    // ricampionare; AudioWorklet se servisse meno latenza.
    const source = ctx.createMediaStreamSource(mic);
    const proc = ctx.createScriptProcessor(2048, 1, 1);
    const step = ctx.sampleRate / RATE;
    proc.onaudioprocess = (ev) => {
      if (ws.readyState !== WebSocket.OPEN) return;
      const inp = ev.inputBuffer.getChannelData(0);
      const n = Math.floor(inp.length / step);
      const out = new Uint8Array(1 + n * 2);
      const view = new DataView(out.buffer);
      out[0] = 0x02;
      for (let i = 0; i < n; i++) {
        let sum = 0;
        const a = Math.floor(i * step), b = Math.floor((i + 1) * step);
        for (let j = a; j < b; j++) sum += inp[j];
        const s = Math.max(-1, Math.min(1, sum / (b - a)));
        view.setInt16(1 + i * 2, s * 32767, true);
      }
      ws.send(out);
    };
    source.connect(proc);
    proc.connect(ctx.destination);  // necessario perché onaudioprocess giri (esce silenzio)
    this._audio = { ctx, mic, proc, source };
    this._render();
  }

  _stopAudio() {
    const a = this._audio;
    this._audio = null;
    if (a) {
      a.proc.disconnect();
      a.source.disconnect();
      a.mic.getTracks().forEach((t) => t.stop());
      a.ctx.close();
    }
    const ws = this._ws;
    this._ws = null;
    if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
    this._talkGain = null;
    if ((a || ws) && this._root) this._render();
  }
};

export { CardAudio };
