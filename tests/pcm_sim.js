// pcm.js against a synthetic microphone at the usual rates: prints one JSON line, read by tests/test_pcm_tap.py.
var sampleRate = 48000, registered = null;
class AudioWorkletProcessor { constructor() { this.port = { postMessage: function () {} }; } }
function registerProcessor(n, c) { registered = c; }
load('web/pcm.js');

function run(rate, hz, amp, seconds) {
  sampleRate = rate;
  var tap = new registered();
  var chunks = [];
  tap.port.postMessage = function (buf) { chunks.push(new Int16Array(buf)); };
  var total = Math.round(rate * seconds), t = 0;
  for (var b = 0; b < total; b += 128) {
    var x = new Float32Array(128);
    for (var i = 0; i < 128; i++, t++) x[i] = amp * Math.sin(2 * Math.PI * hz * t / rate);
    tap.process([[x]], []);
  }
  var samples = 0, peak = 0, zc = 0, prev = 0, allFull = true;
  chunks.forEach(function (c) {
    if (c.length !== 2400) allFull = false;
    samples += c.length;
    for (var i = 0; i < c.length; i++) {
      peak = Math.max(peak, Math.abs(c[i]));
      if (prev < 0 && c[i] >= 0) zc++;
      prev = c[i];
    }
  });
  return { chunks: chunks.length, samples: samples, allFull: allFull, peak: peak, hz: Math.round(zc / (samples / 24000)) };
}

print(JSON.stringify({ r48: run(48000, 440, 0.5, 2.05), r44: run(44100, 440, 0.5, 2.05), clip: run(48000, 100, 3, 0.5) }));
