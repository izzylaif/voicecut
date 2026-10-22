import { describe, expect, it } from 'vitest';
import { delta, duration, timecode } from '../src/format';
import { PeakStore } from '../src/timeline/peaks';

function bins(values: number[][]): Int16Array {
  return Int16Array.from(values.flat());
}

describe('PeakStore', () => {
  const count = 3000;
  const base = bins(Array.from({ length: count }, (_, index) => [-(index % 97) * 100, (index % 89) * 100]));

  it('answers min/max over any range from the coarsest sufficient level', () => {
    const store = new PeakStore(base, 256, async () => new Int16Array(), () => {});
    expect(store.levels.map((level) => level.spp)).toEqual([256, 1024, 4096]);
    const out = new Float32Array(2);
    for (const [from, to] of [
      [0, 256],
      [256 * 10, 256 * 50],
      [256 * 7, 256 * 2900],
    ] as const) {
      expect(store.range(from, to, out)).toBe(true);
      let low = 32767;
      let high = -32767;
      for (let bin = Math.floor(from / 256); bin < Math.ceil(to / 256); bin += 1) {
        low = Math.min(low, base[bin * 2]!);
        high = Math.max(high, base[bin * 2 + 1]!);
      }
      // Coarser levels may widen the envelope only by their own alignment.
      expect(out[0]).toBeLessThanOrEqual(low / 32767 + 1e-6);
      expect(out[1]).toBeGreaterThanOrEqual(high / 32767 - 1e-6);
    }
    expect(store.range(256 * count, 256 * count + 10, out)).toBe(false);
  });

  it('fetches fine tiles when zoomed in and uses them once they arrive', async () => {
    const requests: number[][] = [];
    let redraws = 0;
    const fine = Int16Array.from({ length: 4096 * 2 }, (_, index) => (index % 2 ? 7 : -7));
    const store = new PeakStore(
      base,
      256,
      async (spp, first, length) => {
        requests.push([spp, first, length]);
        return fine;
      },
      () => {
        redraws += 1;
      },
    );
    const out = new Float32Array(2);
    store.range(64, 96, out); // 32 samples: needs the 32-sample level
    expect(requests).toEqual([[32, 0, 4096]]);
    await Promise.resolve();
    await Promise.resolve();
    expect(redraws).toBe(1);
    store.range(64, 96, out);
    expect(out[0]).toBeCloseTo(-7 / 32767);
    expect(out[1]).toBeCloseTo(7 / 32767);
  });

  it('scales the display to a robust peak, not a single spike', () => {
    const quiet = bins(Array.from({ length: 10_000 }, (_, index) => (index === 5 ? [-32767, 32767] : [-3000, 3000])));
    const store = new PeakStore(quiet, 256, async () => new Int16Array(), () => {});
    expect(store.peak).toBeCloseTo(3000 / 32767);
  });
});

describe('format', () => {
  it('formats times', () => {
    expect(timecode(0)).toBe('0:00.000');
    expect(timecode(61.5)).toBe('1:01.500');
    expect(timecode(3725.25, 1)).toBe('1:02:05.3');
    expect(duration(4.256)).toBe('4.26 s');
    expect(duration(125)).toBe('2 min 05 s');
    expect(delta(-0.25)).toBe('−0.250 s');
  });
});
