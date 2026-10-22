/**
 * The editor inside the VoiceCut desktop app.
 *
 * The app (Tauri) shows this same frontend in native windows, one per `.vc`
 * document, and exposes itself as `window.__TAURI__`.  This module is the
 * only place that talks to it: native dialogs and file panels, the app's menu
 * commands, recent documents.  In a browser `desktop` is false and every
 * helper falls back to what a web page can do.
 */

interface TauriEvent<T> {
  payload: T;
}

interface Tauri {
  core: { invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> };
  event: { listen<T>(event: string, handler: (event: TauriEvent<T>) => void): Promise<() => void> };
}

declare global {
  interface Window {
    __TAURI__?: Tauri;
  }
}

export const desktop = typeof window !== 'undefined' && window.__TAURI__ !== undefined;

function tauri(): Tauri {
  if (!window.__TAURI__) throw new Error('not running in the VoiceCut app');
  return window.__TAURI__;
}

export function invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  return tauri().core.invoke<T>(command, args);
}

/** Subscribe to an app event; returns the unsubscribe function. */
export function listen<T>(event: string, handler: (payload: T) => void): () => void {
  if (!desktop) return () => undefined;
  let stop: (() => void) | null = null;
  let stopped = false;
  void tauri()
    .event.listen<T>(event, (message) => handler(message.payload))
    .then((unlisten) => {
      if (stopped) unlisten();
      else stop = unlisten;
    });
  return () => {
    stopped = true;
    stop?.();
  };
}

/** A command from the app's menu (or the app itself, like `flush`). */
export interface MenuCommand {
  command: string;
  token?: number;
  source?: string | null;
}

export function onMenu(handler: (command: MenuCommand) => void): () => void {
  return listen<MenuCommand>('menu', handler);
}

/** `window.confirm`, as a native sheet in the app. */
export function confirmAction(message: string, options: { title?: string; ok?: string } = {}): Promise<boolean> {
  if (!desktop) return Promise.resolve(window.confirm(message));
  return invoke<boolean>('confirm_dialog', { message, title: options.title ?? null, ok: options.ok ?? null });
}

export interface RecentDocument {
  path: string;
  name: string;
  exists: boolean;
}

/** Tell the app this window shows a document (title, Open Recent). */
export function documentOpened(projectId: string, path: string, name: string): void {
  if (desktop) void invoke('document_opened', { projectId, path, name });
}

export function openExternal(url: string): void {
  if (desktop) void invoke('open_external', { url });
  else window.open(url, '_blank', 'noreferrer');
}

/** The text field (if any) that edit commands like Undo should act on. */
export function editableTarget(): HTMLElement | null {
  const element = document.activeElement as HTMLElement | null;
  if (!element) return null;
  if (element.tagName === 'TEXTAREA' || element.isContentEditable) return element;
  if (element.tagName === 'INPUT') {
    const type = (element as HTMLInputElement).type;
    return ['text', 'password', 'search', 'url', 'email', 'number'].includes(type) ? element : null;
  }
  return null;
}

/** Run an Edit menu command on the focused text field, if there is one. */
export function editText(command: 'undo' | 'redo' | 'select-all'): boolean {
  const target = editableTarget();
  if (!target) return false;
  // execCommand is deprecated but remains the way to drive WebKit's own
  // undo stack and selection for form fields.
  document.execCommand(command === 'select-all' ? 'selectAll' : command);
  return true;
}

if (desktop) {
  // Links meant for a new tab open in the user's browser.
  document.addEventListener('click', (event) => {
    const link = (event.target as HTMLElement | null)?.closest?.('a[target="_blank"]') as HTMLAnchorElement | null;
    if (link && /^https?:/.test(link.href)) {
      event.preventDefault();
      openExternal(link.href);
    }
  });
}
