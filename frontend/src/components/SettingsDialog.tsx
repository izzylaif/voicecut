import { useEffect, useState, useSyncExternalStore } from 'react';
import {
  Cpu,
  Download,
  ExternalLink,
  KeyRound,
  Loader2,
  type LucideIcon,
  Monitor,
  Moon,
  Sun,
  Trash2,
  X,
} from 'lucide-react';
import { api, ApiError } from '../api';
import { confirmAction, desktop } from '../desktop';
import { localModelAction, pullText, sizeText, useLocalModels } from '../localModels';
import { type Appearance, appearance, setAppearance, subscribe } from '../theme';
import type { ModelInfo, PlannerInfo, RunSettings, SettingsState } from '../types';
import { setAutomaticUpdates, useUpdate } from '../updates';
import { downloadLocalModel, PullBar } from './VoiceCutControls';

const APPEARANCE_OPTIONS: { value: Appearance; label: string; icon: LucideIcon }[] = [
  { value: 'system', label: 'System', icon: Monitor },
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
];

/**
 * Settings: appearance, the desktop app's updates, API keys of the cloud
 * planners, the local planner's models, and (in a project) Voice Cut options.
 */
export function SettingsDialog({
  state,
  settings,
  onState,
  onSettings,
  onClose,
}: {
  state: SettingsState;
  settings?: RunSettings;
  onState: (state: SettingsState) => void;
  onSettings?: (settings: RunSettings) => void;
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
          <h3>Appearance</h3>
          <AppearancePicker />
          <p className="muted small">
            System follows the light or dark mode of your {desktop ? 'Mac' : 'computer'}, also while the editor is open.
          </p>
        </section>

        {desktop && <UpdatesSection />}

        <section className="settings-section">
          <h3>API keys</h3>
          {state.key_store && (state.key_store.kind !== 'env_file' || desktop) ? (
            <p className="muted small">
              Keys are kept in {state.key_store.location} on this computer and are sent only to their provider.
            </p>
          ) : (
            <p className="muted small">
              Keys are saved in <code>{state.env_file}</code> on this computer — the file the <code>voicecut</code>{' '}
              command reads — and are sent only to their provider.
            </p>
          )}
          {state.planners
            .filter((planner) => planner.key_env)
            .map((planner) => (
              <KeyRow key={planner.id} planner={planner} state={state} onState={onState} />
            ))}
        </section>

        {state.planners.some((planner) => planner.id === 'ollama') && <LocalModelsSection />}

        {settings && onSettings && (
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
        )}

        <footer>
          <button type="button" className="button primary" onClick={onClose}>
            Done
          </button>
        </footer>
      </div>
    </div>
  );
}

function AppearancePicker() {
  const current = useSyncExternalStore(subscribe, appearance);
  return (
    <div className="segmented" role="radiogroup" aria-label="Appearance">
      {APPEARANCE_OPTIONS.map(({ value, label, icon: Icon }) => (
        <button
          key={value}
          type="button"
          role="radio"
          aria-checked={current === value}
          className={current === value ? 'selected' : undefined}
          onClick={() => setAppearance(value)}
        >
          <Icon size={14} /> {label}
        </button>
      ))}
    </div>
  );
}

function UpdatesSection() {
  const update = useUpdate();
  if (!update) return null;
  return (
    <section className="settings-section">
      <h3>Updates</h3>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={update.automatic}
          onChange={(event) => setAutomaticUpdates(event.target.checked)}
        />
        Install updates automatically
      </label>
      <p className="muted small">
        This is VoiceCut {update.current}. It looks for a new version when it starts and every few hours;{' '}
        {update.automatic
          ? 'a new version downloads in the background and installs when you quit VoiceCut.'
          : 'when there is one, the download button in the toolbar installs it.'}
      </p>
    </section>
  );
}

function KeyRow({
  planner,
  state,
  onState,
}: {
  planner: PlannerInfo;
  state: SettingsState;
  onState: (state: SettingsState) => void;
}) {
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
          The {planner.key_env} environment variable of the editor's process is set and takes precedence over the saved
          key.
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
        {key?.set && key.source === (state.key_store?.kind ?? 'env_file') && (
          <button
            type="button"
            className="button subtle"
            disabled={busy}
            onClick={() => {
              const where = state.key_store?.kind === 'env_file' ? planner.key_env : state.key_store?.location;
              void confirmAction(`Remove the ${planner.label} key from ${where}?`, { ok: 'Remove' }).then((ok) => {
                if (ok) void save('');
              });
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

/**
 * The local planner: where Ollama runs, the models (download, delete) and
 * whether each one reasons before it answers.
 */
function LocalModelsSection() {
  const { list, error: actionError } = useLocalModels();
  const [name, setName] = useState('');
  const status = list?.ollama;
  const models = (list?.models ?? []).filter((model) => model.recommended || model.downloaded || model.pull);

  return (
    <section className="settings-section">
      <h3>Local models</h3>
      <p className="muted small">
        {status?.available
          ? `${status.source === 'voicecut' ? `Ollama ${status.version ?? ''}, installed by VoiceCut,` : `Your Ollama ${status.version ?? ''} (${status.url})`}` +
            ' runs these models on this computer; the recording never leaves it.'
          : (list?.error?.message ?? 'Looking for Ollama…')}{' '}
        A model that reasons first is slower and often more accurate.
      </p>
      {actionError && <p className="error-text small">{actionError}</p>}
      <ul className="local-models">
        {models.map((model) => (
          <LocalModelRow key={model.id} model={model} usable={Boolean(status?.available)} />
        ))}
      </ul>
      {status?.available && (
        <form
          className="key-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!name.trim()) return;
            void downloadLocalModel(name.trim()).then((started) => started && setName(''));
          }}
        >
          <input
            value={name}
            placeholder="Another model from ollama.com/library, e.g. qwen3:32b"
            spellCheck={false}
            onChange={(event) => setName(event.target.value)}
          />
          <button type="submit" className="button" disabled={!name.trim() || /\s/.test(name.trim())}>
            <Download size={14} /> Download
          </button>
        </form>
      )}
    </section>
  );
}

function LocalModelRow({ model, usable }: { model: ModelInfo; usable: boolean }) {
  const pulling = model.pull && !model.pull.done;
  return (
    <li className="local-model">
      <div className="local-model-head">
        <Cpu size={14} />
        <strong>{model.label}</strong>
        {model.label !== model.id && <code className="muted">{model.id}</code>}
        {model.engine && <span className="badge">{model.engine}</span>}
        {model.default && <span className="badge">default</span>}
        <span className={`key-state${model.downloaded ? ' set' : ''}`}>{stateText(model)}</span>
        <span className="local-model-actions">
          {pulling ? (
            <button type="button" className="button subtle" onClick={() => void localModelAction('cancel', model.id)}>
              Stop
            </button>
          ) : model.downloaded ? (
            <button
              type="button"
              className="icon-button"
              title={`Delete ${model.id} from this computer`}
              onClick={() =>
                void confirmAction(`Delete ${model.id}${model.size_gb ? ` (${sizeText(model)})` : ''} from this computer?`, {
                  ok: 'Delete',
                }).then((ok) => ok && localModelAction('delete', model.id))
              }
            >
              <Trash2 size={14} />
            </button>
          ) : (
            usable && (
              <button type="button" className="button" onClick={() => void downloadLocalModel(model)}>
                <Download size={14} /> Download
              </button>
            )
          )}
          {model.pull?.error && !pulling && (
            <button type="button" className="button subtle" onClick={() => void localModelAction('cancel', model.id)}>
              Dismiss
            </button>
          )}
        </span>
      </div>
      {pulling && <PullBar fraction={model.pull?.fraction ?? null} />}
      {model.can_think !== false && (
        <label className="checkbox small">
          <input
            type="checkbox"
            checked={Boolean(model.think)}
            disabled={!usable}
            onChange={(event) => void localModelAction('think', model.id, { think: event.target.checked })}
          />
          Reason before answering
        </label>
      )}
    </li>
  );
}

function stateText(model: ModelInfo): string {
  if (model.pull) return pullText(model);
  const size = sizeText(model);
  if (model.downloaded) return size ? `Downloaded · ${size}` : 'Downloaded';
  return size ? `${size} download` : 'Not downloaded';
}
