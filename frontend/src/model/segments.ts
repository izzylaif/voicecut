/**
 * The editable timeline: an ordered list of source intervals of the original
 * recording (canonical samples), in any order and as often as the user puts a
 * stretch in.  Every operation here is pure and returns a new array, so
 * undo/redo is just keeping old arrays.
 *
 * Edge provenance follows the backend (timeline.py): an edge is `voicecut`
 * when it sits exactly on a boundary of that kind VoiceCut chose, `source` at
 * the start/end of the recording, `manual` anywhere else.  Dragging an edge
 * back onto VoiceCut's sample therefore restores VoiceCut's fade and pause.
 */
import type { Origin, Proposal, WireClip } from '../types';

export interface Segment {
  id: string;
  start: number;
  end: number;
  startOrigin: Origin;
  endOrigin: Origin;
}

export type Edge = 'start' | 'end';

export interface EditContext {
  proposal: Proposal | null;
  sourceFrames: number;
  sampleRate: number;
}

/** Shortest clip an edit may leave (20 ms). */
export function minimumLength(sampleRate: number): number {
  return Math.round(0.02 * sampleRate);
}

let counter = 0;
export function newSegmentId(): string {
  counter += 1;
  return `m-${Date.now().toString(36)}-${counter.toString(36)}`;
}

export function fromWire(segment: WireClip): Segment {
  return {
    id: segment.id,
    start: segment.source_start_sample,
    end: segment.source_end_sample,
    startOrigin: segment.start_origin,
    endOrigin: segment.end_origin,
  };
}

export function toWire(segment: Segment): WireClip {
  return {
    kind: 'clip',
    id: segment.id,
    source_start_sample: segment.start,
    source_end_sample: segment.end,
    start_origin: segment.startOrigin,
    end_origin: segment.endOrigin,
  };
}

export function originFor(edge: Edge, sample: number, context: EditContext): Origin {
  const proposal = context.proposal;
  if (proposal?.segments.some((segment) => (edge === 'start' ? segment.start : segment.end) === sample)) {
    return 'voicecut';
  }
  if (edge === 'start' ? sample === 0 : sample === context.sourceFrames) return 'source';
  return 'manual';
}

/**
 * The range an edge of segment `index` may move in: anywhere in the recording,
 * except into the material of a neighbour that comes after it in the
 * recording too (dragging there would play those words twice).  A clip moved
 * away from its place, or a copy, extends freely.
 */
export function edgeLimits(
  segments: readonly Segment[],
  index: number,
  edge: Edge,
  context: EditContext,
): [number, number] {
  const segment = segments[index]!;
  const minimum = minimumLength(context.sampleRate);
  if (edge === 'start') {
    const before = index > 0 ? segments[index - 1]!.end : 0;
    const previous = before <= segment.start ? before : 0;
    return [previous, Math.max(previous, segment.end - minimum)];
  }
  const after = index + 1 < segments.length ? segments[index + 1]!.start : context.sourceFrames;
  const following = after >= segment.end ? after : context.sourceFrames;
  return [Math.min(following, segment.start + minimum), following];
}

/** The same stretches as new clips, for a paste or a copy. */
export function copiesOf(segments: readonly Segment[]): Segment[] {
  return segments.map((segment) => ({ ...segment, id: newSegmentId() }));
}

/** Move one edge to `sample` (clamped); everything after it ripples. */
export function trimEdge(
  segments: readonly Segment[],
  index: number,
  edge: Edge,
  sample: number,
  context: EditContext,
): Segment[] {
  const [low, high] = edgeLimits(segments, index, edge, context);
  const value = Math.round(Math.min(high, Math.max(low, sample)));
  const segment = segments[index]!;
  if ((edge === 'start' ? segment.start : segment.end) === value) return segments.slice();
  const next =
    edge === 'start'
      ? { ...segment, start: value, startOrigin: originFor('start', value, context) }
      : { ...segment, end: value, endOrigin: originFor('end', value, context) };
  return segments.map((item, position) => (position === index ? next : item));
}

/** Split segment `index` at source sample `sample`; null if too close to an edge. */
export function splitAt(
  segments: readonly Segment[],
  index: number,
  sample: number,
  context: EditContext,
): Segment[] | null {
  const segment = segments[index];
  if (!segment) return null;
  const minimum = minimumLength(context.sampleRate);
  const at = Math.round(sample);
  if (at - segment.start < minimum || segment.end - at < minimum) return null;
  const left: Segment = { ...segment, end: at, endOrigin: originFor('end', at, context) };
  const right: Segment = {
    ...segment,
    id: newSegmentId(),
    start: at,
    startOrigin: originFor('start', at, context),
  };
  return [...segments.slice(0, index), left, right, ...segments.slice(index + 1)];
}

/**
 * Snap targets for an edge drag: VoiceCut's own boundaries of that edge kind
 * and the transcript's word edges (word starts for clip starts, ends for ends).
 */
export function snapTargets(edge: Edge, context: EditContext): number[] {
  const proposal = context.proposal;
  if (!proposal) return [];
  const targets = new Set<number>();
  for (const segment of proposal.segments) targets.add(edge === 'start' ? segment.start : segment.end);
  for (const word of proposal.words) targets.add(edge === 'start' ? word.start : word.end);
  return [...targets].sort((a, b) => a - b);
}

/** Nearest target within `tolerance` samples of `sample`, if any. */
export function nearestTarget(targets: readonly number[], sample: number, tolerance: number): number | null {
  let low = 0;
  let high = targets.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (targets[middle]! < sample) low = middle + 1;
    else high = middle;
  }
  let best: number | null = null;
  for (const candidate of [targets[low - 1], targets[low]]) {
    if (candidate === undefined) continue;
    const distance = Math.abs(candidate - sample);
    if (distance <= tolerance && (best === null || distance < Math.abs(best - sample))) best = candidate;
  }
  return best;
}
