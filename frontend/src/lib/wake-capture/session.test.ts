import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { WakeDetection, WakeSnapshot, WakeTap } from '../wake/controller';
import { WakeCaptureSession, type CaptureDeps, type CaptureSnapshot } from './session';
import { handsFreePhase } from './phase';

const CHUNK = 1280;
const LOUD = 3000;
const QUIET = 20;

class FakeHost {
  tap: WakeTap | null = null;
  paused = false;
  pauseCalls: boolean[] = [];
  private listeners = new Set<(s: WakeSnapshot) => void>();
  setTap(tap: WakeTap | null) { this.tap = tap; }
  setDetectionPaused(p: boolean) { this.paused = p; this.pauseCalls.push(p); }
  subscribe(fn: (s: WakeSnapshot) => void) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(state: WakeSnapshot['state']) {
    const snap: WakeSnapshot = { state, modelId: 'jarvis', error: null, detection: null, droppedChunks: 0 };
    this.listeners.forEach((fn) => fn(snap));
  }
  chunk(level: number, n = 1) {
    for (let i = 0; i < n; i++) this.tap?.onChunk(new Float32Array(CHUNK).fill(level));
  }
  wake() {
    const d: WakeDetection = { modelId: 'jarvis', phrase: 'Jarvis', at: 1, score: 0.9 };
    this.tap?.onDetection(d);
  }
}

interface Pending { audio: Blob; filename: string; signal: AbortSignal; resolve(v: { text: string }): void; reject(e: unknown): void }

function build(opts: Partial<ConstructorParameters<typeof WakeCaptureSession>[1]> = {}) {
  const host = new FakeHost();
  const calls: Pending[] = [];
  const timers: Array<{ fn: () => void; ms: number; live: boolean }> = [];
  const deps: CaptureDeps = {
    host,
    transcribe: (audio, filename, signal) =>
      new Promise((resolve, reject) => { calls.push({ audio, filename, signal, resolve, reject }); }),
    setTimeout: (fn, ms) => { timers.push({ fn, ms, live: true }); return timers.length - 1; },
    clearTimeout: (h) => { const t = timers[h as number]; if (t) t.live = false; },
  };
  const session = new WakeCaptureSession(deps, opts);
  return { host, session, calls, timers };
}

const tick = () => new Promise((r) => setTimeout(r, 0));
/** Wake, a short request, then enough silence to end it. */
function speakRequest(h: ReturnType<typeof build>) {
  h.host.wake();
  h.host.chunk(LOUD, 10);
  h.host.chunk(QUIET, 15);
}

describe('wake → capture', () => {
  it('stays waiting, keeps only pre-roll, and sends nothing before a wake', () => {
    const h = build();
    h.host.chunk(LOUD, 50);
    expect(h.session.getSnapshot().phase).toBe('waiting');
    expect(h.calls).toHaveLength(0);
    expect(h.host.paused).toBe(false);
  });

  it('moves to listening on detection and pauses the detector', () => {
    const h = build();
    h.host.wake();
    expect(h.session.getSnapshot().phase).toBe('listening');
    expect(h.host.paused).toBe(true);
  });

  it('ignores a second detection while a request is active', () => {
    const h = build();
    h.host.wake();
    h.host.chunk(LOUD, 5);
    h.host.wake();
    expect(h.host.pauseCalls).toEqual([true]);
  });

  it('includes the pre-roll and the speech right after the phrase, as a single 16 kHz WAV', async () => {
    const h = build({ preRollMs: 800 }); // 10 chunks
    h.host.chunk(QUIET, 30); // older audio is out of the pre-roll window
    h.host.chunk(LOUD, 10); // the wake phrase and the first words
    h.host.wake();
    h.host.chunk(LOUD, 10);
    h.host.chunk(QUIET, 15);
    await tick();
    expect(h.calls).toHaveLength(1);
    const call = h.calls[0];
    expect(call.filename).toBe('request.wav');
    expect(call.audio.type).toBe('audio/wav');
    // 10 pre-roll + 10 speech + 15 trailing silence chunks, 16-bit samples, 44-byte header
    expect(call.audio.size).toBe(44 + (10 + 10 + 15) * CHUNK * 2);
    const view = new DataView(await call.audio.arrayBuffer());
    expect(view.getUint32(24, true)).toBe(16000);
    expect(view.getInt16(44, true)).toBe(LOUD); // first pre-roll sample is loud, not the older silence
  });

  it('never keeps more than the two-second maximum pre-roll', async () => {
    const h = build({ preRollMs: 60_000 });
    h.host.chunk(LOUD, 100);
    h.host.wake();
    h.host.chunk(LOUD, 5);
    h.host.chunk(QUIET, 15);
    await tick();
    expect(h.calls[0].audio.size).toBe(44 + (25 + 5 + 15) * CHUNK * 2);
  });

  it('transitions listening → transcribing → waiting with a cleaned transcript', async () => {
    const h = build();
    speakRequest(h);
    expect(h.session.getSnapshot().phase).toBe('transcribing');
    expect(h.host.paused).toBe(true);
    h.calls[0].resolve({ text: ' Jarvis, what is the time? ' });
    await tick();
    const snap = h.session.getSnapshot();
    expect(snap.phase).toBe('waiting');
    expect(snap.transcripts.map((t) => t.text)).toEqual(['what is the time?']);
    expect(h.host.paused).toBe(false);
    expect(handsFreePhase('listening', snap)).toBe('ready');
  });

  it('can take another request after the first', async () => {
    const h = build();
    speakRequest(h);
    h.calls[0].resolve({ text: 'first one' });
    await tick();
    speakRequest(h);
    h.calls[1].resolve({ text: 'second one' });
    await tick();
    expect(h.session.getSnapshot().transcripts.map((t) => t.text)).toEqual(['first one', 'second one']);
  });

  it('keeps at most three unreviewed transcripts, dropping the oldest', async () => {
    const h = build();
    for (let i = 1; i <= 4; i++) {
      speakRequest(h);
      h.calls[i - 1].resolve({ text: `request ${i}` });
      await tick();
    }
    expect(h.session.getSnapshot().transcripts.map((t) => t.text)).toEqual(['request 2', 'request 3', 'request 4']);
  });
});

describe('endpointing outcomes', () => {
  it('gives no_speech and uploads nothing when nobody speaks within 6 s', () => {
    const h = build();
    h.host.wake();
    h.host.chunk(QUIET, 75);
    const snap = h.session.getSnapshot();
    expect(snap.phase).toBe('waiting');
    expect(snap.outcome).toBe('no_speech');
    expect(h.calls).toHaveLength(0);
    expect(h.host.paused).toBe(false);
    expect(handsFreePhase('listening', snap)).toBe('no_speech');
  });

  it('stops at the 20 s hard limit and still transcribes what was said', async () => {
    const h = build();
    h.host.wake();
    h.host.chunk(LOUD, 250);
    await tick();
    expect(h.calls).toHaveLength(1);
    expect(h.session.getSnapshot().phase).toBe('transcribing');
  });

  it.each(['', '   ', '[BLANK_AUDIO]', '...', 'Jarvis'])('treats transcript %j as no speech', async (text) => {
    const h = build();
    speakRequest(h);
    h.calls[0].resolve({ text });
    await tick();
    const snap = h.session.getSnapshot();
    expect(snap.outcome).toBe('no_speech');
    expect(snap.transcripts).toHaveLength(0);
    expect(h.host.paused).toBe(false);
  });

  it('treats the backend "no speech" error as no speech', async () => {
    const h = build();
    speakRequest(h);
    h.calls[0].reject(new Error('No audio data'));
    await tick();
    expect(h.session.getSnapshot().outcome).toBe('no_speech');
  });
});

describe('failures and recovery', () => {
  it('reports a transcription error, re-arms, and works again', async () => {
    const h = build();
    speakRequest(h);
    h.calls[0].reject(new Error('Local speech recognition crashed'));
    await tick();
    let snap = h.session.getSnapshot();
    expect(snap.outcome).toBe('error');
    expect(snap.error).toBe('Local speech recognition crashed');
    expect(h.host.paused).toBe(false);
    expect(handsFreePhase('listening', snap)).toBe('error');

    speakRequest(h);
    expect(h.session.getSnapshot().outcome).toBe('none');
    h.calls[1].resolve({ text: 'try again' });
    await tick();
    snap = h.session.getSnapshot();
    expect(snap.transcripts.map((t) => t.text)).toEqual(['try again']);
  });

  it('times out a stuck transcription, aborts it and re-arms', async () => {
    const h = build();
    speakRequest(h);
    const timer = h.timers.find((t) => t.ms === 60_000 && t.live);
    expect(timer).toBeDefined();
    timer!.fn();
    expect(h.calls[0].signal.aborted).toBe(true);
    h.calls[0].reject(new DOMException('aborted', 'AbortError'));
    await tick();
    const snap = h.session.getSnapshot();
    expect(snap.outcome).toBe('error');
    expect(snap.error).toMatch(/timed out/i);
    expect(h.host.paused).toBe(false);
  });

  it('resets when the detector stops (mic ended, worklet failure, switched off)', async () => {
    for (const state of ['off', 'error'] as const) {
      const h = build();
      h.host.wake();
      h.host.chunk(LOUD, 5);
      h.host.emit(state);
      expect(h.session.getSnapshot().phase).toBe('waiting');
      h.host.chunk(QUIET, 40);
      await tick();
      expect(h.calls).toHaveLength(0);
    }
  });

  it('aborts an in-flight transcription when the detector stops and ignores its late result', async () => {
    const h = build();
    speakRequest(h);
    h.host.emit('off');
    expect(h.calls[0].signal.aborted).toBe(true);
    h.calls[0].resolve({ text: 'late words' });
    await tick();
    expect(h.session.getSnapshot().transcripts).toHaveLength(0);
  });
});

describe('cancellation', () => {
  it('cancel during capture uploads nothing, clears audio and returns to waiting', async () => {
    const h = build();
    h.host.wake();
    h.host.chunk(LOUD, 8);
    h.session.cancel();
    expect(h.session.getSnapshot()).toMatchObject({ phase: 'waiting', outcome: 'none' });
    expect(h.host.paused).toBe(false);
    h.host.chunk(LOUD, 5); // arrives after cancel: only pre-roll now
    await tick();
    expect(h.calls).toHaveLength(0);
  });

  it('cancel during transcription aborts the request and inserts nothing', async () => {
    const h = build();
    speakRequest(h);
    h.session.cancel();
    expect(h.calls[0].signal.aborted).toBe(true);
    h.calls[0].resolve({ text: 'should not appear' });
    await tick();
    const snap = h.session.getSnapshot();
    expect(snap.phase).toBe('waiting');
    expect(snap.transcripts).toHaveLength(0);
    expect(h.host.paused).toBe(false);
  });

  it('a fresh request after cancel is not affected by the cancelled one', async () => {
    const h = build();
    speakRequest(h);
    h.session.cancel();
    speakRequest(h);
    h.calls[0].resolve({ text: 'old' });
    h.calls[1].resolve({ text: 'new' });
    await tick();
    expect(h.session.getSnapshot().transcripts.map((t) => t.text)).toEqual(['new']);
  });

  it('dispose detaches the tap', () => {
    const h = build();
    h.session.dispose();
    expect(h.host.tap).toBeNull();
  });

  it('keeps unreviewed transcripts (text only) when the detector is turned off, and lets them be dismissed', async () => {
    const h = build();
    speakRequest(h);
    h.calls[0].resolve({ text: 'keep me' });
    await tick();
    h.host.emit('off');
    const [t] = h.session.getSnapshot().transcripts;
    expect(t.text).toBe('keep me');
    h.session.dismissTranscript(t.id);
    expect(h.session.getSnapshot().transcripts).toHaveLength(0);
  });
});

describe('phase composition', () => {
  const idle: CaptureSnapshot = { phase: 'waiting', outcome: 'none', error: null, transcripts: [] };
  it('maps detector and capture state to one label', () => {
    expect(handsFreePhase('off', idle)).toBe('off');
    expect(handsFreePhase('error', idle)).toBe('error');
    expect(handsFreePhase('initialising', idle)).toBe('initialising');
    expect(handsFreePhase('listening', idle)).toBe('waiting');
    expect(handsFreePhase('detected', { ...idle, phase: 'listening' })).toBe('listening');
    expect(handsFreePhase('listening', { ...idle, phase: 'transcribing' })).toBe('transcribing');
  });
});

beforeEach(() => vi.useRealTimers());
