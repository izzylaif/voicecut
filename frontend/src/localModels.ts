/**
 * Local (Ollama) models, shared by the model picker and Settings: the list,
 * downloads in progress (polled while one runs) and each model's reasoning.
 */
import { useEffect, useSyncExternalStore } from 'react';
import { api, ApiError } from './api';
import type { ModelInfo, ModelList } from './types';

export interface LocalModelsState {
  list: ModelList | null;
  loading: boolean;
  /** The last action that failed. */
  error: string | null;
}

const POLL_MS = 1000;
let state: LocalModelsState = { list: null, loading: false, error: null };
const listeners = new Set<() => void>();
let timer: number | undefined;

function set(change: Partial<LocalModelsState>): void {
  state = { ...state, ...change };
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function adopt(list: ModelList): void {
  set({ list, error: null });
  window.clearTimeout(timer);
  if (list.models.some((model) => model.pull && !model.pull.done)) {
    timer = window.setTimeout(() => void refreshLocalModels(), POLL_MS);
  }
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

export async function refreshLocalModels(): Promise<void> {
  set({ loading: true });
  try {
    adopt(await api.models('ollama'));
  } catch (error) {
    set({ error: message(error) });
  } finally {
    set({ loading: false });
  }
}

/** Run an action on a model; the list that comes back replaces the shown one. */
export async function localModelAction(
  action: 'pull' | 'cancel' | 'delete' | 'think',
  model: string,
  extra: { think?: boolean } = {},
): Promise<boolean> {
  try {
    adopt(await api.localModel(action, model, extra));
    return true;
  } catch (error) {
    set({ error: message(error) });
    return false;
  }
}

/** The local models, loaded when a component first shows them. */
export function useLocalModels(active = true): LocalModelsState {
  const current = useSyncExternalStore(subscribe, () => state);
  useEffect(() => {
    if (active) void refreshLocalModels();
  }, [active]);
  return current;
}

export function findLocalModel(id: string): ModelInfo | undefined {
  return state.list?.models.find((model) => model.id === id);
}

export function sizeText(model: ModelInfo): string {
  return model.size_gb ? `${model.size_gb.toFixed(1)} GB` : '';
}

export function pullText(model: ModelInfo): string {
  const pull = model.pull;
  if (!pull) return '';
  if (pull.error) return pull.error === 'cancelled' ? 'Download cancelled' : `Download failed: ${pull.error}`;
  if (pull.fraction === null) return pull.status === 'pulling manifest' ? 'Starting the download…' : `${pull.status}…`;
  const total = pull.total / 1e9;
  return `Downloading ${Math.floor(pull.fraction * 100)}% of ${total.toFixed(1)} GB`;
}
