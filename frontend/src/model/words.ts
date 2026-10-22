import type { Word } from '../types';

/**
 * Indices `[first, last)` of the transcript words that overlap source samples
 * `[from, to)`.  Words are in recording order, so this is a binary search.
 */
export function wordsBetween(words: readonly Word[], from: number, to: number): [number, number] {
  let low = 0;
  let high = words.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (words[middle]!.end <= from) low = middle + 1;
    else high = middle;
  }
  let last = low;
  while (last < words.length && words[last]!.start < to) last += 1;
  return [low, last];
}
