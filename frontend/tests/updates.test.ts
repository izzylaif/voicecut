import { describe, expect, it } from 'vitest';
import { updateButton, type UpdateStatus } from '../src/updates';

const none: UpdateStatus = {
  current: '0.2.0',
  version: null,
  state: 'none',
  progress: null,
  error: null,
  automatic: true,
};

const offering = (changes: Partial<UpdateStatus>): UpdateStatus => ({ ...none, version: '0.3.0', ...changes });

describe('the update button', () => {
  it('is hidden without a newer version', () => {
    expect(updateButton(null)).toBeNull();
    expect(updateButton(none)).toBeNull();
  });

  it('offers the newer version', () => {
    expect(updateButton(offering({ state: 'available' }))).toEqual({ title: 'Update available: 0.3.0', busy: false });
    // Downloaded in the background: one click installs it.
    expect(updateButton(offering({ state: 'ready' }))).toEqual({ title: 'Update available: 0.3.0', busy: false });
    // A failed download can be tried again.
    expect(updateButton(offering({ state: 'available', error: 'offline' }))?.busy).toBe(false);
  });

  it('shows the download and the install while they run', () => {
    expect(updateButton(offering({ state: 'downloading', progress: 0.426 }))).toEqual({
      title: 'Downloading VoiceCut 0.3.0… 42%',
      busy: true,
    });
    expect(updateButton(offering({ state: 'downloading' }))?.title).toBe('Downloading VoiceCut 0.3.0…');
    expect(updateButton(offering({ state: 'installing' }))).toEqual({
      title: 'Installing VoiceCut 0.3.0…',
      busy: true,
    });
  });
});
