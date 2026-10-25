import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CHUNK_FRAMES, OutputPlayer, RESUME_TIMEOUT_MS, SUSPEND_AFTER_MS } from '../src/audio/player';

const RATE = 48_000;

interface Started {
  when: number;
  offset: number;
  data: Float32Array;
}

/** Just enough of the Web Audio API to see what the player schedules. */
class FakeContext {
  static last: FakeContext | null = null;
  static made = 0;
  currentTime = 1;
  state = 'running';
  sampleRate: number;
  outputLatency = 0;
  baseLatency = 0;
  destination = {};
  started: Started[] = [];
  calls: string[] = [];
  /** resume() never settles (WebKit's stuck resume). */
  stuck = false;

  constructor(options: { sampleRate: number }) {
    this.sampleRate = options.sampleRate;
    FakeContext.last = this;
    FakeContext.made += 1;
  }

  resume() {
    this.calls.push('resume');
    if (this.stuck) return new Promise<void>(() => undefined);
    this.state = 'running';
    return Promise.resolve();
  }

  suspend() {
    this.calls.push('suspend');
    this.state = 'suspended';
    return Promise.resolve();
  }

  close() {
    this.calls.push('close');
    this.state = 'closed';
    return Promise.resolve();
  }

  createGain() {
    return { connect() {}, disconnect() {} };
  }

  createBuffer(_channels: number, length: number) {
    const data = new Float32Array(length);
    return { copyToChannel: (source: Float32Array) => data.set(source), data };
  }

  createBufferSource() {
    const node = {
      buffer: null as null | { data: Float32Array },
      onended: null,
      connect() {},
      disconnect() {},
      stop() {},
      start: (when: number, offset: number) => {
        this.started.push({ when, offset, data: node.buffer!.data });
      },
    };
    return node;
  }
}

function signal(start: number, frames: number): Float32Array {
  return Float32Array.from({ length: frames }, (_, index) => start + index);
}

describe('OutputPlayer', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('AudioContext', FakeContext);
    vi.stubGlobal('window', globalThis);
    vi.stubGlobal('performance', { now: () => Date.now() });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('schedules the output in back-to-back chunks at exact sample times', async () => {
    const total = CHUNK_FRAMES * 3 + 1234;
    const requests: [number, number][] = [];
    const player = new OutputPlayer(RATE, 1, async (_revision, start, frames) => {
      requests.push([start, frames]);
      return [signal(start, frames)];
    });
    const from = 5000;
    await player.play(from, 'rev', total);
    await vi.advanceTimersByTimeAsync(100);
    const context = FakeContext.last!;
    expect(requests.map(([start]) => start)).toEqual([0, CHUNK_FRAMES, CHUNK_FRAMES * 2, CHUNK_FRAMES * 3]);
    expect(requests[3]![1]).toBe(1234);
    const [first, ...rest] = context.started;
    // The first chunk starts mid-way, at the requested frame.
    expect(first!.offset * RATE).toBeCloseTo(from, 6);
    const startTime = first!.when;
    expect((startTime * RATE) % 1).toBeCloseTo(0, 6);
    rest.forEach((chunk, index) => {
      const chunkStart = (index + 1) * CHUNK_FRAMES;
      expect(chunk.offset).toBe(0);
      expect((chunk.when - startTime) * RATE).toBeCloseTo(chunkStart - from, 6);
      expect(chunk.data[0]).toBe(chunkStart);
    });
    player.dispose();
  });

  it('reports the frame being heard and stops cleanly', async () => {
    const player = new OutputPlayer(RATE, 1, async (_revision, start, frames) => [signal(start, frames)]);
    await player.play(1000, 'rev', CHUNK_FRAMES * 2);
    const context = FakeContext.last!;
    const startTime = context.started[0]!.when;
    context.currentTime = startTime + 0.5;
    expect(player.currentFrame()).toBe(1000 + RATE / 2);
    expect(player.stop()).toBe(1000 + RATE / 2);
    expect(player.isPlaying).toBe(false);
    expect(player.currentFrame()).toBe(1000 + RATE / 2);
    player.dispose();
  });

  it('skips the part of a late chunk that has already passed', async () => {
    let release: (() => void) | null = null;
    const player = new OutputPlayer(RATE, 1, (_revision, start, frames) => {
      if (start === 0) return Promise.resolve([signal(start, frames)]);
      return new Promise((resolve) => {
        release = () => resolve([signal(start, frames)]);
      });
    });
    await player.play(0, 'rev', CHUNK_FRAMES * 2);
    await vi.advanceTimersByTimeAsync(50);
    const context = FakeContext.last!;
    const startTime = context.started[0]!.when;
    context.currentTime = startTime + CHUNK_FRAMES / RATE + 0.1;
    release!();
    await vi.advanceTimersByTimeAsync(0);
    const late = context.started[1]!;
    expect(late.when).toBeCloseTo(context.currentTime, 9);
    expect(late.offset).toBeCloseTo(0.1, 9);
    player.dispose();
  });

  it('plays faster through the time stretcher, gaplessly, with the playhead at the rate', async () => {
    const total = CHUNK_FRAMES * 4;
    const player = new OutputPlayer(RATE, 1, async (_revision, start, frames) => [
      Float32Array.from({ length: frames }, (_, index) => Math.sin(((start + index) / RATE) * 2 * Math.PI * 200) * 0.5),
    ]);
    await player.play(0, 'rev', total, 2);
    expect(player.playbackRate).toBe(2);
    const context = FakeContext.last!;
    // Let the clock run in real time until everything is scheduled.
    for (let step = 0; step < 60; step += 1) {
      await vi.advanceTimersByTimeAsync(40);
      context.currentTime += 0.04;
    }
    const started = context.started;
    expect(started.length).toBeGreaterThan(3);
    for (let index = 1; index < started.length; index += 1) {
      const previous = started[index - 1]!;
      expect(started[index]!.when).toBeCloseTo(previous.when + previous.data.length / RATE, 9);
    }
    const produced = started.reduce((sum, item) => sum + item.data.length, 0);
    expect(Math.abs(produced - total / 2)).toBeLessThan(0.04 * RATE);
    player.dispose();
  });

  it('moves the playhead at the playback rate', async () => {
    const player = new OutputPlayer(RATE, 1, async (_revision, start, frames) => [signal(start, frames)]);
    await player.play(1000, 'rev', CHUNK_FRAMES * 8, 1.5);
    const context = FakeContext.last!;
    const startTime = context.started[0]!.when;
    context.currentTime = startTime + 0.4;
    expect(player.currentFrame()).toBe(1000 + Math.round(RATE * 0.4 * 1.5));
    player.dispose();
  });

  it('ignores chunks that arrive after a stop', async () => {
    let release: (() => void) | null = null;
    const player = new OutputPlayer(RATE, 1, (_revision, start, frames) =>
      start === 0
        ? Promise.resolve([signal(start, frames)])
        : new Promise((resolve) => {
            release = () => resolve([signal(start, frames)]);
          }),
    );
    await player.play(0, 'rev', CHUNK_FRAMES * 2);
    await vi.advanceTimersByTimeAsync(50);
    player.stop();
    release!();
    await vi.advanceTimersByTimeAsync(0);
    expect(FakeContext.last!.started).toHaveLength(1);
    player.dispose();
  });

  it('suspends the context while stopped and resumes the same one to play', async () => {
    const player = new OutputPlayer(RATE, 1, async (_revision, start, frames) => [signal(start, frames)]);
    await player.play(0, 'rev', CHUNK_FRAMES * 2);
    const context = FakeContext.last!;
    player.stop();
    await vi.advanceTimersByTimeAsync(SUSPEND_AFTER_MS);
    expect(context.state).toBe('suspended');
    const scheduled = context.started.length;
    await player.play(0, 'rev', CHUNK_FRAMES * 2);
    expect(FakeContext.last).toBe(context);
    expect(context.calls).toEqual(['suspend', 'resume']);
    expect(context.state).toBe('running');
    expect(context.started.length).toBeGreaterThan(scheduled);
    player.dispose();
  });

  it('does not suspend for a seek while playing', async () => {
    const player = new OutputPlayer(RATE, 1, async (_revision, start, frames) => [signal(start, frames)]);
    await player.play(0, 'rev', CHUNK_FRAMES * 2);
    const context = FakeContext.last!;
    player.stop();
    await player.play(1000, 'rev', CHUNK_FRAMES * 2);
    await vi.advanceTimersByTimeAsync(SUSPEND_AFTER_MS * 2);
    expect(context.calls).toEqual([]);
    expect(player.isPlaying).toBe(true);
    player.dispose();
  });

  it('suspends and resumes a context left running idle before playing again', async () => {
    const player = new OutputPlayer(RATE, 1, async (_revision, start, frames) => [signal(start, frames)]);
    await player.play(0, 'rev', CHUNK_FRAMES * 2);
    const context = FakeContext.last!;
    player.stop();
    // Half an hour later, with the suspend held back (a background window's timers).
    vi.setSystemTime(Date.now() + 30 * 60_000);
    await player.play(0, 'rev', CHUNK_FRAMES * 2);
    expect(FakeContext.last).toBe(context);
    expect(context.calls).toEqual(['suspend', 'resume']);
    expect(player.isPlaying).toBe(true);
    player.dispose();
  });

  it('replaces a context that does not resume', async () => {
    const player = new OutputPlayer(RATE, 1, async (_revision, start, frames) => [signal(start, frames)]);
    await player.play(0, 'rev', CHUNK_FRAMES * 2);
    const context = FakeContext.last!;
    player.stop();
    await vi.advanceTimersByTimeAsync(SUSPEND_AFTER_MS);
    context.stuck = true;
    const made = FakeContext.made;
    const playing = player.play(0, 'rev', CHUNK_FRAMES * 2);
    await vi.advanceTimersByTimeAsync(RESUME_TIMEOUT_MS);
    await playing;
    expect(context.calls).toEqual(['suspend', 'resume', 'close']);
    expect(FakeContext.made).toBe(made + 1);
    expect(FakeContext.last).not.toBe(context);
    expect(FakeContext.last!.started.length).toBeGreaterThan(0);
    expect(player.isPlaying).toBe(true);
    player.dispose();
  });

  it('reports an audio output that will not start', async () => {
    const errors: unknown[] = [];
    const player = new OutputPlayer(RATE, 1, async (_revision, start, frames) => [signal(start, frames)]);
    player.onError = (error) => errors.push(error);
    vi.stubGlobal(
      'AudioContext',
      class extends FakeContext {
        override state = 'suspended';
        override stuck = true;
      },
    );
    const playing = player.play(0, 'rev', CHUNK_FRAMES * 2);
    await vi.advanceTimersByTimeAsync(RESUME_TIMEOUT_MS * 2);
    await playing;
    expect(player.isPlaying).toBe(false);
    expect(String(errors[0])).toContain('audio output did not start');
    player.dispose();
  });
});
