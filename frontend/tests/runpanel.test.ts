import { describe, expect, it } from 'vitest';
import { leftText, reduceProgress, startProgress, timeLeft } from '../src/components/RunPanel';

describe('Voice Cut progress', () => {
  const estimate = { preflight: 2, media_input: 2, analysis: 5, transcription: 60, hidden_retry_recovery: 30, gap_recovery: 10, semantic_plan: 100, final_render: 80, publication: 1 };

  it('estimates the time left from the stages done, the running one and the plan', () => {
    let progress = startProgress('job');
    progress = reduceProgress(progress, { type: 'stage', stage: 'preflight', status: 'started', label: '', at: 0 });
    progress = reduceProgress(progress, { type: 'stage', stage: 'preflight', status: 'completed', label: '', at: 2 });
    progress = reduceProgress(progress, { type: 'estimate', audio_seconds: 600, stages: estimate, at: 2 });
    progress = reduceProgress(progress, { type: 'stage', stage: 'media_input', status: 'started', label: '', at: 2 });
    progress = reduceProgress(progress, { type: 'stage', stage: 'media_input', status: 'completed', label: '', at: 4 });
    progress = reduceProgress(progress, { type: 'stage', stage: 'analysis', status: 'started', label: '', at: 4 });
    progress = reduceProgress(progress, { type: 'stage', stage: 'analysis', status: 'completed', label: '', at: 14 });
    // The short stages say little (start-up time); the transcription, half done
    // in 60 s, runs at twice its estimate, so the stages to come do too.
    progress = reduceProgress(progress, { type: 'stage', stage: 'transcription', status: 'started', label: '', at: 14 });
    progress = reduceProgress(progress, { type: 'stage_progress', stage: 'transcription', fraction: 0.5, at: 74 });
    const left = timeLeft(progress, 74)!;
    expect(left.seconds).toBeCloseTo(60 + 2 * 221, 0);
    expect(left.fraction).toBeCloseTo(74 / (74 + 502), 2);
    expect(progress.stages.transcription!.fraction).toBe(0.5);
  });

  it('waits for a measurement on this computer before it estimates', () => {
    let progress = startProgress('job');
    progress = reduceProgress(progress, { type: 'estimate', audio_seconds: 600, stages: estimate, at: 2 });
    progress = reduceProgress(progress, { type: 'stage', stage: 'transcription', status: 'started', label: '', at: 2 });
    expect(timeLeft(progress, 10)).toBeNull();
    progress = reduceProgress(progress, { type: 'stage_progress', stage: 'transcription', fraction: 0.1, at: 10 });
    expect(timeLeft(progress, 10)).not.toBeNull();
    // Speeds learned from earlier runs count from the start.
    let learned = startProgress('job');
    learned = reduceProgress(learned, { type: 'estimate', audio_seconds: 600, stages: estimate, learned: true, at: 2 });
    expect(timeLeft(learned, 2)).not.toBeNull();
  });

  it('says nothing without an estimate and words the time', () => {
    expect(timeLeft(startProgress('job'), 0)).toBeNull();
    expect(leftText(30)).toBe('less than a minute left');
    expect(leftText(6 * 60)).toBe('about 6 min left');
    expect(leftText(125 * 60)).toBe('about 2 h 5 min left');
  });
});
