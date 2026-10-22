import { describe, expect, it } from 'vitest';
import fixture from './fixtures/layout_cases.json';
import { clipAt, computeLayout, editPoints, outputAt, pauseAt, sourceAt } from '../src/model/layout';
import { fromItems, proposalTimeline, toItems } from '../src/model/timeline';
import type { Proposal, WireItem } from '../src/types';

interface PythonPiece {
  kind: 'source' | 'room_tone';
  output_start_sample: number;
  output_end_sample: number;
  source_start_sample: number | null;
  fade_in_samples: number;
  fade_out_samples: number;
  source_interval_index: number | null;
}

interface PythonTrace {
  kind: 'source' | 'room_tone';
  output_start_sample: number;
  output_end_sample: number;
  source_interval_index?: number;
  source_start_sample?: number;
}

const proposal = fixture.proposal as unknown as Proposal;

describe('layout matches the Python exporter (tests/test_editor_plan.py)', () => {
  for (const testCase of fixture.cases) {
    it(testCase.name, () => {
      const items = testCase.items as WireItem[];
      const timeline = fromItems(items);
      expect(toItems(timeline)).toEqual(items);
      const layout = computeLayout(timeline, testCase.with_proposal ? proposal : null, fixture.sample_rate);
      expect(layout.total).toBe(testCase.total);
      const placed = testCase.placed as PythonPiece[];
      expect(layout.pieces.length).toBe(placed.length);
      layout.pieces.forEach((piece, index) => {
        const expected = placed[index]!;
        expect(piece.kind).toBe(expected.kind);
        expect(piece.outStart).toBe(expected.output_start_sample);
        expect(piece.outEnd).toBe(expected.output_end_sample);
        expect(piece.placedFadeIn).toBe(expected.fade_in_samples);
        expect(piece.placedFadeOut).toBe(expected.fade_out_samples);
        if (piece.kind === 'source') {
          expect(piece.sourceStart).toBe(expected.source_start_sample);
          expect(piece.segment).toBe(expected.source_interval_index);
        }
      });
      const trace = testCase.trace as PythonTrace[];
      const clips = trace.filter((item) => item.kind === 'source');
      const pauses = trace.filter((item) => item.kind === 'room_tone');
      expect(layout.clips.map((clip) => [clip.segment, clip.outStart, clip.outEnd, clip.sourceAtStart])).toEqual(
        clips.map((item) => [item.source_interval_index, item.output_start_sample, item.output_end_sample, item.source_start_sample]),
      );
      expect(layout.pauses.map((pause) => [pause.outStart, pause.outEnd])).toEqual(
        pauses.map((item) => [item.output_start_sample, item.output_end_sample]),
      );
    });
  }
});

describe('output <-> source mapping', () => {
  const timeline = fromItems(fixture.cases[0]!.items as WireItem[]);
  const layout = computeLayout(timeline, proposal, fixture.sample_rate);

  it('builds VoiceCut’s timeline like the server does', () => {
    expect(toItems(proposalTimeline(proposal))).toEqual(fixture.cases[0]!.items);
  });

  it('round-trips every sample of every clip', () => {
    for (const clip of layout.clips) {
      for (const frame of [clip.outStart, (clip.outStart + clip.outEnd) >> 1, clip.outEnd - 1]) {
        const sample = sourceAt(layout, frame);
        expect(sample).not.toBeNull();
        expect(outputAt(layout, sample!)).toBe(frame);
      }
    }
  });

  it('finds clips by output frame and reports pauses as gaps', () => {
    expect(clipAt(layout, 0)).toBe(-1); // lead-in room tone
    expect(sourceAt(layout, 0)).toBeNull();
    expect(layout.pauses[pauseAt(layout, 0)]!.join).toBe('lead');
    const second = layout.clips[1]!;
    expect(clipAt(layout, second.outStart)).toBe(1);
    expect(clipAt(layout, second.outEnd - 1)).toBe(1);
    expect(outputAt(layout, 1)).toBeNull(); // cut out by Voice Cut
  });

  it('lists edit points in order', () => {
    const points = editPoints(layout);
    expect(points[0]).toBe(0);
    expect(points[points.length - 1]).toBe(layout.total);
    expect([...points].sort((a, b) => a - b)).toEqual(points);
  });
});
