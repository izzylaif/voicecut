import React from 'react';
import { Img, staticFile } from 'remotion';
import { C, FONT } from '../theme';

/** VoiceCut's app icon with its wordmark. */
export const Logo: React.FC<{ size: number; wordmark?: boolean; glow?: number; gap?: number; style?: React.CSSProperties }> = ({
  size,
  wordmark = true,
  glow = 0.6,
  gap,
  style,
}) => (
  <div style={{ display: 'flex', alignItems: 'center', gap: gap ?? size * 0.32, ...style }}>
    <div style={{ position: 'relative', width: size, height: size }}>
      <div
        style={{
          position: 'absolute',
          inset: -size * 0.35,
          borderRadius: '50%',
          background: `radial-gradient(circle, rgba(52,199,160,${0.45 * glow}) 0%, transparent 65%)`,
          filter: `blur(${size * 0.12}px)`,
        }}
      />
      <Img src={staticFile('img/voicecut-icon.png')} style={{ position: 'relative', width: size, height: size }} />
    </div>
    {wordmark && (
      <div style={{ fontFamily: FONT, fontWeight: 750, fontSize: size * 0.62, letterSpacing: -size * 0.02, color: C.strong }}>
        Voice<span style={{ color: C.accent }}>Cut</span>
      </div>
    )}
  </div>
);
