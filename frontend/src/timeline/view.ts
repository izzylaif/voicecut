/**
 * The timeline: a canvas that draws the edit and turns pointer gestures into
 * edits.
 *
 * Rows, top to bottom: an overview of the whole original recording (what is
 * kept, where the playhead is in the source), the ruler (output time), the
 * picture lane for video, and the sound lane with each clip's waveform and
 * words.  Clips are laid out exactly where they land in the exported file.
 *
 * Trimming is a ripple edit that keeps the dragged edge under the pointer:
 * dragging a clip's end moves everything after it; dragging a clip's start
 * pulls material in from (or gives it back to) the left while the rest of the
 * clip and everything after it stay where they are on screen.
 *
 * Dragging a clip's body moves the selected clips to the cut nearest the
 * pointer (⌥ drops copies instead); a press that does not move puts the
 * playhead there.  ⇧-click selects a range, ⌘/Ctrl-click one item more or less.
 */
import { delta as formatDelta, duration as formatDuration, timecode } from '../format';
import { clipAt, sourceAt, type ClipView, type Layout } from '../model/layout';
import { type Edge, edgeLimits, nearestTarget, snapTargets } from '../model/segments';
import { type Timeline, isThroughEdit, pauseLimits, resizePause, trimClip } from '../model/timeline';
import type { EditorStore } from '../store';
import { wordsBetween } from '../model/words';
import type { Filmstrip } from '../types';
import { onThemeChange, theme } from '../theme';
import { PALETTES, type Palette } from './palette';
import type { PeakStore } from './peaks';

const GESTURE_EVENTS = ['gesturestart', 'gesturechange', 'gestureend'] as const;
const OVERVIEW_HEIGHT = 26;
const RULER_HEIGHT = 24;
const VIDEO_HEIGHT = 56;
const GAP = 6;
const EDGE_HIT_PX = 7;
/** Blocks narrower than this on screen offer no edge: a click there seeks. */
const MIN_GRAB_WIDTH_PX = 8;
const SNAP_PX = 8;
const AUTOSCROLL_ZONE = 48;
/** How far a press on a clip must travel to become a move. */
const MOVE_THRESHOLD_PX = 5;
const WORD_ROW = 18;
const TITLE_ROW = 16;

type Row = 'overview' | 'ruler' | 'video' | 'audio' | 'none';

interface TrimDrag {
  kind: 'trim';
  /** A clip edge (index = clip) or a pause edge (index = gap). */
  target: 'clip' | 'pause';
  index: number;
  edge: Edge;
  original: Timeline;
  /** The clip edge's source sample, or the pause's length. */
  originalValue: number;
  startX: number;
  startScroll: number;
  /** Output frame of the dragged item's end when the drag began. */
  anchor: number;
  autoScroll: number;
  pointerX: number;
  altKey: boolean;
  targets: number[];
  snapped: number | null;
  moved: boolean;
}

interface ScrubDrag {
  kind: 'scrub';
  row: Row;
  resume: boolean;
}

/** A press on a clip's body: a click, or a move once it travels. */
interface MoveDrag {
  kind: 'move';
  /** The clip pressed. */
  id: string;
  startX: number;
  pointerX: number;
  altKey: boolean;
  moving: boolean;
  /** The clip boundary the clips would drop at (timeline.ts insertClips). */
  boundary: number | null;
  /** Released without moving, a press on one clip of several selects it alone. */
  narrow: boolean;
  resume: boolean;
}

interface EdgeHit {
  target: 'clip' | 'pause';
  /** Clip index, or gap index for a pause. */
  index: number;
  edge: Edge;
}

export interface ContextTarget {
  clientX: number;
  clientY: number;
  frame: number;
  clip: string | null;
  pause: string | null;
}

export class TimelineView {
  private readonly context: CanvasRenderingContext2D;
  private width = 0;
  private height = 0;
  private ratio = 1;
  private drag: TrimDrag | ScrubDrag | MoveDrag | null = null;
  private hover: EdgeHit | null = null;
  private readonly sheets = new Map<number, HTMLImageElement>();
  private readonly textWidths = new Map<string, number>();
  private readonly range = new Float32Array(2);
  private readonly unsubscribe: () => void;
  private readonly unsubscribeTheme: () => void;
  private readonly observer: ResizeObserver;
  private colors: Palette = PALETTES[theme()];
  peaks: PeakStore | null = null;
  /** Scale of the trackpad pinch in progress (WebKit's gesture events). */
  private gestureScale: number | null = null;
  /** Right-click on the timeline (the playhead has moved there). */
  onMenu: ((target: ContextTarget) => void) | null = null;

  constructor(
    private readonly canvas: HTMLCanvasElement,
    private readonly store: EditorStore,
  ) {
    const context = canvas.getContext('2d');
    if (!context) throw new Error('canvas 2D is not available');
    this.context = context;
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(canvas.parentElement ?? canvas);
    this.resize();
    canvas.addEventListener('pointerdown', this.onPointerDown);
    canvas.addEventListener('pointermove', this.onPointerMove);
    canvas.addEventListener('pointerup', this.onPointerUp);
    canvas.addEventListener('pointercancel', this.onPointerUp);
    canvas.addEventListener('pointerleave', this.onPointerLeave);
    canvas.addEventListener('wheel', this.onWheel, { passive: false });
    for (const type of GESTURE_EVENTS) canvas.addEventListener(type, this.onGesture);
    canvas.addEventListener('contextmenu', this.onContextMenu);
    this.unsubscribe = store.onFrame((frame, playing) => this.tick(frame, playing));
    this.unsubscribeTheme = onThemeChange((next) => {
      this.colors = PALETTES[next];
      store.redraw();
    });
  }

  dispose(): void {
    this.unsubscribe();
    this.unsubscribeTheme();
    this.observer.disconnect();
    this.canvas.removeEventListener('pointerdown', this.onPointerDown);
    this.canvas.removeEventListener('pointermove', this.onPointerMove);
    this.canvas.removeEventListener('pointerup', this.onPointerUp);
    this.canvas.removeEventListener('pointercancel', this.onPointerUp);
    this.canvas.removeEventListener('pointerleave', this.onPointerLeave);
    this.canvas.removeEventListener('wheel', this.onWheel);
    for (const type of GESTURE_EVENTS) this.canvas.removeEventListener(type, this.onGesture);
    this.canvas.removeEventListener('contextmenu', this.onContextMenu);
  }

  // -- geometry ---------------------------------------------------------------------
  private get video(): boolean {
    return this.store.project.media.kind === 'video';
  }

  private rows() {
    const ruler = OVERVIEW_HEIGHT + GAP;
    const videoTop = ruler + RULER_HEIGHT + GAP;
    const audioTop = videoTop + (this.video ? VIDEO_HEIGHT + GAP : 0);
    return {
      ruler,
      videoTop,
      audioTop,
      audioHeight: Math.max(80, this.height - audioTop - GAP),
    };
  }

  private rowAt(y: number): Row {
    const rows = this.rows();
    if (y < OVERVIEW_HEIGHT) return 'overview';
    if (y < rows.ruler + RULER_HEIGHT) return 'ruler';
    if (this.video && y >= rows.videoTop && y < rows.videoTop + VIDEO_HEIGHT) return 'video';
    if (y >= rows.audioTop && y < rows.audioTop + rows.audioHeight) return 'audio';
    return 'none';
  }

  private resize(): void {
    const parent = this.canvas.parentElement ?? this.canvas;
    const bounds = parent.getBoundingClientRect();
    this.ratio = window.devicePixelRatio || 1;
    this.width = Math.max(100, Math.floor(bounds.width));
    this.height = Math.max(160, Math.floor(bounds.height));
    this.canvas.width = Math.round(this.width * this.ratio);
    this.canvas.height = Math.round(this.height * this.ratio);
    this.canvas.style.width = `${this.width}px`;
    this.canvas.style.height = `${this.height}px`;
    this.store.viewportWidth = this.width;
    // A fitted view stays fitted as the window changes size.
    if (this.store.fitted) this.store.zoomToFit();
    this.store.redraw();
  }

  private get sampleRate(): number {
    return this.store.sampleRate;
  }

  private xOf(frame: number): number {
    const view = this.store.view;
    return (frame / this.sampleRate - view.scroll) * view.pxPerSecond;
  }

  private frameAt(x: number): number {
    const view = this.store.view;
    return Math.round((view.scroll + x / view.pxPerSecond) * this.sampleRate);
  }

  private local(event: PointerEvent | WheelEvent): { x: number; y: number } {
    const bounds = this.canvas.getBoundingClientRect();
    return { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
  }

  private hitEdge(x: number, y: number): EdgeHit | null {
    const row = this.rowAt(y);
    if (row !== 'audio' && row !== 'video') return null;
    const layout = this.store.layout;
    const items: { target: 'clip' | 'pause'; index: number; outStart: number; outEnd: number }[] = [
      ...layout.clips.map((clip) => ({ target: 'clip' as const, index: clip.segment, outStart: clip.outStart, outEnd: clip.outEnd })),
      ...(row === 'audio'
        ? layout.pauses.map((pause) => ({ target: 'pause' as const, index: pause.gap, outStart: pause.outStart, outEnd: pause.outEnd }))
        : []),
    ];
    let best: EdgeHit | null = null;
    let distance = EDGE_HIT_PX;
    for (const item of items) {
      const left = this.xOf(item.outStart);
      const right = this.xOf(item.outEnd);
      if (right < -EDGE_HIT_PX || left > this.width + EDGE_HIT_PX || right - left < MIN_GRAB_WIDTH_PX) continue;
      // The half of a block nearer an edge decides which edge you grab, so at
      // a shared boundary the block the pointer is inside wins.
      const leftDistance = Math.abs(x - left) + (x < left ? 0.5 : 0);
      const rightDistance = Math.abs(x - right) + (x > right ? 0.5 : 0);
      if (leftDistance < distance && x <= (left + right) / 2 + 0.5) {
        distance = leftDistance;
        best = { target: item.target, index: item.index, edge: 'start' };
      }
      if (rightDistance < distance && x >= (left + right) / 2 - 0.5) {
        distance = rightDistance;
        best = { target: item.target, index: item.index, edge: 'end' };
      }
    }
    return best;
  }

  /** The clip or pause under `x` in the sound lane. */
  private itemAt(x: number): { clip: string | null; pause: string | null } {
    const store = this.store;
    const frame = this.frameAt(x);
    const pause = store.layout.pauses.find((item) => frame >= item.outStart && frame < item.outEnd);
    if (pause) return { clip: null, pause: pause.id };
    const index = clipAt(store.layout, frame);
    const clip = index >= 0 ? store.segments[store.layout.clips[index]!.segment] : undefined;
    return { clip: clip?.id ?? null, pause: null };
  }

  // -- pointer ------------------------------------------------------------------------
  private onPointerDown = (event: PointerEvent): void => {
    if (event.button !== 0) return;
    const { x, y } = this.local(event);
    const row = this.rowAt(y);
    this.canvas.setPointerCapture(event.pointerId);
    const store = this.store;
    if (row === 'audio' || row === 'video') {
      const hit = this.hitEdge(x, y);
      if (hit) {
        this.beginTrim(hit, x, event.altKey);
        return;
      }
      const under = this.itemAt(x);
      const id = under.clip ?? (row === 'audio' ? under.pause : null);
      if (id && (event.shiftKey || event.metaKey || event.ctrlKey)) {
        store.select(id, event.shiftKey ? 'range' : 'toggle');
        return;
      }
      if (under.clip) {
        const narrow = store.selection.has(under.clip) && store.selection.size > 1;
        if (!store.selection.has(under.clip)) store.select(under.clip);
        const resume = store.playing;
        if (resume) store.pause();
        this.scrubTo(x);
        this.drag = {
          kind: 'move',
          id: under.clip,
          startX: x,
          pointerX: x,
          altKey: event.altKey,
          moving: false,
          boundary: null,
          narrow,
          resume,
        };
        return;
      }
      if (id) store.select(id);
      else store.select(null);
    }
    const resume = store.playing;
    if (resume) store.pause();
    this.drag = { kind: 'scrub', row, resume };
    this.scrubTo(x);
  };

  private onPointerMove = (event: PointerEvent): void => {
    const { x, y } = this.local(event);
    const drag = this.drag;
    if (drag?.kind === 'trim') {
      drag.pointerX = x;
      drag.altKey = event.altKey;
      this.updateTrim();
      return;
    }
    if (drag?.kind === 'scrub') {
      this.scrubTo(x);
      return;
    }
    if (drag?.kind === 'move') {
      drag.pointerX = x;
      drag.altKey = event.altKey;
      if (!drag.moving && Math.abs(x - drag.startX) >= MOVE_THRESHOLD_PX) drag.moving = true;
      if (drag.moving) {
        drag.boundary = this.boundaryAt(x);
        this.canvas.style.cursor = drag.altKey ? 'copy' : 'grabbing';
        this.store.redraw();
      }
      return;
    }
    const hit = this.hitEdge(x, y);
    const row = this.rowAt(y);
    const onClip = !hit && (row === 'audio' || row === 'video') && this.itemAt(x).clip !== null;
    this.canvas.style.cursor = hit ? 'ew-resize' : onClip ? 'grab' : row === 'ruler' || row === 'overview' ? 'pointer' : 'default';
    if (hit?.target !== this.hover?.target || hit?.index !== this.hover?.index || hit?.edge !== this.hover?.edge) {
      this.hover = hit;
      this.store.redraw();
    }
  };

  private onPointerUp = (event: PointerEvent): void => {
    const drag = this.drag;
    this.drag = null;
    if (this.canvas.hasPointerCapture(event.pointerId)) this.canvas.releasePointerCapture(event.pointerId);
    if (drag?.kind === 'trim') {
      if (drag.moved) {
        this.store.commit(this.store.timeline);
        const scroll = this.store.view.scroll;
        if (scroll < 0) this.store.setView({ scroll: 0 });
      } else {
        this.store.setDraft(null);
      }
    } else if (drag?.kind === 'scrub' && drag.resume) {
      void this.store.play();
    } else if (drag?.kind === 'move') {
      this.canvas.style.cursor = 'grab';
      if (drag.moving && drag.boundary !== null) this.store.dropSelection(drag.boundary, drag.altKey);
      else if (drag.narrow) this.store.select(drag.id);
      if (drag.resume) void this.store.play();
    }
    this.store.redraw();
  };

  /** The clip boundary whose cut is nearest `x` (0: before the first clip). */
  private boundaryAt(x: number): number {
    const layout = this.store.layout;
    const count = this.store.timeline.clips.length;
    const starts = new Map(layout.clips.map((clip) => [clip.segment, clip.outStart]));
    const frame = this.frameAt(x);
    let best = count;
    let distance = Math.abs(layout.total - frame);
    for (let boundary = 0; boundary < count; boundary += 1) {
      const at = starts.get(boundary);
      if (at === undefined) continue;
      if (Math.abs(at - frame) < distance) {
        distance = Math.abs(at - frame);
        best = boundary;
      }
    }
    return best;
  }

  /** Output frame where clips dropped at `boundary` would start. */
  private boundaryFrame(boundary: number): number {
    const layout = this.store.layout;
    return layout.clips.find((clip) => clip.segment === boundary)?.outStart ?? layout.total;
  }

  private onContextMenu = (event: MouseEvent): void => {
    event.preventDefault();
    const bounds = this.canvas.getBoundingClientRect();
    const x = event.clientX - bounds.left;
    const row = this.rowAt(event.clientY - bounds.top);
    if (row === 'overview') return;
    const store = this.store;
    if (store.playing) store.pause();
    const frame = Math.max(0, Math.min(store.layout.total, this.frameAt(x)));
    store.setPlayhead(frame);
    const under = row === 'audio' || row === 'video' ? this.itemAt(x) : { clip: null, pause: null };
    const id = under.clip ?? under.pause;
    if (id && !store.selection.has(id)) store.select(id);
    this.onMenu?.({ clientX: event.clientX, clientY: event.clientY, frame, ...under });
  };

  private onPointerLeave = (): void => {
    if (!this.drag && this.hover) {
      this.hover = null;
      this.store.redraw();
    }
  };

  private onWheel = (event: WheelEvent): void => {
    event.preventDefault();
    const store = this.store;
    // A pinch WebKit reports as gesture events too is zoomed there.
    if (this.gestureScale !== null && event.ctrlKey) return;
    if (event.ctrlKey || event.metaKey) {
      const { x } = this.local(event);
      store.zoom(Math.exp(-event.deltaY * 0.01), x);
      return;
    }
    const pixels = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
    const lines = event.deltaMode === 1 ? 16 : 1;
    store.setView({ scroll: store.clampScroll(store.view.scroll + (pixels * lines) / store.view.pxPerSecond) });
  };

  /**
   * Trackpad pinch in Safari and the desktop app (WKWebView): WebKit reports
   * it as `gesturestart/change/end` with a cumulative `scale` rather than
   * the ctrl+wheel events Chrome sends.
   */
  private onGesture = (event: Event): void => {
    event.preventDefault();
    if (event.type === 'gestureend') {
      this.gestureScale = null;
      return;
    }
    const gesture = event as Event & { scale: number; clientX: number };
    if (event.type === 'gesturestart' || this.gestureScale === null) {
      this.gestureScale = gesture.scale || 1;
      return;
    }
    const factor = gesture.scale / this.gestureScale;
    this.gestureScale = gesture.scale;
    if (Number.isFinite(factor) && factor > 0) {
      this.store.zoom(factor, gesture.clientX - this.canvas.getBoundingClientRect().left);
    }
  };

  private scrubTo(x: number): void {
    const store = this.store;
    const drag = this.drag;
    if (drag?.kind === 'scrub' && drag.row === 'overview') {
      const sample = Math.round((Math.max(0, Math.min(this.width, x)) / this.width) * store.sourceFrames);
      const frame = this.outputNear(sample);
      if (frame !== null) {
        store.setPlayhead(frame);
        store.reveal(frame);
      }
      return;
    }
    store.setPlayhead(Math.max(0, Math.min(store.layout.total, this.frameAt(x))));
  }

  /** Output frame of a source sample, or of the nearest kept material after it. */
  private outputNear(sample: number): number | null {
    let fallback: number | null = null;
    for (const clip of this.store.layout.clips) {
      const length = clip.outEnd - clip.outStart;
      if (sample >= clip.sourceAtStart && sample < clip.sourceAtStart + length) {
        return clip.outStart + (sample - clip.sourceAtStart);
      }
      if (fallback === null && clip.sourceAtStart > sample) fallback = clip.outStart;
    }
    return fallback ?? this.store.layout.total;
  }

  // -- trimming -------------------------------------------------------------------------
  private beginTrim(hit: EdgeHit, x: number, altKey: boolean): void {
    const store = this.store;
    if (store.playing) store.pause();
    const timeline = store.timeline;
    let id: string;
    let originalValue: number;
    let anchor: number;
    if (hit.target === 'clip') {
      const segment = timeline.clips[hit.index]!;
      id = segment.id;
      originalValue = hit.edge === 'start' ? segment.start : segment.end;
      anchor = store.layout.clips.find((clip) => clip.segment === hit.index)!.outEnd;
    } else {
      const pause = timeline.gaps[hit.index]!;
      id = pause.id;
      originalValue = pause.length;
      anchor = store.layout.pauses.find((item) => item.gap === hit.index)!.outEnd;
    }
    this.drag = {
      kind: 'trim',
      target: hit.target,
      index: hit.index,
      edge: hit.edge,
      original: timeline,
      originalValue,
      startX: x,
      startScroll: store.view.scroll,
      anchor,
      autoScroll: 0,
      pointerX: x,
      altKey,
      targets: hit.target === 'clip' ? snapTargets(hit.edge, store.context) : [],
      snapped: null,
      moved: false,
    };
    store.select(id);
  }

  private updateTrim(): void {
    const drag = this.drag;
    if (drag?.kind !== 'trim') return;
    const store = this.store;
    const pxPerSecond = store.view.pxPerSecond;
    const seconds = (drag.pointerX - drag.startX) / pxPerSecond + drag.autoScroll;
    const change = Math.round(seconds * this.sampleRate);
    drag.snapped = null;
    let next: Timeline;
    if (drag.target === 'clip') {
      let target = drag.originalValue + change;
      if (store.snapping && !drag.altKey) {
        const tolerance = (SNAP_PX / pxPerSecond) * this.sampleRate;
        const [low, high] = edgeLimits(drag.original.clips, drag.index, drag.edge, store.context);
        const candidates = [...drag.targets.filter((value) => value >= low && value <= high), low, high];
        if (drag.originalValue >= low && drag.originalValue <= high) candidates.push(drag.originalValue);
        candidates.sort((a, b) => a - b);
        const snapped = nearestTarget(candidates, target, tolerance);
        if (snapped !== null) {
          target = snapped;
          drag.snapped = snapped;
        }
      }
      next = trimClip(drag.original, drag.index, drag.edge, target, store.context);
    } else {
      // Dragging a pause's start to the left lengthens it, like its end to the right.
      const length = drag.edge === 'end' ? drag.originalValue + change : drag.originalValue - change;
      next = resizePause(drag.original, drag.index, length, this.sampleRate);
    }
    drag.moved = drag.moved || Math.abs(drag.pointerX - drag.startX) > 1 || drag.autoScroll !== 0;
    store.setDraft(next);
    let scroll = drag.startScroll + drag.autoScroll;
    if (drag.edge === 'start') {
      // Keep the item's end (and everything after it) still on screen.
      const end =
        drag.target === 'clip'
          ? store.layout.clips.find((item) => item.segment === drag.index)?.outEnd
          : store.layout.pauses.find((item) => item.gap === drag.index)?.outEnd;
      if (end !== undefined) scroll += (end - drag.anchor) / this.sampleRate;
    }
    store.view = { ...store.view, scroll };
    store.redraw();
  }

  /** Whether the dragged edge has reached the end of its range in `direction`. */
  private atLimit(drag: TrimDrag, direction: number): boolean {
    const timeline = this.store.timeline;
    if (drag.target === 'clip') {
      const segment = timeline.clips[drag.index]!;
      const at = drag.edge === 'start' ? segment.start : segment.end;
      const [low, high] = edgeLimits(drag.original.clips, drag.index, drag.edge, this.store.context);
      return (direction < 0 && at <= low) || (direction > 0 && at >= high);
    }
    const length = timeline.gaps[drag.index]?.length ?? 0;
    const [low, high] = pauseLimits(this.sampleRate);
    const growing = drag.edge === 'end' ? direction > 0 : direction < 0;
    return growing ? length >= high : length <= low;
  }

  /** Auto-scroll while an edge or clips are dragged near the side of the timeline. */
  private autoScroll(): void {
    const drag = this.drag;
    if (drag?.kind === 'move' && drag.moving) {
      const x = drag.pointerX;
      let speed = 0;
      if (x < AUTOSCROLL_ZONE) speed = -(AUTOSCROLL_ZONE - x) / AUTOSCROLL_ZONE;
      else if (x > this.width - AUTOSCROLL_ZONE) speed = (x - (this.width - AUTOSCROLL_ZONE)) / AUTOSCROLL_ZONE;
      if (speed === 0) return;
      const store = this.store;
      const scroll = store.clampScroll(store.view.scroll + (Math.max(-1, Math.min(1, speed)) * 14) / store.view.pxPerSecond);
      if (scroll === store.view.scroll) return;
      store.view = { ...store.view, scroll };
      drag.boundary = this.boundaryAt(x);
      store.redraw();
      return;
    }
    if (drag?.kind !== 'trim') return;
    const x = drag.pointerX;
    let speed = 0;
    if (x < AUTOSCROLL_ZONE) speed = -(AUTOSCROLL_ZONE - x) / AUTOSCROLL_ZONE;
    else if (x > this.width - AUTOSCROLL_ZONE) speed = (x - (this.width - AUTOSCROLL_ZONE)) / AUTOSCROLL_ZONE;
    if (speed === 0 || this.atLimit(drag, speed)) return;
    drag.autoScroll += (Math.max(-1, Math.min(1, speed)) * 14) / this.store.view.pxPerSecond;
    this.updateTrim();
  }

  // -- drawing ---------------------------------------------------------------------------
  private tick(frame: number, playing: boolean): void {
    this.autoScroll();
    if (playing && !this.drag) {
      const view = this.store.view;
      const x = this.xOf(frame);
      if (x > this.width * 0.92 || x < 0) {
        this.store.view = { ...view, scroll: this.store.clampScroll(frame / this.sampleRate - (this.width * 0.08) / view.pxPerSecond) };
      }
    }
    this.draw(frame);
  }

  private draw(frame: number): void {
    const context = this.context;
    context.setTransform(this.ratio, 0, 0, this.ratio, 0, 0);
    context.fillStyle = this.colors.background;
    context.fillRect(0, 0, this.width, this.height);
    const layout = this.store.layout;
    this.drawOverview(layout, frame);
    this.drawRuler();
    const rows = this.rows();
    context.fillStyle = this.colors.laneBackground;
    if (this.video) context.fillRect(0, rows.videoTop, this.width, VIDEO_HEIGHT);
    context.fillRect(0, rows.audioTop, this.width, rows.audioHeight);
    this.drawPauses(layout, rows.audioTop, rows.audioHeight);
    layout.clips.forEach((clip, index) => this.drawClip(clip, index, rows));
    this.drawThroughEdits(layout, rows);
    this.drawEdgeFeedback(layout, rows);
    this.drawMove(layout, rows);
    this.drawPlayhead(frame);
  }

  /** Clips being moved show faded, and a bar marks the cut they would drop at. */
  private drawMove(layout: Layout, rows: ReturnType<TimelineView['rows']>): void {
    const drag = this.drag;
    if (drag?.kind !== 'move' || !drag.moving || drag.boundary === null) return;
    const context = this.context;
    const top = this.video ? rows.videoTop : rows.audioTop;
    const bottom = rows.audioTop + rows.audioHeight;
    const selection = this.store.selection;
    const clips = this.store.timeline.clips;
    if (!drag.altKey) {
      context.fillStyle = this.colors.laneBackground;
      context.globalAlpha = 0.6;
      for (const clip of layout.clips) {
        if (!selection.has(clips[clip.segment]!.id)) continue;
        const left = this.xOf(clip.outStart);
        context.fillRect(left, top, this.xOf(clip.outEnd) - left, bottom - top);
      }
      context.globalAlpha = 1;
    }
    const x = Math.round(this.xOf(this.boundaryFrame(drag.boundary)));
    context.fillStyle = this.colors.selection;
    context.fillRect(x - 1, top - 4, 3, bottom - top + 8);
    context.fillRect(x - 5, top - 4, 11, 3);
    context.fillRect(x - 5, bottom + 1, 11, 3);
  }

  private drawOverview(layout: Layout, frame: number): void {
    const context = this.context;
    const width = this.width;
    const frames = this.store.sourceFrames;
    context.fillStyle = this.colors.overview;
    context.fillRect(0, 0, width, OVERVIEW_HEIGHT);
    const middle = OVERVIEW_HEIGHT / 2;
    const peaks = this.peaks;
    if (peaks) {
      context.fillStyle = this.colors.overviewWave;
      const gain = this.gain();
      for (let x = 0; x < width; x += 1) {
        if (!peaks.range((x / width) * frames, ((x + 1) / width) * frames, this.range)) continue;
        const top = Math.max(-1, Math.min(1, this.range[1]! * gain));
        const bottom = Math.max(-1, Math.min(1, this.range[0]! * gain));
        context.fillRect(x, middle - top * (middle - 2), 1, Math.max(1, (top - bottom) * (middle - 2)));
      }
    }
    const selection = this.store.selection;
    this.store.segments.forEach((segment) => {
      context.fillStyle = selection.has(segment.id) ? this.colors.overviewSelected : this.colors.overviewKept;
      const left = (segment.start / frames) * width;
      context.fillRect(left, 0, Math.max(1, (segment.end / frames) * width - left), OVERVIEW_HEIGHT);
    });
    // What the timeline currently shows, mapped back onto the recording.
    const view = this.store.view;
    const first = view.scroll * this.sampleRate;
    const last = (view.scroll + width / view.pxPerSecond) * this.sampleRate;
    context.strokeStyle = this.colors.overviewWindow;
    context.lineWidth = 1;
    for (const clip of layout.clips) {
      const from = Math.max(first, clip.outStart);
      const to = Math.min(last, clip.outEnd);
      if (to <= from) continue;
      const left = ((clip.sourceAtStart + from - clip.outStart) / frames) * width;
      const right = ((clip.sourceAtStart + to - clip.outStart) / frames) * width;
      context.strokeRect(left + 0.5, 0.5, Math.max(1, right - left - 1), OVERVIEW_HEIGHT - 1);
    }
    const sample = sourceAt(layout, Math.min(frame, layout.total - 1));
    if (sample !== null) {
      context.fillStyle = this.colors.playhead;
      context.fillRect(Math.round((sample / frames) * width) - 1, 0, 2, OVERVIEW_HEIGHT);
    }
  }

  private drawRuler(): void {
    const context = this.context;
    const top = OVERVIEW_HEIGHT + GAP;
    const view = this.store.view;
    context.fillStyle = this.colors.ruler;
    context.fillRect(0, top, this.width, RULER_HEIGHT);
    const steps = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
    const step = steps.find((value) => value * view.pxPerSecond >= 90) ?? 3600;
    const minor = step / (String(step).startsWith('2') ? 4 : 5);
    const start = Math.floor(view.scroll / minor) * minor;
    const end = view.scroll + this.width / view.pxPerSecond;
    context.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace';
    context.textBaseline = 'top';
    const digits = step < 0.01 ? 3 : step < 0.1 ? 2 : step < 1 ? 1 : 0;
    for (let index = 0; ; index += 1) {
      const time = start + index * minor;
      if (time > end) break;
      if (time < 0) continue;
      const x = Math.round((time - view.scroll) * view.pxPerSecond) + 0.5;
      const major = Math.abs(time / step - Math.round(time / step)) < 1e-6;
      context.fillStyle = this.colors.rulerTick;
      context.fillRect(x, top + (major ? 10 : 16), 1, major ? RULER_HEIGHT - 10 : RULER_HEIGHT - 16);
      if (major) {
        context.fillStyle = this.colors.rulerText;
        context.fillText(timecode(time, digits), x + 3, top + 3);
      }
    }
  }

  private gain(): number {
    const peak = this.peaks?.peak ?? 1;
    return Math.min(8, 0.95 / Math.max(peak, 0.02));
  }

  private drawPauses(layout: Layout, top: number, height: number): void {
    const context = this.context;
    const selection = this.store.selection;
    for (const pause of layout.pauses) {
      const left = this.xOf(pause.outStart);
      const right = this.xOf(pause.outEnd);
      if (right < 0 || left > this.width) continue;
      const selected = selection.has(pause.id);
      const inset = right - left > 3 ? 0.5 : 0;
      const x = left + inset;
      const width = right - left - inset * 2;
      context.fillStyle = selected ? this.colors.pauseSelected : this.colors.pause;
      roundRect(context, x, top + 1, width, height - 2, 4);
      context.fill();
      context.save();
      context.beginPath();
      context.rect(x, top + 1, width, height - 2);
      context.clip();
      context.strokeStyle = pause.join === null ? this.colors.pauseHatchAdded : this.colors.pauseHatch;
      context.lineWidth = 1;
      for (let hatch = left - height; hatch < right; hatch += 7) {
        context.beginPath();
        context.moveTo(hatch, top + height);
        context.lineTo(hatch + height, top);
        context.stroke();
      }
      if (right - left > 34) {
        const seconds = (pause.outEnd - pause.outStart) / this.sampleRate;
        context.fillStyle = selected ? this.colors.title : this.colors.pauseText;
        context.font = '600 10px system-ui, sans-serif';
        context.textBaseline = 'top';
        context.fillText(right - left > 76 ? `pause ${seconds.toFixed(2)} s` : `${seconds.toFixed(1)} s`, x + 5, top + 4);
      }
      context.restore();
      if (selected) this.outline(x, top + 1, width, height - 2);
    }
  }

  private drawClip(
    clip: ClipView,
    index: number,
    rows: ReturnType<TimelineView['rows']>,
  ): void {
    const left = this.xOf(clip.outStart);
    const right = this.xOf(clip.outEnd);
    if (right < 0 || left > this.width) return;
    const context = this.context;
    const segment = this.store.segments[clip.segment]!;
    const selected = this.store.selection.has(segment.id);
    const x0 = Math.max(left, -4);
    const x1 = Math.min(right, this.width + 4);
    const inset = right - left > 3 ? 0.5 : 0;

    if (this.video) {
      context.fillStyle = selected ? this.colors.videoSelected : this.colors.video;
      roundRect(context, x0 + inset, rows.videoTop + 1, x1 - x0 - inset * 2, VIDEO_HEIGHT - 2, 4);
      context.fill();
      this.drawFilmstrip(clip, x0 + inset, x1 - inset, rows.videoTop + 1, VIDEO_HEIGHT - 2);
      if (selected) this.outline(x0 + inset, rows.videoTop + 1, x1 - x0 - inset * 2, VIDEO_HEIGHT - 2);
    }

    const top = rows.audioTop + 1;
    const height = rows.audioHeight - 2;
    context.fillStyle = selected ? this.colors.clipSelected : this.colors.clip;
    roundRect(context, x0 + inset, top, x1 - x0 - inset * 2, height, 4);
    context.fill();
    context.save();
    context.beginPath();
    context.rect(x0 + inset, top, x1 - x0 - inset * 2, height);
    context.clip();
    this.drawWaveform(clip, Math.max(0, left), Math.min(this.width, right), top + TITLE_ROW, height - TITLE_ROW - WORD_ROW);
    this.drawWords(clip, left, right, top + height - WORD_ROW + 3);
    if (right - left > 36) {
      context.fillStyle = this.colors.title;
      context.font = '600 10px system-ui, sans-serif';
      context.textBaseline = 'top';
      const seconds = (clip.outEnd - clip.outStart) / this.sampleRate;
      const label = right - left > 110 ? `${index + 1}  ·  ${formatDuration(seconds)}` : `${index + 1}`;
      context.fillText(label, Math.max(left, 0) + 6, top + 3);
    }
    context.restore();
    if (selected) this.outline(x0 + inset, top, x1 - x0 - inset * 2, height);
  }

  private outline(x: number, y: number, width: number, height: number): void {
    const context = this.context;
    context.strokeStyle = this.colors.selection;
    context.lineWidth = 2;
    roundRect(context, x + 1, y + 1, width - 2, height - 2, 4);
    context.stroke();
  }

  private drawWaveform(clip: ClipView, left: number, right: number, top: number, height: number): void {
    const peaks = this.peaks;
    if (!peaks || height < 8) return;
    const context = this.context;
    const ratio = this.ratio;
    const middle = top + height / 2;
    const half = height / 2 - 1;
    const gain = this.gain();
    const samplesPerPixel = this.sampleRate / this.store.view.pxPerSecond / ratio;
    const kept = this.store.proposal?.segments ?? null;
    let keptIndex = 0;
    const firstPixel = Math.floor(left * ratio);
    const lastPixel = Math.ceil(right * ratio);
    const restored: number[] = [];
    context.fillStyle = this.colors.wave;
    for (let pixel = firstPixel; pixel < lastPixel; pixel += 1) {
      const frame = (this.store.view.scroll + pixel / ratio / this.store.view.pxPerSecond) * this.sampleRate;
      const from = clip.sourceAtStart + (frame - clip.outStart);
      if (!peaks.range(Math.floor(from), Math.floor(from + samplesPerPixel), this.range)) continue;
      const high = Math.min(1, this.range[1]! * gain);
      const low = Math.max(-1, this.range[0]! * gain);
      const y = middle - high * half;
      const h = Math.max(1 / ratio, (high - low) * half);
      let isRestored = false;
      if (kept) {
        while (keptIndex < kept.length && kept[keptIndex]!.end <= from) keptIndex += 1;
        const interval = kept[keptIndex];
        isRestored = !interval || from < interval.start;
      }
      if (isRestored) restored.push(pixel / ratio, y, h);
      else context.fillRect(pixel / ratio, y, 1 / ratio, h);
    }
    if (restored.length) {
      context.fillStyle = this.colors.bodyRestored;
      for (let index = 0; index < restored.length; index += 3) {
        context.fillRect(restored[index]!, top - TITLE_ROW, 1 / ratio, height + TITLE_ROW + WORD_ROW);
      }
      context.fillStyle = this.colors.waveRestored;
      for (let index = 0; index < restored.length; index += 3) {
        context.fillRect(restored[index]!, restored[index + 1]!, 1 / ratio, restored[index + 2]!);
      }
    }
  }

  private drawWords(clip: ClipView, left: number, right: number, baseline: number): void {
    const words = this.store.proposal?.words;
    if (!words?.length) return;
    const context = this.context;
    const view = this.store.view;
    const from = clip.sourceAtStart;
    const to = clip.sourceAtStart + (clip.outEnd - clip.outStart);
    let index = wordsBetween(words, from, to)[0];
    context.font = '11px system-ui, sans-serif';
    context.textBaseline = 'top';
    let lastEnd = -Infinity;
    for (; index < words.length; index += 1) {
      const word = words[index]!;
      if (word.start >= to) break;
      if (word.end <= from) continue;
      const x = Math.max(left, this.xOf(clip.outStart + Math.max(0, word.start - from))) + 2;
      if (x > this.width) break;
      const width = this.measure(word.text);
      const span = ((Math.min(word.end, to) - Math.max(word.start, from)) / this.sampleRate) * view.pxPerSecond;
      if (x < lastEnd + 4 || x + width > right + 2 || (span < 8 && width > span * 3)) continue;
      if (x + width < 0) continue;
      context.fillStyle = word.kept ? this.colors.word : this.colors.wordRemoved;
      context.fillText(word.text, x, baseline);
      lastEnd = x + width;
    }
  }

  private measure(text: string): number {
    let width = this.textWidths.get(text);
    if (width === undefined) {
      width = this.context.measureText(text).width;
      this.textWidths.set(text, width);
    }
    return width;
  }

  private sheet(strip: Filmstrip, index: number): HTMLImageElement | null {
    let image = this.sheets.get(index);
    if (!image) {
      image = new Image();
      image.decoding = 'async';
      image.onload = () => this.store.redraw();
      image.src = strip.url.replace('{sheet}', String(index));
      this.sheets.set(index, image);
    }
    return image.complete && image.naturalWidth > 0 ? image : null;
  }

  private drawFilmstrip(clip: ClipView, left: number, right: number, top: number, height: number): void {
    const video = this.store.project.media.video;
    const strip = video?.filmstrip;
    if (!video || !strip) return;
    const context = this.context;
    const width = (height * strip.thumb_width) / strip.thumb_height;
    const perSheet = strip.columns * strip.rows;
    context.save();
    context.beginPath();
    context.rect(left, top, right - left, height);
    context.clip();
    const start = this.xOf(clip.outStart);
    const firstSlot = Math.max(0, Math.floor((Math.max(left, 0) - start) / width));
    for (let slot = firstSlot; ; slot += 1) {
      const x = start + slot * width;
      if (x > Math.min(right, this.width)) break;
      const frame = clip.outStart + ((slot * width) / this.store.view.pxPerSecond) * this.sampleRate;
      const time = (clip.sourceAtStart + (frame - clip.outStart)) / this.sampleRate + video.offset;
      const thumb = Math.max(0, Math.min(strip.count - 1, Math.round(time / strip.interval)));
      const image = this.sheet(strip, Math.floor(thumb / perSheet));
      if (!image) continue;
      const cell = thumb % perSheet;
      context.drawImage(
        image,
        (cell % strip.columns) * strip.thumb_width,
        Math.floor(cell / strip.columns) * strip.thumb_height,
        strip.thumb_width,
        strip.thumb_height,
        x,
        top,
        width,
        height,
      );
    }
    context.restore();
  }

  private drawThroughEdits(layout: Layout, rows: ReturnType<TimelineView['rows']>): void {
    const context = this.context;
    const timeline = this.store.timeline;
    context.strokeStyle = this.colors.throughEdit;
    context.setLineDash([3, 3]);
    context.lineWidth = 1;
    for (const clip of layout.clips) {
      if (!isThroughEdit(timeline, clip.segment)) continue;
      const x = Math.round(this.xOf(clip.outEnd)) + 0.5;
      if (x < 0 || x > this.width) continue;
      context.beginPath();
      context.moveTo(x, rows.audioTop + 2);
      context.lineTo(x, rows.audioTop + rows.audioHeight - 2);
      context.stroke();
    }
    context.setLineDash([]);
  }

  private drawEdgeFeedback(layout: Layout, rows: ReturnType<TimelineView['rows']>): void {
    const context = this.context;
    const drag = this.drag?.kind === 'trim' ? this.drag : null;
    const target = drag ?? (this.hover && !this.drag ? this.hover : null);
    if (!target) return;
    const item =
      target.target === 'clip'
        ? layout.clips.find((clip) => clip.segment === target.index)
        : layout.pauses.find((pause) => pause.gap === target.index);
    if (!item) return;
    const x = this.xOf(target.edge === 'start' ? item.outStart : item.outEnd);
    const top = this.video && target.target === 'clip' ? rows.videoTop : rows.audioTop;
    const bottom = rows.audioTop + rows.audioHeight;
    const color = drag?.snapped !== null && drag?.snapped !== undefined ? this.colors.snapped : this.colors.edge;
    context.fillStyle = color;
    const inward = target.edge === 'start' ? 1 : -1;
    const barX = target.edge === 'start' ? x : x - 3;
    context.fillRect(barX, top, 3, bottom - top);
    context.fillRect(Math.min(x, x + inward * 9), top, 9, 3);
    context.fillRect(Math.min(x, x + inward * 9), bottom - 3, 9, 3);
    if (!drag) return;
    context.fillRect(Math.round(x), OVERVIEW_HEIGHT + GAP, 1, RULER_HEIGHT);
    let text: string;
    if (drag.target === 'clip') {
      const segment = this.store.segments[drag.index]!;
      const now = drag.edge === 'start' ? segment.start : segment.end;
      const change = drag.edge === 'start' ? drag.originalValue - now : now - drag.originalValue;
      const length = (segment.end - segment.start) / this.sampleRate;
      text = `${formatDelta(change / this.sampleRate)}   clip ${length.toFixed(3)} s${drag.snapped !== null ? '   ⟂ snapped' : ''}`;
    } else {
      const length = this.store.timeline.gaps[drag.index]?.length ?? drag.originalValue;
      text = `${formatDelta((length - drag.originalValue) / this.sampleRate)}   pause ${(length / this.sampleRate).toFixed(3)} s`;
    }
    context.font = '600 11px system-ui, sans-serif';
    const width = context.measureText(text).width + 14;
    const boxX = Math.max(4, Math.min(this.width - width - 4, x - width / 2));
    const boxY = rows.audioTop + 4;
    context.fillStyle = this.colors.label;
    roundRect(context, boxX, boxY, width, 20, 5);
    context.fill();
    context.fillStyle = color;
    context.textBaseline = 'middle';
    context.fillText(text, boxX + 7, boxY + 10);
  }

  private drawPlayhead(frame: number): void {
    const x = Math.round(this.xOf(frame));
    if (x < -10 || x > this.width + 10) return;
    const context = this.context;
    const top = OVERVIEW_HEIGHT + GAP;
    context.fillStyle = this.colors.playhead;
    context.fillRect(x - 0.5, top, 1.5, this.height - top);
    context.beginPath();
    context.moveTo(x - 6, top);
    context.lineTo(x + 6.5, top);
    context.lineTo(x + 0.25, top + 8);
    context.closePath();
    context.fill();
  }
}

function roundRect(context: CanvasRenderingContext2D, x: number, y: number, width: number, height: number, radius: number): void {
  context.beginPath();
  const w = Math.max(0, width);
  const h = Math.max(0, height);
  context.roundRect(x, y, w, h, Math.max(0, Math.min(radius, w / 2, h / 2)));
}
