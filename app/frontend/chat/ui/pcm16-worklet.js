/**
 * AudioWorklet for voice dictation: microphone audio → 16 kHz mono PCM16.
 *
 * AssemblyAI's stream wants 16 kHz signed 16-bit little-endian samples in
 * 50–1000 ms chunks. The AudioContext runs at whatever the device uses (44.1 or
 * 48 kHz; forcing 16 kHz on the context is not reliable in Safari or Firefox), so
 * this averages each window of input samples down to one output sample and posts
 * a transferable 50 ms buffer whenever one fills.
 *
 * Self-contained on purpose — worklet modules run in their own scope, and not
 * every browser we support lets them import.
 */

const TARGET_RATE = 16000;
const CHUNK_SAMPLES = 800; // 50 ms at 16 kHz

class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    this.chunk = new Int16Array(CHUNK_SAMPLES);
    this.filled = 0;
    this.pos = 0;   // input samples into the current output window
    this.acc = 0;
    this.count = 0;
    this.last = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      this.acc += channel[i];
      this.count += 1;
      this.pos += 1;
      while (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        if (this.count) this.last = this.acc / this.count;
        this.acc = 0;
        this.count = 0;
        this._push(this.last);
      }
    }
    return true;
  }

  _push(sample) {
    const clamped = Math.max(-1, Math.min(1, sample));
    this.chunk[this.filled++] = Math.round(clamped * 0x7fff);
    if (this.filled === CHUNK_SAMPLES) {
      this.port.postMessage(this.chunk.buffer, [this.chunk.buffer]);
      this.chunk = new Int16Array(CHUNK_SAMPLES);
      this.filled = 0;
    }
  }
}

registerProcessor('pcm16-downsampler', Pcm16Downsampler);
