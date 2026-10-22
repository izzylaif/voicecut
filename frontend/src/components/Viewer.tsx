import { useEffect, useRef } from 'react';
import { Film, Loader2, RotateCcw } from 'lucide-react';
import { api } from '../api';
import { blockAt, captionBlocks, pageOf, wordAt, type CaptionBlock } from '../model/caption';
import type { Layout } from '../model/layout';
import type { EditorStore } from '../store';
import { useStore } from '../useStore';
import { VideoSync } from '../video/videoSync';

/** Picture of the edit for video projects; the spoken words for audio. */
export function Viewer({ store }: { store: EditorStore }) {
  useStore(store);
  const video = store.project.media.video;
  if (!video) return <Caption store={store} />;
  if (video.proxy_url) return <VideoView store={store} url={video.proxy_url} offset={video.offset} />;
  return (
    <div className="viewer viewer-status">
      {video.status === 'failed' ? (
        <>
          <Film size={28} />
          <p>The video preview could not be prepared.</p>
          <pre className="error-detail">{video.error}</pre>
          <button
            type="button"
            className="button"
            onClick={() => {
              api.prepare(store.id).then(() => store.notify('Preparing the video preview again'));
            }}
          >
            <RotateCcw size={14} /> Try again
          </button>
        </>
      ) : (
        <>
          <Loader2 className="spin" size={28} />
          <p>Preparing the video preview…</p>
          {typeof video.progress === 'number' && (
            <div className="meter">
              <div style={{ width: `${Math.round(video.progress * 100)}%` }} />
            </div>
          )}
          <p className="muted">You can already listen and edit; the picture appears when it is ready.</p>
        </>
      )}
    </div>
  );
}

function VideoView({ store, url, offset }: { store: EditorStore; url: string; offset: number }) {
  const container = useRef<HTMLDivElement>(null);
  const video = store.project.media.video!;
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const sync = new VideoSync(element, url, offset, store.sampleRate);
    let layout = store.layout;
    const stopFrames = store.onFrame((frame, playing) => {
      if (store.layout !== layout) {
        layout = store.layout;
        sync.invalidate();
      }
      sync.update(store.layout, frame, playing, store.player.playbackRate);
    });
    return () => {
      stopFrames();
      sync.dispose();
    };
  }, [store, url, offset]);
  const portrait = (video.height ?? 9) > (video.width ?? 16);
  return (
    <div className={`viewer viewer-video-frame${portrait ? ' portrait' : ''}`}>
      <div ref={container} className="viewer-video-stack" />
    </div>
  );
}

/**
 * For audio projects: subtitles of the edit.  One clip's words (the same
 * numbered block as in the transcript panel) stand still on screen while a
 * highlight slides from word to word; the next block appears when playback
 * reaches the next clip.  Nothing reflows while a block is shown.
 */
function Caption({ store }: { store: EditorStore }) {
  const line = useRef<HTMLParagraphElement>(null);
  const caret = useRef<HTMLSpanElement>(null);
  const label = useRef<HTMLSpanElement>(null);
  const words = store.proposal?.words;

  useEffect(() => {
    const element = line.current;
    const highlight = caret.current;
    const paragraph = element?.parentElement;
    if (!element || !highlight || !paragraph) return;
    let layout: Layout | null = null;
    let blocks: CaptionBlock[] = [];
    let shown = '';
    let lit: HTMLElement | null = null;
    let spans: HTMLElement[] = [];
    let first = 0;

    const place = (span: HTMLElement | null, animate: boolean) => {
      if (lit && lit !== span) lit.classList.remove('now');
      lit = span;
      if (!span) {
        highlight.style.opacity = '0';
        return;
      }
      span.classList.add('now');
      // Position relative to the paragraph the caret lives in.
      const box = span.getBoundingClientRect();
      const origin = paragraph.getBoundingClientRect();
      highlight.style.transition = animate ? '' : 'none';
      highlight.style.opacity = '1';
      highlight.style.width = `${box.width + 8}px`;
      highlight.style.height = `${box.height + 2}px`;
      highlight.style.transform = `translate(${box.left - origin.left - 4}px, ${box.top - origin.top - 1}px)`;
    };
    // Re-measure when the viewer is resized (the block rewraps).
    const observer = new ResizeObserver(() => place(lit, false));
    observer.observe(element);

    const stop = store.onFrame((frame) => {
      if (store.layout !== layout) {
        layout = store.layout;
        blocks = words ? captionBlocks(layout, words) : [];
        shown = '';
      }
      if (!words) {
        if (shown !== 'none') {
          shown = 'none';
          spans = [];
          element.innerHTML = '<span class="caption-muted">Run Voice Cut to see the words here</span>';
          place(null, false);
          if (label.current) label.current.textContent = '';
        }
        return;
      }
      if (blocks.length === 0) return;
      const at = Math.min(frame, layout.total - 1);
      const index = blockAt(blocks, at);
      const block = blocks[index]!;
      const word = wordAt(block, at);
      const page = pageOf(block, Math.max(0, word));
      const key = `${index}:${page}`;
      if (key !== shown) {
        shown = key;
        const [from, to] = block.pages[page] ?? [0, 0];
        first = from;
        element.innerHTML =
          block.words
            .slice(from, to)
            .map((item) => `<span class="${item.kept ? 'word' : 'word restored'}">${escapeHtml(item.text)}</span>`)
            .join(' ') || '<span class="caption-muted">(no words in this clip)</span>';
        spans = [...element.querySelectorAll<HTMLElement>('.word')];
        lit = null;
        if (label.current) {
          const pages = block.pages.length > 1 ? `  ·  part ${page + 1} of ${block.pages.length}` : '';
          label.current.textContent = `${index + 1} / ${blocks.length}${pages}`;
        }
        place(null, false);
      }
      // The highlight rests on the last word heard, and leaves after the clip.
      const inside = at >= block.outStart && at < block.outEnd;
      const span = inside && word >= 0 ? (spans[word - first] ?? null) : null;
      if (span !== lit) place(span, lit !== null);
    });
    return () => {
      stop();
      observer.disconnect();
    };
  }, [store, words]);

  return (
    <div className="viewer viewer-caption">
      <span ref={label} className="caption-clip" />
      <p className="caption-line">
        <span ref={caret} className="caption-caret" aria-hidden="true" />
        <span ref={line} className="caption-words" />
      </p>
    </div>
  );
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (character) => `&#${character.charCodeAt(0)};`);
}
