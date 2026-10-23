import React from 'react';
import { AbsoluteFill, useVideoConfig } from 'remotion';
import { Key, Laptop } from '../components/Icons';
import { Logo } from '../components/Logo';
import { pop, ramp, useTime } from '../lib';
import { C, FONT, MONO } from '../theme';
import { said, SCENES } from '../timeline';

const PROVIDERS: { name: string; word: string }[] = [
  { name: 'OpenRouter', word: 'openrouter' },
  { name: 'Gemini', word: 'gemini' },
  { name: 'OpenAI', word: 'openai' },
  { name: 'DeepSeek', word: 'deepseq' },
];

/** Free and open source; on your computer, or with your own key. */
export const Free: React.FC = () => {
  const t = useTime();
  const { fps } = useVideoConfig();
  const enter = ramp(t, SCENES.free.start, 0.6);
  const leave = ramp(t, SCENES.free.end - 0.4, 0.4);
  const free = pop(t, said('vo09', 'free'), fps);
  const open = pop(t, said('vo09', 'open'), fps);
  const local = pop(t, said('vo09', 'run') - 0.05, fps, { damping: 15, stiffness: 120, mass: 0.8 });
  const keyCard = pop(t, said('vo09', 'with') - 0.05, fps, { damping: 15, stiffness: 120, mass: 0.8 });
  const systems = ramp(t, said('vo09', 'deepseq') + 0.4, 0.6);

  return (
    <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center', opacity: enter * (1 - leave) }}>
      <div style={{ transform: `scale(${0.9 + 0.1 * enter})` }}>
        <Logo size={96} />
      </div>
      <div style={{ marginTop: 44, display: 'flex', gap: 24, fontFamily: FONT, fontSize: 72, fontWeight: 750, letterSpacing: -2, color: C.strong }}>
        <span style={{ opacity: free, transform: `translateY(${(1 - free) * 20}px)` }}>Free</span>
        <span style={{ opacity: open, transform: `translateY(${(1 - open) * 20}px)`, color: C.muted }}>and</span>
        <span style={{ opacity: open, transform: `translateY(${(1 - open) * 20}px)` }}>open source.</span>
      </div>

      <div style={{ marginTop: 70, display: 'flex', gap: 44 }}>
        <Card progress={local} icon={<Laptop size={40} color={C.accent} />} title="On your computer" accent>
          <div style={{ fontSize: 28, color: C.strong, fontWeight: 600 }}>Gemma 4 · through Ollama</div>
          <div style={{ fontSize: 22, color: C.muted, marginTop: 8 }}>Nothing leaves your machine.</div>
        </Card>
        <Card progress={keyCard} icon={<Key size={40} color={C.primary} />} title="Or with your own key">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, marginTop: 4 }}>
            {PROVIDERS.map((provider) => {
              const p = pop(t, said('vo09', provider.word) - 0.08, fps, { damping: 12, stiffness: 200, mass: 0.5 });
              return (
                <span
                  key={provider.name}
                  style={{
                    padding: '10px 18px',
                    borderRadius: 999,
                    background: C.panel2,
                    border: `1px solid ${C.lineStrong}`,
                    fontSize: 24,
                    fontWeight: 600,
                    color: C.strong,
                    opacity: Math.min(1, p * 1.5),
                    transform: `scale(${0.6 + 0.4 * p})`,
                  }}
                >
                  {provider.name}
                </span>
              );
            })}
          </div>
        </Card>
      </div>

      <div style={{ marginTop: 64, fontFamily: MONO, fontSize: 26, color: C.muted, letterSpacing: 1, opacity: systems }}>
        macOS · Windows · Linux &nbsp;—&nbsp; MIT license · no account
      </div>
    </AbsoluteFill>
  );
};

const Card: React.FC<{ progress: number; icon: React.ReactNode; title: string; accent?: boolean; children: React.ReactNode }> = ({
  progress,
  icon,
  title,
  accent,
  children,
}) => (
  <div
    style={{
      width: 640,
      minHeight: 250,
      padding: '34px 38px',
      boxSizing: 'border-box',
      borderRadius: 26,
      background: 'linear-gradient(180deg, rgba(31,33,38,0.95), rgba(21,23,27,0.95))',
      border: `1px solid ${accent ? 'rgba(52,199,160,0.45)' : C.lineStrong}`,
      boxShadow: accent ? '0 30px 90px rgba(52,199,160,0.12)' : '0 30px 90px rgba(0,0,0,0.4)',
      fontFamily: FONT,
      opacity: Math.min(1, progress * 1.4),
      transform: `translateY(${(1 - progress) * 50}px) scale(${0.94 + 0.06 * progress})`,
    }}
  >
    <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 20 }}>
      {icon}
      <span style={{ fontSize: 34, fontWeight: 700, color: C.strong }}>{title}</span>
    </div>
    {children}
  </div>
);
