/**
 * Shows the picture of the edited timeline, driven by the audio clock.
 *
 * The proxy is the source video, upright (FFmpeg applied its rotation), so
 * the preview shows exactly the orientation of the export.  Two <video>
 * elements take turns: while one plays the current clip, the other waits,
 * already seeked, at the start of the next clip, so a cut shows the right
 * frame immediately instead of waiting for a seek.
 */
import { clipAt, type Layout } from '../model/layout';

const SEEK_TOLERANCE = 0.25;
const RESYNC_SECONDS = 0.2;
const NUDGE_SECONDS = 0.02;

interface Deck {
  element: HTMLVideoElement;
  /** Clip index this deck is cued to (or playing). */
  clip: number;
  target: number | null;
}

export class VideoSync {
  private readonly decks: [Deck, Deck];
  private active: 0 | 1 = 0;
  private wasPlaying = false;
  private lastFrame = -1;

  constructor(
    container: HTMLElement,
    url: string,
    private readonly offset: number,
    private readonly sampleRate: number,
  ) {
    const make = (): Deck => {
      const element = document.createElement('video');
      element.src = url;
      element.muted = true;
      element.playsInline = true;
      element.preload = 'auto';
      element.disablePictureInPicture = true;
      element.className = 'viewer-video';
      container.appendChild(element);
      const deck: Deck = { element, clip: -1, target: null };
      element.addEventListener('seeked', () => {
        // Apply the latest target that arrived while a seek was running.
        if (deck.target !== null && Math.abs(element.currentTime - deck.target) > 0.02) {
          element.currentTime = deck.target;
        }
      });
      return deck;
    };
    this.decks = [make(), make()];
    this.show();
  }

  private get other(): 0 | 1 {
    return this.active === 0 ? 1 : 0;
  }

  private show(): void {
    this.decks.forEach((deck, index) => {
      deck.element.style.opacity = index === this.active ? '1' : '0';
    });
  }

  private seek(deck: Deck, time: number): void {
    deck.target = time;
    if (!deck.element.seeking) deck.element.currentTime = time;
  }

  private proxyTime(sourceSample: number): number {
    return Math.max(0, sourceSample / this.sampleRate + this.offset);
  }

  /** Call every animation frame with the output frame being heard. */
  update(layout: Layout, frame: number, playing: boolean, rate = 1): void {
    const index = clipAt(layout, Math.min(frame, layout.total - 1));
    const active = this.decks[this.active];
    const standby = this.decks[this.other];
    if (index < 0) {
      active.element.pause();
      return;
    }
    const clip = layout.clips[index]!;
    const time = this.proxyTime(clip.sourceAtStart + (frame - clip.outStart));

    if (!playing) {
      if (this.wasPlaying) for (const deck of this.decks) deck.element.pause();
      this.wasPlaying = false;
      active.clip = index;
      if (Math.abs(active.element.currentTime - time) > 0.5 / 60) this.seek(active, time);
      return;
    }

    if (active.clip !== index) {
      if (standby.clip === index && Math.abs(standby.element.currentTime - time) < SEEK_TOLERANCE) {
        this.active = this.other;
        this.show();
        active.element.pause();
      } else {
        active.clip = index;
        if (Math.abs(active.element.currentTime - time) > NUDGE_SECONDS) this.seek(active, time);
      }
    }
    const current = this.decks[this.active];
    current.clip = index;
    // The audio clock stands still until the first samples are heard; the
    // picture waits for it instead of running ahead.
    const audioMoving = frame !== this.lastFrame;
    this.lastFrame = frame;
    if (current.element.paused) {
      if (!audioMoving) {
        if (Math.abs(current.element.currentTime - time) > 0.5 / 60) this.seek(current, time);
        return;
      }
      void current.element.play().catch(() => undefined);
    }
    // The picture runs at the audio's rate, nudged towards the audio clock.
    const drift = current.element.currentTime - time;
    if (Math.abs(drift) > RESYNC_SECONDS * Math.max(1, rate) && !current.element.seeking) {
      this.seek(current, time);
      current.element.playbackRate = rate;
    } else if (Math.abs(drift) > NUDGE_SECONDS) {
      current.element.playbackRate = rate * (1 - Math.max(-0.3, Math.min(0.3, (drift * 3) / rate)));
    } else {
      current.element.playbackRate = rate;
    }
    this.wasPlaying = true;

    // Cue the other deck at the first frame of the next clip.
    const other = this.decks[this.other];
    const next = layout.clips[index + 1];
    if (next && other.clip !== index + 1) {
      other.clip = index + 1;
      other.element.pause();
      this.seek(other, this.proxyTime(next.sourceAtStart));
    }
  }

  /** The timeline changed: cues prepared for the old layout are stale. */
  invalidate(): void {
    this.decks[this.other].clip = -1;
    this.decks[this.active].clip = -1;
  }

  dispose(): void {
    for (const deck of this.decks) {
      deck.element.pause();
      deck.element.removeAttribute('src');
      deck.element.load();
      deck.element.remove();
    }
  }
}
