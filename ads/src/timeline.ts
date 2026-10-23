/**
 * When everything happens, in seconds, derived from the narrator's lines:
 * each scene lasts as long as its line (plus the recording's own audio where
 * the film lets it speak), and the visuals hit the narrator's words.
 */
import vo from './data/vo.json';
import voWords from './data/vo-words.json';

export const FPS = 60;
export const WIDTH = 1920;
export const HEIGHT = 1080;

type LineId = keyof typeof vo;

const durations = Object.fromEntries(
  Object.entries(vo).map(([id, line]) => [id, line.seconds]),
) as Record<LineId, number>;

/**
 * The raw recording speaks first, in three pieces with a fast-forward between
 * them: the first take; a mumble and the same sentence again (the retake);
 * "and it might begin with familiar — with the familiar words" (the false start).
 */
const PIECES = [
  { from: 2.85, to: 5.52 },
  { from: 8.42, to: 12.06 },
  { from: 14.0, to: 16.95 },
];
const SKIP = 0.42;
export const RAW_PIECES = (() => {
  let at = 0.35;
  return PIECES.map((piece) => {
    const placed = { ...piece, at };
    at += piece.to - piece.from + SKIP;
    return placed;
  });
})();
const lastPiece = RAW_PIECES[RAW_PIECES.length - 1];
export const RAW_END = lastPiece.at + (lastPiece.to - lastPiece.from);

/** The recording's time heard at film time ``t``, and how far into a skip. */
export function rawAt(t: number): { source: number; skipping: number } {
  for (let index = 0; index < RAW_PIECES.length; index++) {
    const piece = RAW_PIECES[index];
    const end = piece.at + (piece.to - piece.from);
    if (t < piece.at) {
      if (index === 0) return { source: piece.from, skipping: 0 };
      const previous = RAW_PIECES[index - 1];
      const p = (t - (piece.at - SKIP)) / SKIP;
      const eased = p * p * (3 - 2 * p);
      return { source: previous.to + (piece.from - previous.to) * eased, skipping: Math.sin(Math.PI * p) };
    }
    if (t <= end) return { source: piece.from + (t - piece.at), skipping: 0 };
  }
  return { source: lastPiece.to, skipping: 0 };
}

/** The moments the speaker restarts: the retake and the false start. */
export const RESTARTS = { retake: 9.53, falseStart: 15.68 };
export function filmTimeOf(source: number): number {
  const piece = RAW_PIECES.find((item) => source >= item.from && source <= item.to);
  return piece ? piece.at + (source - piece.from) : 0;
}

const starts: Partial<Record<LineId, number>> = {};
starts.vo01 = RAW_END + 0.2;
starts.vo03 = starts.vo01 + durations.vo01 + 0.55;
starts.vo04 = starts.vo03 + durations.vo03 + 0.45;
starts.vo05 = starts.vo04 + durations.vo04 + 0.9;
starts.vo06 = starts.vo05 + durations.vo05 + 0.45;

/** The takes fall away, then the clean result speaks: the same passage, fixed. */
export const FOLD = { at: starts.vo06 + durations.vo06 + 1.0, duration: 1.5 };
export const CLEAN = { at: FOLD.at + FOLD.duration + 0.35, from: 0, to: 7.3 };
starts.vo09 = CLEAN.at + (CLEAN.to - CLEAN.from) + 0.6;
starts.vo10 = starts.vo09 + durations.vo09 + 0.55;

export const VO = Object.fromEntries(
  (Object.keys(starts) as LineId[]).map((id) => [id, { start: starts[id]!, end: starts[id]! + durations[id] }]),
) as Record<'vo01' | 'vo03' | 'vo04' | 'vo05' | 'vo06' | 'vo09' | 'vo10', { start: number; end: number }>;

/** Absolute time of the narrator saying ``word`` (its ``nth`` match) in a line. */
export function said(line: keyof typeof VO, word: string, nth = 0): number {
  const words = (voWords as Record<string, { w: string; s: number; e: number }[]>)[line];
  const matches = words.filter((item) => item.w.toLowerCase().replace(/[^a-z0-9]/g, '').startsWith(word.toLowerCase()));
  const match = matches[nth] ?? matches[0];
  if (!match) throw new Error(`${line} never says ${word}`);
  return VO[line].start + match.s;
}

/** The scenes, end to end. */
export const SCENES = {
  mess: { start: 0, end: VO.vo01.end + 0.35 },
  reveal: { start: VO.vo01.end + 0.35, end: VO.vo04.start - 0.05 },
  app: { start: VO.vo04.start - 0.05, end: VO.vo06.start - 0.2 },
  detail: { start: VO.vo06.start - 0.2, end: VO.vo06.end + 0.5 },
  result: { start: VO.vo06.end + 0.5, end: VO.vo09.start - 0.15 },
  free: { start: VO.vo09.start - 0.15, end: VO.vo10.start - 0.1 },
  outro: { start: VO.vo10.start - 0.1, end: VO.vo10.end + 2.2 },
};

export const DURATION_SECONDS = SCENES.outro.end;
export const DURATION_FRAMES = Math.ceil(DURATION_SECONDS * FPS);
