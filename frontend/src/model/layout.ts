/**
 * Output layout of a timeline: where every clip and pause lands in the edited
 * result, in output samples.
 *
 * This is a port of `derive_pieces` (src/voicecut/editor/plan.py) and of
 * `cut_render.place_pieces` / `_contiguous_trace`, the arithmetic the export
 * and the preview renderer use.  tests/layout.test.ts checks it against cases
 * computed by Python (tests/fixtures/layout_cases.json), so the timeline the
 * user sees is the file they get, sample for sample.
 */
import type { PauseRef, Proposal } from '../types';
import type { Timeline } from './timeline';

export const DECLICK_SECONDS = 0.002;
/** A pause at either end of the timeline fades in over 50 ms, out over 100 ms. */
export const LEAD_FADE_SECONDS = 0.05;
export const TAIL_FADE_SECONDS = 0.1;

export function declickSamples(sampleRate: number): number {
  return Math.round(DECLICK_SECONDS * sampleRate);
}

export interface Piece {
  kind: 'source' | 'room_tone';
  sourceStart: number;
  sourceEnd: number;
  fadeIn: number;
  fadeOut: number;
  /** Clip index of a source piece. */
  segment: number | null;
  /** Gap index of a pause. */
  gap: number | null;
}

export interface PlacedPiece extends Piece {
  outStart: number;
  outEnd: number;
  /** Fades as applied: the overlap with each neighbour (crossfade length). */
  placedFadeIn: number;
  placedFadeOut: number;
}

/** A clip as drawn: the output range it owns and the source it shows there. */
export interface ClipView {
  segment: number;
  outStart: number;
  outEnd: number;
  /** Source sample shown at `outStart`; source advances 1:1 with output. */
  sourceAtStart: number;
}

/** A pause as drawn: exactly its heard length, between the clips' crossfades. */
export interface PauseView {
  gap: number;
  id: string;
  join: PauseRef | null;
  outStart: number;
  outEnd: number;
}

export interface Layout {
  pieces: PlacedPiece[];
  total: number;
  clips: ClipView[];
  pauses: PauseView[];
}

interface Lookup {
  starts: Map<number, number>;
  ends: Map<number, number>;
}

const lookups = new WeakMap<Proposal, Lookup>();

function lookup(proposal: Proposal): Lookup {
  let value = lookups.get(proposal);
  if (!value) {
    value = {
      starts: new Map(proposal.segments.map((segment, index) => [segment.start, index])),
      ends: new Map(proposal.segments.map((segment, index) => [segment.end, index])),
    };
    lookups.set(proposal, value);
  }
  return value;
}

function edgeFade(
  edge: number,
  neighbour: number | null,
  origin: string,
  voicecutFade: number | null,
  declick: number,
  length: number,
  besidePause: boolean,
): number {
  if (!besidePause && (neighbour === edge || origin === 'source')) return 0;
  if (origin === 'voicecut' && voicecutFade !== null) return voicecutFade;
  return Math.min(declick, Math.floor(length / 2));
}

/** Port of `plan.derive_pieces` (room tone is chosen on the server). */
export function derivePieces(
  timeline: Timeline,
  proposal: Proposal | null,
  declick: number,
  sampleRate: number,
): Piece[] {
  const { clips, gaps } = timeline;
  const count = clips.length;
  const found = proposal ? lookup(proposal) : null;
  const fades = clips.map((clip, index): [number, number] => {
    const pauseBefore = gaps[index] != null;
    const pauseAfter = gaps[index + 1] != null;
    const previous = index > 0 && !pauseBefore ? clips[index - 1]! : null;
    const following = index + 1 < count && !pauseAfter ? clips[index + 1]! : null;
    const length = clip.end - clip.start;
    let voicecutIn: number | null = null;
    let voicecutOut: number | null = null;
    if (proposal && found) {
      const startAt = found.starts.get(clip.start);
      if (startAt !== undefined) voicecutIn = proposal.segments[startAt]!.fade_in;
      const endAt = found.ends.get(clip.end);
      if (endAt !== undefined) voicecutOut = proposal.segments[endAt]!.fade_out;
    }
    let fadeIn = edgeFade(clip.start, previous ? previous.end : null, clip.startOrigin, voicecutIn, declick, length, pauseBefore);
    let fadeOut = edgeFade(clip.end, following ? following.start : null, clip.endOrigin, voicecutOut, declick, length, pauseAfter);
    if (fadeIn + fadeOut > length) {
      fadeIn = Math.min(fadeIn, Math.floor(length / 2));
      fadeOut = Math.min(fadeOut, length - fadeIn);
    }
    return [fadeIn, fadeOut];
  });
  const pieces: Piece[] = [];
  for (let position = 0; position <= count; position += 1) {
    const gap = gaps[position];
    if (gap) {
      const afterClip = position > 0;
      const beforeClip = position < count;
      const fadeIn = afterClip ? fades[position - 1]![1] : Math.min(Math.trunc(LEAD_FADE_SECONDS * sampleRate), gap.length);
      const fadeOut = beforeClip ? fades[position]![0] : Math.min(Math.trunc(TAIL_FADE_SECONDS * sampleRate), gap.length);
      const length = gap.length + (afterClip ? fadeIn : 0) + (beforeClip ? fadeOut : 0);
      pieces.push({ kind: 'room_tone', sourceStart: 0, sourceEnd: length, fadeIn, fadeOut, segment: null, gap: position });
    }
    if (position < count) {
      const clip = clips[position]!;
      pieces.push({
        kind: 'source',
        sourceStart: clip.start,
        sourceEnd: clip.end,
        fadeIn: fades[position]![0],
        fadeOut: fades[position]![1],
        segment: position,
        gap: null,
      });
    }
  }
  return pieces;
}

/** Port of `cut_render.place_pieces`. */
export function placePieces(pieces: readonly Piece[]): PlacedPiece[] {
  const placed: PlacedPiece[] = [];
  let cursor = 0;
  pieces.forEach((piece, index) => {
    const overlapIn = index > 0 ? Math.min(pieces[index - 1]!.fadeOut, piece.fadeIn) : 0;
    cursor -= overlapIn;
    const fadeOut =
      index + 1 < pieces.length ? Math.min(piece.fadeOut, pieces[index + 1]!.fadeIn) : piece.fadeOut;
    const length = piece.sourceEnd - piece.sourceStart;
    placed.push({
      ...piece,
      outStart: cursor,
      outEnd: cursor + length,
      placedFadeIn: index > 0 ? overlapIn : piece.fadeIn,
      placedFadeOut: fadeOut,
    });
    cursor += length;
  });
  return placed;
}

/** Crossfades belong to the source side (the left one between two sources). */
function leftOwns(left: PlacedPiece, right: PlacedPiece): boolean {
  return left.kind === 'source' || right.kind !== 'source';
}

export function computeLayout(timeline: Timeline, proposal: Proposal | null, sampleRate: number): Layout {
  const placed = placePieces(derivePieces(timeline, proposal, declickSamples(sampleRate), sampleRate));
  const clips: ClipView[] = [];
  const pauses: PauseView[] = [];
  placed.forEach((piece, index) => {
    // Port of cut_render._contiguous_trace: disjoint display ranges.
    let start = piece.outStart;
    let end = piece.outEnd;
    if (index > 0 && leftOwns(placed[index - 1]!, piece)) start = placed[index - 1]!.outEnd;
    if (index + 1 < placed.length && !leftOwns(piece, placed[index + 1]!)) end = placed[index + 1]!.outStart;
    if (end <= start) return;
    if (piece.kind === 'source') {
      clips.push({
        segment: piece.segment!,
        outStart: start,
        outEnd: end,
        sourceAtStart: piece.sourceStart + (start - piece.outStart),
      });
    } else {
      const gap = timeline.gaps[piece.gap!]!;
      pauses.push({ gap: piece.gap!, id: gap.id, join: gap.join, outStart: start, outEnd: end });
    }
  });
  return {
    pieces: placed,
    total: placed.length ? placed[placed.length - 1]!.outEnd : 0,
    clips,
    pauses,
  };
}

/** Index (in `layout.pauses`) of the pause containing output sample `frame`. */
export function pauseAt(layout: Layout, frame: number): number {
  return layout.pauses.findIndex((pause) => frame >= pause.outStart && frame < pause.outEnd);
}

/** Index of the clip whose display range contains output sample `frame`. */
export function clipAt(layout: Layout, frame: number): number {
  const clips = layout.clips;
  let low = 0;
  let high = clips.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    const clip = clips[middle]!;
    if (frame < clip.outStart) high = middle - 1;
    else if (frame >= clip.outEnd) low = middle + 1;
    else return middle;
  }
  return -1;
}

/** Source sample heard at output sample `frame`, or null inside a pause. */
export function sourceAt(layout: Layout, frame: number): number | null {
  const index = clipAt(layout, frame);
  if (index < 0) return null;
  const clip = layout.clips[index]!;
  return clip.sourceAtStart + (frame - clip.outStart);
}

/** Output sample where source sample `sample` plays, or null if cut out. */
export function outputAt(layout: Layout, sample: number): number | null {
  for (const clip of layout.clips) {
    const offset = sample - clip.sourceAtStart;
    if (offset >= 0 && offset < clip.outEnd - clip.outStart) return clip.outStart + offset;
  }
  return null;
}

/** Output sample of each edit point (clip and pause boundaries), sorted. */
export function editPoints(layout: Layout): number[] {
  const points = new Set<number>([0, layout.total]);
  for (const clip of layout.clips) {
    points.add(clip.outStart);
    points.add(clip.outEnd);
  }
  return [...points].sort((a, b) => a - b);
}
