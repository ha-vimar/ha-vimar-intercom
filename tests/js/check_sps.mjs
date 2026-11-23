// node tests/js/check_sps.mjs (tests/test_card_js.py): the SPS that nal-player.js gives the decoder.
// The panel's SPS has no VUI bitstream_restriction: decoders then held ~17 frames and released
// them at each IDR. The rewritten SPS must keep every field and add max_num_reorder_frames 0.
import assert from "node:assert/strict";
import { NalPlayer, lowDelaySps, parseSps } from "../../custom_components/vimar_intercom/www/nal-player.js";

const hex = (h) => Uint8Array.from(h.match(/../g).map((x) => parseInt(x, 16)));
const SPS = hex("000000016742c01f8d68141f90");  // the 40515's, as the server sends it
const PPS = hex("0000000168ee01444480");
const same = (a, b) => ["profile", "fnBits", "refs", "width", "height"].every((k) => a[k] === b[k]);
const bitsOf = (s, n) => Array.from({ length: n }, (_, i) => (s.bytes[i >> 3] >> (7 - (i & 7))) & 1).join("");

// 1. No VUI (the panel): VUI added, nothing else changed, a valid RBSP.
const orig = parseSps(SPS), low = lowDelaySps(SPS), got = parseSps(low);
assert.equal(orig.vui, false);
assert.ok(same(orig, got), JSON.stringify([orig, got]));
assert.equal(bitsOf(got, orig.vuiAt), bitsOf(orig, orig.vuiAt));
assert.ok(got.vui && got.reorder === 0 && got.dpb === Math.max(1, orig.refs), JSON.stringify(got));
assert.equal(got.end, got.bytes.length * 8 - (got.bytes.length * 8 - got.end));  // parsed to the end
const last = low.at(-1), stop = 7 - Math.log2(last & -last);  // rbsp_stop_one_bit position in the last byte
assert.equal(got.end, (got.bytes.length - 1) * 8 + stop, "rbsp_trailing_bits right after the VUI");
for (let i = 7; i < low.length; i++) assert.ok(!(low[i - 2] === 0 && low[i - 1] === 0 && low[i] <= 3) || low[i] === 3, "emulation prevention");
console.log(`SPS della targa: ${orig.width}x${orig.height}, profilo ${orig.profile}, ${orig.refs} rif.;`,
  `riscritto ${Buffer.from(low.subarray(4)).toString("hex")}`);

// 2. VUI without bitstream_restriction (video signal + timing, then optionally an HRD, or an
// existing restriction): kept bit for bit, restriction appended (or left as is).
const withVui = (hrd, restrict) => {
  const out = bitsOf(orig, orig.vuiAt).split("").map(Number);
  const put = (v, n) => { for (let i = n - 1; i >= 0; i--) out.push(Math.floor(v / 2 ** i) % 2); };
  put(1, 1); put(0, 1); put(0, 1);                              // VUI, no aspect, no overscan
  put(1, 1); put(5, 3); put(0, 1); put(1, 1); put(0x060606, 24); // video signal + colour description
  put(0, 1); put(1, 1); put(1, 32); put(30, 32); put(1, 1);     // no chroma loc; timing
  if (hrd) { put(1, 1); put(1, 1); put(0, 8); put(1, 1); put(1, 1); put(1, 1); put(0, 20); put(0, 1); put(1, 1); }  // NAL HRD (1 cpb), no VCL HRD, low_delay
  else put(0, 2);
  put(0, 1);                                                    // no pic_struct
  if (restrict) { put(1, 1); put(1, 1); put(0b111111, 6); }  // restriction: mv flag, then six ue(0) (reorder 0, dpb 0)
  else put(0, 1);
  out.push(1); while (out.length % 8) out.push(0);
  return Uint8Array.from([0, 0, 0, 1, 0x67, ...Array.from({ length: out.length / 8 }, (_, i) => parseInt(out.slice(i * 8, i * 8 + 8).join(""), 2))]);
};
for (const [hrd, restrict] of [[false, false], [true, false], [false, true]]) {
  const x = withVui(hrd, restrict), v = parseSps(x), v2 = parseSps(lowDelaySps(x));
  assert.ok(v.vui && same(v, v2), JSON.stringify([hrd, restrict, v, v2]));
  assert.equal(v.reorder === undefined, !restrict, JSON.stringify([hrd, restrict, v]));
  if (restrict) assert.equal(lowDelaySps(x), x);
  else {
    assert.equal(bitsOf(v2, v.restrictAt), bitsOf(v, v.restrictAt));
    assert.ok(v2.reorder === 0 && v2.dpb >= 1, JSON.stringify(v2));
  }
}

// 3. Already restricted: unchanged.
assert.equal(lowDelaySps(low), low);

// 4. The player configures the decoder with it (the avcC description carries the SPS).
let cfg;
globalThis.VideoDecoder = class { configure(c) { cfg = c; } close() {} };
globalThis.cancelAnimationFrame ??= () => {};
const p = new NalPlayer({ callWS: () => new Promise(() => {}) }, {}, () => {});
Object.assign(p, { _sps: SPS, _pps: PPS });
p._configure();
p.close();
const d = new Uint8Array(cfg.description), n = d[6] << 8 | d[7];
const inDesc = parseSps(Uint8Array.from([0, 0, 0, 1, ...d.subarray(8, 8 + n)]));
assert.equal(cfg.codec, "avc1.42C01F");
assert.ok(inDesc.reorder === 0 && same(orig, inDesc), `description senza bitstream_restriction: ${JSON.stringify(inDesc)}`);
// 5. A malformed SPS (cut after the level, or zeros where the ids are): the decoder gets it as is.
for (const bad of [hex("000000016742c01f"), hex("000000016742c01f0000000000")]) {
  assert.equal(lowDelaySps(bad), bad);
  const q = new NalPlayer({ callWS: () => new Promise(() => {}) }, {}, () => {});
  Object.assign(q, { _sps: bad, _pps: PPS });
  q._configure();
  q.close();
  const e = new Uint8Array(cfg.description);
  assert.deepEqual(e.subarray(8, 8 + (e[6] << 8 | e[7])), bad.subarray(4));
}
console.log("SPS ok");
