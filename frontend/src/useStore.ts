import { useSyncExternalStore } from 'react';
import type { EditorStore } from './store';

/** Re-render when the editor state changes; returns its version number. */
export function useStore(store: EditorStore): number {
  return useSyncExternalStore(store.subscribe, () => store.version);
}
