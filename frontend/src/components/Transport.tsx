import { useEffect, useRef, useState } from 'react';
import {
  Magnet,
  Pause,
  Play,
  Redo2,
  RotateCcw,
  Scissors,
  SkipBack,
  SkipForward,
  Trash2,
  UnfoldHorizontal,
  Undo2,
  ZoomIn,
  ZoomOut,
  Maximize2,
} from 'lucide-react';
import { timecode } from '../format';
import { MAX_PX_PER_SECOND, MIN_PX_PER_SECOND, PLAYBACK_RATES, type EditorStore } from '../store';
import { useStore } from '../useStore';

const mod = navigator.platform.toLowerCase().includes('mac') ? '⌘' : 'Ctrl+';

export function Transport({ store }: { store: EditorStore }) {
  useStore(store);
  const time = useRef<HTMLSpanElement>(null);
  const total = store.layout.total / store.sampleRate;

  useEffect(
    () =>
      store.onFrame((frame) => {
        if (time.current) time.current.textContent = timecode(frame / store.sampleRate);
      }),
    [store],
  );

  return (
    <div className="transport">
      <div className="transport-group">
        <IconButton title="Previous cut (↑)" onClick={() => store.jumpEdit(-1)}>
          <SkipBack size={16} />
        </IconButton>
        <button
          type="button"
          className="play-button"
          title="Play / pause (Space)"
          onClick={() => store.togglePlay()}
        >
          {store.playing ? <Pause size={18} /> : <Play size={18} />}
        </button>
        <IconButton title="Next cut (↓)" onClick={() => store.jumpEdit(1)}>
          <SkipForward size={16} />
        </IconButton>
        <div className="timecode" title="Playhead / length of the edit">
          <span ref={time}>{timecode(store.frame() / store.sampleRate)}</span>
          <span className="timecode-total"> / {timecode(total)}</span>
        </div>
        <span className="muted source-length" title="Length of the original recording">
          from {timecode(store.sourceFrames / store.sampleRate, 1)}
        </span>
      </div>

      <div className="transport-group">
        <IconButton title={`Split at playhead (S, ${mod}B)`} onClick={() => store.splitAtPlayhead()}>
          <Scissors size={16} />
        </IconButton>
        <IconButton
          title={store.isVideo ? 'Pauses are for audio projects' : 'Insert a 1 s pause at the playhead (P)'}
          disabled={store.isVideo}
          onClick={() => store.insertPause()}
        >
          <UnfoldHorizontal size={16} />
        </IconButton>
        <IconButton
          title="Delete selected clips or pauses (Delete)"
          disabled={store.selection.size === 0}
          onClick={() => store.deleteSelection()}
        >
          <Trash2 size={16} />
        </IconButton>
        <RateMenu store={store} />
        <span className="divider" />
        <IconButton title={`Undo (${mod}Z)`} disabled={!store.canUndo} onClick={() => store.undo()}>
          <Undo2 size={16} />
        </IconButton>
        <IconButton title={`Redo (⇧${mod}Z)`} disabled={!store.canRedo} onClick={() => store.redo()}>
          <Redo2 size={16} />
        </IconButton>
        <IconButton
          title={store.proposal ? 'Back to Voice Cut’s edit' : 'Back to the whole recording'}
          disabled={!store.isEdited}
          onClick={() => store.resetToProposal()}
        >
          <RotateCcw size={16} />
        </IconButton>
      </div>

      <div className="transport-group">
        <IconButton
          title="Snap to words and Voice Cut’s cuts (N); hold ⌥ while dragging to bypass"
          active={store.snapping}
          onClick={() => store.toggleSnapping()}
        >
          <Magnet size={16} />
        </IconButton>
        <span className="divider" />
        <IconButton title={`Zoom out (${mod}−)`} onClick={() => store.zoom(1 / 1.6)}>
          <ZoomOut size={16} />
        </IconButton>
        <input
          className="zoom-slider"
          type="range"
          min={0}
          max={1000}
          value={Math.round(zoomToSlider(store.view.pxPerSecond))}
          title="Zoom"
          onChange={(event) => {
            const target = sliderToZoom(Number(event.target.value));
            store.zoom(target / store.view.pxPerSecond);
          }}
        />
        <IconButton title={`Zoom in (${mod}+)`} onClick={() => store.zoom(1.6)}>
          <ZoomIn size={16} />
        </IconButton>
        <IconButton title="Fit the whole edit (⇧Z)" onClick={() => store.zoomToFit()}>
          <Maximize2 size={16} />
        </IconButton>
      </div>
    </div>
  );
}

function rateLabel(rate: number): string {
  return `${rate}×`;
}

/** Preview speed: a button showing the rate and a menu of the usual rates. */
function RateMenu({ store }: { store: EditorStore }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === 'Escape' : !root.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener('pointerdown', close);
    window.addEventListener('keydown', close);
    return () => {
      window.removeEventListener('pointerdown', close);
      window.removeEventListener('keydown', close);
    };
  }, [open]);
  return (
    <div className="rate-menu" ref={root}>
      <button
        type="button"
        className={`rate-button${store.rate !== 1 ? ' active' : ''}`}
        title="Playback speed of the preview (< and >); the voice keeps its pitch and the export is not affected"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {rateLabel(store.rate)}
      </button>
      {open && (
        <div className="popover rate-options" role="menu">
          <span className="rate-heading">Playback speed</span>
          {[...PLAYBACK_RATES].reverse().map((rate) => (
            <button
              key={rate}
              type="button"
              role="menuitemradio"
              aria-checked={rate === store.rate}
              className={rate === store.rate ? 'selected' : ''}
              onClick={() => {
                store.setRate(rate);
                setOpen(false);
              }}
            >
              {rate === 1 ? '1× (normal)' : rateLabel(rate)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const LOG_MIN = Math.log(MIN_PX_PER_SECOND);
const LOG_MAX = Math.log(MAX_PX_PER_SECOND);

function zoomToSlider(pxPerSecond: number): number {
  return ((Math.log(pxPerSecond) - LOG_MIN) / (LOG_MAX - LOG_MIN)) * 1000;
}

function sliderToZoom(value: number): number {
  return Math.exp(LOG_MIN + (value / 1000) * (LOG_MAX - LOG_MIN));
}

function IconButton({
  title,
  onClick,
  disabled,
  active,
  children,
}: {
  title: string;
  onClick: () => void;
  disabled?: boolean;
  active?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      className={`icon-button${active ? ' active' : ''}`}
      title={title}
      aria-label={title}
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </button>
  );
}
