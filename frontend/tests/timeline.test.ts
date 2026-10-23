import { describe, expect, it } from 'vitest';
import fixture from './fixtures/layout_cases.json';
import { computeLayout } from '../src/model/layout';
import { type EditContext, copiesOf, edgeLimits } from '../src/model/segments';
import {
  type Pause,
  insertClips,
  isProposal,
  manualPause,
  pauseLimits,
  moveClips,
  placePause,
  proposalTimeline,
  removeItems,
  resizePause,
  splitClip,
  trimClip,
} from '../src/model/timeline';
import type { Proposal } from '../src/types';

const proposal = fixture.proposal as unknown as Proposal;
const rate = fixture.sample_rate;
const context: EditContext = { proposal, sourceFrames: fixture.source_frames, sampleRate: rate };
const original = proposalTimeline(proposal);

function pauseLengths(timeline: ReturnType<typeof proposalTimeline>): (number | null)[] {
  return timeline.gaps.map((gap) => gap?.length ?? null);
}

describe('pauses are part of the timeline', () => {
  it('stay where they are when the clips around them are trimmed', () => {
    const extended = trimClip(original, 0, 'end', 80_000, context);
    const trimmed = trimClip(extended, 1, 'start', 95_000, context);
    expect(pauseLengths(trimmed)).toEqual(pauseLengths(original));
    const before = computeLayout(original, proposal, rate);
    const after = computeLayout(trimmed, proposal, rate);
    expect(after.pauses.map((pause) => pause.outEnd - pause.outStart)).toEqual(
      before.pauses.map((pause) => pause.outEnd - pause.outStart),
    );
    expect(after.total - before.total).toBe(8_000 - 5_000);
    expect(isProposal(trimmed, proposal)).toBe(false);
  });

  it('can be resized within limits, and the edit grows by exactly that much', () => {
    const longer = resizePause(original, 1, 18_720 + rate, rate);
    expect(longer.gaps[1]!.length).toBe(18_720 + rate);
    expect(computeLayout(longer, proposal, rate).total - computeLayout(original, proposal, rate).total).toBe(rate);
    const [low, high] = pauseLimits(rate);
    expect(resizePause(original, 1, 1, rate).gaps[1]!.length).toBe(low);
    expect(resizePause(original, 1, 10 ** 12, rate).gaps[1]!.length).toBe(high);
    expect(resizePause(original, 2, 5_000, rate)).toBe(original); // no pause there
  });

  it('are inserted inside a clip by splitting it', () => {
    const pause = manualPause(rate);
    const next = splitClip(original, 3, 360_000, context, pause)!;
    expect(next.clips).toHaveLength(5);
    expect(next.gaps[4]).toBe(pause);
    expect(next.clips[3]!.end).toBe(360_000);
    expect(next.clips[4]!.start).toBe(360_000);
    expect(computeLayout(next, proposal, rate).total - computeLayout(original, proposal, rate).total).toBe(rate);
  });

  it('go into an empty cut, or lengthen the pause already there', () => {
    const pause = manualPause(rate, 0.5);
    const placed = placePause(original, 2, pause, rate);
    expect(placed.gaps[2]).toBe(pause);
    const grown = placePause(original, 1, pause, rate);
    expect(grown.gaps[1]!.length).toBe(18_720 + rate / 2);
    expect(grown.gaps[1]!.id).toBe(original.gaps[1]!.id);
  });
});

describe('deleting', () => {
  const ids = (...values: string[]) => new Set(values);

  it('a pause leaves a direct cut', () => {
    const next = removeItems(original, ids(original.gaps[1]!.id))!;
    expect(next.clips).toEqual(original.clips);
    expect(next.gaps[1]).toBeNull();
  });

  it('a clip between two pauses keeps the longer pause', () => {
    // Clip 3 sits between VoiceCut's 3 840-sample pause and the 28 800-sample lead-out.
    const next = removeItems(original, ids(original.clips[3]!.id))!;
    expect(next.clips).toHaveLength(3);
    expect(next.gaps).toHaveLength(4);
    expect(next.gaps[3]!.join).toBe('tail');
    const short: Pause = { ...original.gaps[4]!, length: 100 };
    const reversed = { clips: original.clips, gaps: [...original.gaps.slice(0, 4), short] };
    expect(removeItems(reversed, ids(original.clips[3]!.id))!.gaps[3]!.join).toBe(2);
  });

  it('a clip next to one pause keeps that pause', () => {
    const next = removeItems(original, ids(original.clips[1]!.id))!;
    expect(next.gaps.map((gap) => gap?.join ?? null)).toEqual(['lead', 0, 2, 'tail']);
  });

  it('never removes every clip, and reports when nothing changed', () => {
    expect(removeItems(original, new Set(original.clips.map((clip) => clip.id)))).toBeNull();
    expect(removeItems(original, ids('missing'))).toBeNull();
  });
});

describe('moving and copying clips', () => {
  const ids = (...values: string[]) => new Set(values);
  const order = (timeline: typeof original) => timeline.clips.map((clip) => clip.id);
  const [first, second, third, last] = original.clips.map((clip) => clip.id) as [string, string, string, string];

  it('moves a clip to another cut, joining the pauses where it left', () => {
    const next = moveClips(original, ids(last), 0)!;
    expect(order(next)).toEqual([last, first, second, third]);
    // It goes in after the lead-in and meets the first clip directly; the
    // tail, longer than its own pause, closes the edit.
    expect(next.gaps.map((gap) => gap?.join ?? null)).toEqual(['lead', null, 0, null, 'tail']);
    expect(computeLayout(next, proposal, rate).total).toBeLessThan(computeLayout(original, proposal, rate).total);
  });

  it('does nothing when the clips are dropped onto their own place', () => {
    expect(moveClips(original, ids(second), 1)).toBeNull();
    expect(moveClips(original, ids(second), 2)).toBeNull();
    expect(moveClips(original, ids(second, third), 3)).toBeNull();
    expect(moveClips(original, new Set(order(original)), 0)).toBeNull();
  });

  it('gathers clips from different places in their order', () => {
    expect(order(moveClips(original, ids(first, third), 4)!)).toEqual([second, last, first, third]);
    expect(order(moveClips(original, ids(first, third), 2)!)).toEqual([second, first, third, last]);
  });

  it('inserts copies of the same stretches as new clips', () => {
    const copies = copiesOf([original.clips[1]!]);
    const next = insertClips(original, copies, 4);
    expect(next.clips).toHaveLength(5);
    expect(next.gaps).toHaveLength(6);
    const copy = next.clips[4]!;
    expect([copy.start, copy.end, copy.startOrigin, copy.endOrigin]).toEqual([
      original.clips[1]!.start,
      original.clips[1]!.end,
      'voicecut',
      'voicecut',
    ]);
    expect(copy.id).not.toBe(second);
    // The tail stays before the copy at the very end.
    expect(next.gaps[4]?.join).toBe('tail');
    expect(next.gaps[5]).toBeNull();
  });

  it('lets an edge extend into a neighbour only where it comes later in the recording', () => {
    const minimum = Math.round(0.02 * rate);
    // In recording order an edge stops at its neighbour's material.
    expect(edgeLimits(original.clips, 0, 'end', context)).toEqual([original.clips[0]!.start + minimum, original.clips[1]!.start]);
    // Moved first, the last clip extends as far as the recording goes.
    const moved = moveClips(original, ids(last), 0)!;
    expect(edgeLimits(moved.clips, 0, 'end', context)[1]).toBe(fixture.source_frames);
    expect(edgeLimits(moved.clips, 1, 'start', context)[0]).toBe(0);
  });
});
