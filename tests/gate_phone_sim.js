// A phone: the speaker sits next to the microphone, so her echo in it is as loud as (or louder than) the sound that made it.
// Nobody speaks. How much of 40 s does the gate take for the user's voice (the near flag the page stops her by), and how
// much of her echo reaches Realtime (output 1)? Prints one JSON line.
var sampleRate = 48000, registered = null;
class AudioWorkletProcessor { constructor() { this.port = { postMessage: function () {} }; } }
function registerProcessor(n, c) { registered = c; }
var source = readFile('web/gate.js');
if (typeof PATCH !== 'undefined') source = source.replace(PATCH[0], PATCH[1]); // lets a run try an older setting
eval(source);

function run(echoK, jitter, seconds, seed0) {
  var g = new registered({ processorOptions: { sens: 2 } });
  var t = 0, seed = seed0, burst = 1, frames = 0, near = 0, sumIn = 0, sumOut = 0, n = 0;
  var rnd = function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647 - 0.5; };
  var delay = Math.round(0.25 * sampleRate), hist = [];
  var total = seconds * sampleRate, warm = 3 * sampleRate;
  for (var b = 0; b < total; b += 128) {
    var mic = new Float32Array(128), rem = new Float32Array(128), o0 = new Float32Array(128), o1 = new Float32Array(128);
    if (b % 960 === 0) burst = 1 + jitter * 2 * rnd(); // the echo canceller leaves bursts, every 20 ms
    for (var i = 0; i < 128; i++, t++) {
      var a = 0.3 * Math.sin(2 * Math.PI * 220 * t / sampleRate) * (1 + 0.5 * Math.sin(2 * Math.PI * 3 * t / sampleRate));
      rem[i] = a;
      mic[i] = echoK * burst * a + 0.003 * rnd();
      hist.push(mic[i]);
    }
    g.process([[mic], [rem]], [[o0], [o1]]);
    if (b > warm) {
      frames++; if (g.speaking) near++;
      for (var j = 0; j < 128; j++) { var k = b + j - delay; sumIn += Math.pow(hist[k], 2); sumOut += o1[j] * o1[j]; n++; }
    }
  }
  return { nearPct: Math.round(1000 * near / frames) / 10, toRealtime: sumIn ? Math.sqrt(sumOut / n) / Math.sqrt(sumIn / n) : 0 };
}

var res = {};
[0.5, 0.8, 1.2, 1.8].forEach(function (k) {
  var worst = { nearPct: 0, toRealtime: 0 };
  for (var seed = 1; seed <= 3; seed++) { var r = run(k, 0.3, 40, seed); worst.nearPct = Math.max(worst.nearPct, r.nearPct); worst.toRealtime = Math.max(worst.toRealtime, r.toRealtime); }
  res[k] = worst;
});
print(JSON.stringify(res));
