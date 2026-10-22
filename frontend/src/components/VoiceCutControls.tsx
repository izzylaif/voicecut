import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AudioLines, Check, ChevronDown, FileText, KeyRound, Loader2, RefreshCw } from 'lucide-react';
import { api } from '../api';
import type { ModelList, PlannerInfo, RunSettings, SettingsState } from '../types';

const LANGUAGE_NAMES: Record<string, string> = { en: 'English', ru: 'Русский' };

/** The planner shown for a project's setting (qwen/gemma are local aliases). */
export function plannerOf(settings: RunSettings, planners: PlannerInfo[]): PlannerInfo | undefined {
  const id = settings.planner_backend === 'qwen' || settings.planner_backend === 'gemma' ? 'local' : settings.planner_backend;
  return planners.find((planner) => planner.id === id);
}

/** The model a run will use: the chosen one or the planner's default. */
export function modelOf(settings: RunSettings, state: SettingsState | null): string {
  return settings.planner_model ?? state?.default_models[settings.planner_backend] ?? '';
}

/**
 * Quick Voice Cut choices in the top bar: the planner provider, its model
 * (listed live from the provider, or any name typed in) and the language.
 */
export function VoiceCutControls({
  settings,
  state,
  disabled,
  onChange,
  onOpenSettings,
}: {
  settings: RunSettings;
  state: SettingsState | null;
  disabled: boolean;
  onChange: (settings: RunSettings) => void;
  onOpenSettings: () => void;
}) {
  const planners = state?.planners ?? [];
  const planner = plannerOf(settings, planners);
  const languages = state?.languages ?? [settings.language];

  return (
    <div className="voicecut-controls">
      <label className="control" title="Which model chooses the takes">
        <span className="control-label">Planner</span>
        <select
          value={planner?.id ?? settings.planner_backend}
          disabled={disabled}
          onChange={(event) => onChange({ ...settings, planner_backend: event.target.value, planner_model: null })}
        >
          {planners.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
              {item.key_env && !item.key?.set ? ' — no key' : ''}
            </option>
          ))}
        </select>
      </label>
      {planner && (
        <span
          className={`listens${planner.listens_to_audio ? ' yes' : ''}`}
          title={
            planner.listens_to_audio
              ? 'This planner listens to the audio of every window as well as reading the transcript'
              : 'This planner reads the transcript only; Gemini also listens to the audio'
          }
        >
          {planner.listens_to_audio ? <AudioLines size={13} /> : <FileText size={13} />}
          {planner.listens_to_audio ? 'hears audio' : 'text only'}
        </span>
      )}
      {planner && (
        <ModelPicker
          planner={planner}
          value={modelOf(settings, state)}
          disabled={disabled}
          onChange={(model) =>
            onChange({ ...settings, planner_backend: planner.id, planner_model: model === planner.default_model ? null : model })
          }
          onOpenSettings={onOpenSettings}
        />
      )}
      <label className="control" title="Language of the recording">
        <span className="control-label">Language</span>
        <select
          value={settings.language}
          disabled={disabled}
          onChange={(event) => onChange({ ...settings, language: event.target.value })}
        >
          {languages.map((language) => (
            <option key={language} value={language}>
              {LANGUAGE_NAMES[language] ?? language}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

function ModelPicker({
  planner,
  value,
  disabled,
  onChange,
  onOpenSettings,
}: {
  planner: PlannerInfo;
  value: string;
  disabled: boolean;
  onChange: (model: string) => void;
  onOpenSettings: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [list, setList] = useState<ModelList | null>(null);
  const [loading, setLoading] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const keyStamp = `${planner.id}:${planner.key?.masked ?? ''}:${planner.key?.source ?? ''}`;

  const load = useCallback(
    (refresh: boolean) => {
      setLoading(true);
      api
        .models(planner.id, refresh)
        .then(setList, (error: unknown) =>
          setList({ models: [], default: planner.default_model, error: { kind: 'network', message: String(error) } }),
        )
        .finally(() => setLoading(false));
    },
    [planner.id, planner.default_model],
  );

  // Fetch the list for this planner (again when its key changes).
  useEffect(() => {
    setList(null);
    load(false);
  }, [keyStamp, load]);

  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    const close = (event: PointerEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === 'Escape' : !root.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener('pointerdown', close);
    window.addEventListener('keydown', close);
    return () => {
      window.removeEventListener('pointerdown', close);
      window.removeEventListener('keydown', close);
    };
  }, [open]);

  const models = list?.models ?? [];
  const current = models.find((model) => model.id === value);
  const typed = query.trim();
  const shown = useMemo(() => {
    const needle = typed.toLowerCase();
    return needle
      ? models.filter((model) => model.id.toLowerCase().includes(needle) || model.label.toLowerCase().includes(needle))
      : models;
  }, [models, typed]);
  const choose = (model: string) => {
    onChange(model);
    setOpen(false);
    setQuery('');
  };
  const error = list?.error;

  return (
    <div className="model-picker" ref={root}>
      <button
        type="button"
        className={`model-button${error && error.kind !== 'network' ? ' warning' : ''}`}
        disabled={disabled}
        title={value}
        onClick={() => setOpen((state) => !state)}
      >
        <span className="model-name">{current?.label ?? value}</span>
        <ChevronDown size={14} />
      </button>
      {open && (
        <div className="popover model-menu" role="dialog" aria-label={`${planner.label} models`}>
          <div className="model-search">
            <input
              ref={search}
              value={query}
              placeholder="Search or type a model name"
              spellCheck={false}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key !== 'Enter' || !typed) return;
                const exact = models.find((model) => model.id === typed)?.id;
                if (exact || !/\s/.test(typed)) choose(exact ?? typed);
                else if (shown[0]) choose(shown[0].id);
              }}
            />
            <button
              type="button"
              className="icon-button"
              title="Fetch the list again"
              disabled={loading}
              onClick={() => load(true)}
            >
              {loading ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
            </button>
          </div>
          {error && (
            <div className={`model-status model-status-${error.kind}`}>
              <p>{error.message}</p>
              {(error.kind === 'missing_key' || error.kind === 'invalid_key') && (
                <button
                  type="button"
                  className="button subtle"
                  onClick={() => {
                    setOpen(false);
                    onOpenSettings();
                  }}
                >
                  <KeyRound size={14} /> Open Settings
                </button>
              )}
            </div>
          )}
          <ul className="model-list">
            {typed && !/\s/.test(typed) && !models.some((model) => model.id === typed) && (
              <li>
                <button type="button" onClick={() => choose(typed)}>
                  <span className="model-id">Use “{typed}”</span>
                  <span className="model-detail">a model name that is not in the list</span>
                </button>
              </li>
            )}
            {shown.map((model) => (
              <li key={model.id}>
                <button type="button" className={model.id === value ? 'selected' : ''} onClick={() => choose(model.id)}>
                  <span className="model-id">
                    {model.label}
                    {model.default && <span className="badge">default</span>}
                  </span>
                  <span className="model-detail">
                    {model.id}
                    {model.detail ? ` · ${model.detail}` : ''}
                  </span>
                  {model.id === value && <Check size={14} className="model-check" />}
                </button>
              </li>
            ))}
            {!error && !loading && models.length === 0 && <li className="model-empty">No models listed</li>}
          </ul>
          <p className="model-footnote">
            {planner.id === 'local'
              ? 'Any MLX-LM model from Hugging Face works; type its name.'
              : `Listed live from ${planner.label}. Type any other model name to use it.`}
          </p>
        </div>
      )}
    </div>
  );
}
