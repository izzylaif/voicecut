/** The example recording and what Voice Cut did to it (scripts/prepare_data.py). */
import example from './data/example.json';
import type { RegionKind } from './theme';

export type Word = { id: number; text: string; start: number; end: number; kept: boolean };
export type Region = { start: number; end: number; kind: RegionKind };

export const EX = example as unknown as {
  rate: number;
  duration: number;
  cleanDuration: number;
  raw: number[];
  clean: number[];
  kept: [number, number][];
  keptOut: number[];
  regions: Region[];
  words: Word[];
  lines: { text: string; first: number; last: number }[];
  zoom: { start: number; end: number; rate: number; peaks: number[] };
};

export function regionAt(t: number): Region | undefined {
  return EX.regions.find((region) => t >= region.start && t < region.end);
}

/** Where recording time ``t`` lands in Voice Cut's result (removed time folds to the next kept start). */
export function outputTime(t: number): number {
  for (let index = 0; index < EX.kept.length; index++) {
    const [start, end] = EX.kept[index];
    if (t < start) return EX.keptOut[index];
    if (t <= end) return EX.keptOut[index] + (t - start);
  }
  return EX.cleanDuration;
}

/** The recording's time at ``t`` seconds of Voice Cut's result. */
export function sourceTime(out: number): number {
  for (let index = EX.kept.length - 1; index >= 0; index--) {
    if (out >= EX.keptOut[index]) return EX.kept[index][0] + (out - EX.keptOut[index]);
  }
  return EX.kept[0][0];
}

export const KEPT_WORDS = EX.words.filter((word) => word.kept);

export function countOf(kind: RegionKind): number {
  return EX.regions.filter((region) => region.kind === kind).length;
}
