// The whole chain on synthetic signals: the gate's reports -> the page's window rule (copied from web/app.js; a
// Python test checks the constants match). Speech is syllables with gaps, not a steady tone.
var sampleRate = 48000, registered = null;
class AudioWorkletProcessor { constructor() { this.port = { postMessage: function () {} }; } }
function registerProcessor(n, c) { registered = c; }
load('web/gate.js');

var DELAY_MS = 120;
var VOICE_WINDOW = 10, DUCK_AT = 3, STOP_AT = 5; // the same as in web/app.js: tests/test_gate_chain.py checks

// One run: she talks the whole time (echo ratio `echoK`, bursts +-`jitter`); the user, if `voice` > 0, starts after
// 3 s of her talking, speaking with syllable gaps. Returns { stopAfterMs: or -1, duckAfterMs: or -1 }.
function run(sens, sec, echoK, jitter, voice, seed0, delayMs) {
  // Real echo is late: the speaker, the air and the sound card add 100-200 ms and a reverberant tail.
  delayMs = delayMs === undefined ? DELAY_MS : delayMs;
  var ring = new Float32Array(sampleRate), d1 = Math.round(delayMs / 1000 * sampleRate), d2 = Math.round(delayMs * 2 / 1000 * sampleRate);
  var msgs = [];
  var g = new registered({ processorOptions: { sens: sens } });
  g.port = { postMessage: function (m) { msgs.push(m); } };
  var t = 0, seed = seed0 || 7, burst = 1, warm = 3 * sampleRate, n = Math.round(sec * sampleRate) + warm;
  var rnd = function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647 - 0.5; };
  var win = [], ducked = false, res = { stopAfterMs: -1, duckAfterMs: -1 }, samples = 0;
  for (var b = 0; b < n; b += 128) {
    var mic = new Float32Array(128), rem = new Float32Array(128), out = new Float32Array(128);
    if (b % 960 === 0) burst = 1 + jitter * 2 * rnd();
    for (var i = 0; i < 128; i++, t++) {
      var a = 0.3 * Math.sin(2 * Math.PI * 220 * t / sampleRate) * (1 + 0.5 * Math.sin(2 * Math.PI * 3 * t / sampleRate));
      rem[i] = a;
      ring[t % ring.length] = a;
      var late = delayMs ? ring[(t - d1 + ring.length) % ring.length] + 0.35 * ring[(t - d2 + ring.length) % ring.length] : a;
      // syllables: ~4 per second, each with a short gap between (the envelope falls to 15% of its peak)
      var env = 0.15 + 0.85 * Math.pow(Math.abs(Math.sin(Math.PI * 4 * t / sampleRate)), 1.5);
      var u = b > warm && voice ? voice * env * Math.sin(2 * Math.PI * 150 * t / sampleRate) : 0;
      mic[i] = echoK * burst * late + u + 0.003 * rnd();
    }
    g.process([[mic], [rem]], [[out]]);
    while (msgs.length) {
      var m = msgs.shift();
      if (!m.levels || b <= warm) continue;
      samples++;
      win.push(m.levels.speaking ? 1 : 0);
      if (win.length > VOICE_WINDOW) win.shift();
      var v = win.reduce(function (x, y) { return x + y; }, 0);
      var ms = samples * 100;
      if (!ducked && v >= DUCK_AT) { ducked = true; res.duckAfterMs = ms; }
      if (ducked && v >= STOP_AT && res.stopAfterMs < 0) res.stopAfterMs = ms;
    }
  }
  return res;
}

var out = {};
[0.10, 0.20].forEach(function (k) {
  out[k] = [];
  for (var s = 0; s < 4; s++) {
    var falseStops = 0, falseDucks = 0;
    for (var seed = 1; seed <= 6; seed++) {
      var r = run(s, 20, k, 0.3, 0, seed);
      if (r.stopAfterMs >= 0) falseStops++;
      if (r.duckAfterMs >= 0) falseDucks++;
    }
    var normal = run(s, 6, k, 0.3, 0.17, 3), quiet = run(s, 6, k, 0.3, 0.10, 3);
    out[k].push({ falseStops: falseStops, falseDucks: falseDucks, normalStopMs: normal.stopAfterMs, quietStopMs: quiet.stopAfterMs });
  }
});
print(JSON.stringify(out));
