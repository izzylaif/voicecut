import React from 'react';
import { AbsoluteFill, Audio, interpolate, Sequence, staticFile } from 'remotion';
import { Background } from './components/Background';
import { useTime } from './lib';
import { App, CHOOSE, DROP, ENTER, OPEN_MENU, PRESS, STAGE_DONE } from './scenes/App';
import { Detail } from './scenes/Detail';
import { Free } from './scenes/Free';
import { LIVE_TAGS, Mess, NAMED } from './scenes/Mess';
import { Outro } from './scenes/Outro';
import { Result } from './scenes/Result';
import { Reveal } from './scenes/Reveal';
import { CLEAN, DURATION_FRAMES, FOLD, FPS, RAW_PIECES, said, SCENES, VO } from './timeline';

const f = (seconds: number) => Math.round(seconds * FPS);
/** Every sound effect's level, on top of its own. */
const SFX = 0.78;

/** One sound at ``at`` seconds. */
const Cue: React.FC<{ at: number; src: string; volume?: number; trim?: number; length?: number; fade?: number }> = ({
  at,
  src,
  volume = 1,
  trim = 0,
  length,
  fade = 0,
}) => {
  const duration = length ? f(length) : undefined;
  return (
    <Sequence from={f(at)} durationInFrames={duration} layout="none">
      <Audio
        src={staticFile(src)}
        trimBefore={f(trim)}
        volume={(frame) =>
          duration && fade
            ? volume * interpolate(frame, [duration - f(fade), duration], [1, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' })
            : volume
        }
      />
    </Sequence>
  );
};

/** A scene is drawn only around its own time. */
const During: React.FC<{ from: number; to: number; children: React.ReactNode }> = ({ from, to, children }) => {
  const t = useTime();
  if (t < from - 0.05 || t > to + 0.05) return null;
  return <>{children}</>;
};

export const Promo: React.FC<{ music: boolean }> = ({ music }) => {
  return (
    <AbsoluteFill>
      <Background />
      <During from={SCENES.mess.start} to={SCENES.mess.end}>
        <Mess />
      </During>
      <During from={SCENES.reveal.start} to={SCENES.reveal.end}>
        <Reveal />
      </During>
      <During from={SCENES.app.start} to={SCENES.app.end}>
        <App />
      </During>
      <During from={SCENES.detail.start} to={SCENES.detail.end}>
        <Detail />
      </During>
      <During from={SCENES.result.start} to={SCENES.result.end}>
        <Result />
      </During>
      <During from={SCENES.free.start} to={SCENES.free.end}>
        <Free />
      </During>
      <During from={SCENES.outro.start} to={SCENES.outro.end}>
        <Outro />
      </During>

      {/* The recording itself: its first take, then Voice Cut's result */}
      {RAW_PIECES.map((piece) => (
        <Cue key={piece.at} at={piece.at} src="audio/raw.wav" trim={piece.from} length={piece.to - piece.from + 0.04} fade={0.06} />
      ))}
      {RAW_PIECES.slice(1).map((piece) => (
        <Cue key={piece.at} at={piece.at - 0.42} src="sfx/whoosh.wav" volume={SFX * 0.04} />
      ))}
      {LIVE_TAGS.map((at) => (
        <Cue key={at} at={at - 0.02} src="sfx/pop.wav" volume={SFX * 0.15} />
      ))}
      <Cue at={CLEAN.at} src="audio/clean.wav" trim={CLEAN.from} length={CLEAN.to - CLEAN.from + 0.15} fade={0.15} />

      {/* The narrator */}
      {(Object.keys(VO) as (keyof typeof VO)[]).map((id) => (
        <Cue key={id} at={VO[id].start} src={`audio/vo/${id}.wav`} volume={1} />
      ))}

      {/* Sound design */}
      <Cue at={VO.vo01.start - 0.25} src="sfx/whoosh-long.wav" volume={SFX * 0.07} />
      {(['retake', 'false-start', 'breath', 'silence'] as const).map((kind) => (
        <Cue key={kind} at={NAMED[kind] - 0.02} src="sfx/pop.wav" volume={SFX * 0.13} />
      ))}
      <Cue at={SCENES.reveal.start - 0.05} src="sfx/swell.wav" volume={SFX * 0.18} />
      <Cue at={ENTER} src="sfx/whoosh.wav" volume={SFX * 0.07} />
      {[DROP, OPEN_MENU, CHOOSE, PRESS].map((at) => (
        <Cue key={at} at={at - 0.01} src="sfx/click.wav" volume={SFX * 0.35} />
      ))}
      {STAGE_DONE.map((at, index) => (
        <Cue key={at} at={at} src={index === STAGE_DONE.length - 1 ? 'sfx/tick-high.wav' : 'sfx/tick.wav'} volume={SFX * 0.11} />
      ))}
      <Cue at={SCENES.detail.start - 0.15} src="sfx/whoosh.wav" volume={SFX * 0.06} />
      <Cue at={said('vo06', 'between')} src="sfx/snip.wav" volume={SFX * 0.24} />
      <Cue at={said('vo06', 'between') + 0.12} src="sfx/snip.wav" volume={SFX * 0.2} />
      <Cue at={FOLD.at - 0.15} src="sfx/whoosh-long.wav" volume={SFX * 0.07} />
      <Cue at={SCENES.free.start} src="sfx/whoosh.wav" volume={SFX * 0.06} />
      {(['openrouter', 'gemini', 'openai', 'deepseq'] as const).map((word) => (
        <Cue key={word} at={said('vo09', word) - 0.08} src="sfx/pop.wav" volume={SFX * 0.12} />
      ))}
      <Cue at={said('vo10', 'get') - 0.05} src="sfx/chime.wav" volume={SFX * 0.05} />
      {music && <Cue at={0} src="sfx/pad.wav" volume={0.075} length={DURATION_FRAMES / FPS} fade={2} />}
    </AbsoluteFill>
  );
};
