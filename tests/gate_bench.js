// A fairer benchmark of the gate than a steady tone over a steady echo: BOTH the agent and the user speak with syllable-like
// loudness (new level every 120-250 ms, short pauses), the agent's echo has a loudness K (how much of her sound is in the
// microphone: 0.2 desktop speakers, 0.5 loud laptop, 1.0 a phone), bursts of +-jitter and now and then a glitch (the
// residue 2.5x louder for 150-400 ms). Run: jsc tests/gate_bench.js -- <path to the gate>; prints one JSON line:
//   selfStopsPerMin[K]  how many times in a minute of her echo alone she would stop herself (the page's rule: voice in 5 of
//                       the last 10 samples), worst of the harder echoes
//   userStops[case]     of 8 runs, how often the user talking over her stops her within 3 s, and the median time
var sampleRate = 48000, registered = null;
class AudioWorkletProcessor { constructor() { this.port = { postMessage: function () {} }; } }
function registerProcessor(n, c) { registered = c; }
eval(readFile(arguments[0] || 'web/gate.js'));

function speechEnv(rnd) { // a syllable curve: a level for 120-250 ms, a pause now and then
  var level = 0, left = 0;
  return function () {
    if (left <= 0) { left = 6 + Math.floor((rnd() + 0.5) * 7); level = (rnd() + 0.5) < 0.18 ? 0.03 : 0.25 + (rnd() + 0.5) * 0.75; }
    left--; return level;
  };
}

// What the page does with the gate's flag (web/app.js, bargeFromLevels): ten samples a second, and she is stopped when the
// voice is on in 5 of the last 10 (and not again within 1.5 s).
function run(echoK, jitter, glitchP, userAmp, seconds, userAt, seed0) {
  var g = new registered({ processorOptions: { sens: 2 } });
  var seed = seed0, t = 0, burst = 1, glitchLeft = 0, win = [], sinceStop = 99, stops = 0, firstStop = -1, frames = 0, flagged = 0;
  var rnd = function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647 - 0.5; };
  var agentEnv = speechEnv(rnd), userEnv = speechEnv(rnd), ae = 0.5, ue = 0;
  for (var b = 0; b < seconds * sampleRate; b += 128) {
    var mic = new Float32Array(128), rem = new Float32Array(128), o0 = new Float32Array(128), o1 = new Float32Array(128);
    if (b % 960 === 0) {
      burst = 1 + jitter * 2 * rnd();
      if (glitchLeft > 0) glitchLeft--; else if (rnd() + 0.5 < glitchP) glitchLeft = 8 + Math.floor((rnd() + 0.5) * 12);
      ae = agentEnv(); ue = userEnv();
    }
    var sec = b / sampleRate, talking = userAmp > 0 && sec >= userAt;
    for (var i = 0; i < 128; i++, t++) {
      var a = 0.3 * ae * Math.sin(2 * Math.PI * 220 * t / sampleRate);
      rem[i] = a;
      mic[i] = echoK * burst * (glitchLeft > 0 ? 2.5 : 1) * a + (talking ? userAmp * ue * Math.sin(2 * Math.PI * 150 * t / sampleRate) : 0) + 0.003 * rnd();
    }
    g.process([[mic], [rem]], [[o0], [o1]]);
    if (sec > 3 && (b / 128) % 38 === 0) { // ~ every 100 ms
      win.push(g.speaking ? 1 : 0); if (win.length > 10) win.shift();
      sinceStop += 0.1;
      if (win.reduce(function (x, y) { return x + y; }, 0) >= 5 && sinceStop > 1.5) {
        stops++; sinceStop = 0;
        if (talking && firstStop < 0) firstStop = Math.round((sec - userAt) * 1000);
      }
    }
    if (talking) { frames++; if (g.speaking) flagged++; }
  }
  return { stops: stops, firstStop: firstStop, flaggedPct: frames ? Math.round(100 * flagged / frames) : 0 };
}

var res = { selfStopsPerMin: {}, userStops: {} };
// her echo alone: how many times a minute she stops herself (worst of the harder echoes)
[0.2, 0.5, 1.0, 1.5].forEach(function (k) {
  var worst = 0;
  [[0.3, 0], [0.5, 0.02], [0.8, 0.04]].forEach(function (c) {
    for (var seed = 1; seed <= 3; seed++) worst = Math.max(worst, run(k, c[0], c[1], 0, 60, 0, seed).stops);
  });
  res.selfStopsPerMin[k] = worst;
});
// the user over her at a normal voice (0.30) and a quiet one (0.15): does she stop, and how fast
[[0.2, 0.30], [0.2, 0.15], [0.5, 0.30], [1.0, 0.30], [1.0, 0.60]].forEach(function (c) {
  var ok = 0, times = [], n = 8;
  for (var seed = 1; seed <= n; seed++) {
    var r = run(c[0], 0.3, 0, c[1], 14, 8, seed);
    if (r.firstStop >= 0 && r.firstStop <= 3000) { ok++; times.push(r.firstStop); }
  }
  times.sort(function (a, b) { return a - b; });
  res.userStops["echo " + c[0] + " voice " + c[1]] = { stoppedPct: Math.round(100 * ok / n), medianMs: times.length ? times[Math.floor(times.length / 2)] : -1 };
});
print(JSON.stringify(res));
