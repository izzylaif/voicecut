import { useEffect, useState } from 'react';
import { api } from './api';
import { DesktopWelcome } from './components/DesktopWelcome';
import { Editor } from './components/Editor';
import { Welcome } from './components/Welcome';
import { desktop } from './desktop';
import type { ProjectState } from './types';

function projectInUrl(): string | null {
  return new URLSearchParams(window.location.search).get('project');
}

function setProjectInUrl(id: string | null): void {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set('project', id);
  else url.searchParams.delete('project');
  window.history.replaceState(null, '', url);
}

export function App() {
  const [project, setProject] = useState<ProjectState | null>(null);
  const [loading, setLoading] = useState(projectInUrl() !== null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .config()
      .catch(() =>
        setError(
          desktop
            ? 'Cannot reach VoiceCut’s engine. Quit and reopen VoiceCut.'
            : 'Cannot reach the VoiceCut editor server. Is voicecut-editor running?',
        ),
      );
    const id = projectInUrl();
    if (id) {
      api
        .project(id)
        .then(setProject, () => setProjectInUrl(null))
        .finally(() => setLoading(false));
    }
  }, []);

  if (loading) return <div className="loading">Opening…</div>;
  if (project) {
    return (
      <Editor
        key={project.id}
        initial={project}
        onClose={() => {
          setProject(null);
          setProjectInUrl(null);
        }}
      />
    );
  }
  return (
    <>
      {error && <div className="banner">{error}</div>}
      {desktop ? (
        <DesktopWelcome
          onOpen={(state) => {
            setProjectInUrl(state.id);
            setProject(state);
          }}
        />
      ) : (
        <Welcome
          onOpen={(state) => {
            setProjectInUrl(state.id);
            setProject(state);
          }}
        />
      )}
    </>
  );
}
