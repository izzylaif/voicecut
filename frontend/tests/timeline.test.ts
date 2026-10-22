import { describe, expect, it } from 'vitest';
import fixture from './fixtures/layout_cases.json';
import { computeLayout } from '../src/model/layout';
import type { EditContext } from '../src/model/segments';
import {
  type Pause,
  isProposal,
  manualPause,
  pauseLimits,
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
