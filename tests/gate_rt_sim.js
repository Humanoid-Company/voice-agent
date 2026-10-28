// Output 1 of the gate (the microphone for Realtime) against output 0 (the one the recognizer gets): prints one JSON line.
var sampleRate = 48000, registered = null;
class AudioWorkletProcessor { constructor() { this.port = { postMessage: function () {} }; } }
function registerProcessor(n, c) { registered = c; }
load('web/gate.js');

// agent(t) -> 0..1 of her loudness, user(t) -> amplitude of the user's voice, echoK = echo of her in the mic.
// Returns the RMS of the input (delayed by the gate's 250 ms) and of the two outputs in the window [from, to] seconds.
function run(agent, user, echoK, seconds, from, to) {
  var g = new registered({ processorOptions: { sens: 2 } });
  var t = 0, seed = 5, sum = { inp: 0, o0: 0, o1: 0 }, n = 0, history = [];
  var rnd = function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647 - 0.5; };
  var delay = Math.round(0.25 * sampleRate);
  for (var b = 0; b < seconds * sampleRate; b += 128) {
    var mic = new Float32Array(128), rem = new Float32Array(128), o0 = new Float32Array(128), o1 = new Float32Array(128);
    for (var i = 0; i < 128; i++, t++) {
      var sec = t / sampleRate;
      var a = 0.3 * agent(sec) * Math.sin(2 * Math.PI * 220 * t / sampleRate);
      var u = user(sec);
      rem[i] = a;
      mic[i] = echoK * a + (u ? u * Math.sin(2 * Math.PI * 150 * t / sampleRate) : 0) + 0.0005 * rnd();
      history.push(mic[i]);
    }
    g.process([[mic], [rem]], [[o0], [o1]]);
    for (var j = 0; j < 128; j++) {
      var tt = (b + j) / sampleRate;
      if (tt >= from && tt <= to) {
        var k = b + j - delay;
        sum.inp += Math.pow(k >= 0 ? history[k] : 0, 2); sum.o0 += o0[j] * o0[j]; sum.o1 += o1[j] * o1[j]; n++;
      }
    }
  }
  var r = function (x) { return Math.sqrt(x / n); };
  return { inp: r(sum.inp), o0: r(sum.o0), o1: r(sum.o1) };
}

var res = {
  // she is silent, the user speaks quietly: the recognizer's output is turned down, Realtime's is not
  quietStart: run(function () { return 0; }, function (s) { return s > 1 ? 0.004 : 0; }, 0.2, 3, 2, 3),
  // she is talking, only her echo is in the microphone: nothing of it reaches Realtime
  echoOnly: run(function () { return 1; }, function () { return 0; }, 0.2, 8, 5, 8),
  // she is talking and the user talks loudly over her: it passes
  talkOver: run(function () { return 1; }, function (s) { return s > 4 ? 0.17 : 0; }, 0.2, 8, 5.6, 8),
  // she stops at 4 s, the user answers at once (4.05 s), not loudly: the first words are not cut
  answerAtOnce: run(function (s) { return s < 4 ? 1 : 0; }, function (s) { return s > 4.05 ? 0.012 : 0; }, 0.2, 6, 4.31, 4.6),
};
print(JSON.stringify(res));
