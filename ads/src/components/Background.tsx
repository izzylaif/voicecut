import React from 'react';
import { AbsoluteFill, useCurrentFrame } from 'remotion';
import { C } from '../theme';

/** Near-black stage with two slow glows, fine grain and a vignette. */
export const Background: React.FC<{ glow?: number; hue?: 'accent' | 'mixed' }> = ({ glow = 1, hue = 'mixed' }) => {
  const frame = useCurrentFrame();
  const t = frame / 60;
  const ax = 30 + Math.sin(t * 0.21) * 8;
  const ay = 28 + Math.cos(t * 0.17) * 6;
  const bx = 72 + Math.cos(t * 0.15) * 9;
  const by = 74 + Math.sin(t * 0.19) * 7;
  const second = hue === 'accent' ? 'rgba(52, 199, 160, 0.10)' : 'rgba(61, 124, 255, 0.13)';
  return (
    <AbsoluteFill style={{ backgroundColor: C.bg, overflow: 'hidden' }}>
      <AbsoluteFill
        style={{
          opacity: glow,
          background: `radial-gradient(60% 55% at ${ax}% ${ay}%, rgba(52, 199, 160, 0.16), transparent 70%),
            radial-gradient(55% 60% at ${bx}% ${by}%, ${second}, transparent 70%)`,
        }}
      />
      <AbsoluteFill style={{ opacity: 0.07, mixBlendMode: 'overlay' }}>
        <svg width="100%" height="100%">
          <filter id="grain">
            <feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" seed={frame % 7} stitchTiles="stitch" />
            <feColorMatrix type="saturate" values="0" />
          </filter>
          <rect width="100%" height="100%" filter="url(#grain)" />
        </svg>
      </AbsoluteFill>
      <AbsoluteFill
        style={{ background: 'radial-gradient(120% 90% at 50% 50%, transparent 55%, rgba(0, 0, 0, 0.55) 100%)' }}
      />
    </AbsoluteFill>
  );
};
