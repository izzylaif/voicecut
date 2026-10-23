import { Easing, interpolate, spring, useCurrentFrame, useVideoConfig } from 'remotion';

/** The film's time in seconds (a Sequence's local time inside one). */
export function useTime(): number {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return frame / fps;
}

export const clamp = (value: number, low = 0, high = 1) => Math.min(high, Math.max(low, value));
export const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

/** 0 before ``at``, easing to 1 over ``duration`` seconds. */
export function ramp(t: number, at: number, duration = 0.6, easing = Easing.bezier(0.22, 1, 0.36, 1)): number {
  return interpolate(t, [at, at + duration], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing,
  });
}

/** A spring that starts at ``at`` seconds (0 → 1, may overshoot a little). */
export function pop(t: number, at: number, fps: number, config = { damping: 14, stiffness: 160, mass: 0.7 }): number {
  if (t < at) return 0;
  return spring({ frame: Math.round((t - at) * fps), fps, config });
}

/** Fade in at ``a``, fade out at ``b`` (each over ``edge`` seconds). */
export function between(t: number, a: number, b: number, edge = 0.4): number {
  return Math.min(ramp(t, a, edge), 1 - ramp(t, b - edge, edge));
}

export function clock(seconds: number, digits = 1): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds - minutes * 60;
  return `${minutes}:${rest.toFixed(digits).padStart(digits ? digits + 3 : 2, '0')}`;
}
