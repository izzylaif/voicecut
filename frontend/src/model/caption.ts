/**
 * Subtitles of the edit: one clip's words at a time, like the numbered blocks
 * of the transcript panel.  The block on screen stays put while a highlight
 * moves from word to word; it changes only when playback reaches the next
 * clip.  A very long clip is shown in pages that break after a sentence when
 * possible, so a block always fits on screen.
 */
import type { Layout } from './layout';
import { wordsBetween } from './words';
import type { Word } from '../types';

export const PAGE_WORDS = 32;
export const PAGE_CHARACTERS = 200;

export interface CaptionWord {
  text: string;
  kept: boolean;
  /** Output frames the word is heard in. */
  outStart: number;
  outEnd: number;
}

export interface CaptionBlock {
  /** Index of the clip (the block's number in the transcript is clip + 1). */
  clip: number;
  outStart: number;
  outEnd: number;
  words: CaptionWord[];
  /** Word index ranges `[first, last)` shown together. */
  pages: [number, number][];
}

/** Split words into pages of at most `maxWords` words / `maxCharacters`. */
export function paginate(
  texts: readonly string[],
  maxWords = PAGE_WORDS,
  maxCharacters = PAGE_CHARACTERS,
): [number, number][] {
  const pages: [number, number][] = [];
  let start = 0;
  while (start < texts.length) {
    let end = start;
    let characters = 0;
    let sentence = -1;
    while (end < texts.length && end - start < maxWords && characters + texts[end]!.length <= maxCharacters) {
      characters += texts[end]!.length + 1;
      end += 1;
      if (/[.!?…]["»”)]?$/.test(texts[end - 1]!)) sentence = end;
    }
    if (end === start) end = start + 1; // a single over-long word
    // Prefer to break after a sentence that fills at least 40% of the page.
    if (end < texts.length && sentence > start && sentence - start >= Math.ceil((end - start) * 0.4)) end = sentence;
    pages.push([start, end]);
    start = end;
  }
  return pages;
}

export function captionBlocks(layout: Layout, words: readonly Word[]): CaptionBlock[] {
  return layout.clips.map((clip, index) => {
    const length = clip.outEnd - clip.outStart;
    const [first, last] = wordsBetween(words, clip.sourceAtStart, clip.sourceAtStart + length);
    const spoken = words.slice(first, last).map((word) => ({
      text: word.text,
      kept: word.kept,
      outStart: clip.outStart + Math.max(0, word.start - clip.sourceAtStart),
      outEnd: clip.outStart + Math.min(length, word.end - clip.sourceAtStart),
    }));
    return {
      clip: index,
      outStart: clip.outStart,
      outEnd: clip.outEnd,
      words: spoken,
      pages: paginate(spoken.map((word) => word.text)),
    };
  });
}

/**
 * The block to show at output frame `frame`: the clip being heard; during a
 * pause the clip that just ended (or, before the first clip, the first one).
 */
export function blockAt(blocks: readonly CaptionBlock[], frame: number): number {
  let low = 0;
  let high = blocks.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (blocks[middle]!.outStart <= frame) low = middle + 1;
    else high = middle;
  }
  return Math.max(0, Math.min(blocks.length - 1, low - 1));
}

/** The last word of `block` that has started by `frame` (-1 before the first). */
export function wordAt(block: CaptionBlock, frame: number): number {
  let low = 0;
  let high = block.words.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (block.words[middle]!.outStart <= frame) low = middle + 1;
    else high = middle;
  }
  return low - 1;
}

/** The page of `block` that holds word `word` (the first page before it starts). */
export function pageOf(block: CaptionBlock, word: number): number {
  const index = block.pages.findIndex(([first, last]) => word >= first && word < last);
  return index < 0 ? 0 : index;
}
