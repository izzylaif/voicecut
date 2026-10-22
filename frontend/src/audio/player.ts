/**
 * Plays the edited result.
 *
 * The server renders any stretch of the output with the export's own
 * renderer (fades, crossfades and room tone included), so what plays here is
 * the exported audio, sample for sample.  The player fetches the output in
 * fixed chunks just ahead of the playhead and schedules them back to back on
 * an AudioContext running at the recording's sample rate, which makes the
 * joins between chunks seamless.  Only a few seconds are held in memory, so
 * hour-long recordings play as easily as short ones.
 *
 * At a playback rate other than 1 the same chunks are fed, in order, through
 * a pitch-preserving time stretcher (stretch.ts) and its output is scheduled
 * back to back instead; the playhead then advances `rate` output samples per
 * sample heard.
 */
import { Stretcher } from './stretch';

export const CHUNK_FRAMES = 1 << 15;
const AHEAD_SECONDS = 2.5;
const START_DELAY_SECONDS = 0.03;
const PUMP_MS = 40;
const CACHE_CHUNKS = 96;
/** Stretched output is scheduled in buffers of about this many samples. */
const STRETCH_BLOCK = 8192;

export type FetchChunk = (revision: string, start: number, frames: number) => Promise<Float32Array[]>;

export class OutputPlayer {
  private context: AudioContext | null = null;
  private output: GainNode | null = null;
  private readonly cache = new Map<string, Promise<Float32Array[]>>();
  private readonly nodes = new Set<AudioBufferSourceNode>();
  private generation = 0;
  private playing = false;
  private revision = '';
  private total = 0;
  private startFrame = 0;
  private startTime = 0;
  private nextChunk = 0;
  private timer: number | undefined;
  private stoppedAt = 0;
  private rate = 1;
  private stretcher: Stretcher | null = null;
  /** Stretched samples scheduled since `startTime`. */
  private scheduled = 0;
  /** Next input chunk to feed the stretcher, and whether one is on its way. */
  private feedChunk = 0;
  private feeding = false;

  onEnded: (() => void) | null = null;
  onError: ((error: unknown) => void) | null = null;

  constructor(
    private readonly sampleRate: number,
    private readonly channels: number,
    private readonly fetchChunk: FetchChunk,
  ) {}

  get isPlaying(): boolean {
    return this.playing;
  }

  get playbackRate(): number {
    return this.rate;
  }

  private ensureContext(): AudioContext {
    if (this.context) return this.context;
    try {
      this.context = new AudioContext({ sampleRate: this.sampleRate, latencyHint: 'interactive' });
    } catch {
      this.context = new AudioContext({ latencyHint: 'interactive' });
    }
    this.output = this.context.createGain();
    this.output.connect(this.context.destination);
    return this.context;
  }

  private chunk(revision: string, index: number, total: number): Promise<Float32Array[]> {
    const key = `${revision}:${index}`;
    const cached = this.cache.get(key);
    if (cached) {
      this.cache.delete(key);
      this.cache.set(key, cached);
      return cached;
    }
    const start = index * CHUNK_FRAMES;
    const promise = this.fetchChunk(revision, start, Math.min(CHUNK_FRAMES, total - start));
    promise.catch(() => this.cache.delete(key));
    this.cache.set(key, promise);
    while (this.cache.size > CACHE_CHUNKS) this.cache.delete(this.cache.keys().next().value!);
    return promise;
  }

  /** Warm the cache so that playing from `frame` starts instantly. */
  prefetch(frame: number, revision: string, total: number): void {
    if (!revision || frame >= total) return;
    const first = Math.floor(frame / CHUNK_FRAMES);
    for (let index = first; index < first + 2 && index * CHUNK_FRAMES < total; index += 1) {
      this.chunk(revision, index, total).catch(() => undefined);
    }
  }

  async play(frame: number, revision: string, total: number, rate = 1): Promise<void> {
    this.halt();
    const generation = this.generation;
    this.revision = revision;
    this.total = total;
    this.stoppedAt = frame;
    this.rate = rate;
    if (frame >= total) return;
    const context = this.ensureContext();
    this.playing = true;
    try {
      if (context.state === 'suspended') await context.resume();
      await this.chunk(revision, Math.floor(frame / CHUNK_FRAMES), total);
    } catch (error) {
      if (generation === this.generation) {
        this.playing = false;
        this.onError?.(error);
      }
      return;
    }
    if (generation !== this.generation) return;
    this.startFrame = frame;
    // Start on a sample boundary of the context clock.
    this.startTime =
      Math.round((context.currentTime + START_DELAY_SECONDS) * context.sampleRate) / context.sampleRate;
    this.nextChunk = Math.floor(frame / CHUNK_FRAMES);
    if (rate !== 1) {
      this.stretcher = new Stretcher(this.sampleRate, this.channels, rate);
      this.scheduled = 0;
      this.feedChunk = this.nextChunk;
      this.feeding = false;
    }
    this.pump();
    this.timer = window.setInterval(() => this.pump(), PUMP_MS);
  }

  /** Stop and return the output frame that was playing. */
  stop(): number {
    if (this.playing) this.stoppedAt = this.currentFrame();
    this.halt();
    return this.stoppedAt;
  }

  private halt(): void {
    this.generation += 1;
    this.playing = false;
    window.clearInterval(this.timer);
    this.timer = undefined;
    for (const node of this.nodes) {
      try {
        node.stop();
      } catch {
        // already stopped
      }
      node.disconnect();
    }
    this.nodes.clear();
    this.stretcher = null;
  }

  /** The output frame being heard right now. */
  currentFrame(): number {
    if (!this.playing || !this.context || this.timer === undefined) return this.stoppedAt;
    const heard = this.heardTime(this.context);
    if (heard <= this.startTime) return this.startFrame;
    const elapsed = (heard - this.startTime) * this.sampleRate * this.rate;
    return Math.min(this.total, this.startFrame + Math.round(elapsed));
  }

  private heardTime(context: AudioContext): number {
    const stamp = context.getOutputTimestamp?.();
    if (stamp?.contextTime && stamp.performanceTime) {
      return stamp.contextTime + (performance.now() - stamp.performanceTime) / 1000;
    }
    return context.currentTime - (context.outputLatency || context.baseLatency || 0);
  }

  private pump(): void {
    const context = this.context;
    if (!this.playing || !context) return;
    if (this.stretcher) {
      this.pumpStretched(context);
      return;
    }
    const generation = this.generation;
    const horizon = context.currentTime + AHEAD_SECONDS;
    while (this.nextChunk * CHUNK_FRAMES < this.total) {
      const first = Math.max(this.nextChunk * CHUNK_FRAMES, this.startFrame);
      if (this.startTime + (first - this.startFrame) / this.sampleRate > horizon) break;
      const index = this.nextChunk;
      this.nextChunk += 1;
      this.chunk(this.revision, index, this.total).then(
        (planar) => {
          if (generation === this.generation) this.schedule(context, index, planar);
        },
        (error: unknown) => {
          if (generation !== this.generation) return;
          this.stop();
          this.onError?.(error);
        },
      );
    }
    this.checkEnded();
  }

  private checkEnded(): void {
    if (this.currentFrame() >= this.total) {
      this.stop();
      this.stoppedAt = this.total;
      this.onEnded?.();
    }
  }

  /** Feed the next chunk of output, in order, into the stretcher. */
  private feed(context: AudioContext): void {
    const stretcher = this.stretcher;
    if (!stretcher || this.feeding || this.feedChunk * CHUNK_FRAMES >= this.total) return;
    const generation = this.generation;
    const index = this.feedChunk;
    this.feeding = true;
    // Keep the chunk after this one on its way too.
    if ((index + 1) * CHUNK_FRAMES < this.total) this.chunk(this.revision, index + 1, this.total).catch(() => undefined);
    this.chunk(this.revision, index, this.total).then(
      (planar) => {
        if (generation !== this.generation) return;
        const skip = Math.max(0, this.startFrame - index * CHUNK_FRAMES);
        stretcher.push(planar.map((channel) => channel.subarray(skip)));
        this.feedChunk = index + 1;
        if (this.feedChunk * CHUNK_FRAMES >= this.total) stretcher.end();
        this.feeding = false;
        this.pumpStretched(context);
      },
      (error: unknown) => {
        if (generation !== this.generation) return;
        this.stop();
        this.onError?.(error);
      },
    );
  }

  private pumpStretched(context: AudioContext): void {
    const stretcher = this.stretcher;
    if (!stretcher) return;
    const horizon = context.currentTime + AHEAD_SECONDS;
    while (this.startTime + this.scheduled / this.sampleRate < horizon) {
      const blocks: Float32Array[][] = [];
      let length = 0;
      while (length < STRETCH_BLOCK) {
        const block = stretcher.pull();
        if (!block) break;
        blocks.push(block);
        length += block[0]!.length;
      }
      if (length > 0) this.scheduleStretched(context, blocks, length);
      if (length < STRETCH_BLOCK) {
        this.feed(context);
        break;
      }
    }
    this.checkEnded();
  }

  private scheduleStretched(context: AudioContext, blocks: Float32Array[][], length: number): void {
    let when = this.startTime + this.scheduled / this.sampleRate;
    const late = context.currentTime - when;
    if (late > 0) {
      // Input arrived late: shift the clock so the playhead waits with the sound.
      this.startTime += late + 0.01;
      when += late + 0.01;
    }
    const buffer = context.createBuffer(this.channels, length, this.sampleRate);
    for (let channel = 0; channel < this.channels; channel += 1) {
      const data = new Float32Array(length);
      let offset = 0;
      for (const block of blocks) {
        data.set(block[channel]!, offset);
        offset += block[channel]!.length;
      }
      buffer.copyToChannel(data, channel);
    }
    const node = context.createBufferSource();
    node.buffer = buffer;
    node.connect(this.output!);
    node.onended = () => {
      this.nodes.delete(node);
      node.disconnect();
    };
    node.start(when);
    this.nodes.add(node);
    this.scheduled += length;
  }

  private schedule(context: AudioContext, index: number, planar: Float32Array[]): void {
    const length = planar[0]?.length ?? 0;
    const chunkStart = index * CHUNK_FRAMES;
    const begin = Math.max(chunkStart, this.startFrame);
    if (length === 0 || begin >= chunkStart + length) return;
    let when = this.startTime + (begin - this.startFrame) / this.sampleRate;
    let offset = (begin - chunkStart) / this.sampleRate;
    const late = context.currentTime - when;
    if (late > 0) {
      // The chunk arrived after its time: skip what has already passed.
      when += late;
      offset += late;
      if (offset >= length / this.sampleRate) return;
    }
    const buffer = context.createBuffer(this.channels, length, this.sampleRate);
    planar.forEach((data, channel) => buffer.copyToChannel(data as Float32Array<ArrayBuffer>, channel));
    const node = context.createBufferSource();
    node.buffer = buffer;
    node.connect(this.output!);
    node.onended = () => {
      this.nodes.delete(node);
      node.disconnect();
    };
    node.start(when, offset);
    this.nodes.add(node);
  }

  dispose(): void {
    this.halt();
    this.cache.clear();
    void this.context?.close();
    this.context = null;
  }
}
