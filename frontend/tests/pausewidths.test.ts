import { describe, expect, it } from 'vitest';
import fixture from './fixtures/layout_cases.json';
import { applyPauseWidths, isProposal, proposalTimeline, resizePause, voicecutPauseLength } from '../src/model/timeline';
import type { Proposal } from '../src/types';

const base = fixture.proposal as unknown as Proposal;
const rate = fixture.sample_rate;
// The fixture's first join is a sentence pause; the renderer aimed it at 0.5 s.
const proposal: Proposal = {
  ...base,
  joins: base.joins.map((join, index) => (join ? { ...join, pause_class: index === 0 ? 'sentence' : 'comma' } : join)),
  pause_targets: { none: 0.1, comma: 0.28, sentence: 0.5, paragraph: 0.85, lead: 0.35, tail: 0.6 },
};
const join = proposal.joins.findIndex((tone) => tone !== null);

describe('pause widths', () => {
  it('move each VoiceCut pause by the difference to the renderer’s target', () => {
    const plain = voicecutPauseLength(proposal, join, {});
    const wider = voicecutPauseLength(proposal, join, { sentence: 0.8, comma: 0.28 });
    expect(wider - plain).toBe(Math.round(0.3 * rate));
    // The defaults change nothing; a pause never gets shorter than the minimum.
    expect(voicecutPauseLength(proposal, join, { sentence: 0.5 })).toBe(plain);
    expect(voicecutPauseLength(proposal, join, { sentence: 0 })).toBeGreaterThan(0);
  });

  it('apply to VoiceCut’s pauses and leave the ones the user resized', () => {
    const original = proposalTimeline(proposal);
    const gap = original.gaps.findIndex((pause) => pause?.origin === 'voicecut' && pause.join === join);
    const resized = resizePause(original, gap, original.gaps[gap]!.length + 1000, rate);
    // Resizing makes the pause the user's.
    expect(resized.gaps[gap]!.origin).toBe('manual');
    expect(resized.gaps[gap]!.join).toBeNull();
    const widths = { sentence: 1.0, comma: 0.6 };
    const widened = applyPauseWidths(original, proposal, widths);
    expect(widened.gaps[gap]!.length).toBe(voicecutPauseLength(proposal, join, widths));
    const kept = applyPauseWidths(resized, proposal, widths);
    expect(kept.gaps[gap]!.length).toBe(resized.gaps[gap]!.length);
    // VoiceCut's edit at the new widths is still VoiceCut's edit.
    expect(isProposal(widened, proposal, widths)).toBe(true);
    expect(isProposal(widened, proposal, {})).toBe(false);
  });
});
