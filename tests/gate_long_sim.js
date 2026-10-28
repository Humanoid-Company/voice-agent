// How the gate treats the same user's voice early and late in a LONG answer of the agent: prints one JSON line.
var sampleRate = 48000, registered = null;
class AudioWorkletProcessor { constructor() { this.port = { postMessage: function () {} }; } }
function registerProcessor(n, c) { registered = c; }
load('web/gate.js');

// The user speaks `voiceAmp` for 3 s starting `startAt` s into an answer that never stops; `tries` earlier attempts of
// `triesAmp` (too quiet to count) happen before. Returns { share: % of the 3 s flagged as the user's voice, first: ms }.
function trial(sens, echoK, startAt, voiceAmp, triesAmp, seed0) {
  var g = new registered({ processorOptions: { sens: sens } });
  var t = 0, seed = seed0 || 7, burst = 1, frames = 0, nearFrames = 0, first = -1;
  var rnd = function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647 - 0.5; };
  var total = Math.round((startAt + 3) * sampleRate);
  for (var b = 0; b < total; b += 128) {
    var mic = new Float32Array(128), rem = new Float32Array(128), out = new Float32Array(128);
    if (b % 960 === 0) burst = 1 + 0.3 * 2 * rnd();
    var sec = b / sampleRate;
    var amp = sec >= startAt ? voiceAmp : (triesAmp && (sec % 8) > 5 ? triesAmp : 0); // a quiet try for 3 s of every 8
    for (var i = 0; i < 128; i++, t++) {
      var a = 0.3 * Math.sin(2 * Math.PI * 220 * t / sampleRate) * (1 + 0.5 * Math.sin(2 * Math.PI * 3 * t / sampleRate));
      rem[i] = a;
      mic[i] = echoK * burst * a + (amp ? amp * Math.sin(2 * Math.PI * 150 * t / sampleRate) : 0) + 0.003 * rnd();
    }
    g.process([[mic], [rem]], [[out]]);
    if (sec >= startAt) { frames++; if (g.speaking) { nearFrames++; if (first < 0) first = Math.round((sec - startAt) * 1000); } }
  }
  return { share: Math.round(100 * nearFrames / frames), first: first };
}

var res = {};
[0.10, 0.20, 0.35].forEach(function (k) {
  res[k] = {};
  [4, 20, 40, 60].forEach(function (at) {
    res[k][at] = { normal: trial(2, k, at, 0.17, 0, 3), quietTries: trial(2, k, at, 0.17, 0.08, 3) };
  });
});
print(JSON.stringify(res));
