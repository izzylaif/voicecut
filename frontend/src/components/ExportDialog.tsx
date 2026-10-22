import { useEffect, useMemo, useState } from 'react';
import { Check, Download, FolderOpen, Loader2, X } from 'lucide-react';
import { api, ApiError, followJob } from '../api';
import { duration } from '../format';
import type { EditorStore } from '../store';
import type { ExportRecord } from '../types';
import { reduceProgress, startProgress, type JobProgress } from './RunPanel';

const AUDIO_FORMATS = [
  { extension: '.wav', label: 'WAV (24-bit)' },
  { extension: '.flac', label: 'FLAC' },
  { extension: '.m4a', label: 'AAC (.m4a)' },
  { extension: '.mp3', label: 'MP3' },
];
const VIDEO_FORMATS = [
  { extension: '.mp4', label: 'MP4 video (H.264)' },
  { extension: '.mov', label: 'MOV video (H.264)' },
];
const EXPORT_STAGES = [
  { id: 'plan', label: 'Deriving the edit' },
  { id: 'render', label: 'Rendering audio' },
  { id: 'publish', label: 'Writing the file' },
];

function withExtension(path: string, extension: string): string {
  const slash = path.lastIndexOf('/');
  const dot = path.lastIndexOf('.');
  return (dot > slash ? path.slice(0, dot) : path) + extension;
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

export function ExportDialog({ store, onClose }: { store: EditorStore; onClose: () => void }) {
  const project = store.project;
  const formats = useMemo(
    () => (project.media.kind === 'video' ? [...VIDEO_FORMATS, ...AUDIO_FORMATS] : AUDIO_FORMATS),
    [project.media.kind],
  );
  const initialExtension = (() => {
    const match = /\.[^./]+$/.exec(project.default_export_path)?.[0]?.toLowerCase() ?? '';
    return formats.some((format) => format.extension === match) ? match : formats[0]!.extension;
  })();
  const [extension, setExtension] = useState(initialExtension);
  const [destination, setDestination] = useState(withExtension(project.default_export_path, initialExtension));
  const [overwrite, setOverwrite] = useState(false);
  const [exists, setExists] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<JobProgress | null>(null);
  const [now, setNow] = useState(Date.now() / 1000);
  const record = progress?.result as ExportRecord | undefined;

  useEffect(() => {
    if (progress?.status !== 'running') return;
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 300);
    return () => window.clearInterval(timer);
  }, [progress?.status]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && progress?.status !== 'running') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, progress?.status]);

  const start = async () => {
    setError(null);
    try {
      const { job_id } = await api.startExport(store.id, {
        timeline: store.wireTimeline(),
        destination: destination.trim(),
        overwrite,
      });
      setProgress(startProgress(job_id));
      followJob(job_id, (event) => {
        setProgress((current) => (current ? reduceProgress(current, event) : current));
        if (event.type === 'complete') api.project(store.id).then((state) => store.updateProject(state));
      });
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409) setExists(true);
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  };

  const running = progress?.status === 'running';
  const done = progress?.status === 'complete' && record;

  return (
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && !running && onClose()}>
      <div className="modal" role="dialog" aria-label="Export">
        <header>
          <h2>Export</h2>
          {!running && (
            <button type="button" className="icon-button" title="Close" onClick={onClose}>
              <X size={16} />
            </button>
          )}
        </header>

        {!progress && (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void start();
            }}
          >
            <label className="field">
              <span>Format</span>
              <select
                value={extension}
                onChange={(event) => {
                  setExtension(event.target.value);
                  setDestination((path) => withExtension(path, event.target.value));
                  setExists(false);
                }}
              >
                {formats.map((format) => (
                  <option key={format.extension} value={format.extension}>
                    {format.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Save to</span>
              <input
                value={destination}
                spellCheck={false}
                onChange={(event) => {
                  setDestination(event.target.value);
                  setExists(false);
                }}
              />
            </label>
            {(exists || overwrite) && (
              <label className="checkbox">
                <input type="checkbox" checked={overwrite} onChange={(event) => setOverwrite(event.target.checked)} />
                Replace the existing file
              </label>
            )}
            <p className="muted small">
              {store.isEdited
                ? 'Your edit is rendered from the original recording with Voice Cut’s own renderer: its crossfades and pauses are kept wherever you left them alone, and pauses you resized or added are filled with the recording’s own room tone.'
                : 'Unchanged Voice Cut edit.'}{' '}
              The file has exactly the sound you hear in the preview.
            </p>
            {error && <p className="error-text">{error}</p>}
            <footer>
              <button type="button" className="button subtle" onClick={onClose}>
                Cancel
              </button>
              <button type="submit" className="button primary" disabled={!destination.trim()}>
                Export
              </button>
            </footer>
          </form>
        )}

        {progress && (
          <div className="export-progress">
            <ol className="stages">
              {EXPORT_STAGES.map(({ id, label }) => {
                const stage = progress.stages[id];
                const skipped = progress.status === 'complete' && !stage;
                return (
                  <li key={id} className={`stage stage-${stage?.status ?? (skipped ? 'skipped' : 'pending')}`}>
                    {stage?.status === 'started' ? (
                      <Loader2 size={14} className="spin" />
                    ) : stage?.status === 'completed' ? (
                      <Check size={14} />
                    ) : stage?.status === 'failed' ? (
                      <X size={14} />
                    ) : (
                      <span className="stage-dot" />
                    )}
                    <span className="stage-label">{label}</span>
                    <span className="stage-time">
                      {skipped
                        ? 'not needed'
                        : stage?.started !== undefined
                          ? duration(Math.max(0, (stage.finished ?? now) - stage.started))
                          : ''}
                    </span>
                  </li>
                );
              })}
            </ol>
            {progress.status === 'failed' && <pre className="error-detail">{progress.error}</pre>}
            {done && (
              <div className="export-done">
                <p>
                  <strong>Saved</strong> {record.output_path}
                </p>
                <p className="muted small">
                  {duration(record.duration_seconds)}
                  {record.reused_voicecut_output
                    ? ' · Voice Cut’s own file, unchanged'
                    : ` · ${plural(record.manual_boundaries, 'manual edge')} · ${record.kept_pauses} of ${record.kept_pauses + record.dropped_pauses} Voice Cut pauses kept${record.inserted_pauses ? ` · ${plural(record.inserted_pauses, 'pause')} added` : ''}`}
                </p>
              </div>
            )}
            <footer>
              {done && (
                <>
                  <button type="button" className="button subtle" onClick={() => void api.revealExport(store.id, record.export_id)}>
                    <FolderOpen size={14} /> Show in Finder
                  </button>
                  <a className="button subtle" href={api.downloadUrl(store.id, record.export_id)} download>
                    <Download size={14} /> Download
                  </a>
                </>
              )}
              {progress.status === 'failed' && (
                <button type="button" className="button subtle" onClick={() => setProgress(null)}>
                  Back
                </button>
              )}
              <button type="button" className="button primary" disabled={running} onClick={onClose}>
                {running ? 'Exporting…' : 'Done'}
              </button>
            </footer>
          </div>
        )}
      </div>
    </div>
  );
}
