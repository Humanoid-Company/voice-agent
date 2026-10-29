// Takes the (gated) microphone and hands it to the page as PCM16, 24 kHz, mono, 40 ms at a time: the format of
// the Realtime input. Each output sample is the average of the input samples it covers (a cheap low-pass).
class PcmTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 24000; // input samples per output sample
    this.pos = 0;
    this.acc = 0;
    this.n = 0;
    this.out = new Int16Array(960);
    this.k = 0;
  }

  process(inputs) {
    const x = inputs[0] && inputs[0][0];
    if (!x) return true;
    for (let i = 0; i < x.length; i++) {
      this.acc += x[i];
      this.n += 1;
      this.pos += 1;
      if (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        const v = Math.max(-1, Math.min(1, this.acc / this.n));
        this.out[this.k++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        this.acc = 0;
        this.n = 0;
        if (this.k === this.out.length) {
          const buf = this.out.slice().buffer;
          this.port.postMessage(buf, [buf]);
          this.k = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("pcm-tap", PcmTap);
