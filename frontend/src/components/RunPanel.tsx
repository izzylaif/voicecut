import { useEffect, useRef, useState } from 'react';
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
  /** How far a running stage has got (0..1), when the stage reports it. */
  fraction?: number;
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
  /** Expected seconds per stage: this computer's own speeds where earlier runs measured them. */
  estimate?: Record<string, number>;
  /** Whether the estimate comes from earlier runs on this computer. */
  learned?: boolean;
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
    case 'stage_progress': {
      const stage = progress.stages[event.stage];
      if (!stage || stage.status !== 'started') return progress;
      return { ...progress, stages: { ...progress.stages, [event.stage]: { ...stage, fraction: event.fraction } } };
    }
    case 'estimate':
      return { ...progress, estimate: event.stages, learned: Boolean(event.learned) };
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

/**
 * How far the whole run has got and how many seconds are left: finished stages
 * count as they took, the running stage by its own rate once it reports
 * progress, and the stages to come by the pipeline's estimates scaled by how
 * fast this computer was on the stages already done.
 */
/** Stages expected to take at least this long tell how fast this computer is. */
const MEASURE_SECONDS = 10;

export function timeLeft(progress: JobProgress, now: number): { fraction: number; seconds: number } | null {
  const estimate = progress.estimate;
  if (!estimate || progress.status !== 'running' || !measured(progress)) return null;
  // How fast this computer is against the estimate: from the stages of some
  // length done so far (short ones are mostly start-up time), else from the
  // running stage's own rate once it is under way.
  let actual = 0;
  let expected = 0;
  for (const { id } of PIPELINE_STAGES) {
    const stage = progress.stages[id];
    const planned = estimate[id] ?? 0;
    if (stage?.status === 'completed' && stage.started !== undefined && stage.finished !== undefined && planned >= MEASURE_SECONDS) {
      actual += stage.finished - stage.started;
      expected += planned;
    } else if (stage?.status === 'started' && stage.started !== undefined && planned >= MEASURE_SECONDS && (stage.fraction ?? 0) >= 0.1) {
      actual += (now - stage.started) / stage.fraction!;
      expected += planned;
    }
  }
  const speed = expected > 0 ? Math.min(4, Math.max(0.25, actual / expected)) : 1;
  let done = 0;
  let left = 0;
  for (const { id } of PIPELINE_STAGES) {
    const stage = progress.stages[id];
    const planned = (estimate[id] ?? 0) * speed;
    if (!stage) {
      left += planned;
    } else if (stage.status === 'completed') {
      done += (stage.finished ?? now) - (stage.started ?? now);
    } else if (stage.status === 'started') {
      const elapsed = Math.max(0, now - (stage.started ?? now));
      const byEstimate = Math.max(planned - elapsed, 0.1 * planned);
      const fraction = stage.fraction ?? 0;
      let remaining = byEstimate;
      if (fraction >= 0.03) {
        // Early on a stage's own rate is noisy (models still loading): lean on the estimate.
        const byRate = (elapsed * (1 - fraction)) / fraction;
        const trust = Math.min(1, fraction / 0.3);
        remaining = trust * byRate + (1 - trust) * byEstimate;
      }
      done += elapsed;
      left += remaining;
    }
  }
  const total = done + left;
  return total > 0 ? { fraction: done / total, seconds: left } : null;
}

/**
 * Whether this computer has measured anything to scale the estimate by: its
 * earlier runs, a finished stage of some length, or the running stage's rate.
 */
export function measured(progress: JobProgress): boolean {
  if (progress.learned) return true;
  return PIPELINE_STAGES.some(({ id }) => {
    const stage = progress.stages[id];
    if (stage?.status === 'completed') return (progress.estimate?.[id] ?? 0) >= MEASURE_SECONDS;
    return stage?.status === 'started' && (stage.fraction ?? 0) >= 0.03;
  });
}

export function leftText(seconds: number): string {
  if (seconds < 60) return 'less than a minute left';
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `about ${minutes} min left`;
  const hours = Math.floor(minutes / 60);
  return `about ${hours} h ${minutes - 60 * hours} min left`;
}

/** The stage to show beside an open log: the running one, else the failed or the last one. */
export function focusStage(progress: JobProgress): string {
  const ids = PIPELINE_STAGES.map(({ id }) => id);
  const having = (status: StageStatus) => ids.find((id) => progress.stages[id]?.status === status);
  return having('started') ?? having('failed') ?? [...ids].reverse().find((id) => progress.stages[id]) ?? ids[0]!;
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
  // The estimate moves with every event; the panel shows it every few seconds.
  const latest = useRef(progress);
  latest.current = progress;
  const [remaining, setRemaining] = useState<ReturnType<typeof timeLeft>>(null);
  const hasEstimate = running && progress.estimate !== undefined;
  const isMeasured = hasEstimate && measured(progress);
  useEffect(() => {
    if (!running) {
      setRemaining(null);
      return;
    }
    const update = () => setRemaining(timeLeft(latest.current, Date.now() / 1000));
    update();
    const timer = window.setInterval(update, 3000);
    return () => window.clearInterval(timer);
  }, [running, isMeasured]);
  const title =
    progress.status === 'running'
      ? 'Voice Cut is editing the recording'
      : progress.status === 'complete'
        ? 'Voice Cut finished'
        : progress.status === 'cancelled'
          ? 'Voice Cut was cancelled'
          : 'Voice Cut failed';

  return (
    <section className={`run-panel run-${progress.status}${showLog ? ' log-open' : ''}`} aria-live="polite">
      <header>
        <div>
          <h2>{title}</h2>
          <span className="muted">
            {duration(Math.max(0, elapsed))}
            {remaining
              ? ` · ${Math.round(100 * remaining.fraction)}% · ${leftText(remaining.seconds)}`
              : hasEstimate
                ? ' · estimating the time left…'
                : ''}
          </span>
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
      {remaining && (
        <div className="run-bar" aria-hidden="true">
          <span style={{ width: `${Math.round(100 * remaining.fraction)}%` }} />
        </div>
      )}
      <ol className="stages">
        {/* An open log needs the room: only the stage that matters stays. */}
        {(showLog ? PIPELINE_STAGES.filter(({ id }) => id === focusStage(progress)) : PIPELINE_STAGES).map(({ id, label }) => {
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
                      ? `${stage?.status === 'started' && stage.fraction !== undefined ? `${Math.round(100 * stage.fraction)}% · ` : ''}${duration(Math.max(0, seconds))}`
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
        {showLog ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Log ({progress.logs.length}{' '}
        {progress.logs.length === 1 ? 'line' : 'lines'})
      </button>
      {showLog && (
        <pre className="log">{progress.logs.length ? progress.logs.slice(-200).join('\n') : 'Nothing logged yet.'}</pre>
      )}
    </section>
  );
}
