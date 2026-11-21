// node tests/js/check_pacing.mjs (tests/test_card_js.py): _paint/_tick di nal-player.js con un orologio finto e vsync a 60 Hz.
import assert from "node:assert/strict";

let T = 0, raf = null;
Object.defineProperty(globalThis, "performance", { value: { now: () => T } });
Object.assign(globalThis, {
  window: {}, document: { hidden: false },
  requestAnimationFrame: (f) => ((raf = f), 1), cancelAnimationFrame: () => (raf = null),
  WebSocket: { OPEN: 1 },
});
const { NalPlayer } = await import("../../custom_components/vimar_intercom/www/nal-player.js");
const QUEUE_MAX = 3;  // as in nal-player.js

const run = (arrivals, until) => {
  let drawn = 0, closed = 0, i = 0;
  const canvas = { width: 0, height: 0, getContext: () => ({ drawImage: () => drawn++ }) };
  const p = new NalPlayer({ callWS: () => new Promise(() => {}) }, canvas, () => {});
  const shown = [];
  for (let v = 0; v <= until; v += 1000 / 60) {
    for (; i < arrivals.length && arrivals[i] <= v; i++) {
      T = arrivals[i];
      p._paint({ displayWidth: 720, displayHeight: 576, close: () => closed++ });
    }
    T = v;
    const f = raf; raf = null;
    const before = drawn;
    f?.(v);
    if (drawn > before) shown.push(v);
  }
  const dropped = closed - drawn;  // closed without being drawn (before close() empties the queue)
  p.close();
  return { p, drawn, shown, dropped };
};

// 24 fps regolari per 4 s: ~24 dipinti al secondo, mai più di 1-2 in coda, nessuno buttato.
const reg = Array.from({ length: 96 }, (_, k) => 10 + k * 1000 / 24);
const a = run(reg, 4100);
assert.ok(a.drawn >= 94, `regolare: ${a.drawn}`);
assert.ok(Math.abs(a.p._interval - 1000 / 24) < 2, `intervallo ${a.p._interval}`);

// 5G: 500 ms di niente poi 12 fotogrammi insieme, poi regolari. Della raffica restano i
// QUEUE_MAX più recenti (gli altri si buttano subito: la latenza non cresce), disegnati uno per
// intervallo e non nello stesso vsync.
const N = 12, burst = [...reg.slice(0, 24), ...Array(N).fill(1500), ...Array.from({ length: 24 }, (_, k) => 1520 + k * 1000 / 24)];
const b = run(burst, 2700);
assert.equal(b.dropped, N - QUEUE_MAX, `buttati ${b.dropped}`);
const after = b.shown.filter((t) => t >= 1500);
const gaps = after.slice(1).map((t, i) => t - after[i]);
const mean = (after.at(-1) - after[0]) / gaps.length;
assert.ok(gaps.every((g) => g > 15) && Math.abs(mean - 1000 / 24) < 3, `uno per vsync al massimo, 24 al secondo: ${gaps}`);
// TCP in ordine con jitter (|gauss| sd 25 ms, seme fisso): i fotogrammi arrivano a grappoli. Gli
// arrivi ravvicinati contano per l'intervallo, che resta ~41,7 ms: la coda non butta (quasi) niente.
let seed = 7;
const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
const gauss = () => Math.sqrt(-2 * Math.log(rnd() + 1e-12)) * Math.cos(2 * Math.PI * rnd());
let prev = 0;
const jit = Array.from({ length: 480 }, (_, k) => (prev = Math.max(prev, 10 + k * 1000 / 24 + 50 + Math.abs(gauss()) * 25)));
const c = run(jit, 20500);
assert.ok(c.dropped <= 2 && Math.abs(c.p._interval - 1000 / 24) < 2, `jitter: buttati ${c.dropped}, intervallo ${c.p._interval}`);
console.log("pacing ok: regolare", a.drawn, "disegnati; raffica: buttati", b.dropped, "; jitter sd 25: buttati", c.dropped);
