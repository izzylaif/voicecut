/**
 * Editor state of one open project.
 *
 * The committed timeline (clips and pauses) lives in an undo history of
 * immutable snapshots; a gesture in progress (an edge being dragged) is a draft on top of
 * it and becomes one history entry when the gesture ends.  Every commit is
 * autosaved; the server answers with the timeline's revision, which the
 * player uses to fetch rendered audio.  Per-frame consumers (timeline canvas,
 * video, timecode) register with `onFrame` and run in one animation loop.
 */
import { api, ApiError } from './api';
import { OutputPlayer } from './audio/player';
import { History } from './model/history';
import { computeLayout, outputAt, sourceAt, clipAt, editPoints, pauseAt, type Layout } from './model/layout';
import type { EditContext, Edge, Segment } from './model/segments';
import {
  type Timeline,
  fromItems,
  isProposal,
  itemIds,
  manualPause,
  placePause,
  proposalTimeline,
  removeItems,
  sameTimeline,
  splitClip,
  toItems,
  trimClip,
} from './model/timeline';
import type { ProjectState, Proposal, WireTimeline } from './types';

export type SaveState =
  | { state: 'saved' }
  | { state: 'pending' }
  | { state: 'saving' }
  | { state: 'error'; message: string };

export interface ViewState {
  /** Horizontal zoom. */
  pxPerSecond: number;
  /** Output time (s) at the left edge of the timeline. */
  scroll: number;
}

export const MIN_PX_PER_SECOND = 0.02;
export const MAX_PX_PER_SECOND = 4000;
const SAVE_DELAY_MS = 250;
/** Preview playback rates offered in the toolbar (the export is never sped up). */
export const PLAYBACK_RATES = [0.5, 1, 1.5, 2, 2.5] as const;
const RATE_KEY = 'voicecut.playbackRate';

function storedRate(): number {
  try {
    const value = Number(window.localStorage.getItem(RATE_KEY));
    return (PLAYBACK_RATES as readonly number[]).includes(value) ? value : 1;
  } catch {
    return 1;
  }
}

type Listener = () => void;
type FrameListener = (frame: number, playing: boolean) => void;

export class EditorStore {
  readonly id: string;
  readonly sampleRate: number;
  readonly sourceFrames: number;
  readonly context: EditContext;
  readonly player: OutputPlayer;
  readonly sourceTimeline: Timeline;

  proposal: Proposal | null;
  private history: History<Timeline>;
  private draft: Timeline | null = null;
  layout: Layout;
  selection: ReadonlySet<string> = new Set();
  view: ViewState = { pxPerSecond: 50, scroll: 0 };
  viewportWidth = 1000;
  /** The whole edit fits the view (until the user zooms). */
  fitted = true;
  snapping = true;
  /** Preview playback rate; pitch is preserved. */
  rate = storedRate();
  save: SaveState = { state: 'saved' };
  message: { text: string; tone: 'info' | 'error' } | null = null;
  version = 0;

  private playhead = 0;
  private revision: string | null = null;
  private serverFrames: number | null = null;
  private saveTimer: number | undefined;
  private saving: Promise<string> | null = null;
  private dirty = false;
  private listeners = new Set<Listener>();
  private frameListeners = new Set<FrameListener>();
  private animation = 0;
  private needsFrame = true;
  private messageTimer: number | undefined;

  constructor(public project: ProjectState) {
    this.id = project.id;
    this.sampleRate = project.media.sample_rate;
    this.sourceFrames = project.media.frames;
    this.proposal = project.proposal;
    this.context = { proposal: this.proposal, sourceFrames: this.sourceFrames, sampleRate: this.sampleRate };
    this.sourceTimeline = fromItems(project.source_items);
    const initial = project.timeline
      ? fromItems(project.timeline.items)
      : this.proposal
        ? proposalTimeline(this.proposal)
        : this.sourceTimeline;
    this.history = new History(initial);
    this.layout = computeLayout(initial, this.proposal, this.sampleRate);
    this.player = new OutputPlayer(this.sampleRate, project.media.channels, (revision, start, frames) =>
      this.renderChunk(revision, start, frames),
    );
    this.player.onEnded = () => {
      this.playhead = this.layout.total;
      this.changed();
    };
    this.player.onError = (error) => this.notify(`Playback failed: ${errorText(error)}`, 'error');
    this.dirty = true; // register the loaded timeline with the server
    this.scheduleSave(0);
    this.loop();
  }

  // -- subscriptions ----------------------------------------------------------------
  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  onFrame(listener: FrameListener): () => void {
    this.frameListeners.add(listener);
    this.needsFrame = true;
    return () => this.frameListeners.delete(listener);
  }

  /** Something visible changed: re-render React and redraw the canvas. */
  changed(): void {
    this.version += 1;
    this.needsFrame = true;
    for (const listener of this.listeners) listener();
  }

  /** Redraw the canvas only (hover, drag feedback). */
  redraw(): void {
    this.needsFrame = true;
  }

  private loop = (): void => {
    this.animation = requestAnimationFrame(this.loop);
    const playing = this.player.isPlaying;
    if (!playing && !this.needsFrame) return;
    this.needsFrame = false;
    const frame = this.frame();
    for (const listener of this.frameListeners) listener(frame, playing);
  };

  dispose(): void {
    cancelAnimationFrame(this.animation);
    this.player.dispose();
    window.clearTimeout(this.saveTimer);
    window.clearTimeout(this.messageTimer);
    this.listeners.clear();
    this.frameListeners.clear();
  }

  notify(text: string, tone: 'info' | 'error' = 'info'): void {
    this.message = { text, tone };
    window.clearTimeout(this.messageTimer);
    this.messageTimer = window.setTimeout(() => {
      this.message = null;
      this.changed();
    }, tone === 'error' ? 8000 : 3500);
    this.changed();
  }

  // -- timeline ---------------------------------------------------------------------
  /** The timeline on screen (a gesture's draft while one is in progress). */
  get timeline(): Timeline {
    return this.draft ?? this.history.present;
  }

  /** The clips on screen. */
  get segments(): Segment[] {
    return this.timeline.clips;
  }

  /** The committed timeline (unchanged while a gesture is in progress). */
  get committed(): Timeline {
    return this.history.present;
  }

  get canUndo(): boolean {
    return this.history.canUndo;
  }

  get canRedo(): boolean {
    return this.history.canRedo;
  }

  get isEdited(): boolean {
    return this.proposal
      ? !isProposal(this.timeline, this.proposal)
      : !sameTimeline(this.timeline, this.sourceTimeline);
  }

  get isVideo(): boolean {
    return this.project.media.kind === 'video';
  }

  private relayout(): void {
    this.layout = computeLayout(this.timeline, this.proposal, this.sampleRate);
  }

  /** Show `timeline` while a gesture is in progress (not in history). */
  setDraft(timeline: Timeline | null): void {
    this.draft = timeline;
    this.relayout();
    this.changed();
  }

  /** Record `timeline` as one undoable edit. */
  commit(timeline: Timeline, options: { keepPlayhead?: boolean } = {}): void {
    const before = this.layout;
    this.draft = null;
    if (sameTimeline(timeline, this.history.present)) {
      this.relayout();
      this.changed();
      return;
    }
    this.history.push(timeline);
    this.afterChange(before, options.keepPlayhead ?? false);
  }

  undo(): void {
    const before = this.layout;
    if (this.history.undo()) this.afterChange(before, false);
  }

  redo(): void {
    const before = this.layout;
    if (this.history.redo()) this.afterChange(before, false);
  }

  private afterChange(before: Layout, keepPlayhead: boolean): void {
    const wasPlaying = this.player.isPlaying;
    const frame = this.frame();
    if (wasPlaying) this.player.stop();
    this.relayout();
    const ids = itemIds(this.timeline);
    this.selection = new Set([...this.selection].filter((id) => ids.has(id)));
    this.playhead = keepPlayhead ? Math.min(frame, this.layout.total) : this.remap(before, frame);
    this.dirty = true;
    this.scheduleSave();
    this.changed();
    if (wasPlaying) void this.play();
  }

  /** Keep the playhead on the same moment of the recording when possible. */
  private remap(before: Layout, frame: number): number {
    const sample = sourceAt(before, frame);
    const moved = sample === null ? null : outputAt(this.layout, sample);
    return Math.max(0, Math.min(this.layout.total, moved ?? frame));
  }

  /** Take fresh project state from the server (media, runs, exports). */
  updateProject(project: ProjectState): void {
    if ((project.proposal?.plan_sha256 ?? null) !== (this.proposal?.plan_sha256 ?? null)) {
      this.adoptProject(project);
      return;
    }
    this.project = project;
    this.changed();
  }

  /** Replace the proposal after a new Voice Cut run (history starts over). */
  adoptProject(project: ProjectState): void {
    this.project = project;
    this.player.stop();
    this.proposal = project.proposal;
    this.context.proposal = project.proposal;
    const timeline = project.timeline
      ? fromItems(project.timeline.items)
      : project.proposal
        ? proposalTimeline(project.proposal)
        : this.sourceTimeline;
    this.history.reset(timeline);
    this.draft = null;
    this.selection = new Set();
    this.relayout();
    this.playhead = 0;
    this.view = { ...this.view, scroll: 0 };
    this.revision = null;
    this.dirty = true;
    this.scheduleSave(0);
    this.zoomToFit();
    this.changed();
  }

  resetToProposal(): void {
    this.commit(this.proposal ? proposalTimeline(this.proposal) : this.sourceTimeline);
    this.notify(this.proposal ? 'Restored Voice Cut’s edit' : 'Restored the whole recording');
  }

  splitAtPlayhead(): void {
    const frame = this.frame();
    const index = clipAt(this.layout, frame);
    const sample = sourceAt(this.layout, frame);
    if (index < 0 || sample === null) {
      this.notify('Put the playhead inside a clip to split it');
      return;
    }
    const segment = this.layout.clips[index]!.segment;
    const next = splitClip(this.timeline, segment, sample, this.context);
    if (!next) {
      this.notify('Too close to the edge of the clip to split');
      return;
    }
    this.commit(next, { keepPlayhead: true });
  }

  /**
   * Insert a 1 s pause at the playhead: inside a clip the clip is split there;
   * at a cut the pause goes between the clips; inside a pause it grows by 1 s.
   */
  insertPause(frame = this.frame()): void {
    if (this.isVideo) {
      this.notify('Pauses are for audio: a video has no picture to show during one', 'error');
      return;
    }
    const pause = manualPause(this.sampleRate);
    const inPause = pauseAt(this.layout, frame);
    const index = inPause >= 0 ? -1 : clipAt(this.layout, Math.min(frame, this.layout.total - 1));
    const clip = index >= 0 ? this.layout.clips[index]! : null;
    // The cut the pause goes into; null splits the clip. Within 10 ms of a
    // clip's edge the pause goes into the cut itself.
    const near = Math.round(0.01 * this.sampleRate);
    let gap: number | null = null;
    if (inPause >= 0) gap = this.layout.pauses[inPause]!.gap;
    else if (!clip) gap = this.timeline.clips.length;
    else if (frame - clip.outStart < near) gap = clip.segment;
    else if (clip.outEnd - frame < near) gap = clip.segment + 1;
    const next =
      gap !== null
        ? placePause(this.timeline, gap, pause, this.sampleRate)
        : splitClip(this.timeline, clip!.segment, clip!.sourceAtStart + (frame - clip!.outStart), this.context, pause);
    if (!next) {
      this.notify('Too close to the edge of the clip');
      return;
    }
    const grown = gap !== null && this.timeline.gaps[gap] != null;
    const placed = gap ?? clip!.segment + 1;
    this.commit(next, { keepPlayhead: true });
    this.selection = new Set([next.gaps[placed]!.id]);
    this.notify(grown ? 'Pause 1 s longer' : '1 s pause inserted — drag its edges to change it');
  }

  deleteSelection(): void {
    if (this.selection.size === 0) {
      this.notify('Select a clip or a pause to delete it');
      return;
    }
    const next = removeItems(this.timeline, this.selection);
    if (!next) {
      this.notify('At least one clip has to stay on the timeline', 'error');
      return;
    }
    const pauses = this.timeline.gaps.filter((gap) => gap && this.selection.has(gap.id)).length;
    const clips = this.timeline.clips.filter((clip) => this.selection.has(clip.id)).length;
    // The playhead lands where the deleted material was.
    const firstClip = this.timeline.clips.findIndex((clip) => this.selection.has(clip.id));
    const firstPause = this.layout.pauses.find((pause) => this.selection.has(pause.id));
    const at = this.layout.clips.find((item) => item.segment === firstClip)?.outStart ?? firstPause?.outStart;
    this.selection = new Set();
    this.commit(next, { keepPlayhead: true });
    if (at !== undefined) this.setPlayhead(Math.min(at, this.layout.total));
    const parts = [clips ? (clips === 1 ? '1 clip' : `${clips} clips`) : '', pauses ? (pauses === 1 ? '1 pause' : `${pauses} pauses`) : ''];
    this.notify(`Deleted ${parts.filter(Boolean).join(' and ')}`);
  }

  /** Trim the clip under the playhead so its start/end is at the playhead. */
  trimToPlayhead(edge: Edge): void {
    const frame = this.frame();
    const index = clipAt(this.layout, frame);
    const sample = sourceAt(this.layout, frame);
    if (index < 0 || sample === null) return;
    const segment = this.layout.clips[index]!.segment;
    this.commit(trimClip(this.timeline, segment, edge, sample, this.context));
  }

  // -- selection --------------------------------------------------------------------
  select(id: string | null, mode: 'replace' | 'toggle' = 'replace'): void {
    if (id === null) this.selection = new Set();
    else if (mode === 'toggle') {
      const next = new Set(this.selection);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      this.selection = next;
    } else this.selection = new Set([id]);
    this.changed();
  }

  selectAll(): void {
    this.selection = itemIds(this.timeline);
    this.changed();
  }

  // -- playhead & playback ------------------------------------------------------------
  frame(): number {
    return this.player.isPlaying ? this.player.currentFrame() : this.playhead;
  }

  get playing(): boolean {
    return this.player.isPlaying;
  }

  setPlayhead(frame: number): void {
    const wasPlaying = this.player.isPlaying;
    if (wasPlaying) this.player.stop();
    this.playhead = Math.max(0, Math.min(this.layout.total, Math.round(frame)));
    if (wasPlaying) void this.play();
    else if (this.revision) this.player.prefetch(this.playhead, this.revision, this.layout.total);
    this.changed();
  }

  async play(): Promise<void> {
    if (this.player.isPlaying) return;
    if (this.playhead >= this.layout.total) this.playhead = 0;
    let revision: string;
    try {
      revision = await this.flushSave();
    } catch (error) {
      this.notify(`Cannot play: ${errorText(error)}`, 'error');
      return;
    }
    const total = this.serverFrames ?? this.layout.total;
    await this.player.play(this.playhead, revision, total, this.rate);
    this.changed();
  }

  pause(): void {
    if (!this.player.isPlaying) return;
    this.playhead = this.player.stop();
    this.changed();
  }

  togglePlay(): void {
    if (this.player.isPlaying) this.pause();
    else void this.play();
  }

  /** Jump to the previous/next cut. */
  jumpEdit(direction: -1 | 1): void {
    const frame = this.frame();
    const points = editPoints(this.layout);
    const target =
      direction > 0
        ? points.find((point) => point > frame + 1)
        : [...points].reverse().find((point) => point < frame - 1);
    if (target !== undefined) {
      this.setPlayhead(target);
      this.reveal(target);
    }
  }

  nudge(seconds: number): void {
    const frame = this.frame() + Math.round(seconds * this.sampleRate);
    this.setPlayhead(frame);
    this.reveal(this.playhead);
  }

  // -- view -------------------------------------------------------------------------
  setView(view: Partial<ViewState>): void {
    const pxPerSecond = Math.min(
      MAX_PX_PER_SECOND,
      Math.max(MIN_PX_PER_SECOND, view.pxPerSecond ?? this.view.pxPerSecond),
    );
    const scroll = view.scroll ?? this.view.scroll;
    this.view = { pxPerSecond, scroll };
    this.changed();
  }

  /** Clamp scrolling to the timeline (with a little room at the end). */
  clampScroll(scroll: number): number {
    const visible = this.viewportWidth / this.view.pxPerSecond;
    const end = this.layout.total / this.sampleRate;
    return Math.max(0, Math.min(scroll, Math.max(0, end - visible * 0.5)));
  }

  zoom(factor: number, anchorX?: number): void {
    this.fitted = false;
    const anchor = anchorX ?? ((this.frame() / this.sampleRate - this.view.scroll) * this.view.pxPerSecond);
    const time = this.view.scroll + anchor / this.view.pxPerSecond;
    const pxPerSecond = Math.min(MAX_PX_PER_SECOND, Math.max(MIN_PX_PER_SECOND, this.view.pxPerSecond * factor));
    this.view = { pxPerSecond, scroll: this.clampScroll(time - anchor / pxPerSecond) };
    this.changed();
  }

  zoomToFit(): void {
    this.fitted = true;
    const seconds = Math.max(0.5, this.layout.total / this.sampleRate);
    const pxPerSecond = Math.min(MAX_PX_PER_SECOND, Math.max(MIN_PX_PER_SECOND, (this.viewportWidth - 24) / seconds));
    this.view = { pxPerSecond, scroll: 0 };
    this.changed();
  }

  /** Scroll so that output `frame` is visible. */
  reveal(frame: number): void {
    const time = frame / this.sampleRate;
    const visible = this.viewportWidth / this.view.pxPerSecond;
    if (time < this.view.scroll || time > this.view.scroll + visible * 0.95) {
      this.view = { ...this.view, scroll: this.clampScroll(time - visible * 0.3) };
      this.changed();
    }
  }

  setRate(rate: number): void {
    if (rate === this.rate) return;
    this.rate = rate;
    try {
      window.localStorage.setItem(RATE_KEY, String(rate));
    } catch {
      // the choice just is not remembered
    }
    if (this.player.isPlaying) {
      this.playhead = this.player.stop();
      void this.play();
    }
    this.changed();
  }

  /** Step to the next slower (-1) or faster (+1) rate. */
  stepRate(direction: -1 | 1): void {
    const index = PLAYBACK_RATES.indexOf(this.rate as (typeof PLAYBACK_RATES)[number]);
    const next = PLAYBACK_RATES[Math.max(0, Math.min(PLAYBACK_RATES.length - 1, (index < 0 ? 1 : index) + direction))]!;
    this.setRate(next);
  }

  toggleSnapping(): void {
    this.snapping = !this.snapping;
    this.notify(this.snapping ? 'Snapping on' : 'Snapping off');
  }

  // -- saving & rendering ---------------------------------------------------------------
  wireTimeline(): WireTimeline {
    return {
      schema_version: 3,
      proposal_sha256: this.proposal?.plan_sha256 ?? null,
      items: toItems(this.history.present),
    };
  }

  private scheduleSave(delay = SAVE_DELAY_MS): void {
    this.save = { state: 'pending' };
    window.clearTimeout(this.saveTimer);
    this.saveTimer = window.setTimeout(() => {
      this.flushSave().catch(() => undefined);
    }, delay);
  }

  /** Save now if needed; resolve with the revision of the saved timeline. */
  async flushSave(): Promise<string> {
    window.clearTimeout(this.saveTimer);
    while (this.saving) await this.saving.catch(() => undefined);
    if (!this.dirty && this.revision) return this.revision;
    this.dirty = false;
    const timeline = this.wireTimeline();
    this.save = { state: 'saving' };
    this.changed();
    this.saving = api.saveTimeline(this.id, timeline).then(
      (response) => {
        this.revision = response.revision;
        this.serverFrames = response.output_frames;
        if (response.output_frames !== computeLayout(this.history.present, this.proposal, this.sampleRate).total) {
          console.warn('VoiceCut editor: timeline layout differs from the server', response);
        }
        this.save = this.dirty ? { state: 'pending' } : { state: 'saved' };
        this.saving = null;
        this.changed();
        return response.revision;
      },
      (error: unknown) => {
        this.dirty = true;
        this.save = { state: 'error', message: errorText(error) };
        this.saving = null;
        this.changed();
        throw error;
      },
    );
    return this.saving;
  }

  private async renderChunk(revision: string, start: number, frames: number): Promise<Float32Array[]> {
    try {
      return await api.render(this.id, revision, start, frames);
    } catch (error) {
      // The server forgot the revision (restarted): register it again.
      if (error instanceof ApiError && error.status === 409) {
        this.dirty = true;
        const again = await this.flushSave();
        return api.render(this.id, again, start, frames);
      }
      throw error;
    }
  }
}

export function errorText(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
