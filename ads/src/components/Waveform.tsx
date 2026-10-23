import React from 'react';

export type WaveformProps = {
  peaks: number[];
  /** Peaks per second of ``peaks``. */
  rate: number;
  /** Recording time (s) at the left and right edges. */
  from: number;
  to: number;
  width: number;
  height: number;
  barWidth?: number;
  gap?: number;
  /** Colour of the bar at recording time ``t``. */
  color?: (t: number) => string;
  /** Height multiplier at ``t`` (morphing, emphasis). */
  gain?: (t: number) => number;
  /** Opacity at ``t``. */
  alpha?: (t: number) => number;
  /** Custom x (px) of time ``t`` (layouts that move clips around). */
  xOf?: (t: number) => number;
  /** Final say over a bar's height (px) at ``t``. */
  shape?: (t: number, height: number) => number;
  /** Bars right of reveal * width are not drawn yet. */
  reveal?: number;
  minHeight?: number;
  /** Amplitude curve: below 1 makes quiet parts fuller. */
  curve?: number;
  style?: React.CSSProperties;
};

/** Mirrored, round-capped bars of a recording's loudness. */
export const Waveform: React.FC<WaveformProps> = ({
  peaks,
  rate,
  from,
  to,
  width,
  height,
  barWidth = 3,
  gap = 2,
  color = () => '#34c7a0',
  gain = () => 1,
  alpha = () => 1,
  xOf,
  shape,
  reveal = 1,
  minHeight = 2,
  curve = 0.62,
  style,
}) => {
  const pitch = barWidth + gap;
  const count = Math.max(1, Math.floor(width / pitch));
  const span = (to - from) / count;
  const groups = new Map<string, string[]>();
  const middle = height / 2;
  for (let index = 0; index < count; index++) {
    const t0 = from + index * span;
    const t1 = t0 + span;
    const tm = (t0 + t1) / 2;
    const x = xOf ? xOf(tm) : index * pitch + barWidth / 2;
    if (!xOf && x > reveal * width) break;
    const a = Math.round(alpha(tm) * 20) / 20;
    if (a <= 0) continue;
    let value = 0;
    const first = Math.max(0, Math.floor(t0 * rate));
    const last = Math.min(peaks.length, Math.max(first + 1, Math.ceil(t1 * rate)));
    for (let bin = first; bin < last; bin++) value = Math.max(value, peaks[bin] ?? 0);
    const scaled = Math.pow(value, curve) * (height - barWidth) * gain(tm);
    const h = Math.max(minHeight, shape ? shape(tm, scaled) : scaled);
    const key = `${color(tm)}|${a}`;
    const segment = `M${x.toFixed(1)} ${(middle - h / 2).toFixed(1)}V${(middle + h / 2).toFixed(1)}`;
    const list = groups.get(key);
    if (list) list.push(segment);
    else groups.set(key, [segment]);
  }
  return (
    <svg width={width} height={height} style={{ overflow: 'visible', ...style }}>
      {[...groups.entries()].map(([key, segments]) => {
        const [stroke, opacity] = key.split('|');
        return (
          <path
            key={key}
            d={segments.join('')}
            stroke={stroke}
            strokeOpacity={Number(opacity)}
            strokeWidth={barWidth}
            strokeLinecap="round"
            fill="none"
          />
        );
      })}
    </svg>
  );
};
