// Echo-aware noise gate, ported from the Eva web client and rebuilt on a fairer model of the echo (see tests/gate_bench.js).
// Input 0: the microphone. Input 1: what the assistant is playing (measured only, never passed on).
// Output 0: the microphone, 250 ms late, shut while the assistant is audible, so that the speech recognizer fed
// with it never hears her. It opens only for the person at the microphone. It also tells the page when the
// assistant is audible and when that person speaks.
// Output 1: the same microphone for the Realtime model. It is shut only while she is audible (and ~0.2 s after) and
// the person's voice is not «near» (the same test that lets the page stop her: ~120 ms of voice clearly over her echo,
// so her echo bursts, which do open output 0, never reach the model); otherwise it passes at full level. Output 0 is
// also turned down by 14 dB when she is silent and for 0.3 s after her, which cut the first words of a quiet start or
// of an answer given at once.
//
// How her echo is told from the user's voice: by LEVEL, against a ceiling for the echo. Her echo in the microphone is her
// own loudness, a little late, times a gain (≈0.1-0.2 on desktop speakers, up to ≈1 on a phone whose speaker sits beside
// its microphone), with bursts. The ceiling is the best (highest) ratio of the microphone to her loudness over the last
// ~160 ms, held for a second and smoothed a little: a PEAK, not an average. An average learned «quickly down, slowly up»
// fell during her quiet syllables, the echo of her loud ones then passed for the user, and she stopped herself 16-38 times
// a minute on speech-like echo (tests/gate_bench.js; the steady tones of the earlier simulations never showed it). Frames
// that may be the user's own voice are kept out of the ceiling, so a quiet voice cannot raise it.
// (A match of the microphone's loudness curve with hers was tried as a second witness: over the 200 ms of a syllable it
// is high by chance for a voice too, and it swallowed voices into the ceiling at their start. It is not used.)
//
// How easily the user's voice counts while she is audible, per sensitivity level (the page's «Чутливість перебивання»):
// the margin over the echo ceiling, the floor of the bar on speakers, and the extra margin the near-voice test wants.
// Higher lets a quieter voice through, and with it more of her own echo bursts (she may cut herself off).
const ECHO_MARGIN = [2.2, 1.6, 1.3, 1.1];
const SPEAKER_BAR = [0.06, 0.04, 0.03, 0.02];
const NEAR_MARGIN = [1.6, 1.3, 1.15, 1.0];

const HIST = 8; // frames (20 ms each) of her loudness kept: the echo comes up to ~160 ms after the sound
const PEAK_SPAN = 50; // frames the ratio is held for: one second
const G_START = 2; // the echo gain assumed before anything is learned: a phone's, the loudest there is
const G_ATTACK = 0.3, G_RELEASE = 0.04; // per frame
const G_MAX = 4;
const BASE_UP = 0.003, BASE_DOWN = 0.04; // the slow baseline of the ceiling, per frame (rises in ~7 s, falls in ~0.5 s)
const BURST = 2; // the ceiling may rise at once only this far over the baseline: her echo bursts, not a voice

class EchoGate extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.sens = Math.min(3, Math.max(0, (options.processorOptions || {}).sens ?? 1));
    this.reported = 0;
    this.peak = 0; // the loudest 20 ms frame since the last report
    this.port.onmessage = (e) => {
      if (e.data && "sens" in e.data) this.sens = Math.min(3, Math.max(0, Number(e.data.sens) || 0));
    };
    this.frame = Math.round(sampleRate * 0.02); // judge loudness per 20 ms
    // 250 ms look-ahead: the gate opens on voiced sound, and a soft start before it («с», «п») must not be lost.
    this.ring = new Float32Array(Math.round(sampleRate * 0.25));
    this.pos = 0;
    this.gain = 0;
    this.sum = 0; this.outSum = 0; this.count = 0;
    this.floor = 0.003; this.open = false; this.loud = 0; this.quietFor = 1;
    this.near = (options.processorOptions || {}).near || 0.06;
    // How loud this user speaks, learned while the assistant is silent: a quiet user can still interrupt.
    this.voice = 0;
    this.speaking = false; this.nearFrames = 0; this.nearQuietFor = 1;
    this.audible = false; this.outQuietFor = 1;
    this.gain1 = 1; // output 1, see above
    // The echo ceiling: her loudness of the last frames, the held ratios, the gain.
    this.outH = new Float32Array(HIST); this.outPos = 0;
    this.outPeak = 0;
    this.g = G_START;
    this.gBase = G_START;
    this.ratios = new Float32Array(PEAK_SPAN); // the held ratios (0 for a frame that is kept out)
    this.ratioPos = 0;
    this.pending = []; // the last few frames' ratios, not yet trusted: a voice flagged meanwhile taints them
  }

  // The highest ratio of the microphone to her loudness within the held second (a frame kept out is 0).
  heldRatio() {
    let m = 0;
    for (let i = 0; i < PEAK_SPAN; i++) if (this.ratios[i] > m) m = this.ratios[i];
    return m;
  }

  judge(level, outLevel) {
    this.outH[this.outPos] = outLevel;
    this.outPos = (this.outPos + 1) % HIST;
    if (outLevel > 0.01) this.outQuietFor = 0; else this.outQuietFor += 0.02;
    const audible = this.outQuietFor < 0.3;
    if (audible !== this.audible) { this.audible = audible; this.port.postMessage({ audible }); }

    // Her loudness over the last ~160 ms: the echo of any of it may be arriving now.
    let peak = 0;
    for (let i = 0; i < HIST; i++) if (this.outH[i] > peak) peak = this.outH[i];
    this.outPeak = peak;

    // The ceiling learns only from frames the user can hardly be in: a frame becomes trusted HIST frames after it was
    // heard, and not if the voice was flagged meanwhile (the flag takes ~120 ms: its first frames are not in the ceiling).
    if (audible && peak > 0.01) {
      this.pending.push({ r: level / peak, tainted: this.speaking });
      this.ratios[this.ratioPos] = 0;
      if (this.pending.length > HIST) {
        const f = this.pending.shift();
        this.ratios[this.ratioPos] = f.tainted ? 0 : f.r;
      }
      this.ratioPos = (this.ratioPos + 1) % PEAK_SPAN;
      const held = this.heldRatio();
      if (held > 0) {
        // The ceiling follows the held ratio, but only up to BURST times a baseline that rises slowly. A voice a little over
        // the echo is not flagged (it is under the bar) and used to be learned as echo: the ceiling doubled in a second,
        // the voice sank under it, and the louder it got the more of it was learned. Her real echo rises slowly (the volume
        // is turned up) or in bursts, and both fit.
        this.gBase += (held - this.gBase) * (held > this.gBase ? BASE_UP : BASE_DOWN);
        const target = Math.min(held, this.gBase * BURST);
        this.g += (target - this.g) * (target > this.g ? G_ATTACK : G_RELEASE);
      }
      this.g = Math.min(Math.max(this.g, 0.01), G_MAX);
    } else if (!audible) {
      this.pending.length = 0;
    }
    const ceiling = audible ? this.g * peak : 0;
    const echoLevel = ceiling * ECHO_MARGIN[this.sens];
    const headset = this.g < 0.05;
    // While she talks on speakers keep a fixed bar above the browser canceller's leftovers.
    const threshold = Math.max(audible && !headset ? SPEAKER_BAR[this.sens] : 0.006, this.floor * 3, echoLevel);
    if (level > threshold) { this.loud += 1; this.quietFor = 0; }
    else { this.quietFor += 0.02; if (level < threshold * 0.6) this.loud = 0; }
    if (!this.open && this.loud >= 2) this.open = true;
    else if (this.open && this.quietFor > 1.0) this.open = false;
    if (this.open && !audible && level > threshold * 1.5) this.voice += (level - this.voice) * (this.voice ? 0.02 : 1);

    const near = this.voice ? Math.min(this.near, Math.max(0.01, this.voice * 0.5)) : this.near;
    const nearMargin = NEAR_MARGIN[this.sens];
    const nearThreshold = Math.max(audible ? near * nearMargin : near, this.floor * 8, echoLevel * nearMargin);
    if (level > nearThreshold) { this.nearFrames += 1; this.nearQuietFor = 0; }
    // To START, the loud frames must follow one another (gaps up to 100 ms: the dips between syllables): scattered spikes of
    // her echo, even many, are not a voice. Once started, a voice is held through gaps up to 250 ms.
    else { this.nearQuietFor += 0.02; if (this.nearQuietFor > (this.speaking ? 0.25 : 0.1)) this.nearFrames = 0; }
    // Near voice: ~120 ms of loud frames to start (a click/bang is shorter), 250 ms gap to end.
    const speaking = this.speaking ? this.nearQuietFor <= 0.25 : this.nearFrames >= 6;
    if (speaking !== this.speaking) {
      this.speaking = speaking;
      if (speaking) for (const f of this.pending) f.tainted = true; // the frames just heard may be the voice
      this.port.postMessage({ speaking });
    }
    // Ten times a second: how loud the microphone is and what it takes to count as the user. The page shows it,
    // so that «the microphone is silent», «too quiet against her echo» and «all fine» can be told apart.
    // The loudest frame of the 100 ms, not the last one: speech is syllables with gaps, and the last frame is
    // often in a gap.
    this.peak = Math.max(this.peak, level);
    if (++this.reported >= 5) {
      this.reported = 0;
      // `active`: the user is still sounding (any sound above the room's noise; while she talks her echo is in the
      // microphone too, so then it is the near-voice flag). The page waits for it to end before it takes the
      // user's phrase as finished.
      const active = audible ? speaking : this.peak > Math.max(0.006, this.floor * 3);
      this.port.postMessage({ levels: { level: this.peak, need: nearThreshold, audible, speaking, active } });
      this.peak = 0;
    }
    // Learn the room's noise only while nobody speaks.
    if (!this.open && !audible) {
      this.floor = level < this.floor ? this.floor * 0.7 + level * 0.3 : this.floor + (level - this.floor) * 0.01;
    }
  }

  process(inputs, outputs) {
    const mic = inputs[0] && inputs[0][0];
    const remote = inputs[1] && inputs[1][0];
    const out = outputs[0] && outputs[0][0];
    const out1 = outputs[1] && outputs[1][0];
    const n = (mic || remote || out || []).length;
    for (let i = 0; i < n; i++) {
      const x = mic ? mic[i] : 0;
      this.sum += x * x;
      if (remote) this.outSum += remote[i] * remote[i];
      if (++this.count >= this.frame) {
        this.judge(Math.sqrt(this.sum / this.count), Math.sqrt(this.outSum / this.count));
        this.sum = 0; this.outSum = 0; this.count = 0;
      }
      const delayed = this.ring[this.pos];
      this.ring[this.pos] = x;
      this.pos = (this.pos + 1) % this.ring.length;
      // Shut while she is silent = −14 dB, not silence: a quiet user below the bar is still heard.
      // While she talks it shuts fully: her echo must not reach the recognizer.
      const closed = this.audible ? 0 : 0.2;
      this.gain += ((this.open ? 1 : closed) - this.gain) * (this.open ? 0.004 : 0.0005);
      if (out) out[i] = delayed * this.gain;
      if (out1) {
        this.gain1 += ((this.outQuietFor < 0.2 && !this.speaking ? 0 : 1) - this.gain1) * 0.01; // ~2 ms at 48 kHz
        out1[i] = delayed * this.gain1;
      }
    }
    return true;
  }
}

registerProcessor("echo-gate", EchoGate);
