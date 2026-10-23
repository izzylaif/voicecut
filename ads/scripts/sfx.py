"""Synthesize the film's sound effects and a soft pad into public/sfx/.

Everything is made from sines and filtered noise, deterministically, so the
film needs no third-party audio.

    python scripts/sfx.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "public" / "sfx"
RATE = 48_000
rng = np.random.default_rng(7)


def t(seconds: float) -> np.ndarray:
    return np.arange(int(seconds * RATE)) / RATE


def env(length: float, attack: float, decay: float) -> np.ndarray:
    time = t(length)
    rise = np.clip(time / max(attack, 1e-4), 0, 1)
    return rise * np.exp(-np.maximum(time - attack, 0) / decay)


def lowpass(signal: np.ndarray, cutoff: np.ndarray | float) -> np.ndarray:
    """One-pole low-pass with a (possibly moving) cutoff in Hz."""

    cutoff = np.broadcast_to(np.asarray(cutoff, dtype=float), signal.shape)
    alpha = 1 - np.exp(-2 * np.pi * cutoff / RATE)
    out = np.empty_like(signal)
    state = 0.0
    for index, (x, a) in enumerate(zip(signal, alpha)):
        state += a * (x - state)
        out[index] = state
    return out


def bandpass(signal: np.ndarray, centre: np.ndarray | float) -> np.ndarray:
    return lowpass(signal, np.asarray(centre) * 1.6) - lowpass(signal, np.asarray(centre) * 0.6)


def normalize(signal: np.ndarray, peak: float) -> np.ndarray:
    return signal / (np.abs(signal).max() + 1e-12) * peak


def save(name: str, signal: np.ndarray, peak: float = 0.9) -> None:
    fade = min(len(signal), int(0.004 * RATE))
    signal = signal.copy()
    signal[-fade:] *= np.linspace(1, 0, fade)
    sf.write(OUT / f"{name}.wav", normalize(signal, peak).astype(np.float32), RATE)


def click() -> np.ndarray:
    time = t(0.06)
    body = np.sin(2 * np.pi * 2300 * time) * env(0.06, 0.0005, 0.006)
    snap = bandpass(rng.standard_normal(len(time)), 4000) * env(0.06, 0.0002, 0.003)
    return body * 0.6 + snap


def tick(pitch: float = 1320) -> np.ndarray:
    time = t(0.35)
    tone = np.sin(2 * np.pi * pitch * time) + 0.35 * np.sin(2 * np.pi * pitch * 2.01 * time)
    return tone * env(0.35, 0.002, 0.07)


def pop() -> np.ndarray:
    time = t(0.25)
    frequency = 420 + 520 * np.exp(-time / 0.03)
    phase = 2 * np.pi * np.cumsum(frequency) / RATE
    return np.sin(phase) * env(0.25, 0.002, 0.05)


def whoosh(length: float = 0.9) -> np.ndarray:
    time = t(length)
    shape = np.sin(np.pi * np.clip(time / length, 0, 1)) ** 2
    centre = 350 + 2600 * np.sin(np.pi * np.clip(time / length, 0, 1)) ** 1.5
    return bandpass(rng.standard_normal(len(time)), centre) * shape


def swell() -> np.ndarray:
    length = 2.6
    time = t(length)
    rise = np.clip(time / 0.9, 0, 1) ** 2 * np.exp(-np.maximum(time - 0.9, 0) / 0.6)
    noise = bandpass(rng.standard_normal(len(time)), 900 + 2400 * np.clip(time / 0.9, 0, 1)) * rise * 0.5
    bloom = np.zeros_like(time)
    for frequency, gain in ((261.63, 1.0), (392.0, 0.7), (523.25, 0.55), (659.25, 0.4), (987.77, 0.18)):
        bloom += gain * np.sin(2 * np.pi * frequency * time)
    bloom *= np.clip((time - 0.75) / 0.05, 0, 1) * np.exp(-np.maximum(time - 0.8, 0) / 0.7)
    sub = np.sin(2 * np.pi * 55 * time) * np.clip((time - 0.75) / 0.03, 0, 1) * np.exp(-np.maximum(time - 0.78, 0) / 0.35)
    return noise + bloom * 0.35 + sub * 0.6


def snip() -> np.ndarray:
    length = 0.22
    time = t(length)
    signal = np.zeros_like(time)
    for at in (0.0, 0.055):
        start = int(at * RATE)
        burst = bandpass(rng.standard_normal(int(0.03 * RATE)), 6000) * env(0.03, 0.0003, 0.004)
        signal[start : start + len(burst)] += burst
    ring = np.sin(2 * np.pi * 3150 * time) * env(length, 0.06, 0.03) * np.clip((time - 0.055) * 400, 0, 1)
    return signal + ring * 0.25


def chime() -> np.ndarray:
    length = 3.2
    time = t(length)
    signal = np.zeros_like(time)
    for index, frequency in enumerate((523.25, 659.25, 783.99, 1046.5, 1174.66)):
        start = index * 0.045
        part = np.clip((time - start) / 0.004, 0, 1) * np.exp(-np.maximum(time - start, 0) / 1.1)
        signal += np.sin(2 * np.pi * frequency * time) * part * (1 - index * 0.13)
    return signal


def pad(length: float) -> np.ndarray:
    """A slow, warm chord bed: Fmaj9, Am7, Cmaj7, G6 — four seconds each."""

    chords = [
        (87.31, 174.61, 220.0, 261.63, 329.63, 392.0),
        (110.0, 164.81, 220.0, 261.63, 329.63, 392.0),
        (130.81, 196.0, 246.94, 329.63, 392.0, 493.88),
        (98.0, 196.0, 246.94, 293.66, 329.63, 392.0),
    ]
    time = t(length)
    signal = np.zeros_like(time)
    span = 4.0
    for index in range(int(np.ceil(length / span)) + 1):
        chord = chords[index % len(chords)]
        start = index * span - 0.8
        local = time - start
        shape = np.clip(local / 1.6, 0, 1) * np.clip((span + 1.6 - local) / 1.6, 0, 1)
        shape = np.where((local > 0) & (local < span + 1.6), shape, 0)
        for voice, frequency in enumerate(chord):
            for detune in (-0.12, 0.12):
                gain = (0.9 if voice == 0 else 0.45) / (1 + voice * 0.15)
                signal += gain * shape * np.sin(2 * np.pi * (frequency + detune) * time + voice)
    tremolo = 0.85 + 0.15 * np.sin(2 * np.pi * 0.17 * time)
    edges = np.clip(time / 2.0, 0, 1) * np.clip((length - time) / 2.5, 0, 1)
    return signal * tremolo * edges


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    save("click", click(), 0.8)
    save("tick", tick(), 0.7)
    save("tick-high", tick(1760), 0.7)
    save("pop", pop(), 0.8)
    save("whoosh", whoosh(), 0.8)
    save("whoosh-long", whoosh(1.4), 0.8)
    save("swell", swell(), 0.85)
    save("snip", snip(), 0.8)
    save("chime", chime(), 0.8)
    save("pad", pad(70.0), 0.8)
    for path in sorted(OUT.glob("*.wav")):
        info = sf.info(path)
        print(f"{path.name:18} {info.duration:5.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
