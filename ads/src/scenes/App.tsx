import React from 'react';
import { AbsoluteFill, interpolate, interpolateColors, useVideoConfig } from 'remotion';
import { Check, ChevronDown, Download, FileAudio, Gear, Laptop, Pointer, Sparkles, Spinner } from '../components/Icons';
import { Waveform } from '../components/Waveform';
import { EX, regionAt } from '../data';
import { clamp, lerp, pop, ramp, useTime } from '../lib';
import { C, FONT, KIND_COLOR, MONO } from '../theme';
import { said, SCENES, VO } from '../timeline';

// The window, in film pixels.
const WIN = { x: 120, y: 64, w: 1680, h: 950 };
const TITLE = 40;
const TOOLBAR = 68;
const SIDE = 450;
const BODY_TOP = TITLE + TOOLBAR;
const TRANSPORT_TOP = 690;
const TRACK = { x: 24, y: 790, w: WIN.w - 48, h: 128 };

// Toolbar controls (window coordinates).
const PLANNER = { x: 470, y: 58, w: 214, h: 46 };
const CHIP = { x: 700, y: 63, w: 196, h: 36 };
const MODEL = { x: 912, y: 58, w: 198, h: 46 };
const LANGUAGE = { x: 1222, y: 58, w: 128, h: 46 };
const VOICECUT = { x: 1364, y: 56, w: 178, h: 50 };
const EXPORT = { x: 1598, y: 56, w: 66, h: 50 };

const PLANNERS = [
  { name: 'Local (Ollama)', detail: 'Gemma 4 31B · on this computer', local: true },
  { name: 'OpenRouter', detail: 'Gemma 4 31B · your API key' },
  { name: 'Gemini', detail: 'Gemini 3.8 Flash · your API key' },
  { name: 'OpenAI', detail: 'your API key' },
  { name: 'DeepSeek', detail: 'your API key' },
];
const MENU = { x: PLANNER.x, y: PLANNER.y + PLANNER.h + 8, w: 470, item: 74 };

const STAGES = [
  'Checking runtimes',
  'Preparing media',
  'Analyzing speech',
  'Transcribing',
  'Recovering hidden retries',
  'Recovering skipped speech',
  'Choosing takes',
  'Aligning and rendering the edit',
  'Publishing',
];

// --- the story's moments --------------------------------------------------------------
export const ENTER = SCENES.app.start;
export const DROP = VO.vo04.start + 0.85;
export const OPEN_MENU = said('vo04', 'pick') + 0.25;
const HOVER_LOCAL = said('vo04', 'locally') - 0.25;
export const CHOOSE = said('vo04', 'computer') + 0.1;
export const PRESS = said('vo04', 'cut') + 0.02;
const RUN = PRESS + 0.15;
const TYPE_FROM = said('vo05', 'transcribes') + 0.1;
const TYPE_TO = said('vo05', 'reads') - 0.1;
const SCAN = said('vo05', 'reads');
const KEEP = said('vo05', 'keeps');
export const STAGE_DONE = [
  RUN + 0.35,
  RUN + 0.7,
  RUN + 1.0,
  TYPE_TO,
  TYPE_TO + 0.25,
  SCAN + 0.5,
  KEEP + 0.1,
  KEEP + 0.9,
  KEEP + 1.3,
];
const LEAVE = SCENES.app.end - 0.6;

/** Where the pointer is, by keyframes (absolute film pixels). */
function pointerAt(t: number): { x: number; y: number; press: number } {
  const keys: [number, number, number][] = [
    [ENTER + 0.05, 140, 1150],
    [VO.vo04.start + 0.15, 520, 900],
    [DROP - 0.05, WIN.x + 620, WIN.y + 420],
    [DROP + 0.5, WIN.x + 700, WIN.y + 430],
    [OPEN_MENU - 0.05, WIN.x + PLANNER.x + 120, WIN.y + PLANNER.y + 26],
    [HOVER_LOCAL - 0.3, WIN.x + PLANNER.x + 160, WIN.y + MENU.y + MENU.item * 1.5],
    [HOVER_LOCAL + 0.2, WIN.x + PLANNER.x + 170, WIN.y + MENU.y + MENU.item * 0.5],
    [CHOOSE + 0.3, WIN.x + PLANNER.x + 175, WIN.y + MENU.y + MENU.item * 0.55],
    [PRESS - 0.08, WIN.x + VOICECUT.x + 96, WIN.y + VOICECUT.y + 28],
    [PRESS + 1.2, WIN.x + VOICECUT.x + 110, WIN.y + VOICECUT.y + 70],
    [LEAVE, WIN.x + VOICECUT.x + 260, WIN.y + 420],
  ];
  const times = keys.map((k) => k[0]);
  const ease = { extrapolateLeft: 'clamp' as const, extrapolateRight: 'clamp' as const };
  // Smooth between keyframes: ease each leg.
  let x = keys[0][1];
  let y = keys[0][2];
  for (let index = 1; index < keys.length; index++) {
    const [t0] = keys[index - 1];
    const [t1, x1, y1] = keys[index];
    if (t1 <= t0) continue;
    if (t >= t0) {
      const p = interpolate(t, [t0, t1], [0, 1], ease);
      const s = p * p * (3 - 2 * p);
      x = lerp(keys[index - 1][1], x1, s);
      y = lerp(keys[index - 1][2], y1, s);
    }
  }
  void times;
  const clicks = [DROP, OPEN_MENU, CHOOSE, PRESS];
  const press = Math.max(...clicks.map((c) => clamp(1 - Math.abs(t - c) / 0.12)));
  return { x, y, press };
}

export const App: React.FC = () => {
  const t = useTime();
  const { fps } = useVideoConfig();
  const enter = pop(t, ENTER, fps, { damping: 18, stiffness: 110, mass: 0.9 });
  const leave = ramp(t, LEAVE, 0.6);
  // Zoom towards the breath on the timeline as the film moves on to it.
  const focus = { x: WIN.x + TRACK.x + (26.6 / EX.duration) * TRACK.w, y: WIN.y + TRACK.y + TRACK.h / 2 };
  const zoom = 1 + leave * 1.6;
  const dropped = ramp(t, DROP, 0.7);
  const menu = clamp(ramp(t, OPEN_MENU, 0.22) - ramp(t, CHOOSE + 0.12, 0.2));
  const chosen = t >= CHOOSE;
  const pressed = clamp(1 - Math.abs(t - PRESS) / 0.14);
  const running = t >= RUN;
  const pointer = pointerAt(t);
  const pointerOpacity = ramp(t, ENTER + 0.2, 0.3) * (1 - ramp(t, LEAVE - 0.4, 0.4));

  return (
    <AbsoluteFill
      style={{
        opacity: Math.min(1, enter * 1.4) * (1 - ramp(t, LEAVE + 0.25, 0.35)),
        transformOrigin: `${focus.x}px ${focus.y}px`,
        transform: `scale(${zoom})`,
      }}
    >
      <div
        style={{
          position: 'absolute',
          left: WIN.x,
          top: WIN.y,
          width: WIN.w,
          height: WIN.h,
          borderRadius: 16,
          overflow: 'hidden',
          background: C.window,
          border: `1px solid ${C.lineStrong}`,
          boxShadow: '0 40px 120px rgba(0,0,0,0.65), 0 0 0 1px rgba(255,255,255,0.03) inset',
          transform: `perspective(2400px) translateY(${(1 - enter) * 120}px) rotateX(${(1 - enter) * 10}deg) scale(${0.94 + 0.06 * enter})`,
          fontFamily: FONT,
          color: C.text,
        }}
      >
        <TitleBar />
        <Toolbar t={t} chosen={chosen} pressed={pressed} running={running} dropped={dropped} />
        <Viewer t={t} dropped={dropped} />
        <SidePanel t={t} running={running} />
        <Transport t={t} dropped={dropped} />
        <Track t={t} dropped={dropped} />
        <PlannerMenu t={t} open={menu} />
      </div>
      <DropCard t={t} />
      <div style={{ position: 'absolute', left: pointer.x, top: pointer.y, opacity: pointerOpacity }}>
        <Pointer size={30} pressed={pointer.press} />
        {[DROP, OPEN_MENU, CHOOSE, PRESS].map((at) => {
          const ring = ramp(t, at, 0.45);
          if (t < at || ring >= 1) return null;
          return (
            <div
              key={at}
              style={{
                position: 'absolute',
                left: -24 * ring - 2,
                top: -24 * ring - 2,
                width: 48 * ring + 4,
                height: 48 * ring + 4,
                borderRadius: '50%',
                border: `2px solid rgba(255,255,255,${0.7 * (1 - ring)})`,
              }}
            />
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

const TitleBar: React.FC = () => (
  <div style={{ height: TITLE, display: 'flex', alignItems: 'center', gap: 9, paddingLeft: 18, borderBottom: `1px solid ${C.line}`, background: C.panel }}>
    {['#ff5f57', '#febc2e', '#28c840'].map((color) => (
      <span key={color} style={{ width: 13, height: 13, borderRadius: 7, background: color }} />
    ))}
    <span style={{ position: 'absolute', left: 0, right: 0, textAlign: 'center', fontSize: 15, fontWeight: 600, color: C.muted }}>
      narration_take
    </span>
  </div>
);

const Select: React.FC<{ box: { x: number; y: number; w: number; h: number }; children: React.ReactNode; glow?: number; accent?: boolean }> = ({
  box,
  children,
  glow = 0,
  accent,
}) => (
  <div
    style={{
      position: 'absolute',
      left: box.x,
      top: box.y,
      width: box.w,
      height: box.h,
      borderRadius: 10,
      border: `1px solid ${accent ? C.accent : C.lineStrong}`,
      background: C.input,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      padding: '0 14px',
      fontSize: 18,
      fontWeight: 500,
      color: accent ? C.accent : C.text,
      boxShadow: glow ? `0 0 0 ${4 * glow}px rgba(52,199,160,${0.25 * glow})` : undefined,
    }}
  >
    {children}
    <ChevronDown size={18} color={C.muted} />
  </div>
);

const Toolbar: React.FC<{ t: number; chosen: boolean; pressed: number; running: boolean; dropped: number }> = ({
  t,
  chosen,
  pressed,
  running,
  dropped,
}) => {
  const chip = pop(t, CHOOSE, 60);
  const choseGlow = clamp(1 - Math.abs(t - CHOOSE - 0.3) / 0.6);
  return (
    <div style={{ position: 'absolute', left: 0, top: TITLE, width: WIN.w, height: TOOLBAR, borderBottom: `1px solid ${C.line}`, background: C.panel }}>
      <div style={{ position: 'absolute', left: 24, top: 20, display: 'flex', alignItems: 'center', gap: 10, fontSize: 18 }}>
        <FileAudio size={22} color={C.muted} />
        <span style={{ fontWeight: 650, color: C.strong, opacity: 0.35 + 0.65 * dropped }}>narration_take</span>
        <span style={{ color: C.dim, marginLeft: 6, opacity: dropped }}>Saved</span>
      </div>
      <span style={{ position: 'absolute', left: PLANNER.x - 74, top: 24, fontSize: 17, color: C.muted }}>Planner</span>
      <Select box={{ ...PLANNER, y: PLANNER.y - TITLE }} glow={choseGlow}>
        {chosen ? 'Local (Ollama)' : 'OpenRouter'}
      </Select>
      <div
        style={{
          position: 'absolute',
          left: CHIP.x,
          top: CHIP.y - TITLE,
          height: CHIP.h,
          padding: '0 14px',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          borderRadius: 999,
          background: 'rgba(52,199,160,0.14)',
          color: C.accent,
          fontSize: 16,
          fontWeight: 600,
          opacity: chip,
          transform: `scale(${0.8 + 0.2 * chip})`,
        }}
      >
        <Laptop size={18} color={C.accent} /> on this computer
      </div>
      <Select box={{ ...MODEL, y: MODEL.y - TITLE }} accent={chosen} glow={choseGlow}>
        Gemma 4 31B
      </Select>
      <span style={{ position: 'absolute', left: LANGUAGE.x - 92, top: 24, fontSize: 17, color: C.muted }}>Language</span>
      <Select box={{ ...LANGUAGE, y: LANGUAGE.y - TITLE }}>English</Select>
      <div
        style={{
          position: 'absolute',
          left: VOICECUT.x,
          top: VOICECUT.y - TITLE,
          width: VOICECUT.w,
          height: VOICECUT.h,
          borderRadius: 11,
          background: running ? C.accentStrong : C.accent,
          color: C.onAccent,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 10,
          fontSize: 20,
          fontWeight: 700,
          transform: `scale(${1 - pressed * 0.06})`,
          boxShadow: `0 0 ${30 + 30 * pressed}px rgba(52,199,160,${0.35 + 0.4 * pressed})`,
        }}
      >
        <Sparkles size={20} color={C.onAccent} /> Voice Cut
      </div>
      <div style={{ position: 'absolute', left: VOICECUT.x + VOICECUT.w + 16, top: 22, color: C.muted }}>
        <Gear size={24} color={C.muted} />
      </div>
      <div
        style={{
          position: 'absolute',
          left: EXPORT.x,
          top: EXPORT.y - TITLE,
          width: EXPORT.w,
          height: EXPORT.h,
          borderRadius: 11,
          background: C.primary,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <Download size={22} color="#fff" />
      </div>
    </div>
  );
};

/** The transcript as Voice Cut reads it: the abandoned take and the false start get struck out. */
const LINES: { words: [number, number]; kind: 'retake' | 'kept'; strike?: [number, number] }[] = [
  { words: [0, 13], kind: 'retake' },
  { words: [14, 26], kind: 'kept' },
  { words: [27, 40], kind: 'kept', strike: [31, 32] },
  { words: [41, 47], kind: 'kept' },
  { words: [48, 60], kind: 'kept' },
];

const Viewer: React.FC<{ t: number; dropped: number }> = ({ t, dropped }) => {
  const width = WIN.w - SIDE;
  const typing = interpolate(t, [TYPE_FROM, TYPE_TO], [0, 61], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const scan = interpolate(t, [SCAN, KEEP - 0.1], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const keep = ramp(t, KEEP, 0.9);
  const ready = 1 - ramp(t, TYPE_FROM - 0.3, 0.3);
  return (
    <div style={{ position: 'absolute', left: 0, top: BODY_TOP, width, height: TRANSPORT_TOP - BODY_TOP, background: '#0b0b0d' }}>
      {/* Before Voice Cut */}
      <div
        style={{
          position: 'absolute',
          inset: 40,
          borderRadius: 18,
          border: `2px dashed ${interpolateColors(dropped, [0, 1], [C.lineStrong, 'rgba(52,199,160,0.0)'])}`,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 14,
          opacity: ready,
          fontSize: 26,
          color: C.muted,
        }}
      >
        {dropped < 0.5 ? (
          <span style={{ opacity: 1 - dropped * 2 }}>Drop a recording here</span>
        ) : (
          <>
            <span style={{ fontSize: 30, fontWeight: 650, color: C.strong, opacity: dropped }}>narration_take.wav</span>
            <span style={{ fontFamily: MONO, fontSize: 22, opacity: dropped }}>0:38.0 · 48 kHz · ready for Voice Cut</span>
          </>
        )}
      </div>
      {/* The transcript */}
      <div style={{ position: 'absolute', left: 56, top: 44, right: 50, fontSize: 29, lineHeight: 1.42, fontWeight: 500 }}>
        {LINES.map((line, index) => {
          const [first, last] = line.words;
          if (typing <= first) return null;
          const words = EX.words.slice(first, last + 1);
          const struck = line.kind === 'retake' ? keep : 0;
          const collapse = line.kind === 'retake' ? ramp(t, KEEP + 0.55, 0.6) : 0;
          const lit = line.kind === 'kept' ? keep : 0;
          const scanned = clamp((scan * LINES.length - index) * 1.5);
          const scanning = scan > 0 && scan < 1 ? clamp(1 - Math.abs(scan * LINES.length - index - 0.5)) : 0;
          return (
            <div
              key={index}
              style={{
                position: 'relative',
                padding: '7px 14px 7px 22px',
                marginBottom: lerp(10, 0, collapse),
                maxHeight: lerp(140, 0, collapse),
                opacity: 1 - collapse,
                overflow: 'hidden',
                borderRadius: 10,
                background: `rgba(255,255,255,${0.05 * scanning})`,
                color: interpolateColors(struck, [0, 1], [interpolateColors(lit, [0, 1], [scanned ? C.soft : C.soft, C.strong]), 'rgba(255,107,97,0.75)']),
              }}
            >
              <span
                style={{
                  position: 'absolute',
                  left: 0,
                  top: 10,
                  bottom: 10,
                  width: 5,
                  borderRadius: 3,
                  background: line.kind === 'kept' ? C.accent : C.retake,
                  transform: `scaleY(${line.kind === 'kept' ? lit : struck})`,
                }}
              />
              {words.map((word) => {
                const visible = clamp(typing - word.id);
                const inStrike = line.strike && word.id >= line.strike[0] && word.id <= line.strike[1];
                const strike = inStrike ? ramp(t, KEEP + 0.15, 0.4) : 0;
                return (
                  <span
                    key={word.id}
                    style={{
                      position: 'relative',
                      opacity: visible * (inStrike ? 1 - 0.55 * strike : 1),
                      color: inStrike ? interpolateColors(strike, [0, 1], [C.soft, C.falseStart]) : undefined,
                      marginRight: 9,
                      display: 'inline-block',
                    }}
                  >
                    {word.text}
                    {(inStrike || line.kind === 'retake') && (
                      <span
                        style={{
                          position: 'absolute',
                          left: -3,
                          top: '54%',
                          height: 3,
                          borderRadius: 2,
                          width: `calc(${(inStrike ? strike : struck) * 100}% + 6px)`,
                          background: inStrike ? C.falseStart : C.retake,
                        }}
                      />
                    )}
                  </span>
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
};

const SidePanel: React.FC<{ t: number; running: boolean }> = ({ t, running }) => {
  const appear = ramp(t, RUN, 0.35);
  const finished = t >= STAGE_DONE[STAGE_DONE.length - 1];
  return (
    <div
      style={{
        position: 'absolute',
        left: WIN.w - SIDE,
        top: BODY_TOP,
        width: SIDE,
        height: TRANSPORT_TOP - BODY_TOP,
        borderLeft: `1px solid ${C.line}`,
        background: C.panel,
        padding: '26px 28px',
        boxSizing: 'border-box',
      }}
    >
      {!running ? (
        <div style={{ fontSize: 19, lineHeight: 1.5, color: C.muted }}>The transcript appears after Voice Cut has run.</div>
      ) : (
        <div style={{ opacity: appear, transform: `translateY(${(1 - appear) * 12}px)` }}>
          <div style={{ fontSize: 21, fontWeight: 700, color: finished ? C.accent : C.strong }}>
            {finished ? 'Voice Cut finished' : 'Voice Cut is editing the recording'}
          </div>
          <div style={{ fontSize: 16, color: C.muted, marginTop: 6 }}>Gemma 4 31B · on this computer</div>
          <div style={{ marginTop: 22, display: 'flex', flexDirection: 'column', gap: 14 }}>
            {STAGES.map((label, index) => {
              const started = index === 0 ? RUN : STAGE_DONE[index - 1];
              const done = t >= STAGE_DONE[index];
              const active = t >= started && !done;
              const check = pop(t, STAGE_DONE[index], 60, { damping: 12, stiffness: 220, mass: 0.5 });
              return (
                <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 18, color: done ? C.stageDone : active ? C.strong : C.dim }}>
                  <span style={{ width: 22, height: 22, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                    {done ? (
                      <span style={{ transform: `scale(${check})` }}>
                        <Check size={20} color={C.accent} stroke={2.6} />
                      </span>
                    ) : active ? (
                      <Spinner size={20} color={C.strong} angle={t * 540} />
                    ) : (
                      <span style={{ width: 14, height: 14, borderRadius: 7, border: `2px dashed ${C.dim}` }} />
                    )}
                  </span>
                  {label}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
};

const Transport: React.FC<{ t: number; dropped: number }> = ({ dropped }) => (
  <div
    style={{
      position: 'absolute',
      left: 0,
      top: TRANSPORT_TOP,
      width: WIN.w,
      height: 64,
      borderTop: `1px solid ${C.line}`,
      borderBottom: `1px solid ${C.line}`,
      background: C.panel,
      display: 'flex',
      alignItems: 'center',
      gap: 22,
      paddingLeft: 28,
    }}
  >
    <div style={{ width: 44, height: 44, borderRadius: 22, background: C.strong, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <svg width="18" height="18" viewBox="0 0 24 24">
        <path d="M7 4v16l13-8z" fill="#111" />
      </svg>
    </div>
    <span style={{ fontFamily: MONO, fontSize: 22, color: C.text }}>
      0:00.000 <span style={{ color: C.muted }}>/ {dropped > 0.5 ? '0:38.000' : '0:00.000'}</span>
    </span>
  </div>
);

const Track: React.FC<{ t: number; dropped: number }> = ({ t, dropped }) => {
  const keep = ramp(t, KEEP, 0.8);
  const ticks = Array.from({ length: 8 }, (_, index) => index * 5);
  return (
    <>
      <div style={{ position: 'absolute', left: TRACK.x, top: TRACK.y - 34, width: TRACK.w, height: 24, fontFamily: MONO, fontSize: 15, color: C.dim }}>
        {ticks.map((second) => (
          <span key={second} style={{ position: 'absolute', left: (second / EX.duration) * TRACK.w }}>
            0:{String(second).padStart(2, '0')}
          </span>
        ))}
      </div>
      <div
        style={{
          position: 'absolute',
          left: TRACK.x,
          top: TRACK.y,
          width: TRACK.w * dropped,
          height: TRACK.h,
          borderRadius: 10,
          background: 'rgba(52,199,160,0.10)',
          border: `1px solid rgba(52,199,160,${0.4 * dropped})`,
          overflow: 'hidden',
        }}
      >
        <div style={{ position: 'absolute', left: 12, top: 8, fontSize: 15, fontWeight: 600, color: C.accent, opacity: dropped }}>
          narration_take.wav · 38.0 s
        </div>
        <div style={{ position: 'absolute', left: 0, top: 26 }}>
          <Waveform
            peaks={EX.raw}
            rate={EX.rate}
            from={0}
            to={EX.duration}
            width={TRACK.w}
            height={TRACK.h - 34}
            barWidth={2}
            gap={1.4}
            reveal={dropped}
            color={(time) => {
              const region = regionAt(time);
              return region ? interpolateColors(keep, [0, 1], [C.accent, KIND_COLOR[region.kind]]) : C.accent;
            }}
          />
        </div>
      </div>
    </>
  );
};

const PlannerMenu: React.FC<{ t: number; open: number }> = ({ t, open }) => {
  if (open <= 0) return null;
  const hover = t >= HOVER_LOCAL ? 0 : 1;
  return (
    <div
      style={{
        position: 'absolute',
        left: MENU.x,
        top: MENU.y,
        width: MENU.w,
        padding: 8,
        borderRadius: 14,
        background: C.panel2,
        border: `1px solid ${C.lineStrong}`,
        boxShadow: '0 30px 80px rgba(0,0,0,0.6)',
        opacity: open,
        transform: `translateY(${(1 - open) * -10}px) scale(${0.97 + 0.03 * open})`,
        transformOrigin: 'top left',
      }}
    >
      {PLANNERS.map((planner, index) => (
        <div
          key={planner.name}
          style={{
            height: MENU.item,
            borderRadius: 10,
            padding: '0 16px',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            background: index === hover ? 'rgba(52,199,160,0.16)' : 'transparent',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 20, fontWeight: 650, color: C.strong }}>
            {planner.name}
            {planner.local && (
              <span style={{ fontSize: 14, fontWeight: 700, color: C.accent, background: 'rgba(52,199,160,0.14)', padding: '3px 9px', borderRadius: 999 }}>
                FREE · PRIVATE
              </span>
            )}
          </div>
          <div style={{ fontSize: 16, color: C.muted, marginTop: 3 }}>{planner.detail}</div>
        </div>
      ))}
    </div>
  );
};

/** The recording, carried in by the pointer and dropped on the window. */
const DropCard: React.FC<{ t: number }> = ({ t }) => {
  const carry = ramp(t, VO.vo04.start + 0.1, 0.6);
  const drop = ramp(t, DROP, 0.35);
  if (t < ENTER || drop >= 1) return null;
  const pointer = pointerAt(t);
  return (
    <div
      style={{
        position: 'absolute',
        left: pointer.x + 18,
        top: pointer.y + 22,
        padding: '16px 22px',
        borderRadius: 14,
        background: C.panel2,
        border: `1px solid ${C.lineStrong}`,
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        fontFamily: FONT,
        fontSize: 21,
        fontWeight: 600,
        color: C.strong,
        boxShadow: '0 24px 60px rgba(0,0,0,0.55)',
        opacity: carry * (1 - drop),
        transform: `scale(${1 - drop * 0.5}) rotate(${(1 - carry) * -6}deg)`,
      }}
    >
      <FileAudio size={30} color={C.accent} />
      <div>
        narration_take.wav
        <div style={{ fontFamily: MONO, fontSize: 16, color: C.muted, fontWeight: 400 }}>0:38 · retakes and all</div>
      </div>
    </div>
  );
};
