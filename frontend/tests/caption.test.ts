import { describe, expect, it } from 'vitest';
import fixture from './fixtures/layout_cases.json';
import { blockAt, captionBlocks, pageOf, paginate, wordAt } from '../src/model/caption';
import { computeLayout } from '../src/model/layout';
import { fromItems } from '../src/model/timeline';
import type { Proposal, WireItem } from '../src/types';

const proposal = fixture.proposal as unknown as Proposal;
const layout = computeLayout(fromItems(fixture.cases[0]!.items as WireItem[]), proposal, fixture.sample_rate);
const blocks = captionBlocks(layout, proposal.words);

describe('caption blocks', () => {
  it('are the clips, with the words each one plays', () => {
    expect(blocks.map((block) => block.words.map((word) => word.text))).toEqual([
      ['Lead', 'in.'],
      ['Second'],
      ['third'],
      ['fourth'],
    ]);
    for (const block of blocks) {
      for (const word of block.words) {
        expect(word.outStart).toBeGreaterThanOrEqual(block.outStart);
        expect(word.outEnd).toBeLessThanOrEqual(block.outEnd);
      }
    }
  });

  it('stay on the clip that just ended during a pause', () => {
    expect(blockAt(blocks, 0)).toBe(0); // lead-in: the first block waits on screen
    expect(blockAt(blocks, blocks[1]!.outStart)).toBe(1);
    expect(blockAt(blocks, blocks[1]!.outStart - 1)).toBe(0); // the pause before clip 2
    expect(blockAt(blocks, layout.total + 10)).toBe(blocks.length - 1);
  });

  it('track the last word that has started', () => {
    const block = blocks[0]!;
    expect(wordAt(block, block.outStart - 1)).toBe(-1);
    expect(wordAt(block, block.words[0]!.outStart)).toBe(0);
    expect(wordAt(block, block.words[1]!.outStart - 1)).toBe(0); // between words: stays
    expect(wordAt(block, block.outEnd - 1)).toBe(1);
  });
});

describe('pages of long clips', () => {
  it('keep short clips whole', () => {
    expect(paginate(['One', 'two', 'three.'])).toEqual([[0, 3]]);
  });

  it('break long clips, preferably after a sentence', () => {
    const texts = Array.from({ length: 70 }, (_, index) => (index === 19 ? 'end.' : `w${index}`));
    const pages = paginate(texts, 32, 200);
    expect(pages[0]).toEqual([0, 20]);
    expect(pages.flatMap(([first, last]) => texts.slice(first, last))).toEqual(texts);
    for (const [first, last] of pages) expect(last - first).toBeLessThanOrEqual(32);
    const block = { clip: 0, outStart: 0, outEnd: 1, words: [], pages };
    expect(pageOf(block, 25)).toBe(1);
    expect(pageOf(block, 0)).toBe(0);
  });

  it('limit characters too', () => {
    const texts = Array.from({ length: 10 }, () => 'x'.repeat(50));
    for (const [first, last] of paginate(texts, 32, 200)) expect(last - first).toBeLessThanOrEqual(4);
  });
});
