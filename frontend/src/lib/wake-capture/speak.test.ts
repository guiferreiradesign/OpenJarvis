import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api', () => ({
  synthesizeSpeech: vi.fn(),
  fetchTtsHealth: vi.fn(),
  transcribeAudio: vi.fn(),
  getBase: () => '',
}));
vi.mock('../wake/store', () => ({
  getWakeController: () => ({ setTap: vi.fn(), setDetectionPaused: vi.fn(), subscribe: () => () => {} }),
  useWakeStore: { getState: () => ({ state: 'listening' }), subscribe: () => () => {} },
}));

import { fetchTtsHealth, synthesizeSpeech } from '../api';
import { __resetTtsForTests, shouldAutoplayFinishedReply, useTtsStore } from '../tts';
import { __resetBridgeForTests, isVoiceReply } from './bridge';
import { speakAndWait } from './store';

class FakeAudio {
  static instances: FakeAudio[] = [];
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public src: string) { FakeAudio.instances.push(this); }
  play() { return Promise.resolve(); }
  pause() {}
}

const synth = vi.mocked(synthesizeSpeech);
const health = vi.mocked(fetchTtsHealth);
const tick = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  FakeAudio.instances = [];
  vi.stubGlobal('Audio', FakeAudio as unknown as typeof Audio);
  vi.stubGlobal('URL', { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} });
  synth.mockReset();
  health.mockReset();
  health.mockResolvedValue({ available: true, backend: 'voicestudio', voice_id: 'bm_george' });
  synth.mockResolvedValue(new Blob(['x'], { type: 'audio/wav' }));
  __resetTtsForTests();
  __resetBridgeForTests();
});
afterEach(() => vi.unstubAllGlobals());

describe('speaking a hands-free reply', () => {
  it('synthesises the finished reply once through the shared queue and resolves when it ends', async () => {
    let resolved = false;
    const done = speakAndWait('m1', 'Good evening. <think>hidden</think>It is nine o clock.').then(() => { resolved = true; });
    await tick();
    expect(synth).toHaveBeenCalledTimes(1);
    expect(synth.mock.calls[0][0]).not.toContain('hidden');
    expect(useTtsStore.getState().state).toBe('speaking');
    expect(resolved).toBe(false); // still playing: the detector must stay paused
    FakeAudio.instances[0].onended?.();
    await done;
    expect(resolved).toBe(true);
    expect(useTtsStore.getState().state).toBe('idle');
  });

  it('marks the reply so typed-message autoplay cannot also speak it', async () => {
    const done = speakAndWait('m2', 'Hello');
    await tick();
    FakeAudio.instances[0].onended?.();
    await done;
    expect(isVoiceReply('m2')).toBe(true);
    expect(useTtsStore.getState().autoSpokenId).toBe('m2');
  });

  it('does not speak error placeholders, cancellation notices or empty text', async () => {
    for (const text of ['Error: Chat request failed: 500', '(Generation stopped)', 'This request timed out after 3 minutes. Please try again.', 'No response was generated. Please try again.', '   ', '<think>only thoughts</think>']) {
      await speakAndWait('m3', text);
    }
    expect(synth).not.toHaveBeenCalled();
  });

  it('does not synthesise when the local voice service is unavailable', async () => {
    health.mockResolvedValue({ available: false });
    await speakAndWait('m4', 'Hello');
    expect(synth).not.toHaveBeenCalled();
  });

  it('resolves (so the detector re-arms) when synthesis fails', async () => {
    synth.mockRejectedValue(new Error('VoiceStudio is down'));
    await speakAndWait('m5', 'Hello');
    expect(useTtsStore.getState().state).toBe('idle');
  });

  it('resolves when playback is stopped from outside (cancel / Off)', async () => {
    const done = speakAndWait('m6', 'A long answer');
    await tick();
    useTtsStore.getState().stop();
    await done;
    expect(useTtsStore.getState().state).toBe('idle');
  });

  it('a second reply replaces the first instead of overlapping it', async () => {
    const first = speakAndWait('a', 'First answer');
    await tick();
    const second = speakAndWait('b', 'Second answer');
    await tick();
    await first; // taken over by the second
    expect(useTtsStore.getState().speakingId).toBe('b');
    FakeAudio.instances[FakeAudio.instances.length - 1].onended?.();
    await second;
  });
});

describe('typed-message playback is unchanged', () => {
  const last = { id: 'typed-1', role: 'assistant' };
  it('still autoplays a finished typed reply exactly as before', () => {
    expect(shouldAutoplayFinishedReply('c1', 'c1', false, last, null)).toBe(true);
    expect(shouldAutoplayFinishedReply('c1', 'c1', false, last, null, false)).toBe(true);
  });

  it('keeps its other guards (streaming, already spoken, wrong conversation)', () => {
    expect(shouldAutoplayFinishedReply('c1', 'c1', true, last, null)).toBe(false);
    expect(shouldAutoplayFinishedReply('c1', 'c1', false, last, 'typed-1')).toBe(false);
    expect(shouldAutoplayFinishedReply('c1', 'c2', false, last, null)).toBe(false);
  });

  it('skips only replies that belong to a hands-free voice turn', () => {
    expect(shouldAutoplayFinishedReply('c1', 'c1', false, last, null, true)).toBe(false);
  });
});
