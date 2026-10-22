import { useEffect, useRef, useState } from 'react';
import { Scissors, Trash2, UnfoldHorizontal } from 'lucide-react';
import { api } from '../api';
import type { EditorStore } from '../store';
import { PeakStore } from './peaks';
import { type ContextTarget, TimelineView } from './view';

export function Timeline({ store }: { store: EditorStore }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [menu, setMenu] = useState<ContextTarget | null>(null);

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const view = new TimelineView(element, store);
    view.onMenu = setMenu;
    let cancelled = false;
    const media = store.project.media;
    api
      .peaks(media.peaks.url)
      .then((base) => {
        if (cancelled) return;
        view.peaks = new PeakStore(
          base,
          media.peaks.spp,
          (spp, first, count) => api.finePeaks(store.id, spp, first, count),
          () => store.redraw(),
        );
        store.redraw();
      })
      .catch((error: unknown) => store.notify(`Cannot load the waveform: ${String(error)}`, 'error'));
    return () => {
      cancelled = true;
      view.dispose();
    };
  }, [store]);

  return (
    <div className="timeline">
      <canvas ref={canvas} aria-label="Timeline" />
      {menu && <TimelineMenu store={store} target={menu} onClose={() => setMenu(null)} />}
    </div>
  );
}

/** Right-click menu: what can be done at the clicked spot. */
function TimelineMenu({ store, target, onClose }: { store: EditorStore; target: ContextTarget; onClose: () => void }) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (event: Event) => {
      if (event instanceof KeyboardEvent ? event.key === 'Escape' : !root.current?.contains(event.target as Node)) onClose();
    };
    window.addEventListener('pointerdown', close, true);
    window.addEventListener('keydown', close);
    window.addEventListener('wheel', onClose, { passive: true });
    return () => {
      window.removeEventListener('pointerdown', close, true);
      window.removeEventListener('keydown', close);
      window.removeEventListener('wheel', onClose);
    };
  }, [onClose]);
  const act = (action: () => void) => () => {
    onClose();
    action();
  };
  const left = Math.min(target.clientX, window.innerWidth - 230);
  const top = Math.min(target.clientY, window.innerHeight - 150);
  return (
    <div ref={root} className="context-menu" role="menu" style={{ left, top }}>
      <button type="button" role="menuitem" disabled={store.isVideo} onClick={act(() => store.insertPause(target.frame))}>
        <UnfoldHorizontal size={14} /> {target.pause ? 'Make this pause 1 s longer' : 'Insert a 1 s pause here'}
        <kbd>P</kbd>
      </button>
      {target.clip && (
        <button type="button" role="menuitem" onClick={act(() => store.splitAtPlayhead())}>
          <Scissors size={14} /> Split here <kbd>S</kbd>
        </button>
      )}
      {(target.clip || target.pause) && (
        <button type="button" role="menuitem" onClick={act(() => store.deleteSelection())}>
          <Trash2 size={14} /> {target.pause ? 'Delete this pause' : 'Delete this clip'} <kbd>⌫</kbd>
        </button>
      )}
    </div>
  );
}
