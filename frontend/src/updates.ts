/**
 * Updates of the VoiceCut app (desktop only).
 *
 * The app looks for a newer release itself and tells every page what it found
 * (the `update_status` command, then `update` events).  Pages show a small
 * download button while there is one; clicking it has the app download it,
 * save the documents, install it and restart.  "Install updates
 * automatically" in Settings is the app's preference too.
 */
import { useSyncExternalStore } from 'react';
import { desktop, invoke, listen } from './desktop';

export interface UpdateStatus {
  /** The running version. */
  current: string;
  /** The newer version on offer, if any. */
  version: string | null;
  state: 'none' | 'available' | 'downloading' | 'ready' | 'installing';
  /** Download progress, 0 to 1, when the size is known. */
  progress: number | null;
  error: string | null;
  automatic: boolean;
}

/** What the update button shows, or null for no button. */
export interface UpdateButtonState {
  title: string;
  busy: boolean;
}

export function updateButton(status: UpdateStatus | null): UpdateButtonState | null {
  if (!status?.version || status.state === 'none') return null;
  switch (status.state) {
    case 'downloading': {
      const percent = status.progress === null ? '' : ` ${Math.floor(status.progress * 100)}%`;
      return { title: `Downloading VoiceCut ${status.version}…${percent}`, busy: true };
    }
    case 'installing':
      return { title: `Installing VoiceCut ${status.version}…`, busy: true };
    default:
      return { title: `Update available: ${status.version}`, busy: false };
  }
}

let status: UpdateStatus | null = null;
let started = false;
const listeners = new Set<() => void>();

function adopt(next: UpdateStatus): void {
  status = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  if (desktop && !started) {
    started = true;
    invoke<UpdateStatus>('update_status').then(adopt, () => undefined);
    listen<UpdateStatus>('update', adopt);
  }
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The app's update status; null in a browser or until the app answers. */
export function useUpdate(): UpdateStatus | null {
  return useSyncExternalStore(subscribe, () => status);
}

export function installUpdate(): void {
  if (desktop) invoke('install_update').catch(() => undefined);
}

export function setAutomaticUpdates(on: boolean): void {
  if (status) adopt({ ...status, automatic: on });
  if (desktop) invoke('set_automatic_updates', { on }).catch(() => undefined);
}
