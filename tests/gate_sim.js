var sampleRate = 48000, registered = null;
class AudioWorkletProcessor { constructor() { this.port = { postMessage: function () {} }; } }
function registerProcessor(n, c) { registered = c; }
load('web/gate.js');

// Seconds of NEAR VOICE out of `sec`: the assistant talks (echo ratio `echoK`, bursts of +-`jitter`), maybe a user over her.
function run(sens, sec, echoK, jitter, voiceAmp, seed0) {
  var g = new registered({ processorOptions: { sens: sens } });
  var t = 0, seed = seed0 || 7, nearBlocks = 0, blocks = 0, burst = 1;
  var rnd = function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647 - 0.5; };
  // she has been talking for a while: the gate has learned the echo before the measurement
  var warm = 3 * sampleRate, n = Math.round(sec * sampleRate) + warm;
  for (var b = 0; b < n; b += 128) {
    var mic = new Float32Array(128), rem = new Float32Array(128), out = new Float32Array(128);
    if (b % 960 === 0) burst = 1 + jitter * 2 * rnd();       // the echo canceller leaves bursts, every 20 ms
    var user = b > warm ? voiceAmp : 0;
    for (var i = 0; i < 128; i++, t++) {
      var a = 0.3 * Math.sin(2 * Math.PI * 220 * t / sampleRate) * (1 + 0.5 * Math.sin(2 * Math.PI * 3 * t / sampleRate));
      rem[i] = a;
      mic[i] = echoK * burst * a + (user ? user * (0.15 + 0.85 * Math.pow(Math.abs(Math.sin(Math.PI * 4 * t / sampleRate)), 1.5)) * Math.sin(2 * Math.PI * 150 * t / sampleRate) : 0) + 0.003 * rnd();
    }
    g.process([[mic], [rem]], [[out]]);
    if (b > warm) { blocks++; if (g.speaking) nearBlocks++; }
  }
  return Math.round(100 * nearBlocks / blocks);
}


// JSON out, for tests/test_gate_sensitivity.py: [echo ratio][level] -> { echoOnly: worst %, quiet: %, normal: % }
var result = {};
[0.10, 0.20].forEach(function (k) {
  result[k] = [];
  for (var s = 0; s < 4; s++) {
    var worst = 0;
    for (var seed = 1; seed <= 5; seed++) worst = Math.max(worst, run(s, 6, k, 0.3, 0, seed));
    result[k].push({ echoOnly: worst, quiet: run(s, 3, k, 0.3, 0.15), normal: run(s, 3, k, 0.3, 0.30) });
  }
});
print(JSON.stringify(result));
