import { describe, expect, it } from 'vitest';
import { PALETTES } from '../src/timeline/palette';
import { parseAppearance, resolveTheme } from '../src/theme';

// The stylesheet as text (Vitest stubs CSS imports; the project has no Node types).
const fs = (await import('node:fs' as string)) as { readFileSync(path: URL, encoding: 'utf8'): string };
const css = fs.readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');

/** WCAG contrast ratio of two `#rrggbb` colours. */
function contrast(a: string, b: string): number {
  const luminance = (hex: string) => {
    const [r, g, b] = [1, 3, 5].map((index) => {
      const channel = parseInt(hex.slice(index, index + 2), 16) / 255;
      return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
  };
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high! + 0.05) / (low! + 0.05);
}

/** The custom properties of one theme block in styles.css. */
function tokens(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`);
  const block = css.slice(start, css.indexOf('}', start));
  return Object.fromEntries([...block.matchAll(/--([\w-]+):\s*([^;]+);/g)].map((match) => [match[1], match[2]]));
}

describe('appearance', () => {
  it('defaults to following the system', () => {
    expect(parseAppearance(null)).toBe('system');
    expect(parseAppearance('sepia')).toBe('system');
    expect(parseAppearance('light')).toBe('light');
  });

  it('resolves System from the system and keeps an explicit choice', () => {
    expect(resolveTheme('system', true)).toBe('dark');
    expect(resolveTheme('system', false)).toBe('light');
    expect(resolveTheme('light', true)).toBe('light');
    expect(resolveTheme('dark', false)).toBe('dark');
  });
});

describe('both themes stay readable', () => {
  it('has every page colour in both themes', () => {
    const dark = tokens(':root');
    const light = tokens(":root[data-theme='light']");
    expect(Object.keys(light).sort()).toEqual(Object.keys(dark).sort());
  });

  for (const [selector, name] of [
    [':root', 'dark'],
    [":root[data-theme='light']", 'light'],
  ] as const) {
    it(`page text has contrast in the ${name} theme`, () => {
      const colour = tokens(selector);
      for (const surface of ['bg', 'panel', 'surface']) {
        expect(contrast(colour.text!, colour[surface]!)).toBeGreaterThan(7);
        expect(contrast(colour.muted!, colour[surface]!)).toBeGreaterThanOrEqual(4.5);
        expect(contrast(colour.accent!, colour[surface]!)).toBeGreaterThanOrEqual(4.3);
        expect(contrast(colour.error!, colour[surface]!)).toBeGreaterThanOrEqual(4.5);
      }
      expect(contrast(colour['on-accent']!, colour['accent-strong']!)).toBeGreaterThanOrEqual(4.5);
      expect(contrast('#ffffff', colour.primary!)).toBeGreaterThanOrEqual(3.5);
    });
  }

  for (const [name, palette] of Object.entries(PALETTES)) {
    it(`timeline marks have contrast in the ${name} theme`, () => {
      expect(contrast(palette.wave, palette.clip)).toBeGreaterThanOrEqual(3.5);
      expect(contrast(palette.wordRemoved, palette.clip)).toBeGreaterThanOrEqual(4.5);
      expect(contrast(palette.rulerText, palette.ruler)).toBeGreaterThanOrEqual(4.5);
      expect(contrast(palette.pauseText, palette.pause)).toBeGreaterThanOrEqual(4.5);
      expect(contrast(palette.selection, palette.clip)).toBeGreaterThanOrEqual(3);
      expect(contrast(palette.playhead, palette.background)).toBeGreaterThanOrEqual(3);
    });
  }
});
