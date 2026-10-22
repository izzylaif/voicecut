import { useEffect, useState } from 'react';
import { Check, ChevronDown, ChevronRight, CircleDashed, Loader2, Minus, X } from 'lucide-react';
import { duration } from '../format';
import type { JobEvent, StageStatus } from '../types';

export const PIPELINE_STAGES: { id: string; label: string }[] = [
  { id: 'preflight', label: 'Checking runtimes' },
  { id: 'media_input', label: 'Preparing media' },
  { id: 'analysis', label: 'Analyzing speech' },
  { id: 'transcription', label: 'Transcribing' },
  { id: 'hidden_retry_recovery', label: 'Recovering hidden retries' },
  { id: 'gap_recovery', label: 'Recovering skipped speech' },
  { id: 'semantic_plan', label: 'Choosing takes' },
  { id: 'final_render', label: 'Aligning and rendering the edit' },
  { id: 'publication', label: 'Publishing' },
];

export interface StageState {
  status: StageStatus;
  label: string;
  started?: number;
  finished?: number;
}

export interface JobProgress {
  jobId: string;
  status: 'running' | 'complete' | 'failed' | 'cancelled';
  stages: Record<string, StageState>;
  logs: string[];
  error: string | null;
  startedAt: number;
  finishedAt?: number;
  fraction?: number;
  result?: Record<string, unknown>;
}

export function startProgress(jobId: string): JobProgress {
  return { jobId, status: 'running', stages: {}, logs: [], error: null, startedAt: Date.now() / 1000 };
}

/** Fold one job event into its progress record. */
export function reduceProgress(progress: JobProgress, event: JobEvent): JobProgress {
  const at = event.at ?? Date.now() / 1000;
  switch (event.type) {
    case 'stage': {
      const previous = progress.stages[event.stage];
      const stage: StageState = {
        status: event.status,
        label: event.label || previous?.label || event.stage,
        ...(event.status === 'started' ? { started: at } : { started: previous?.started ?? at, finished: at }),
      };
      if (Object.keys(progress.stages).length === 0) progress = { ...progress, startedAt: at };
      return { ...progress, stages: { ...progress.stages, [event.stage]: stage } };
    }
    case 'progress':
      return { ...progress, fraction: event.fraction };
    case 'log':
      return { ...progress, logs: [...progress.logs.slice(-399), event.line] };
    case 'complete':
      return { ...progress, status: 'complete', finishedAt: at, result: event.result };
    case 'error': {
      const stages = { ...progress.stages };
      for (const [key, stage] of Object.entries(stages)) {
        if (stage.status === 'started') stages[key] = { ...stage, status: 'failed', finished: at };
      }
      return { ...progress, status: 'failed', error: event.message, finishedAt: at, stages };
    }
    case 'cancelled':
      return { ...progress, status: 'cancelled', finishedAt: at };
  }
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 500);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function StageIcon({ status }: { status: StageStatus | undefined }) {
  switch (status) {
    case 'started':
      return <Loader2 size={14} className="spin" />;
    case 'completed':
    case 'cached':
      return <Check size={14} />;
    case 'skipped':
      return <Minus size={14} />;
    case 'failed':
      return <X size={14} />;
    default:
      return <CircleDashed size={14} />;
  }
}

export function RunPanel({
  progress,
  onCancel,
  onClose,
  onRetry,
}: {
  progress: JobProgress;
  onCancel: () => void;
  onClose: () => void;
  onRetry: () => void;
}) {
  const running = progress.status === 'running';
  const now = useNow(running);
  const [showLog, setShowLog] = useState(false);
  const elapsed = (progress.finishedAt ?? now) - progress.startedAt;
  const title =
    progress.status === 'running'
      ? 'Voice Cut is editing the recording'
      : progress.status === 'complete'
        ? 'Voice Cut finished'
        : progress.status === 'cancelled'
          ? 'Voice Cut was cancelled'
          : 'Voice Cut failed';

  return (
    <section className={`run-panel run-${progress.status}`} aria-live="polite">
      <header>
        <div>
          <h2>{title}</h2>
          <span className="muted">{duration(Math.max(0, elapsed))}</span>
        </div>
        {running ? (
          <button type="button" className="button subtle" onClick={onCancel}>
            Cancel
          </button>
        ) : (
          <button type="button" className="icon-button" title="Close" onClick={onClose}>
            <X size={16} />
          </button>
        )}
      </header>
      <ol className="stages">
        {PIPELINE_STAGES.map(({ id, label }) => {
          const stage = progress.stages[id];
          const seconds =
            stage?.started !== undefined ? (stage.finished ?? now) - stage.started : undefined;
          return (
            <li key={id} className={`stage stage-${stage?.status ?? 'pending'}`}>
              <StageIcon status={stage?.status} />
              <span className="stage-label">{stage?.label ?? label}</span>
              <span className="stage-time">
                {stage?.status === 'cached'
                  ? 'cached'
                  : stage?.status === 'skipped'
                    ? 'skipped'
                    : seconds !== undefined
                      ? duration(Math.max(0, seconds))
                      : ''}
              </span>
            </li>
          );
        })}
      </ol>
      {progress.error && <pre className="error-detail">{progress.error}</pre>}
      {(progress.status === 'failed' || progress.status === 'cancelled') && (
        <button type="button" className="button" onClick={onRetry}>
          Run Voice Cut again
        </button>
      )}
      <button type="button" className="log-toggle" onClick={() => setShowLog((value) => !value)}>
        {showLog ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Log ({progress.logs.length} lines)
      </button>
      {showLog && <pre className="log">{progress.logs.slice(-200).join('\n')}</pre>}
    </section>
  );
}
