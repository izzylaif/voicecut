/**
 * The editable timeline: clips of the original recording, in the order the
 * user arranged them (recording order unless clips were moved or copied),
 * with pauses of room tone between them.
 *
 * `gaps[k]` is the pause before clip `k` (`gaps[clips.length]` the one after
 * the last clip), or null where two clips meet directly, so two pauses can
 * never touch.  A pause belongs to its place in the timeline, not to the clip
 * edges around it: trimming or extending a clip leaves its neighbouring pauses
 * as they are.  Every operation is pure and returns a new timeline, so undo is
 * just keeping old ones.
 */
import type { PauseRef, PauseWidths, Proposal, RoomTone, WireItem, WirePause } from '../types';
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

/**
 * The length of VoiceCut's pause `join` with the pause widths of Settings: the
 * renderer aimed it at the target of its class, so it moves by the difference
 * between the chosen width and that target (the silence around the cut stays).
 */
export function voicecutPauseLength(proposal: Proposal, join: PauseRef, widths?: PauseWidths): number {
  const tone = roomToneOf(proposal, join)!;
  const heard = heardLength(tone, join !== 'lead', join !== 'tail');
  const name = tone.pause_class;
  const target = name ? proposal.pause_targets?.[name] : undefined;
  const width = name ? widths?.[name] : undefined;
  if (target === undefined || width === undefined || width === target) return heard;
  return Math.max(
    Math.round(MIN_PAUSE_SECONDS * proposal.sample_rate),
    Math.round(heard + (width - target) * proposal.sample_rate),
  );
}

function voicecutPause(proposal: Proposal, join: PauseRef, widths?: PauseWidths): Pause {
  return {
    id: `vp-${typeof join === 'number' ? String(join).padStart(4, '0') : join}`,
    length: voicecutPauseLength(proposal, join, widths),
    origin: 'voicecut',
    join,
  };
}

/** Give every VoiceCut pause of `timeline` its length for `widths` (manual pauses stay). */
export function applyPauseWidths(timeline: Timeline, proposal: Proposal | null, widths?: PauseWidths): Timeline {
  if (!proposal) return timeline;
  let changed = false;
  const gaps = timeline.gaps.map((gap) => {
    if (!gap || gap.origin !== 'voicecut' || gap.join === null || !roomToneOf(proposal, gap.join)) return gap;
    const length = voicecutPauseLength(proposal, gap.join, widths);
    if (length === gap.length) return gap;
    changed = true;
    return { ...gap, length };
  });
  return changed ? { clips: timeline.clips, gaps } : timeline;
}

/** VoiceCut's edit as a timeline (mirrors `Proposal.timeline_items`), its pauses at `widths`. */
export function proposalTimeline(proposal: Proposal, widths?: PauseWidths): Timeline {
  const clips: Segment[] = proposal.segments.map((segment) => ({
    id: segment.id,
    start: segment.start,
    end: segment.end,
    startOrigin: 'voicecut',
    endOrigin: 'voicecut',
  }));
  const gaps: (Pause | null)[] = clips.map((_, index) =>
    index === 0
      ? proposal.lead
        ? voicecutPause(proposal, 'lead', widths)
        : null
      : proposal.joins[index - 1]
        ? voicecutPause(proposal, index - 1, widths)
        : null,
  );
  gaps.push(proposal.tail ? voicecutPause(proposal, 'tail', widths) : null);
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
export function isProposal(timeline: Timeline, proposal: Proposal | null, widths?: PauseWidths): boolean {
  if (!proposal) return false;
  const original = proposalTimeline(proposal, widths);
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

/** Move a clip edge (everything after it follows; pauses stay). */
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
  // A VoiceCut pause the user resizes is theirs: the pause widths no longer move it.
  const resized: Pause = pause.origin === 'voicecut' ? { ...pause, length: value, origin: 'manual', join: null } : { ...pause, length: value };
  return { clips: timeline.clips, gaps: timeline.gaps.map((item, index) => (index === gap ? resized : item)) };
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

/**
 * Put `clips` in at clip boundary `boundary` (before clip `boundary`): after
 * the pause there, if any, and meeting the clip that follows directly.
 */
export function insertClips(timeline: Timeline, clips: readonly Segment[], boundary: number): Timeline {
  const at = Math.max(0, Math.min(timeline.clips.length, boundary));
  return {
    clips: [...timeline.clips.slice(0, at), ...clips, ...timeline.clips.slice(at)],
    gaps: [...timeline.gaps.slice(0, at + 1), ...clips.map(() => null), ...timeline.gaps.slice(at + 1)],
  };
}

/**
 * Move the clips `ids` (in their timeline order) to clip boundary `boundary`
 * of `timeline`.  Where they leave, the pauses on either side join as on a
 * delete.  Null when nothing would change: no clip to move, all of them, or a
 * run of clips dropped onto its own place.
 */
export function moveClips(timeline: Timeline, ids: ReadonlySet<string>, boundary: number): Timeline | null {
  const moving = timeline.clips.filter((clip) => ids.has(clip.id));
  if (moving.length === 0 || moving.length === timeline.clips.length) return null;
  const positions = timeline.clips.flatMap((clip, index) => (ids.has(clip.id) ? [index] : []));
  const first = positions[0]!;
  const last = positions[positions.length - 1]!;
  const contiguous = last - first + 1 === positions.length;
  if (contiguous && boundary >= first && boundary <= last + 1) return null;
  const rest = removeItems(timeline, new Set(moving.map((clip) => clip.id)));
  if (!rest) return null;
  const before = timeline.clips.slice(0, boundary).filter((clip) => !ids.has(clip.id)).length;
  return insertClips(rest, moving, before);
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
