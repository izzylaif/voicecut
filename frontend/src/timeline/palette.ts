/**
 * Colours of the timeline canvas, one palette per theme (src/theme.ts).
 *
 * The light palette is drawn for a light window, not inverted from the dark
 * one: clips are mint blocks with a dark green waveform, pauses a cool grey,
 * and the accents (selection, playhead, returned material) are darker so that
 * they keep their contrast on light surfaces.  tests/theme.test.ts checks the
 * contrast of the text and marks that matter.
 */
import type { Theme } from '../theme';

export interface Palette {
  background: string;
  laneBackground: string;
  overview: string;
  overviewWave: string;
  overviewKept: string;
  overviewSelected: string;
  /** Outline of what the timeline shows, on the overview. */
  overviewWindow: string;
  ruler: string;
  rulerText: string;
  rulerTick: string;
  clip: string;
  clipSelected: string;
  wave: string;
  /** Waveform of material Voice Cut had removed and the user brought back. */
  waveRestored: string;
  bodyRestored: string;
  video: string;
  videoSelected: string;
  pause: string;
  pauseSelected: string;
  pauseHatch: string;
  /** Hatching of pauses the user inserted. */
  pauseHatchAdded: string;
  pauseText: string;
  /** Clip number and length, and a selected pause's label. */
  title: string;
  word: string;
  wordRemoved: string;
  selection: string;
  edge: string;
  snapped: string;
  playhead: string;
  throughEdit: string;
  /** Box behind the label shown while dragging an edge. */
  label: string;
}

export const PALETTES: Record<Theme, Palette> = {
  dark: {
    background: '#141519',
    laneBackground: '#1a1c21',
    overview: '#1d2026',
    overviewWave: '#3b404a',
    overviewKept: 'rgba(52, 199, 160, 0.38)',
    overviewSelected: 'rgba(255, 204, 0, 0.55)',
    overviewWindow: 'rgba(255, 255, 255, 0.75)',
    ruler: '#1d2026',
    rulerText: '#8c93a0',
    rulerTick: '#3a3f49',
    clip: '#1e5f50',
    clipSelected: '#277a66',
    wave: '#86e8cb',
    waveRestored: '#ffb056',
    bodyRestored: 'rgba(255, 166, 64, 0.12)',
    video: '#25467e',
    videoSelected: '#30599f',
    pause: '#262a33',
    pauseSelected: '#30364a',
    pauseHatch: '#353b47',
    pauseHatchAdded: '#3d4a66',
    pauseText: '#8b93a3',
    title: 'rgba(230, 255, 247, 0.85)',
    word: 'rgba(225, 240, 235, 0.92)',
    wordRemoved: '#ffbf75',
    selection: '#ffcc00',
    edge: '#ffd60a',
    snapped: '#5ad1ff',
    playhead: '#ff453a',
    throughEdit: 'rgba(255, 255, 255, 0.45)',
    label: 'rgba(12, 13, 16, 0.92)',
  },
  light: {
    background: '#eceef2',
    laneBackground: '#e4e7ec',
    overview: '#e1e4ea',
    overviewWave: '#a7aeb9',
    overviewKept: 'rgba(10, 150, 115, 0.3)',
    overviewSelected: 'rgba(214, 140, 0, 0.5)',
    overviewWindow: 'rgba(27, 29, 34, 0.7)',
    ruler: '#f8f9fb',
    rulerText: '#5f6672',
    rulerTick: '#c2c7cf',
    clip: '#c3e9dc',
    clipSelected: '#a8ddca',
    wave: '#13795f',
    waveRestored: '#c06a00',
    bodyRestored: 'rgba(235, 140, 20, 0.16)',
    video: '#cad8f0',
    videoSelected: '#b1c6ea',
    pause: '#dadee5',
    pauseSelected: '#cbd2de',
    pauseHatch: '#c3c9d2',
    pauseHatchAdded: '#a9b9d6',
    pauseText: '#5a6270',
    title: 'rgba(8, 56, 43, 0.85)',
    word: 'rgba(10, 45, 36, 0.9)',
    wordRemoved: '#955000',
    selection: '#b86e00',
    edge: '#b06800',
    snapped: '#0a6fa3',
    playhead: '#d92c20',
    throughEdit: 'rgba(10, 45, 36, 0.45)',
    label: 'rgba(255, 255, 255, 0.96)',
  },
};
