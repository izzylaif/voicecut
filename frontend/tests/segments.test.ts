import { describe, expect, it } from 'vitest';
import fixture from './fixtures/layout_cases.json';
import { History } from '../src/model/history';
import {
  type EditContext,
  edgeLimits,
  minimumLength,
  nearestTarget,
  originFor,
  snapTargets,
  splitAt,
  trimEdge,
} from '../src/model/segments';
import { isProposal, isThroughEdit, joinThroughEdit, proposalTimeline } from '../src/model/timeline';
import { wordsBetween } from '../src/model/words';
import type { Proposal } from '../src/types';

const proposal = fixture.proposal as unknown as Proposal;
const context: EditContext = { proposal, sourceFrames: fixture.source_frames, sampleRate: fixture.sample_rate };
const original = proposalTimeline(proposal);
const base = original.clips;

describe('trimming', () => {
  it('extends an end into removed material up to the next clip', () => {
    const next = trimEdge(base, 0, 'end', 80_000, context);
    expect(next[0]).toMatchObject({ end: 80_000, endOrigin: 'manual' });
    expect(next.slice(1)).toEqual(base.slice(1));
    const clamped = trimEdge(base, 0, 'end', 10_000_000, context);
    expect(clamped[0]!.end).toBe(base[1]!.start);
  });

  it('extends a start back to the previous clip or the recording start', () => {
    expect(trimEdge(base, 1, 'start', 0, context)[1]!.start).toBe(base[0]!.end);
    const first = trimEdge(base, 0, 'start', -5, context)[0]!;
    expect(first).toMatchObject({ start: 0, startOrigin: 'source' });
  });

  it('never shortens a clip below the minimum length', () => {
    const minimum = minimumLength(context.sampleRate);
    const next = trimEdge(base, 2, 'start', base[2]!.end, context);
    expect(next[2]!.end - next[2]!.start).toBe(minimum);
    expect(edgeLimits(base, 2, 'end', context)).toEqual([base[2]!.start + minimum, base[3]!.start]);
  });

  it('restores VoiceCut provenance when an edge returns to its sample', () => {
    const moved = trimEdge(base, 1, 'start', 95_000, context);
    expect(moved[1]!.startOrigin).toBe('manual');
    const back = trimEdge(moved, 1, 'start', base[1]!.start, context);
    expect(back[1]).toEqual(base[1]);
    expect(isProposal({ clips: back, gaps: original.gaps }, proposal)).toBe(true);
  });

  it('names origins by edge kind', () => {
    expect(originFor('start', base[1]!.start, context)).toBe('voicecut');
    expect(originFor('end', base[1]!.start, context)).toBe('manual');
    expect(originFor('end', context.sourceFrames, context)).toBe('source');
  });
});

describe('split', () => {
  it('splits into two source-contiguous clips', () => {
    const next = splitAt(base, 3, 360_000, context)!;
    expect(next).toHaveLength(base.length + 1);
    expect(next[3]).toMatchObject({ start: 300_000, end: 360_000, startOrigin: 'voicecut', endOrigin: 'manual' });
    expect(next[4]).toMatchObject({ start: 360_000, end: 420_000, startOrigin: 'manual', endOrigin: 'voicecut' });
    expect(next[4]!.id).not.toBe(next[3]!.id);
    const timeline = { clips: next, gaps: [...original.gaps.slice(0, 4), null, ...original.gaps.slice(4)] };
    expect(isThroughEdit(timeline, 3)).toBe(true);
    expect(joinThroughEdit(timeline, 3)).toEqual(original);
  });

  it('refuses to split next to an edge', () => {
    expect(splitAt(base, 3, 300_010, context)).toBeNull();
  });
});

describe('snapping', () => {
  it('offers VoiceCut boundaries and word edges of the dragged kind', () => {
    const starts = snapTargets('start', context);
    expect(starts).toContain(base[2]!.start);
    expect(starts).toContain(proposal.words[2]!.start);
    expect(starts).not.toContain(base[2]!.end);
    expect(nearestTarget(starts, base[2]!.start + 30, 50)).toBe(base[2]!.start);
    expect(nearestTarget(starts, base[2]!.start + 300, 50)).toBeNull();
  });

  it('finds the words of a source range', () => {
    const words = proposal.words;
    const [first, last] = wordsBetween(words, words[1]!.start, words[3]!.start + 1);
    expect([first, last]).toEqual([1, 4]);
  });
});

describe('history', () => {
  it('undoes and redoes whole gestures', () => {
    const history = new History(base);
    const trimmed = trimEdge(base, 0, 'end', 80_000, context);
    history.push(trimmed);
    expect(history.undo()).toBe(base);
    expect(history.canRedo).toBe(true);
    expect(history.redo()).toBe(trimmed);
    history.undo();
    history.push(splitAt(base, 3, 360_000, context)!);
    expect(history.canRedo).toBe(false);
    history.reset(base);
    expect(history.canUndo).toBe(false);
  });
});
