/**
 * Waveform envelope of the original recording at any zoom.
 *
 * The server sends one base level (min/max per 256 samples, int16) for the
 * whole recording; coarser levels are built here by 4:1 reduction.  When the
 * view is zoomed in further than the base level resolves, finer levels are
 * fetched in tiles for just the visible stretch, and the base level is drawn
 * until they arrive.  Every column of the drawn waveform is the true min/max
 * of the source samples under it.
 */

export const FINE_SPPS = [16, 32, 64, 128] as const;
const TILE_BINS = 4096;
const MAX_TILES = 256;

interface Level {
  spp: number;
  data: Int16Array;
}

export type FetchFine = (spp: number, firstBin: number, count: number) => Promise<Int16Array>;

export class PeakStore {
  readonly levels: Level[];
  /** Level drawn at full height (robust peak of the recording), 0..1. */
  readonly peak: number;
  private readonly tiles = new Map<string, Int16Array>();
  private readonly pending = new Set<string>();

  constructor(
    base: Int16Array,
    baseSpp: number,
    private readonly fetchFine: FetchFine,
    private readonly onTile: () => void,
  ) {
    this.levels = [{ spp: baseSpp, data: base }];
    let level = this.levels[0]!;
    while (level.data.length / 2 > 512) {
      const bins = Math.ceil(level.data.length / 2 / 4);
      const data = new Int16Array(bins * 2);
      for (let bin = 0; bin < bins; bin += 1) {
        let low = 32767;
        let high = -32767;
        const last = Math.min(level.data.length / 2, bin * 4 + 4);
        for (let source = bin * 4; source < last; source += 1) {
          low = Math.min(low, level.data[source * 2]!);
          high = Math.max(high, level.data[source * 2 + 1]!);
        }
        data[bin * 2] = low;
        data[bin * 2 + 1] = high;
      }
      level = { spp: level.spp * 4, data };
      this.levels.push(level);
    }
    this.peak = loudness(base) / 32767;
  }

  private tile(spp: number, index: number): Int16Array | null {
    const key = `${spp}:${index}`;
    const tile = this.tiles.get(key);
    if (tile) return tile;
    if (!this.pending.has(key)) {
      this.pending.add(key);
      this.fetchFine(spp, index * TILE_BINS, TILE_BINS)
        .then((data) => {
          this.tiles.set(key, data);
          while (this.tiles.size > MAX_TILES) this.tiles.delete(this.tiles.keys().next().value!);
          this.onTile();
        })
        .catch(() => undefined)
        .finally(() => this.pending.delete(key));
    }
    return null;
  }

  /**
   * Min and max (in -1..1) of source samples `[from, to)` into `out`.
   * Returns false when the range is outside the recording.
   */
  range(from: number, to: number, out: Float32Array): boolean {
    const width = Math.max(1, to - from);
    const base = this.levels[0]!;
    if (width < base.spp) {
      const spp = width >= 128 ? 128 : width >= 64 ? 64 : width >= 32 ? 32 : 16;
      const first = Math.floor(from / spp);
      const last = Math.max(first + 1, Math.ceil(to / spp));
      let low = 32767;
      let high = -32767;
      let complete = true;
      for (let bin = first; bin < last; bin += 1) {
        const tile = this.tile(spp, Math.floor(bin / TILE_BINS));
        if (!tile) {
          complete = false;
          break;
        }
        const offset = (bin % TILE_BINS) * 2;
        if (offset >= tile.length) break;
        low = Math.min(low, tile[offset]!);
        high = Math.max(high, tile[offset + 1]!);
      }
      if (complete && low <= high) {
        out[0] = low / 32767;
        out[1] = high / 32767;
        return true;
      }
    }
    let level = base;
    for (const candidate of this.levels) if (candidate.spp <= width) level = candidate;
    const bins = level.data.length / 2;
    const first = Math.floor(from / level.spp);
    if (first >= bins || to <= 0) return false;
    const last = Math.min(bins, Math.max(first + 1, Math.ceil(to / level.spp)));
    let low = 32767;
    let high = -32767;
    for (let bin = Math.max(0, first); bin < last; bin += 1) {
      low = Math.min(low, level.data[bin * 2]!);
      high = Math.max(high, level.data[bin * 2 + 1]!);
    }
    out[0] = low / 32767;
    out[1] = high / 32767;
    return true;
  }
}

/**
 * A robust "full scale" for display: the 99.7th percentile of the per-bin
 * peaks, so a single click or bump does not shrink the whole waveform.
 */
function loudness(bins: Int16Array): number {
  const count = bins.length / 2;
  if (count === 0) return 32767;
  const step = Math.max(1, Math.floor(count / 50_000));
  const values: number[] = [];
  for (let bin = 0; bin < count; bin += step) {
    values.push(Math.max(Math.abs(bins[bin * 2]!), Math.abs(bins[bin * 2 + 1]!)));
  }
  values.sort((a, b) => a - b);
  return Math.max(1, values[Math.min(values.length - 1, Math.floor(values.length * 0.997))]!);
}
