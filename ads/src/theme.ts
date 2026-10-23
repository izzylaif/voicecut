import { loadFont as loadInter } from '@remotion/google-fonts/Inter';
import { loadFont as loadMono } from '@remotion/google-fonts/JetBrainsMono';

const inter = loadInter('normal', { weights: ['400', '500', '600', '700', '800'], subsets: ['latin'] });
const mono = loadMono('normal', { weights: ['400', '500', '600'], subsets: ['latin'] });

export const FONT = inter.fontFamily;
export const MONO = mono.fontFamily;

/** VoiceCut's dark palette (frontend/src/styles.css), deepened for film. */
export const C = {
  bg: '#07080a',
  window: '#111215',
  panel: '#18191d',
  panel2: '#1f2126',
  surface: '#1c1e23',
  input: '#101114',
  line: '#2a2d34',
  lineStrong: '#3a3e47',
  text: '#e7e9ee',
  strong: '#ffffff',
  soft: '#c9ced8',
  muted: '#8b919c',
  dim: '#5d636e',
  accent: '#34c7a0',
  accentStrong: '#22a884',
  onAccent: '#04130e',
  primary: '#3d7cff',
  retake: '#ff6b61',
  falseStart: '#ffb056',
  breath: '#8fb3ff',
  silence: '#5a606b',
  stageDone: '#b9e8da',
};

export type RegionKind = 'retake' | 'false-start' | 'breath' | 'silence';

export const KIND_COLOR: Record<RegionKind, string> = {
  retake: C.retake,
  'false-start': C.falseStart,
  breath: C.breath,
  silence: C.silence,
};

export const KIND_LABEL: Record<RegionKind, string> = {
  retake: 'Retake',
  'false-start': 'False start',
  breath: 'Breath',
  silence: 'Long pause',
};
