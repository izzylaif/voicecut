import { useEffect, useState } from 'react';
import { ExternalLink, KeyRound, Loader2, X } from 'lucide-react';
import { api, ApiError } from '../api';
import type { PlannerInfo, RunSettings, SettingsState } from '../types';

/** Settings: API keys of the cloud planners, and Voice Cut options. */
export function SettingsDialog({
  state,
  settings,
  onState,
  onSettings,
  onClose,
}: {
  state: SettingsState;
  settings: RunSettings;
  onState: (state: SettingsState) => void;
  onSettings: (settings: RunSettings) => void;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="modal settings-modal" role="dialog" aria-label="Settings">
        <header>
          <h2>Settings</h2>
          <button type="button" className="icon-button" title="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <section className="settings-section">
          <h3>API keys</h3>
          <p className="muted small">
            Keys are saved in <code>{state.env_file}</code> on this computer — the file the <code>voicecut</code> command
            reads — and are sent only to their provider.
          </p>
          {state.planners
            .filter((planner) => planner.key_env)
            .map((planner) => (
              <KeyRow key={planner.id} planner={planner} onState={onState} />
            ))}
        </section>

        <section className="settings-section">
          <h3>Voice Cut</h3>
          <label className="field">
            <span>Breaths in pauses</span>
            <select
              value={settings.breath_cleanup ?? ''}
              onChange={(event) =>
                onSettings({ ...settings, breath_cleanup: (event.target.value || null) as RunSettings['breath_cleanup'] })
              }
            >
              <option value="">Language default</option>
              <option value="replace">Replace with room tone</option>
              <option value="off">Keep them</option>
            </select>
          </label>
          <p className="muted small">The planner, its model and the language are chosen in the top bar.</p>
        </section>

        <footer>
          <button type="button" className="button primary" onClick={onClose}>
            Done
          </button>
        </footer>
      </div>
    </div>
  );
}

function KeyRow({ planner, onState }: { planner: PlannerInfo; onState: (state: SettingsState) => void }) {
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; tone: 'ok' | 'error' } | null>(null);
  const key = planner.key;

  const save = async (next: string) => {
    setBusy(true);
    setMessage(null);
    try {
      onState(await api.saveKey(planner.id, next));
      setValue('');
      setMessage({ text: next ? 'Saved — the key works' : 'Removed', tone: 'ok' });
    } catch (error) {
      setMessage({ text: error instanceof ApiError ? error.message : String(error), tone: 'error' });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="key-row">
      <div className="key-head">
        <KeyRound size={14} />
        <strong>{planner.label}</strong>
        <code className="muted">{planner.key_env}</code>
        <span className={`key-state${key?.set ? ' set' : ''}`}>
          {key?.set ? `Saved ${key.masked}` : 'Not set'}
          {key?.source === 'environment' ? ' · from the environment' : ''}
        </span>
        {planner.key_page && (
          <a className="key-link" href={planner.key_page} target="_blank" rel="noreferrer">
            Get a key <ExternalLink size={12} />
          </a>
        )}
      </div>
      {key?.source === 'environment' && (
        <p className="muted small">
          The {planner.key_env} environment variable of the editor's process is set and takes precedence over the file.
        </p>
      )}
      <form
        className="key-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (value.trim()) void save(value.trim());
        }}
      >
        <input
          type="password"
          value={value}
          placeholder={key?.set ? 'Paste a new key to replace it' : `Paste your ${planner.label} API key`}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => setValue(event.target.value)}
        />
        <button type="submit" className="button" disabled={busy || !value.trim()}>
          {busy ? <Loader2 size={14} className="spin" /> : null} Save
        </button>
        {key?.set && key.source === 'env_file' && (
          <button
            type="button"
            className="button subtle"
            disabled={busy}
            onClick={() => {
              if (window.confirm(`Remove the ${planner.label} key from ${planner.key_env}?`)) void save('');
            }}
          >
            Remove
          </button>
        )}
      </form>
      {message && <p className={message.tone === 'ok' ? 'ok-text small' : 'error-text small'}>{message.text}</p>}
    </div>
  );
}
