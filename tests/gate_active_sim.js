// The gate's `active` flag (the user is still sounding) on synthetic signals: it must stay on through the gaps between
// syllables, go off soon after the speech ends, and never be turned on by her own echo.
var sampleRate = 48000, registered = null;
class AudioWorkletProcessor { constructor() { this.port = { postMessage: function () {} }; } }
function registerProcessor(n, c) { registered = c; }
load('web/gate.js');

function run(phases, echoK) { // phases: [{ sec, voice, assistant }], returns [{ at (ms), phase, active }]
  var msgs = [];
  var g = new registered({ processorOptions: { sens: 2 } });
  g.port = { postMessage: function (m) { msgs.push(m); } };
  var t = 0, seed = 5, ring = new Float32Array(sampleRate), d1 = Math.round(0.12 * sampleRate), samples = [], start = 0;
  var rnd = function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647 - 0.5; };
  phases.forEach(function (ph, pi) {
    var n = Math.round(ph.sec * sampleRate);
    for (var b = 0; b < n; b += 128) {
      var mic = new Float32Array(128), rem = new Float32Array(128), out = new Float32Array(128);
      for (var i = 0; i < 128; i++, t++) {
        var a = ph.assistant ? 0.3 * Math.sin(2 * Math.PI * 220 * t / sampleRate) * (1 + 0.5 * Math.sin(2 * Math.PI * 3 * t / sampleRate)) : 0;
        rem[i] = a; ring[t % ring.length] = a;
        var env = 0.15 + 0.85 * Math.pow(Math.abs(Math.sin(Math.PI * 4 * t / sampleRate)), 1.5); // syllables with gaps
        var u = ph.voice ? ph.voice * env * Math.sin(2 * Math.PI * 150 * t / sampleRate) : 0;
        mic[i] = echoK * ring[(t - d1 + ring.length) % ring.length] + u + 0.003 * rnd();
      }
      g.process([[mic], [rem]], [[out]]);
      while (msgs.length) { var m = msgs.shift(); if (m.levels) samples.push({ at: Math.round((b + start) / sampleRate * 1000), phase: pi, active: m.levels.active }); }
    }
    start += n;
  });
  return samples;
}

var frac = function (list, pred) { var sel = list.filter(pred); return sel.length ? sel.filter(function (x) { return x.active; }).length / sel.length : -1; };

// 1 s of silence (the noise floor is learned), 3 s of speech, 2 s of silence
var s1 = run([{ sec: 1 }, { sec: 3, voice: 0.12 }, { sec: 2 }], 0);
var speechOn = frac(s1, function (x) { return x.phase === 1 && x.at >= 1300; });                 // speaking, after the first 300 ms
var after = frac(s1, function (x) { return x.phase === 2 && x.at >= 4000 + 400; });               // 400 ms after the speech ended
var quietBefore = frac(s1, function (x) { return x.phase === 0 && x.at >= 400; });

// she talks for 6 s with a loud, late echo and nobody else speaks
var s2 = run([{ sec: 1.5, assistant: true }, { sec: 6, assistant: true }], 0.2);
var echoOnly = frac(s2, function (x) { return x.phase === 1; });

print(JSON.stringify({ speechActive: speechOn, silenceActive: after, quietBeforeActive: quietBefore, echoOnlyActive: echoOnly }));
