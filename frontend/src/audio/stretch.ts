/**
 * Pitch-preserving time stretching for preview playback (WSOLA).
 *
 * Playing the preview faster or slower must not change the pitch of the
 * voice.  WSOLA (waveform-similarity overlap-add) does that well for speech:
 * the output is built from 30 ms Hann-windowed frames of the input, laid down
 * every 15 ms; the input is read at `rate` times that hop, and each frame is
 * shifted by up to ±10 ms to the place whose waveform best continues the
 * previous frame, so the overlapping frames add up without phase cancellation.
 *
 * The stretcher is a stream: push input samples as they arrive, pull output
 * hops while there is enough input.  It only ever changes the preview; the
 * export always renders at normal speed.
 */

const FRAME_SECONDS = 0.03;
const TOLERANCE_SECONDS = 0.01;
/** Coarse search step (samples) before refining around the best match. */
const COARSE = 4;

export class Stretcher {
  readonly hop: number;
  private readonly frame: number;
  private readonly tolerance: number;
  private readonly analysisHop: number;
  private readonly window: Float32Array;
  private input: Float32Array[];
  private mono: Float32Array;
  /** Absolute input index of `input[c][0]`. */
  private base = 0;
  private stored = 0;
  private ended = false;
  private index = 0;
  private previous = 0;
  private readonly overlap: Float32Array[];
  private done = false;

  constructor(
    sampleRate: number,
    private readonly channels: number,
    readonly rate: number,
  ) {
    this.frame = 2 * Math.round((FRAME_SECONDS * sampleRate) / 2);
    this.hop = this.frame / 2;
    this.tolerance = Math.round(TOLERANCE_SECONDS * sampleRate);
    this.analysisHop = this.hop * rate;
    this.window = new Float32Array(this.frame);
    for (let n = 0; n < this.frame; n += 1) {
      // Periodic Hann: frames overlapping by half sum to exactly one.
      this.window[n] = 0.5 - 0.5 * Math.cos((2 * Math.PI * n) / this.frame);
    }
    const capacity = this.frame * 8;
    this.input = Array.from({ length: channels }, () => new Float32Array(capacity));
    this.mono = new Float32Array(capacity);
    this.overlap = Array.from({ length: channels }, () => new Float32Array(this.frame));
  }

  get finished(): boolean {
    return this.done;
  }

  /** Append input samples (planar, one array per channel). */
  push(planar: Float32Array[]): void {
    const incoming = planar[0]?.length ?? 0;
    // Forget input no future frame can read.
    const nominal = Math.round(this.index * this.analysisHop);
    const keepFrom = Math.min(nominal - this.tolerance, this.previous + this.hop);
    const drop = Math.max(0, Math.min(this.stored, keepFrom - this.base));
    if (drop > 0) {
      for (const channel of this.input) channel.copyWithin(0, drop, this.stored);
      this.mono.copyWithin(0, drop, this.stored);
      this.stored -= drop;
      this.base += drop;
    }
    if (this.stored + incoming > this.mono.length) {
      const capacity = Math.max(this.mono.length * 2, this.stored + incoming);
      this.input = this.input.map((channel) => {
        const grown = new Float32Array(capacity);
        grown.set(channel.subarray(0, this.stored));
        return grown;
      });
      const mono = new Float32Array(capacity);
      mono.set(this.mono.subarray(0, this.stored));
      this.mono = mono;
    }
    for (let channel = 0; channel < this.channels; channel += 1) {
      this.input[channel]!.set(planar[channel] ?? planar[0]!, this.stored);
    }
    const scale = 1 / this.channels;
    for (let n = 0; n < incoming; n += 1) {
      let sum = 0;
      for (let channel = 0; channel < this.channels; channel += 1) sum += (planar[channel] ?? planar[0]!)[n]!;
      this.mono[this.stored + n] = sum * scale;
    }
    this.stored += incoming;
  }

  /** No more input will come. */
  end(): void {
    this.ended = true;
  }

  private get available(): number {
    return this.base + this.stored;
  }

  private sample(index: number): number {
    const offset = index - this.base;
    return offset >= 0 && offset < this.stored ? this.mono[offset]! : 0;
  }

  /** Normalised similarity of the input at `start` to the input at `target`. */
  private similarity(start: number, target: number, step: number): number {
    let product = 0;
    let energy = 1e-9;
    for (let n = 0; n < this.frame; n += step) {
      const value = this.sample(start + n);
      product += value * this.sample(target + n);
      energy += value * value;
    }
    return product / Math.sqrt(energy);
  }

  private bestPosition(nominal: number, natural: number): number {
    const low = Math.max(this.base, nominal - this.tolerance);
    const high = nominal + this.tolerance;
    let best = Math.max(low, nominal);
    let score = -Infinity;
    for (let start = low; start <= high; start += COARSE) {
      const value = this.similarity(start, natural, COARSE);
      if (value > score) {
        score = value;
        best = start;
      }
    }
    const coarse = best;
    score = -Infinity;
    for (let start = Math.max(low, coarse - COARSE + 1); start <= Math.min(high, coarse + COARSE - 1); start += 1) {
      const value = this.similarity(start, natural, 1);
      if (value > score) {
        score = value;
        best = start;
      }
    }
    return best;
  }

  /** The next `hop` output samples, or null if more input is needed. */
  pull(): Float32Array[] | null {
    if (this.done) return null;
    const nominal = Math.round(this.index * this.analysisHop);
    if (this.ended && nominal >= this.available) {
      // Flush the second half of the last frame.
      this.done = true;
      return this.overlap.map((channel) => channel.slice(0, this.hop));
    }
    let position = nominal;
    if (this.index > 0) {
      const natural = this.previous + this.hop;
      const needed = Math.max(nominal + this.tolerance, natural) + this.frame;
      if (!this.ended && this.available < needed) return null;
      position = this.bestPosition(nominal, natural);
    } else if (!this.ended && this.available < this.frame) {
      return null;
    }
    const output: Float32Array[] = [];
    for (let channel = 0; channel < this.channels; channel += 1) {
      const source = this.input[channel]!;
      const accumulator = this.overlap[channel]!;
      for (let n = 0; n < this.frame; n += 1) {
        const offset = position + n - this.base;
        if (offset >= 0 && offset < this.stored) {
          accumulator[n] = accumulator[n]! + this.window[n]! * source[offset]!;
        }
      }
      output.push(accumulator.slice(0, this.hop));
      accumulator.copyWithin(0, this.hop);
      accumulator.fill(0, this.frame - this.hop);
    }
    this.previous = position;
    this.index += 1;
    return output;
  }
}
