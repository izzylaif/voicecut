import React from 'react';
import { AbsoluteFill, useVideoConfig } from 'remotion';
import { Logo } from '../components/Logo';
import { pop, ramp, useTime } from '../lib';
import { C, FONT } from '../theme';
import { SCENES, VO } from '../timeline';

/** "VoiceCut does it for you": a cut sweeps across, and the app appears. */
export const Reveal: React.FC = () => {
  const t = useTime();
  const { fps } = useVideoConfig();
  const start = SCENES.reveal.start;
  const sweep = ramp(t, start, 0.55);
  const icon = pop(t, start + 0.25, fps, { damping: 12, stiffness: 120, mass: 0.8 });
  const words = ramp(t, VO.vo03.start + 0.15, 0.6);
  const leave = ramp(t, SCENES.reveal.end - 0.4, 0.45);
  return (
    <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center', opacity: 1 - leave }}>
      {/* The cut */}
      <div
        style={{
          position: 'absolute',
          top: 539,
          left: 0,
          height: 2,
          width: `${sweep * 100}%`,
          background: `linear-gradient(90deg, transparent, ${C.accent})`,
          opacity: 1 - ramp(t, start + 0.45, 0.4),
          boxShadow: `0 0 24px ${C.accent}`,
        }}
      />
      <div
        style={{
          transform: `scale(${0.6 + 0.4 * icon}) translateY(${-leave * 40}px)`,
          opacity: Math.min(1, icon * 1.5),
        }}
      >
        <Logo size={150} glow={icon} />
      </div>
      <div
        style={{
          marginTop: 46,
          fontFamily: FONT,
          fontSize: 40,
          fontWeight: 500,
          color: C.soft,
          opacity: words,
          transform: `translateY(${(1 - words) * 16}px)`,
        }}
      >
        does it for you.
      </div>
    </AbsoluteFill>
  );
};
