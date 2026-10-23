import React from 'react';
import { AbsoluteFill, interpolateColors, useVideoConfig } from 'remotion';
import { Scissors } from '../components/Icons';
import { Waveform } from '../components/Waveform';
import { EX } from '../data';
import { clamp, lerp, pop, ramp, useTime } from '../lib';
import { C, FONT, MONO } from '../theme';
import { said, SCENES } from '../timeline';

const LEFT = 140;
const WIDTH = 1640;
const HEIGHT = 420;
const TOP = 330;
// Voice Cut's cut points around this breath (data/example_en_plan.json).
const CUT_IN = 26.387;
const CUT_OUT = 26.873;
// Where the words' sound starts and stops here (the waveform's own edges).
const WORDS = [
  { text: 'operations.', start: 25.55, end: 26.38 },
  { text: 'In this video', start: 26.88, end: 27.6 },
];
const ROOM_TONE = 14; // px: a bar of the recording's quiet room

/** One pause up close: the cuts fall in silence, the breath becomes room tone. */
export const Detail: React.FC = () => {
  const t = useTime();
  const { fps } = useVideoConfig();
  const { start, end, rate, peaks } = EX.zoom;
  const span = end - start;
  const x = (time: number) => LEFT + ((time - start) / span) * WIDTH;
  const enter = ramp(t, SCENES.detail.start, 0.7);
  const leave = ramp(t, SCENES.detail.end - 0.45, 0.45);
  const silence = ramp(t, said('vo06', 'silence'), 0.5);
  const cuts = [CUT_IN, CUT_OUT].map((_, index) => pop(t, said('vo06', 'between') + index * 0.12, fps, { damping: 11, stiffness: 170, mass: 0.6 }));
  const breath = ramp(t, said('vo06', 'breaths'), 0.4);
  const tone = ramp(t, said('vo06', 'room') - 0.15, 0.9);
  const inBreath = (time: number) => time > CUT_IN + 0.04 && time < CUT_OUT - 0.02;

  return (
    <AbsoluteFill style={{ opacity: enter * (1 - leave), transform: `scale(${lerp(1.08, 1, enter)})` }}>
      <div style={{ position: 'absolute', left: LEFT, top: 150, fontFamily: MONO, fontSize: 22, color: C.muted, letterSpacing: 1 }}>
        0:25.6 — 0:27.7 · one pause, up close
      </div>

      {/* The pause between the words */}
      <div
        style={{
          position: 'absolute',
          left: x(CUT_IN),
          width: x(CUT_OUT) - x(CUT_IN),
          top: TOP - 10,
          height: HEIGHT + 20,
          borderRadius: 14,
          background: interpolateColors(tone, [0, 1], ['rgba(143,179,255,0.10)', 'rgba(52,199,160,0.10)']),
          opacity: Math.max(silence * 0.6, breath),
        }}
      />

      {WORDS.map((word) => {
        const left = x(word.start);
        const right = x(word.end);
        return (
          <div key={word.text} style={{ position: 'absolute', left, width: right - left, top: TOP - 92, display: 'flex', justifyContent: 'center' }}>
            <div
              style={{
                padding: '10px 22px',
                borderRadius: 12,
                background: C.panel2,
                border: `1px solid ${C.lineStrong}`,
                fontFamily: FONT,
                fontSize: 32,
                fontWeight: 600,
                color: C.strong,
                opacity: ramp(t, SCENES.detail.start + 0.3, 0.5),
              }}
            >
              {word.text}
            </div>
          </div>
        );
      })}

      <div style={{ position: 'absolute', left: LEFT, top: TOP }}>
        <Waveform
          peaks={peaks}
          rate={rate}
          from={0}
          to={span}
          width={WIDTH}
          height={HEIGHT}
          barWidth={6}
          gap={3}
          curve={0.3}
          reveal={ramp(t, SCENES.detail.start + 0.1, 0.9)}
          color={(rel) => {
            const time = rel + start;
            if (inBreath(time)) return interpolateColors(tone, [0, 1], [interpolateColors(breath, [0, 1], ['#9aa1ad', C.breath]), C.accent]);
            if (time > CUT_IN - 0.01 && time < CUT_OUT + 0.01) return '#6d7380';
            return '#dfe3ea';
          }}
          shape={(rel, height) => {
            const time = rel + start;
            if (!inBreath(time)) return height;
            const wobble = 0.8 + 0.4 * Math.abs(Math.sin(rel * 173.3));
            return lerp(height, ROOM_TONE * wobble, tone);
          }}
        />
      </div>

      {/* The cuts */}
      {[CUT_IN, CUT_OUT].map((time, index) => {
        const p = cuts[index];
        if (p <= 0) return null;
        return (
          <div key={time} style={{ position: 'absolute', left: x(time) - 1.5, top: TOP - 40, height: HEIGHT + 80, width: 3 }}>
            <div
              style={{
                position: 'absolute',
                inset: 0,
                background: C.accent,
                transformOrigin: 'top',
                transform: `scaleY(${clamp(p)})`,
                boxShadow: `0 0 18px ${C.accent}`,
              }}
            />
            <div style={{ position: 'absolute', left: -20, top: -50, transform: `scale(${p}) rotate(${index ? 90 : -90}deg)` }}>
              <Scissors size={42} color={C.accent} stroke={2.2} />
            </div>
          </div>
        );
      })}

      {/* Labels */}
      <div
        style={{
          position: 'absolute',
          left: (x(CUT_IN) + x(CUT_OUT)) / 2,
          top: TOP + HEIGHT + 58,
          transform: `translateX(-50%) translateY(${(1 - silence) * 12}px)`,
          opacity: silence,
          fontFamily: FONT,
          fontSize: 28,
          fontWeight: 600,
          color: C.soft,
          whiteSpace: 'nowrap',
        }}
      >
        cut in the silence between words
      </div>
      <div
        style={{
          position: 'absolute',
          left: (x(CUT_IN) + x(CUT_OUT)) / 2,
          top: TOP + HEIGHT / 2 - 120,
          transform: `translateX(-50%) scale(${0.8 + 0.2 * breath})`,
          opacity: breath,
          padding: '8px 18px',
          borderRadius: 999,
          fontFamily: FONT,
          fontSize: 24,
          fontWeight: 700,
          color: '#0b0c0f',
          background: interpolateColors(tone, [0, 1], [C.breath, C.accent]),
          whiteSpace: 'nowrap',
        }}
      >
        {tone < 0.5 ? 'breath' : 'room tone'}
      </div>

      <div
        style={{
          position: 'absolute',
          left: 0,
          right: 0,
          top: 905,
          textAlign: 'center',
          fontFamily: FONT,
          fontSize: 44,
          fontWeight: 700,
          letterSpacing: -0.8,
          color: C.strong,
          opacity: ramp(t, said('vo06', 'cuts'), 0.5),
        }}
      >
        Cuts land in silence. <span style={{ color: C.accent, opacity: ramp(t, said('vo06', 'breaths'), 0.5) }}>Breaths become room tone.</span>
      </div>
    </AbsoluteFill>
  );
};
