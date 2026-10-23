import React from 'react';
import { AbsoluteFill, Easing, interpolateColors } from 'remotion';
import { Waveform } from '../components/Waveform';
import { EX, KEPT_WORDS, outputTime, regionAt, sourceTime } from '../data';
import { clamp, lerp, ramp, useTime } from '../lib';
import { C, FONT, KIND_COLOR, MONO } from '../theme';
import { CLEAN, FOLD, SCENES } from '../timeline';

const LEFT = 100;
const WIDTH = 1720;
const HEIGHT = 250;
const TOP = 470;
const CLEAN_WIDTH = (WIDTH * EX.cleanDuration) / EX.duration;
const CLEAN_LEFT = LEFT + (WIDTH - CLEAN_WIDTH) / 2;

/** The takes fall away and the clips close up; then the clean result plays. */
export const Result: React.FC = () => {
  const t = useTime();
  const enter = ramp(t, SCENES.result.start, 0.6);
  const leave = ramp(t, SCENES.result.end - 0.45, 0.45);
  const fold = ramp(t, FOLD.at, FOLD.duration, Easing.bezier(0.65, 0, 0.25, 1));
  const marked = ramp(t, SCENES.result.start + 0.2, 0.6);
  const rawX = (time: number) => LEFT + (time / EX.duration) * WIDTH;
  const cleanX = (time: number) => CLEAN_LEFT + (outputTime(time) / EX.cleanDuration) * CLEAN_WIDTH;
  const xOf = (time: number) => lerp(rawX(time), cleanX(time), fold);
  const playing = clamp(t - CLEAN.at, 0, CLEAN.to - CLEAN.from) + CLEAN.from;
  const listening = t >= CLEAN.at - 0.2;
  const seconds = lerp(EX.duration, EX.cleanDuration, ramp(t, FOLD.at + 0.1, FOLD.duration));
  const counterLabel = fold < 0.6 ? 'of takes' : 'of clean narration';
  const source = sourceTime(playing);

  return (
    <AbsoluteFill style={{ opacity: enter * (1 - leave) }}>
      {/* The count */}
      <div style={{ position: 'absolute', left: 0, right: 0, top: 170, textAlign: 'center' }}>
        <div style={{ fontFamily: MONO, fontSize: 132, fontWeight: 600, letterSpacing: -4, color: interpolateColors(fold, [0, 1], [C.strong, C.accent]) }}>
          {seconds.toFixed(1)}
          <span style={{ fontSize: 64, color: C.muted, marginLeft: 12 }}>s</span>
        </div>
        <div style={{ fontFamily: FONT, fontSize: 34, fontWeight: 500, color: C.soft, marginTop: -6 }}>{counterLabel}</div>
      </div>

      {/* Clips, once they close up */}
      {EX.kept.map(([start, end], index) => {
        const left = xOf(start);
        const right = xOf(end);
        return (
          <div
            key={index}
            style={{
              position: 'absolute',
              left: left - 3,
              width: right - left + 6,
              top: TOP - 14,
              height: HEIGHT + 28,
              borderRadius: 10,
              background: 'rgba(52,199,160,0.09)',
              border: '1px solid rgba(52,199,160,0.35)',
              opacity: fold,
            }}
          />
        );
      })}

      <div style={{ position: 'absolute', left: 0, top: TOP }}>
        <Waveform
          peaks={EX.raw}
          rate={EX.rate}
          from={0}
          to={EX.duration}
          width={LEFT + WIDTH}
          height={HEIGHT}
          barWidth={2.4}
          gap={1.4}
          xOf={xOf}
          color={(time) => {
            const region = regionAt(time);
            if (region) return interpolateColors(marked, [0, 1], ['#aeb4bf', KIND_COLOR[region.kind]]);
            if (listening && time <= source) return C.strong;
            return interpolateColors(fold, [0, 1], ['#aeb4bf', C.accent]);
          }}
          alpha={(time) => (regionAt(time) ? 1 - fold : 1)}
          gain={(time) => (regionAt(time) ? 1 - fold * 0.9 : 1)}
        />
      </div>

      {/* Playhead and caption while the result plays */}
      <div
        style={{
          position: 'absolute',
          left: cleanX(source) - 1,
          top: TOP - 40,
          width: 2,
          height: HEIGHT + 80,
          background: C.strong,
          boxShadow: '0 0 16px rgba(255,255,255,0.7)',
          opacity: ramp(t, CLEAN.at - 0.2, 0.25) * (1 - ramp(t, CLEAN.at + CLEAN.to - CLEAN.from + 0.1, 0.3)),
        }}
      />
      <div
        style={{
          position: 'absolute',
          left: 0,
          right: 0,
          top: 820,
          textAlign: 'center',
          fontFamily: FONT,
          fontSize: 52,
          fontWeight: 600,
          letterSpacing: -1,
          color: C.strong,
          opacity: ramp(t, CLEAN.at - 0.1, 0.3),
        }}
      >
        {KEPT_WORDS.filter((word) => word.start <= source + 0.04 && word.start >= source - 2.6).map((word) => {
          const active = source >= word.start && source <= word.end + 0.02;
          return (
            <span key={word.id} style={{ marginRight: 14, color: active ? C.accent : C.strong, opacity: active ? 1 : 0.75 }}>
              {word.text}
            </span>
          );
        })}
      </div>
      <div
        style={{
          position: 'absolute',
          left: 0,
          right: 0,
          top: 905,
          textAlign: 'center',
          fontFamily: MONO,
          fontSize: 22,
          color: C.muted,
          opacity: (1 - ramp(t, CLEAN.at - 0.4, 0.3)) * ramp(t, FOLD.at + 0.9, 0.5),
        }}
      >
        1 retake · 1 false start · 9 breaths · 2 long pauses — gone
      </div>
    </AbsoluteFill>
  );
};
