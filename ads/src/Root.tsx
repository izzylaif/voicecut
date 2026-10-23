import React from 'react';
import { Composition } from 'remotion';
import { Promo } from './Promo';
import { DURATION_FRAMES, FPS, HEIGHT, WIDTH } from './timeline';

export const RemotionRoot: React.FC = () => (
  <Composition
    id="VoiceCutPromo"
    component={Promo}
    durationInFrames={DURATION_FRAMES}
    fps={FPS}
    width={WIDTH}
    height={HEIGHT}
    defaultProps={{ music: true }}
  />
);
