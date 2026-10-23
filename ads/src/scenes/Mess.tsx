import React from 'react';
import { AbsoluteFill, interpolateColors, useVideoConfig } from 'remotion';
import { Waveform } from '../components/Waveform';
import { EX, countOf, regionAt } from '../data';
import { clamp, clock, lerp, pop, ramp, useTime } from '../lib';
import { C, FONT, KIND_COLOR, MONO, type RegionKind } from '../theme';
import { filmTimeOf, RAW_PIECES, rawAt, RESTARTS, said, SCENES, VO } from '../timeline';

const LEFT = 100;
const WIDTH = 1720;
const HEIGHT = 300;
const TOP = 470;

/** When the narrator names each kind of mess. */
export const NAMED: Record<RegionKind, number> = {
  retake: said('vo01', 'retakes'),
  'false-start': said('vo01', 'false'),
  breath: said('vo01', 'breaths'),
  silence: said('vo01', 'long'),
};

const LEGEND: { kind: RegionKind; label: string }[] = [
  { kind: 'retake', label: 'retake' },
  { kind: 'false-start', label: 'false start' },
  { kind: 'breath', label: 'breaths' },
  { kind: 'silence', label: 'long pauses' },
];

/** When the listener hears the speaker start over. */
const HEARD = {
  retake: filmTimeOf(RESTARTS.retake),
  falseStart: filmTimeOf(RESTARTS.falseStart),
};
export const LIVE_TAGS = [HEARD.retake, HEARD.falseStart];

/** The recording as it was made: its takes play, then everything wrong with it lights up. */
export const Mess: React.FC = () => {
  const t = useTime();
  const { fps } = useVideoConfig();
  const { source: playing, skipping } = rawAt(t);
  const heard = (time: number) => RAW_PIECES.some((piece) => time >= piece.from && time <= Math.min(piece.to, playing));
  const zoom = ramp(t, VO.vo01.start - 0.2, 1.6);
  const from = lerp(playing - 3.0, 0, zoom);
  const to = lerp(playing + 5.0, EX.duration, zoom);
  const x = (time: number) => LEFT + ((time - from) / (to - from)) * WIDTH;
  const outro = ramp(t, SCENES.mess.end - 0.55, 0.55);
  const lit = (kind: RegionKind) => ramp(t, NAMED[kind], 0.45);

  const color = (time: number) => {
    const region = regionAt(time);
    const base = zoom < 1 && heard(time) ? C.strong : '#9aa1ad';
    if (!region) return interpolateColors(zoom, [0, 1], [base, '#aeb4bf']);
    return interpolateColors(lit(region.kind), [0, 1], [interpolateColors(zoom, [0, 1], [base, '#aeb4bf']), KIND_COLOR[region.kind]]);
  };
  const alpha = (time: number) => (zoom < 0.5 && !heard(time) ? 0.35 : 1);

  // Karaoke caption of the piece being heard.
  const piece = [...RAW_PIECES].reverse().find((item) => playing >= item.from) ?? RAW_PIECES[0];
  const spoken = EX.words.filter(
    (word) => word.start >= piece.from - 0.05 && word.start <= playing + 0.05 && word.start >= playing - 3.0,
  );
  const captionOpacity = (1 - ramp(t, VO.vo01.start - 0.3, 0.5)) * (1 - skipping);
  const live = (at: number) => pop(t, at, fps) * (1 - zoom);
  // The retake's tag goes before the skip carries it off screen.
  const retakeStays = 1 - ramp(t, RAW_PIECES[2].at - 0.62, 0.3);

  return (
    <AbsoluteFill style={{ opacity: 1 - outro, transform: `scale(${1 - outro * 0.04})` }}>
      <div
        style={{
          position: 'absolute',
          left: LEFT,
          top: 150,
          fontFamily: MONO,
          fontSize: 22,
          letterSpacing: 1,
          color: C.muted,
          opacity: ramp(t, 0.1, 0.6),
          display: 'flex',
          gap: 18,
          alignItems: 'center',
        }}
      >
        <span style={{ width: 10, height: 10, borderRadius: 5, background: C.retake, opacity: 0.6 + 0.4 * Math.sin(t * 6) }} />
        <span>narration_take.wav</span>
        <span style={{ color: C.dim }}>{clock(EX.duration)}</span>
      </div>

      {/* The point, for anyone watching without sound */}
      <div
        style={{
          position: 'absolute',
          left: 0,
          right: 0,
          top: 250,
          textAlign: 'center',
          fontFamily: FONT,
          fontSize: 76,
          fontWeight: 760,
          letterSpacing: -2,
          color: C.strong,
          opacity: ramp(t, VO.vo01.start + 0.3, 0.6),
          transform: `translateY(${(1 - ramp(t, VO.vo01.start + 0.3, 0.6)) * 20}px)`,
        }}
      >
        Real recordings are <span style={{ color: C.retake }}>messy.</span>
      </div>

      {/* Region bands, once the narrator names them */}
      {EX.regions.map((region, index) => {
        const shown = lit(region.kind) * zoom;
        if (shown <= 0) return null;
        const left = x(region.start);
        const right = x(region.end);
        return (
          <div
            key={index}
            style={{
              position: 'absolute',
              left,
              width: Math.max(2, right - left),
              top: TOP - 20,
              height: HEIGHT + 40,
              background: KIND_COLOR[region.kind],
              opacity: 0.13 * shown,
              borderRadius: 6,
            }}
          />
        );
      })}

      <div style={{ position: 'absolute', left: LEFT, top: TOP }}>
        <Waveform
          peaks={EX.raw}
          rate={EX.rate}
          from={from}
          to={to}
          width={WIDTH}
          height={HEIGHT}
          barWidth={zoom > 0.5 ? 2 : 4}
          gap={zoom > 0.5 ? 1.2 : 3}
          color={color}
          alpha={alpha}
          reveal={ramp(t, 0, 1.1)}
        />
      </div>

      {/* Playhead while the take plays */}
      <div
        style={{
          position: 'absolute',
          left: x(playing) - 1,
          top: TOP - 50,
          width: 2,
          height: HEIGHT + 100,
          background: C.strong,
          opacity: (1 - zoom) * ramp(t, RAW_PIECES[0].at - 0.1, 0.3) * (1 - skipping * 0.6),
          boxShadow: '0 0 18px rgba(255,255,255,0.6)',
        }}
      />

      {/* What is being said */}
      <div
        style={{
          position: 'absolute',
          left: 0,
          right: 0,
          top: 290,
          textAlign: 'center',
          fontFamily: FONT,
          fontSize: 64,
          fontWeight: 600,
          letterSpacing: -1.2,
          color: C.strong,
          opacity: captionOpacity,
        }}
      >
        {spoken.map((word) => {
          const age = playing - word.start;
          const active = playing >= word.start && playing <= word.end + 0.12;
          return (
            <span
              key={word.id}
              style={{
                display: 'inline-block',
                marginRight: 18,
                opacity: clamp(age / 0.12) * (active ? 1 : 0.42),
                transform: `translateY(${(1 - clamp(age / 0.18)) * 14}px)`,
              }}
            >
              {word.text}
            </span>
          );
        })}
      </div>

      {/* Labels over the waveform */}
      {(['retake', 'false-start', 'silence'] as RegionKind[]).map((kind) => {
        const region = EX.regions.find((item) => item.kind === kind)!;
        const p = pop(t, NAMED[kind], fps) * zoom;
        if (p <= 0) return null;
        const centre = (x(region.start) + x(region.end)) / 2;
        return (
          <Tag key={kind} kind={kind} left={centre} top={TOP - 78} progress={p}>
            {kind === 'silence' ? 'Long pause' : kind === 'retake' ? 'Retake' : 'False start'}
          </Tag>
        );
      })}
      {EX.regions
        .filter((region) => region.kind === 'breath')
        .map((region, index) => {
          const p = pop(t, NAMED.breath + index * 0.06, fps) * zoom;
          if (p <= 0) return null;
          const centre = (x(region.start) + x(region.end)) / 2;
          return (
            <div
              key={index}
              style={{
                position: 'absolute',
                left: centre - 7,
                top: TOP + HEIGHT + 34,
                width: 14,
                height: 14,
                borderRadius: 7,
                background: C.breath,
                transform: `scale(${p})`,
                boxShadow: `0 0 16px ${C.breath}`,
              }}
            />
          );
        })}
      {(() => {
        const first = EX.regions.find((region) => region.kind === 'breath')!;
        const p = pop(t, NAMED.breath, fps) * zoom;
        if (p <= 0) return null;
        return (
          <Tag kind="breath" left={(x(first.start) + x(first.end)) / 2 + 80} top={TOP + HEIGHT + 66} progress={p}>
            Breaths
          </Tag>
        );
      })()}

      {/* Heard as it happens: the speaker starts over */}
      {live(HEARD.retake) * retakeStays > 0 && (
        <Tag kind="retake" left={x(RESTARTS.retake)} top={TOP - 78} progress={live(HEARD.retake) * retakeStays}>
          ↺ Retake
        </Tag>
      )}
      {live(HEARD.falseStart) > 0 && (
        <Tag kind="false-start" left={x(RESTARTS.falseStart)} top={TOP - 78} progress={live(HEARD.falseStart)}>
          ↺ False start
        </Tag>
      )}
      <div
        style={{
          position: 'absolute',
          left: 0,
          right: 0,
          top: 395,
          textAlign: 'center',
          fontFamily: MONO,
          fontSize: 26,
          color: C.muted,
          opacity: skipping * (1 - zoom),
          letterSpacing: 2,
        }}
      >
        ▶▶ skipping ahead
      </div>

      {/* Tally */}
      <div
        style={{
          position: 'absolute',
          left: 0,
          right: 0,
          top: 930,
          display: 'flex',
          justifyContent: 'center',
          gap: 46,
          fontFamily: FONT,
          fontSize: 30,
          fontWeight: 500,
          color: C.soft,
        }}
      >
        {LEGEND.map(({ kind, label }) => {
          const p = ramp(t, NAMED[kind] + 0.15, 0.5);
          return (
            <div key={kind} style={{ display: 'flex', alignItems: 'center', gap: 12, opacity: p, transform: `translateY(${(1 - p) * 16}px)` }}>
              <span style={{ width: 14, height: 14, borderRadius: 7, background: KIND_COLOR[kind] }} />
              <span style={{ fontFamily: MONO, color: C.strong, fontWeight: 600 }}>{countOf(kind)}</span>
              <span>{label}</span>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

export const Tag: React.FC<{ kind: RegionKind; left: number; top: number; progress: number; children: React.ReactNode }> = ({
  kind,
  left,
  top,
  progress,
  children,
}) => (
  <div
    style={{
      position: 'absolute',
      left,
      top,
      transform: `translate(-50%, ${(1 - progress) * 18}px) scale(${0.7 + 0.3 * progress})`,
      opacity: clamp(progress * 1.4),
      padding: '8px 18px',
      borderRadius: 999,
      fontFamily: FONT,
      fontSize: 24,
      fontWeight: 650,
      letterSpacing: 0.4,
      color: KIND_COLOR[kind] === C.silence ? C.soft : '#0b0c0f',
      background: KIND_COLOR[kind] === C.silence ? '#3a3e47' : KIND_COLOR[kind],
      boxShadow: `0 8px 30px ${KIND_COLOR[kind]}55`,
      whiteSpace: 'nowrap',
    }}
  >
    {children}
  </div>
);
