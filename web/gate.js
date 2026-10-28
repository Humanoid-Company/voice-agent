// Echo-aware noise gate, ported from the Eva web client.
// Input 0: the microphone. Input 1: what the assistant is playing (measured only, never passed on).
// Output 0: the microphone, 250 ms late, shut while the assistant is audible, so that the speech recognizer fed
// with it never hears her. It opens only for the person at the microphone. It also tells the page when the
// assistant is audible and when that person speaks.
// Output 1: the same microphone for the Realtime model. It is shut only while she is audible (and ~0.2 s after) and
// the person's voice is not «near» (the same test that lets the page stop her: ~120 ms of voice clearly over her echo,
// so her echo bursts, which do open output 0, never reach the model); otherwise it passes at full level. Output 0 is also turned down by 14 dB when she is
// silent and for 0.3 s after her, which cut the first words of a quiet start or of an answer given at once.
//
// The assistant's echo in the mic is learned: how loud the mic is per unit of playback while only the
// assistant talks. High on laptop speakers, near zero in headphones. The user's voice counts as «near» only
// when it is clearly louder than that expected echo (and than the room's noise), for ~120 ms in a row,
// so a click, a bang or the assistant's own voice coming back never counts as the user.
// How easily the user's voice opens the gate while the assistant is audible, per sensitivity level (the page's
// «Чутливість перебивання»): the margin over the learned echo, the floor of the bar on speakers, and the extra
// margin the near-voice test wants. 1 is Eva's setting; higher lets a quieter voice through, and with it more of
// the assistant's own echo bursts (she may cut herself off).
const ECHO_MARGIN = [3.5, 2.5, 1.9, 1.5];
const SPEAKER_BAR = [0.06, 0.04, 0.03, 0.02];
const NEAR_MARGIN = [1.6, 1.3, 1.15, 1.0];

class EchoGate extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.sens = Math.min(3, Math.max(0, (options.processorOptions || {}).sens ?? 1));
    this.reported = 0;
    this.peak = 0; // the loudest 20 ms frame since the last report
    this.outEnv = 0; // her output level, held for ~0.3 s: her echo reaches the microphone late
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
    // Starts as a phone's speaker beside its microphone, the loudest echo there is (a phone's AGC and its speaker next to
    // the microphone make the echo louder than the sound that made it). A too high estimate only makes the gate a little
    // deaf for the first half second: the gate is shut, so it learns, and the estimate falls at once to the real echo. A too
    // low one is the dangerous direction: her echo opens the gate, nothing is learned while it is open, and she «interrupts»
    // herself.
    this.echo = 2;
    this.echoMax = 3; // above this the estimate is not believed
  }

  judge(level, outLevel) {
    if (outLevel > 0.01) this.outQuietFor = 0; else this.outQuietFor += 0.02;
    const audible = this.outQuietFor < 0.3;
    if (audible !== this.audible) { this.audible = audible; this.port.postMessage({ audible }); }
    // The echo comes 100-200 ms after the sound left the speaker. Against her level of this very moment the bar fell
    // whenever she dipped between words, while the echo of her last loud moment was still arriving: her echo then
    // passed for the user's voice. So the bar follows a slowly falling peak of her output.
    this.outEnv = Math.max(outLevel, this.outEnv * 0.96);
    const echoLevel = audible ? this.outEnv * this.echo * ECHO_MARGIN[this.sens] : 0;
    const headset = this.echo < 0.05;
    // While she talks on speakers keep a fixed bar above the browser canceller's leftovers.
    const threshold = Math.max(audible && !headset ? SPEAKER_BAR[this.sens] : 0.006, this.floor * 3, echoLevel);
    // Learn the echo while the gate is shut (the user isn't talking over the assistant).
    if (audible && !this.open && this.outEnv > 0.01) {
      const ratio = level / this.outEnv;
      // Slower up than down: the user's own voice, below the bar, was read as echo, which raised the bar further, so
      // that during a long answer the harder they tried the less they counted. (Not capped at the «loud speakers» start:
      // on a phone the real echo is louder than that, and a too low estimate made her echo count as the user.)
      this.echo += (ratio - this.echo) * (ratio < this.echo ? 0.1 : 0.005);
      this.echo = Math.min(this.echo, this.echoMax);
    }
    if (level > threshold) { this.loud += 1; this.quietFor = 0; }
    else { this.quietFor += 0.02; if (level < threshold * 0.6) this.loud = 0; }
    if (!this.open && this.loud >= 2) this.open = true;
    else if (this.open && this.quietFor > 1.0) this.open = false;
    if (this.open && !audible && level > threshold * 1.5) this.voice += (level - this.voice) * (this.voice ? 0.02 : 1);

    const near = this.voice ? Math.min(this.near, Math.max(0.01, this.voice * 0.5)) : this.near;
    const nearMargin = NEAR_MARGIN[this.sens];
    const nearThreshold = Math.max(audible ? near * nearMargin : near, this.floor * 8, echoLevel * nearMargin);
    if (level > nearThreshold) { this.nearFrames += 1; this.nearQuietFor = 0; }
    else { this.nearQuietFor += 0.02; if (this.nearQuietFor > 0.25) this.nearFrames = 0; }
    // Near voice: ~120 ms of loud frames to start (a click/bang is shorter), 250 ms gap to end.
    const speaking = this.speaking ? this.nearQuietFor <= 0.25 : this.nearFrames >= 6;
    if (speaking !== this.speaking) { this.speaking = speaking; this.port.postMessage({ speaking }); }
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
