import React from 'react';
import { AbsoluteFill } from 'remotion';
import { Logo } from '../components/Logo';
import { ramp, useTime } from '../lib';
import { C, FONT, MONO } from '../theme';
import { said, SCENES } from '../timeline';

/** The promise, and where to get it. */
export const Outro: React.FC = () => {
  const t = useTime();
  const first = ramp(t, SCENES.outro.start + 0.05, 0.7);
  const second = ramp(t, said('vo10', 'get') - 0.05, 0.7);
  const footer = ramp(t, said('vo10', 'take', 1) + 0.3, 0.7);
  const out = ramp(t, SCENES.outro.end - 0.7, 0.7);
  return (
    <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center', opacity: 1 - out }}>
      <div style={{ fontFamily: FONT, fontSize: 92, fontWeight: 780, letterSpacing: -3, textAlign: 'center', lineHeight: 1.12 }}>
        <div style={{ color: C.strong, opacity: first, transform: `translateY(${(1 - first) * 26}px)`, filter: `blur(${(1 - first) * 8}px)` }}>
          Record with all your retakes.
        </div>
        <div
          style={{
            opacity: second,
            transform: `translateY(${(1 - second) * 26}px)`,
            filter: `blur(${(1 - second) * 8}px)`,
            background: `linear-gradient(90deg, ${C.accent}, #7fe3c6 50%, ${C.accent})`,
            WebkitBackgroundClip: 'text',
            backgroundClip: 'text',
            color: 'transparent',
          }}
        >
          Get back one clean take.
        </div>
      </div>
      <div style={{ marginTop: 90, display: 'flex', alignItems: 'center', gap: 40, opacity: footer, transform: `translateY(${(1 - footer) * 20}px)` }}>
        <Logo size={64} />
        <div style={{ width: 1, height: 56, background: C.lineStrong }} />
        <div style={{ fontFamily: MONO, fontSize: 28, color: C.soft }}>
          vectozavr.github.io/voicecut
          <div style={{ fontSize: 20, color: C.muted, marginTop: 6 }}>free download · macOS · Windows · Linux</div>
        </div>
      </div>
    </AbsoluteFill>
  );
};
