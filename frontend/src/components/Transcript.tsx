import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { computeLayout, sourceAt } from '../model/layout';
import { wordsBetween } from '../model/words';
import type { EditorStore } from '../store';
import type { Word } from '../types';

interface Block {
  segment: number;
  id: string;
  outStart: number;
  sourceStart: number;
  sourceEnd: number;
  words: { word: Word; index: number }[];
}

/**
 * The text of the edit, clip by clip.  Words VoiceCut had removed and that an
 * edit brought back are marked; clicking a word moves the playhead to it.
 */
export function Transcript({ store }: { store: EditorStore }) {
  const committed = useSyncExternalStore(store.subscribe, () => store.committed);
  const selection = useSyncExternalStore(store.subscribe, () => store.selection);
  const proposal = store.proposal;
  const list = useRef<HTMLDivElement>(null);

  const blocks = useMemo<Block[]>(() => {
    const words = proposal?.words ?? [];
    const layout = computeLayout(committed, proposal, store.sampleRate);
    return layout.clips.map((clip) => {
      const sourceEnd = clip.sourceAtStart + (clip.outEnd - clip.outStart);
      const segment = committed.clips[clip.segment]!;
      const inside: Block['words'] = [];
      const [first, last] = wordsBetween(words, clip.sourceAtStart, sourceEnd);
      for (let index = first; index < last; index += 1) inside.push({ word: words[index]!, index });
      return {
        segment: clip.segment,
        id: segment.id,
        outStart: clip.outStart,
        sourceStart: clip.sourceAtStart,
        sourceEnd,
        words: inside,
      };
    });
  }, [committed, proposal, store.sampleRate]);

  useEffect(() => {
    const words = proposal?.words ?? [];
    let current: HTMLElement | null = null;
    return store.onFrame((frame, playing) => {
      const sample = sourceAt(store.layout, Math.min(frame, store.layout.total - 1));
      let next: HTMLElement | null = null;
      if (sample !== null && words.length) {
        let low = 0;
        let high = words.length;
        while (low < high) {
          const middle = (low + high) >> 1;
          if (words[middle]!.end <= sample) low = middle + 1;
          else high = middle;
        }
        if (low < words.length && words[low]!.start <= sample) {
          next = list.current?.querySelector<HTMLElement>(`[data-word="${low}"]`) ?? null;
        }
      }
      if (next === current) return;
      current?.classList.remove('now');
      next?.classList.add('now');
      current = next;
      if (next && playing) next.scrollIntoView({ block: 'nearest' });
    });
  }, [store, proposal]);

  if (!proposal) {
    return (
      <div className="transcript transcript-empty">
        <p>The transcript appears after Voice Cut has run.</p>
        <p className="muted">Until then the timeline holds the whole recording as one clip.</p>
      </div>
    );
  }

  const seek = (block: Block, word: Word) => {
    const offset = Math.max(0, Math.min(block.sourceEnd - block.sourceStart - 1, word.start - block.sourceStart));
    store.setPlayhead(block.outStart + offset);
    store.reveal(block.outStart + offset);
  };

  return (
    <div className="transcript" ref={list}>
      {blocks.map((block, position) => (
        <div key={block.id} className={`transcript-block${selection.has(block.id) ? ' selected' : ''}`}>
          <button
            type="button"
            className="transcript-clip"
            title="Select this clip"
            onClick={(event) => {
              store.select(block.id, event.shiftKey || event.metaKey ? 'toggle' : 'replace');
              store.setPlayhead(block.outStart);
              store.reveal(block.outStart);
            }}
          >
            {position + 1}
          </button>
          <p>
            {block.words.length === 0 && <span className="muted">(no words)</span>}
            {block.words.map(({ word, index }) => (
              <span
                key={index}
                data-word={index}
                className={word.kept ? 'word' : 'word restored'}
                title={word.kept ? undefined : 'Removed by Voice Cut, brought back by your edit'}
                onClick={() => seek(block, word)}
              >
                {word.text}{' '}
              </span>
            ))}
          </p>
        </div>
      ))}
    </div>
  );
}
