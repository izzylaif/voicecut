/**
 * The editable timeline: clips of the original recording, in recording
 * order, with pauses of room tone between them.
 *
 * `gaps[k]` is the pause before clip `k` (`gaps[clips.length]` the one after
 * the last clip), or null where two clips meet directly, so two pauses can
 * never touch.  A pause belongs to its place in the timeline, not to the clip
 * edges around it: trimming or extending a clip leaves its neighbouring pauses
 * as they are.  Every operation is pure and returns a new timeline, so undo is
 * just keeping old ones.
 */
import type { PauseRef, Proposal, RoomTone, WireItem, WirePause } from '../types';
import { type EditContext, type Edge, type Segment, fromWire, newSegmentId, splitAt, toWire, trimEdge } from './segments';

export interface Pause {
  id: string;
  /** Samples heard between its crossfades with the clips beside it. */
  length: number;
  origin: 'voicecut' | 'manual';
  join: PauseRef | null;
}

export interface Timeline {
  clips: Segment[];
  gaps: (Pause | null)[];
}

/** Length of a newly inserted pause (1 s) and the shortest a pause may get. */
export const NEW_PAUSE_SECONDS = 1;
export const MIN_PAUSE_SECONDS = 0.01;
export const MAX_PAUSE_SECONDS = 600;

export function newPauseId(): string {
  return `p-${newSegmentId().slice(2)}`;
}

export function fromItems(items: readonly WireItem[]): Timeline {
  const clips: Segment[] = [];
  const gaps: (Pause | null)[] = [null];
  for (const item of items) {
    if (item.kind === 'clip') {
      clips.push(fromWire(item));
      gaps.push(null);
    } else {
      gaps[gaps.length - 1] = { id: item.id, length: item.length, origin: item.origin, join: item.join };
    }
  }
  return { clips, gaps };
}

export function toItems(timeline: Timeline): WireItem[] {
  const items: WireItem[] = [];
  const pause = (gap: Pause | null) => {
    if (gap) items.push({ kind: 'pause', id: gap.id, length: gap.length, origin: gap.origin, join: gap.join } satisfies WirePause);
  };
  timeline.clips.forEach((clip, index) => {
    pause(timeline.gaps[index] ?? null);
    items.push(toWire(clip));
  });
  pause(timeline.gaps[timeline.clips.length] ?? null);
  return items;
}

/** Samples of VoiceCut's pause `tone` heard between its crossfades. */
export function heardLength(tone: RoomTone, afterClip: boolean, beforeClip: boolean): number {
  return tone.length - (afterClip ? tone.fade_in : 0) - (beforeClip ? tone.fade_out : 0);
}

export function roomToneOf(proposal: Proposal, join: PauseRef): RoomTone | null {
  if (join === 'lead') return proposal.lead;
  if (join === 'tail') return proposal.tail;
  return proposal.joins[join] ?? null;
}

function voicecutPause(proposal: Proposal, join: PauseRef): Pause {
  const tone = roomToneOf(proposal, join)!;
  return {
    id: `vp-${typeof join === 'number' ? String(join).padStart(4, '0') : join}`,
    length: heardLength(tone, join !== 'lead', join !== 'tail'),
    origin: 'voicecut',
    join,
  };
}

/** VoiceCut's edit as a timeline (mirrors `Proposal.timeline_items`). */
export function proposalTimeline(proposal: Proposal): Timeline {
  const clips: Segment[] = proposal.segments.map((segment) => ({
    id: segment.id,
    start: segment.start,
    end: segment.end,
    startOrigin: 'voicecut',
    endOrigin: 'voicecut',
  }));
  const gaps: (Pause | null)[] = clips.map((_, index) =>
    index === 0 ? (proposal.lead ? voicecutPause(proposal, 'lead') : null) : proposal.joins[index - 1] ? voicecutPause(proposal, index - 1) : null,
  );
  gaps.push(proposal.tail ? voicecutPause(proposal, 'tail') : null);
  return { clips, gaps };
}

function samePause(a: Pause | null, b: Pause | null): boolean {
  return a === b || (a !== null && b !== null && a.id === b.id && a.length === b.length && a.origin === b.origin && a.join === b.join);
}

function sameClip(a: Segment, b: Segment): boolean {
  return (
    a.id === b.id && a.start === b.start && a.end === b.end && a.startOrigin === b.startOrigin && a.endOrigin === b.endOrigin
  );
}

export function sameTimeline(a: Timeline, b: Timeline): boolean {
  return (
    a.clips.length === b.clips.length &&
    a.gaps.length === b.gaps.length &&
    a.clips.every((clip, index) => sameClip(clip, b.clips[index]!)) &&
    a.gaps.every((gap, index) => samePause(gap, b.gaps[index] ?? null))
  );
}

/** Whether the timeline is VoiceCut's edit, untouched. */
export function isProposal(timeline: Timeline, proposal: Proposal | null): boolean {
  if (!proposal) return false;
  const original = proposalTimeline(proposal);
  return (
    timeline.clips.length === original.clips.length &&
    timeline.clips.every(
      (clip, index) =>
        clip.start === original.clips[index]!.start &&
        clip.end === original.clips[index]!.end &&
        clip.startOrigin === 'voicecut' &&
        clip.endOrigin === 'voicecut',
    ) &&
    timeline.gaps.every((gap, index) => {
      const expected = original.gaps[index] ?? null;
      return gap === null ? expected === null : expected !== null && gap.length === expected.length && gap.join === expected.join;
    })
  );
}

/** Move a clip edge (clips keep their recording order; pauses stay). */
export function trimClip(timeline: Timeline, clip: number, edge: Edge, sample: number, context: EditContext): Timeline {
  return { clips: trimEdge(timeline.clips, clip, edge, sample, context), gaps: timeline.gaps };
}

export function pauseLimits(sampleRate: number): [number, number] {
  return [Math.round(MIN_PAUSE_SECONDS * sampleRate), Math.round(MAX_PAUSE_SECONDS * sampleRate)];
}

/** Set the length of the pause at `gap` (clamped). */
export function resizePause(timeline: Timeline, gap: number, length: number, sampleRate: number): Timeline {
  const pause = timeline.gaps[gap];
  if (!pause) return timeline;
  const [low, high] = pauseLimits(sampleRate);
  const value = Math.round(Math.min(high, Math.max(low, length)));
  if (value === pause.length) return timeline;
  return { clips: timeline.clips, gaps: timeline.gaps.map((item, index) => (index === gap ? { ...pause, length: value } : item)) };
}

/** Split clip `clip` at `sample`; the halves meet directly (or around `pause`). */
export function splitClip(
  timeline: Timeline,
  clip: number,
  sample: number,
  context: EditContext,
  pause: Pause | null = null,
): Timeline | null {
  const clips = splitAt(timeline.clips, clip, sample, context);
  if (!clips) return null;
  return { clips, gaps: [...timeline.gaps.slice(0, clip + 1), pause, ...timeline.gaps.slice(clip + 1)] };
}

/** Put `pause` into the empty gap `gap` (or lengthen the pause already there). */
export function placePause(timeline: Timeline, gap: number, pause: Pause, sampleRate: number): Timeline {
  const existing = timeline.gaps[gap];
  if (existing) return resizePause(timeline, gap, existing.length + pause.length, sampleRate);
  return { clips: timeline.clips, gaps: timeline.gaps.map((item, index) => (index === gap ? pause : item)) };
}

export function manualPause(sampleRate: number, seconds = NEW_PAUSE_SECONDS): Pause {
  return { id: newPauseId(), length: Math.round(seconds * sampleRate), origin: 'manual', join: null };
}

/**
 * Remove clips and pauses by id.  Deleting a clip joins the pauses on either
 * side of it into one, keeping the longer of the two.  Refuses (null) to
 * remove every clip or nothing at all.
 */
export function removeItems(timeline: Timeline, ids: ReadonlySet<string>): Timeline | null {
  if (timeline.clips.every((clip) => ids.has(clip.id))) return null;
  let changed = false;
  let gaps = timeline.gaps.map((gap) => {
    if (gap && ids.has(gap.id)) {
      changed = true;
      return null;
    }
    return gap;
  });
  let clips = timeline.clips;
  for (let index = clips.length - 1; index >= 0; index -= 1) {
    if (!ids.has(clips[index]!.id)) continue;
    changed = true;
    const before = gaps[index] ?? null;
    const after = gaps[index + 1] ?? null;
    const kept = before && after ? (after.length > before.length ? after : before) : (before ?? after);
    clips = [...clips.slice(0, index), ...clips.slice(index + 1)];
    gaps = [...gaps.slice(0, index), kept, ...gaps.slice(index + 2)];
  }
  return changed ? { clips, gaps } : null;
}

/** Whether clip `index` and the next one continue the same source audio. */
export function isThroughEdit(timeline: Timeline, index: number): boolean {
  const next = timeline.clips[index + 1];
  return next !== undefined && timeline.gaps[index + 1] === null && timeline.clips[index]!.end === next.start;
}

/** Merge clip `index` with its source-contiguous right neighbour. */
export function joinThroughEdit(timeline: Timeline, index: number): Timeline | null {
  if (!isThroughEdit(timeline, index)) return null;
  const left = timeline.clips[index]!;
  const right = timeline.clips[index + 1]!;
  const merged: Segment = { ...left, end: right.end, endOrigin: right.endOrigin };
  return {
    clips: [...timeline.clips.slice(0, index), merged, ...timeline.clips.slice(index + 2)],
    gaps: [...timeline.gaps.slice(0, index + 1), ...timeline.gaps.slice(index + 2)],
  };
}

/** Ids of every clip and pause. */
export function itemIds(timeline: Timeline): Set<string> {
  const ids = new Set(timeline.clips.map((clip) => clip.id));
  for (const gap of timeline.gaps) if (gap) ids.add(gap.id);
  return ids;
}
