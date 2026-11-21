// node tests/js/check_frame_num.mjs (tests/test_card_js.py): il frame_num di nal-player.js su flussi x264 veri.
import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import { NalPlayer, frameNum, frameNumBits } from "../../custom_components/vimar_intercom/www/nal-player.js";

const SC = [0, 0, 0, 1];
const nal = (hex) => Uint8Array.from([...SC, ...hex.match(/../g).map((h) => parseInt(h, 16))]);

// Slice header veri della targa 40515: IDR 25b80002, P 21e00022/42/62 → frame_num 0,1,2,3 su 16 bit.
assert.equal(frameNum(nal("25b80002"), 16), 0);
assert.deepEqual(["21e00022", "21e00042", "21e00062"].map((h) => frameNum(nal(h), 16)), [1, 2, 3]);

// Flussi x264 (baseline come la targa, high con 4:2:0): la catena +1 regge, un P tolto si vede.
for (const args of [["-profile:v", "baseline"], ["-profile:v", "high", "-bf", "0"]]) {
  const raw = execFileSync("ffmpeg", ["-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
    "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "15", ...args,
    "-bsf:v", "h264_mp4toannexb", "-f", "h264", "pipe:1"], { maxBuffer: 1 << 26 });
  const nals = [];
  for (let i = 0, s = -1; i <= raw.length; i++) {
    const at = i + 3 < raw.length && raw[i] === 0 && raw[i + 1] === 0 && raw[i + 2] === 1;
    if (at || i === raw.length) {
      if (s >= 0) nals.push(Uint8Array.from([...SC, ...raw.subarray(s, at && raw[i - 1] === 0 ? i - 1 : i)]));
      s = i + 3;
    }
  }
  const n = frameNumBits(nals.find((x) => (x[4] & 0x1f) === 7));
  assert.ok(n >= 4 && n <= 16, `bits ${n}`);
  const chain = (list) => {
    let ref = 0, gaps = 0;
    for (const x of list) {
      const t = x[4] & 0x1f;
      if (t === 5) ref = 0;
      if (t !== 1) continue;
      const fn = frameNum(x, n);
      if (fn !== (ref + 1) % 2 ** n) { gaps++; continue; }
      if (x[4] & 0x60) ref = fn;
    }
    return gaps;
  };
  assert.equal(chain(nals), 0, `${args} intero`);
  const p = nals.findIndex((x, i) => i > 5 && (x[4] & 0x1f) === 1);
  assert.ok(chain(nals.filter((_, i) => i !== p)) >= 1, `${args} buco`);
  console.log(args.join(" "), "ok:", nals.length, "NAL, frame_num su", n, "bit");
}
// Il player: la stessa frame_num (un'altra slice o il secondo campo della stessa immagine) passa,
// un salto no (P 1, 1, 3: il 3 resta fuori fino all'IDR).
{
  globalThis.cancelAnimationFrame ??= () => {};  // close() outside a browser
  const p = new NalPlayer({ callWS: () => new Promise(() => {}) }, {}, () => {});
  const fed = [];
  Object.assign(p, { _dec: { decodeQueueSize: 0 }, _fnBits: 16, _decode: (type, n) => fed.push(frameNum(n, 16)) });
  for (const h of ["21e00022", "21e00022", "21e00062"]) p._nal(nal(h));
  p.close();
  assert.deepEqual(fed, [1, 1]);
  assert.equal(p._skip, true);
}
console.log("frame_num ok");
