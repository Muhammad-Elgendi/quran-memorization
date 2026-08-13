/**
 * AudioWorklet: capture mic audio, downsample to 16 kHz mono PCM s16le,
 * and post ~chunkMs binary frames to the main thread.
 *
 * Lightweight: linear downsample only — no Opus encode.
 */
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = options.processorOptions || {};
    this.targetRate = opts.targetRate || 16000;
    this.chunkMs = opts.chunkMs || 250;
    this.outChunk = Math.max(1, Math.floor((this.targetRate * this.chunkMs) / 1000));
    this.ratio = sampleRate / this.targetRate;
    this._out = new Int16Array(this.outChunk);
    this._outIdx = 0;
    this._frac = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input || input.length === 0) {
      return true;
    }

    let i = this._frac;
    while (i < input.length) {
      const sample = input[i | 0];
      const clipped = Math.max(-1, Math.min(1, sample));
      this._out[this._outIdx++] =
        clipped < 0 ? clipped * 0x8000 : clipped * 0x7fff;
      if (this._outIdx >= this.outChunk) {
        const copy = this._out.slice(0);
        this.port.postMessage(copy.buffer, [copy.buffer]);
        this._out = new Int16Array(this.outChunk);
        this._outIdx = 0;
      }
      i += this.ratio;
    }
    this._frac = i - input.length;
    return true;
  }
}

registerProcessor("pcm-capture-processor", PcmCaptureProcessor);
