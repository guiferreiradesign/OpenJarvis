// AudioWorklet that turns microphone blocks into 80 ms chunks (1280 samples at
// 16 kHz) holding 16-bit PCM values, and posts them to the page. It keeps no
// audio beyond the chunk being filled, and never writes or transmits anything.
class WakeCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const target = (options.processorOptions && options.processorOptions.targetRate) || 16000;
    this.step = sampleRate / target; // input samples consumed per output sample
    this.pos = 0; // fractional read position within the current block
    this.prev = 0; // last sample of the previous block
    this.chunk = new Float32Array(1280);
    this.filled = 0;
  }

  emit(value) {
    const clipped = Math.max(-1, Math.min(1, value));
    this.chunk[this.filled++] = Math.round(clipped * 32767);
    if (this.filled === this.chunk.length) {
      this.port.postMessage(this.chunk, [this.chunk.buffer]);
      this.chunk = new Float32Array(1280);
      this.filled = 0;
    }
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    if (this.step === 1) {
      for (let i = 0; i < ch.length; i++) this.emit(ch[i]);
      return true;
    }
    // Linear resampling for contexts that do not run at 16 kHz.
    while (this.pos < ch.length - 1) {
      const i = Math.floor(this.pos);
      const frac = this.pos - i;
      const a = i >= 0 ? ch[i] : this.prev;
      this.emit(a + (ch[i + 1] - a) * frac);
      this.pos += this.step;
    }
    this.pos -= ch.length;
    this.prev = ch[ch.length - 1];
    return true;
  }
}

registerProcessor('wake-capture', WakeCapture);
