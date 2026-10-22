/**
 * Light and dark appearance.
 *
 * The user picks System (the default), Light or Dark in Settings.  System
 * follows the operating system's `prefers-color-scheme`, live: switching macOS
 * to dark mode while the editor is open switches the editor too.  The resolved
 * theme is `<html data-theme>`, which styles.css keys its colours on; the
 * timeline canvas takes its palette from `onThemeChange`.
 *
 * The choice belongs to the user, not to a project.  In a browser it is kept
 * in localStorage (index.html reads it before the first paint, so pages never
 * flash the wrong theme).  In the VoiceCut app the app keeps it: the commands
 * `appearance` / `set_appearance` and the `appearance` event let it theme its
 * native windows and remember the choice across launches.
 */
import { desktop, invoke, listen } from './desktop';

export type Appearance = 'system' | 'light' | 'dark';
export type Theme = 'light' | 'dark';

export const APPEARANCES: readonly Appearance[] = ['system', 'light', 'dark'];

/** localStorage key; index.html reads the same key. */
export const APPEARANCE_KEY = 'voicecut.appearance';

export function parseAppearance(value: unknown): Appearance {
  return value === 'light' || value === 'dark' ? value : 'system';
}

export function resolveTheme(appearance: Appearance, systemDark: boolean): Theme {
  if (appearance === 'system') return systemDark ? 'dark' : 'light';
  return appearance;
}

const media = typeof window !== 'undefined' ? window.matchMedia('(prefers-color-scheme: dark)') : null;
let chosen: Appearance = parseAppearance(readStored());
let resolved: Theme = resolveTheme(chosen, media?.matches ?? true);
const listeners = new Set<() => void>();

function readStored(): string | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage.getItem(APPEARANCE_KEY) : null;
  } catch {
    return null;
  }
}

function store(appearance: Appearance): void {
  try {
    if (appearance === 'system') window.localStorage.removeItem(APPEARANCE_KEY);
    else window.localStorage.setItem(APPEARANCE_KEY, appearance);
  } catch {
    // Private windows may refuse storage; the choice then lasts for this page.
  }
}

function apply(): void {
  resolved = resolveTheme(chosen, media?.matches ?? true);
  if (typeof document !== 'undefined') document.documentElement.dataset.theme = resolved;
  for (const listener of listeners) listener();
}

function adopt(appearance: Appearance): void {
  if (appearance === chosen) return;
  chosen = appearance;
  store(appearance);
  apply();
}

/** The user's choice: System, Light or Dark. */
export function appearance(): Appearance {
  return chosen;
}

/** The theme in effect now. */
export function theme(): Theme {
  return resolved;
}

export function setAppearance(next: Appearance): void {
  adopt(next);
  if (desktop) invoke('set_appearance', { appearance: next }).catch(() => undefined);
}

/** Called whenever the choice or the theme in effect changes. */
export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function onThemeChange(listener: (theme: Theme) => void): () => void {
  let last = resolved;
  return subscribe(() => {
    if (resolved === last) return;
    last = resolved;
    listener(resolved);
  });
}

/** Apply the theme and keep following the system, other tabs and the app. */
export function startTheme(): void {
  apply();
  media?.addEventListener('change', apply);
  // Another tab or window of the editor changed the choice.
  window.addEventListener('storage', (event) => {
    if (event.key === APPEARANCE_KEY || event.key === null) adopt(parseAppearance(readStored()));
  });
  if (desktop) {
    invoke<string>('appearance').then((value) => adopt(parseAppearance(value)), () => undefined);
    listen<string>('appearance', (value) => adopt(parseAppearance(value)));
  }
}
