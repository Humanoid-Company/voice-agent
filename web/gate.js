// Echo-aware noise gate, ported from the Eva web client.
// Input 0: the microphone. Input 1: what the assistant is playing (measured only, never passed on).
// Output: the microphone, 250 ms late, shut while the assistant is audible, so that the speech recognizer fed
// with it never hears her. It opens only for the person at the microphone. It also tells the page when the
// assistant is audible and when that person speaks.
//
// The assistant's echo in the mic is learned: how loud the mic is per unit of playback while only the
// assistant talks. High on laptop speakers, near zero in headphones. The user's voice counts as «near» only
// when it is clearly louder than that expected echo (and than the room's noise), for ~120 ms in a row,
// so a click, a bang or the assistant's own voice coming back never counts as the user.
class EchoGate extends AudioWorkletProcessor {
  constructor(options) {
    super();
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
    // Starts as «loud speakers»; drops within a second or two when there is no echo (headset).
    this.echo = 0.5;
  }

  judge(level, outLevel) {
    if (outLevel > 0.01) this.outQuietFor = 0; else this.outQuietFor += 0.02;
    const audible = this.outQuietFor < 0.3;
    if (audible !== this.audible) { this.audible = audible; this.port.postMessage({ audible }); }
    const echoLevel = audible ? outLevel * this.echo * 2.5 : 0;
    const headset = this.echo < 0.05;
    // While she talks on speakers keep a fixed bar above the browser canceller's leftovers.
    const threshold = Math.max(audible && !headset ? 0.04 : 0.006, this.floor * 3, echoLevel);
    // Learn the echo while the gate is shut (the user isn't talking over the assistant).
    if (audible && !this.open && outLevel > 0.01) {
      const ratio = level / outLevel;
      this.echo += (ratio - this.echo) * (ratio < this.echo ? 0.1 : 0.03);
    }
    if (level > threshold) { this.loud += 1; this.quietFor = 0; }
    else { this.quietFor += 0.02; if (level < threshold * 0.6) this.loud = 0; }
    if (!this.open && this.loud >= 2) this.open = true;
    else if (this.open && this.quietFor > 1.0) this.open = false;
    if (this.open && !audible && level > threshold * 1.5) this.voice += (level - this.voice) * (this.voice ? 0.02 : 1);

    const near = this.voice ? Math.min(this.near, Math.max(0.01, this.voice * 0.5)) : this.near;
    const nearThreshold = Math.max(audible ? near * 1.3 : near, this.floor * 8, echoLevel * 1.3);
    if (level > nearThreshold) { this.nearFrames += 1; this.nearQuietFor = 0; }
    else { this.nearQuietFor += 0.02; if (this.nearQuietFor > 0.25) this.nearFrames = 0; }
    // Near voice: ~120 ms of loud frames to start (a click/bang is shorter), 250 ms gap to end.
    const speaking = this.speaking ? this.nearQuietFor <= 0.25 : this.nearFrames >= 6;
    if (speaking !== this.speaking) { this.speaking = speaking; this.port.postMessage({ speaking }); }
    // Learn the room's noise only while nobody speaks.
    if (!this.open && !audible) {
      this.floor = level < this.floor ? this.floor * 0.7 + level * 0.3 : this.floor + (level - this.floor) * 0.01;
    }
  }

  process(inputs, outputs) {
    const mic = inputs[0] && inputs[0][0];
    const remote = inputs[1] && inputs[1][0];
    const out = outputs[0] && outputs[0][0];
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
    }
    return true;
  }
}

registerProcessor("echo-gate", EchoGate);
