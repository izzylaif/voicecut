import { describe, expect, it } from 'vitest';
import { Stretcher } from '../src/audio/stretch';

const RATE = 48_000;

/** A voiced, speech-like test signal: harmonics of `f0` with a slow envelope. */
function voice(seconds: number, f0: number): Float32Array {
  return Float32Array.from({ length: Math.round(seconds * RATE) }, (_, n) => {
    const t = n / RATE;
    const envelope = 0.6 + 0.4 * Math.sin(2 * Math.PI * 1.5 * t);
    let value = 0;
    for (let harmonic = 1; harmonic <= 5; harmonic += 1) value += Math.sin(2 * Math.PI * f0 * harmonic * t) / harmonic;
    return 0.3 * envelope * value;
  });
}

function stretch(signal: Float32Array, rate: number, pushSize = 7_000): Float32Array {
  const stretcher = new Stretcher(RATE, 1, rate);
  const out: number[] = [];
  let offset = 0;
  for (;;) {
    const block = stretcher.pull();
    if (block) {
      out.push(...block[0]!);
      continue;
    }
    if (stretcher.finished) break;
    if (offset < signal.length) {
      stretcher.push([signal.subarray(offset, offset + pushSize)]);
      offset += pushSize;
    } else {
      stretcher.end();
    }
  }
  return Float32Array.from(out);
}

/**
 * Fundamental frequency by normalised autocorrelation over 60–400 Hz: the
 * first local maximum within 10% of the best one (avoids octave errors).
 */
function pitch(signal: Float32Array, from: number, length: number): number {
  const scores: [number, number][] = [];
  for (let lag = Math.floor(RATE / 400); lag <= Math.ceil(RATE / 60); lag += 1) {
    let sum = 0;
    let a = 1e-9;
    let b = 1e-9;
    for (let n = from; n < from + length; n += 1) {
      sum += signal[n]! * signal[n + lag]!;
      a += signal[n]! ** 2;
      b += signal[n + lag]! ** 2;
    }
    scores.push([lag, sum / Math.sqrt(a * b)]);
  }
  const best = Math.max(...scores.map(([, score]) => score));
  const peak = scores.find(
    ([, score], index) =>
      score >= best * 0.9 && index > 0 && score >= scores[index - 1]![1] && score >= (scores[index + 1]?.[1] ?? -1),
  )!;
  return RATE / peak[0];
}

function maxAbs(signal: Float32Array): number {
  let peak = 0;
  for (const value of signal) peak = Math.max(peak, Math.abs(value));
  return peak;
}

describe('Stretcher', () => {
  for (const rate of [0.5, 1.5, 2, 2.5]) {
    it(`plays ${rate}× faster without changing the pitch`, () => {
      const input = voice(3, 150);
      const output = stretch(input, rate);
      // Duration follows the rate (within one frame).
      expect(Math.abs(output.length - input.length / rate)).toBeLessThan(0.04 * RATE);
      // Pitch stays at 150 Hz.
      for (const at of [0.25, 0.5, 0.75]) {
        expect(Math.abs(pitch(output, Math.floor(output.length * at), 4_800) - 150)).toBeLessThan(2);
      }
      // No clicks: the overlap-added frames stay in phase.
      const peak = maxAbs(input);
      let jump = 0;
      for (let n = RATE * 0.05; n < output.length - 1; n += 1) jump = Math.max(jump, Math.abs(output[n + 1]! - output[n]!));
      let natural = 0;
      for (let n = 1; n < input.length; n += 1) natural = Math.max(natural, Math.abs(input[n]! - input[n - 1]!));
      expect(jump).toBeLessThan(natural * 1.6);
      expect(maxAbs(output)).toBeLessThan(peak * 1.2);
    });
  }

  it('does not depend on how the input is split', () => {
    const input = voice(1, 120);
    expect(stretch(input, 2, 1_000)).toEqual(stretch(input, 2, 9_999));
  });

  it('keeps stereo channels aligned', () => {
    const left = voice(1, 140);
    const right = left.map((value) => -value);
    const stretcher = new Stretcher(RATE, 2, 1.5);
    stretcher.push([left, right]);
    stretcher.end();
    for (let block = stretcher.pull(); block; block = stretcher.pull()) {
      block[0]!.forEach((value, index) => expect(block[1]![index]).toBeCloseTo(-value, 6));
    }
  });
});
