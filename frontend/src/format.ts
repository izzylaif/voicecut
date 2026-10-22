/** Human-readable times. */

/** `m:ss.mmm` (or `h:mm:ss.mmm`) for a time in seconds. */
export function timecode(seconds: number, digits = 3): string {
  const sign = seconds < 0 ? '-' : '';
  const scale = 10 ** digits;
  const total = Math.round(Math.abs(seconds) * scale);
  const fraction = total % scale;
  const whole = Math.floor(total / scale);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  const tail = digits > 0 ? `.${String(fraction).padStart(digits, '0')}` : '';
  const ss = String(secs).padStart(2, '0');
  return hours > 0
    ? `${sign}${hours}:${String(minutes).padStart(2, '0')}:${ss}${tail}`
    : `${sign}${minutes}:${ss}${tail}`;
}

/** Short duration such as `0.25 s`, `12.4 s` or `3 min 05 s`. */
export function duration(seconds: number): string {
  const value = Math.abs(seconds);
  if (value < 10) return `${seconds.toFixed(2)} s`;
  if (value < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(value / 60);
  const rest = Math.round(value - minutes * 60);
  return `${seconds < 0 ? '-' : ''}${minutes} min ${String(rest).padStart(2, '0')} s`;
}

/** Signed delta for drag feedback, e.g. `+0.240 s`. */
export function delta(seconds: number): string {
  return `${seconds >= 0 ? '+' : '−'}${Math.abs(seconds).toFixed(3)} s`;
}
