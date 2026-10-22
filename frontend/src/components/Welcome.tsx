import { useCallback, useEffect, useRef, useState } from 'react';
import { FileAudio, FileVideo, FolderOpen, Loader2, Trash2, Upload } from 'lucide-react';
import { api } from '../api';
import type { ProjectState, ProjectSummary } from '../types';

const ACCEPT = '.wav,.mp3,.m4a,.flac,.aac,.aif,.aiff,.ogg,.oga,.opus,.mov,.mp4,.mkv,.webm';

export function Welcome({ onOpen }: { onOpen: (project: ProjectState) => void }) {
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [path, setPath] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [upload, setUpload] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const refresh = useCallback(() => {
    api.projects().then(setProjects, (failure: unknown) => setError(String(failure)));
  }, []);
  useEffect(refresh, [refresh]);

  const run = async (label: string, action: () => Promise<ProjectState>) => {
    setError(null);
    setBusy(label);
    try {
      onOpen(await action());
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(null);
      setUpload(null);
    }
  };

  const importFile = (file: File) =>
    run(`Importing ${file.name}…`, () =>
      api.importFile(file, (fraction) => {
        setUpload(fraction);
        if (fraction >= 1) setBusy('Reading the recording…');
      }),
    );

  const open = (id: string) => run('Opening…', () => api.project(id));

  return (
    <div className="welcome">
      <div className="welcome-card">
        <h1>
          Voice<span>Cut</span> editor
        </h1>
        <p className="muted">
          Open a narration recording, let Voice Cut remove the retakes and fix the pauses, then fine-tune every cut on
          the timeline and export.
        </p>

        <div
          className={`dropzone${dragging ? ' dragging' : ''}`}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            const file = event.dataTransfer.files[0];
            if (file) void importFile(file);
          }}
        >
          {busy ? (
            <>
              <Loader2 className="spin" size={28} />
              <p>{busy}</p>
              {upload !== null && upload < 1 && (
                <div className="meter">
                  <div style={{ width: `${Math.round(upload * 100)}%` }} />
                </div>
              )}
            </>
          ) : (
            <>
              <Upload size={28} />
              <p>Drop an audio or video file here</p>
              <button type="button" className="button primary" onClick={() => input.current?.click()}>
                Choose a file…
              </button>
            </>
          )}
          <input
            ref={input}
            type="file"
            accept={ACCEPT}
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = '';
              if (file) void importFile(file);
            }}
          />
        </div>

        <form
          className="path-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (path.trim()) void run('Opening…', () => api.importPath(path.trim()));
          }}
        >
          <input
            value={path}
            placeholder="…or open a file in place: /Users/you/Recordings/take.wav"
            spellCheck={false}
            onChange={(event) => setPath(event.target.value)}
          />
          <button type="submit" className="button" disabled={!path.trim() || busy !== null}>
            <FolderOpen size={14} /> Open
          </button>
        </form>
        {error && <p className="error-text">{error}</p>}

        {projects && projects.length > 0 && (
          <div className="recent">
            <h2>Recent projects</h2>
            <ul>
              {projects.map((project) => (
                <li key={project.id}>
                  <button type="button" className="recent-open" onClick={() => void open(project.id)} disabled={busy !== null}>
                    {project.kind === 'video' ? <FileVideo size={16} /> : <FileAudio size={16} />}
                    <span className="recent-name">{project.name}</span>
                    <span className={`badge badge-${project.run_status}`}>
                      {project.run_status === 'complete'
                        ? 'edited'
                        : project.run_status === 'idle'
                          ? 'not cut yet'
                          : project.run_status}
                    </span>
                    <span className="muted small">{new Date(project.created_at).toLocaleString()}</span>
                  </button>
                  <button
                    type="button"
                    className="icon-button"
                    title="Delete this project (the original file is kept)"
                    onClick={() => {
                      if (window.confirm(`Delete the project “${project.name}”? The original file is not touched.`)) {
                        api.deleteProject(project.id).then(refresh, (failure: unknown) => setError(String(failure)));
                      }
                    }}
                  >
                    <Trash2 size={14} />
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
