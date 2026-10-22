import { useCallback, useEffect, useState } from 'react';
import { FileAudio, FilePlus2, FolderOpen, Loader2, Settings, Upload } from 'lucide-react';
import { api } from '../api';
import { invoke, listen, onMenu, type MenuCommand, type RecentDocument } from '../desktop';
import { errorText } from '../store';
import type { ProjectState, SettingsState } from '../types';
import { SettingsDialog } from './SettingsDialog';

function fileName(path: string): string {
  return path.slice(path.lastIndexOf('/') + 1);
}

/**
 * The welcome window of the desktop app: start a project from a recording
 * (it becomes a `.vc` document), open one, or pick a recent one.  Documents
 * opened from here replace this page; the app navigates the window.
 */
export function DesktopWelcome({ onOpen }: { onOpen: (project: ProjectState) => void }) {
  const [recents, setRecents] = useState<RecentDocument[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [settings, setSettings] = useState<SettingsState | null>(null);

  useEffect(() => {
    invoke<RecentDocument[]>('recent_documents').then(setRecents, () => undefined);
  }, []);

  /** New project: the app asks for the recording and where to save it. */
  const create = useCallback(
    async (source: string | null) => {
      setError(null);
      try {
        const choice = await invoke<{ source: string; path: string } | null>('choose_new_project', { source });
        if (!choice) return;
        setBusy(`Reading ${fileName(choice.source)}…`);
        onOpen(await api.createDocument(choice.source, choice.path));
      } catch (failure) {
        setError(errorText(failure));
      } finally {
        setBusy(null);
      }
    },
    [onOpen],
  );

  const open = async (path: string | null) => {
    setError(null);
    try {
      if (path) setBusy('Opening…');
      await invoke<boolean>('open_document', { path });
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(null);
    }
  };

  const openSettings = useCallback(() => {
    api.settings().then(setSettings, (failure: unknown) => setError(errorText(failure)));
  }, []);

  useEffect(
    () =>
      onMenu((message: MenuCommand) => {
        if (message.command === 'new-project') void create(message.source ?? null);
        else if (message.command === 'settings') openSettings();
      }),
    [create, openSettings],
  );
  useEffect(() => listen<{ active: boolean }>('drag', (payload) => setDragging(payload.active)), []);
  // A recording opened with VoiceCut before this window existed.
  useEffect(() => {
    invoke<MenuCommand | null>('take_pending_action').then((action) => {
      if (action?.command === 'new-project') void create(action.source ?? null);
    }, () => undefined);
  }, [create]);

  return (
    <div className="welcome">
      <div className="welcome-card">
        <h1>
          Voice<span>Cut</span>
        </h1>
        <p className="muted">
          Start a project from a narration recording: Voice Cut removes the retakes and fixes the pauses, then you
          fine-tune every cut on the timeline and export. Each project is saved as one .vc document.
        </p>

        <div className={`dropzone${dragging ? ' dragging' : ''}`}>
          {busy ? (
            <>
              <Loader2 className="spin" size={28} />
              <p>{busy}</p>
            </>
          ) : (
            <>
              <Upload size={28} />
              <p>Drop a recording here to start a project, or a .vc project to open it</p>
              <div className="welcome-actions">
                <button type="button" className="button primary" onClick={() => void create(null)}>
                  <FilePlus2 size={14} /> New Project…
                </button>
                <button type="button" className="button" onClick={() => void open(null)}>
                  <FolderOpen size={14} /> Open…
                </button>
              </div>
            </>
          )}
        </div>
        {error && <p className="error-text">{error}</p>}

        {recents.length > 0 && (
          <div className="recent">
            <h2>Recent projects</h2>
            <ul>
              {recents.map((recent) => (
                <li key={recent.path}>
                  <button
                    type="button"
                    className="recent-open"
                    disabled={!recent.exists || busy !== null}
                    title={recent.path}
                    onClick={() => void open(recent.path)}
                  >
                    <FileAudio size={16} />
                    <span className="recent-name">{recent.name}</span>
                    <span className="muted small recent-path">
                      {recent.exists ? recent.path.replace(/^\/Users\/[^/]+/, '~') : 'moved or deleted'}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        <p className="welcome-footer">
          <button type="button" className="button subtle" onClick={openSettings}>
            <Settings size={14} /> Settings…
          </button>
        </p>
      </div>
      {settings && (
        <SettingsDialog state={settings} onState={setSettings} onClose={() => setSettings(null)} />
      )}
    </div>
  );
}
