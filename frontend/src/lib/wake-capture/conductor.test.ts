import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { WakeDetection, WakeSnapshot, WakeTap } from '../wake/controller';
import { __resetBridgeForTests, notifyDraftChanged, type ChatBridge, type VoiceTurnResult } from './bridge';
import { HandsFreeConductor, SUBMIT_COUNTDOWN_MS } from './conductor';
import { handsFreePhase } from './phase';
import { WakeCaptureSession } from './session';

const CHUNK = 1280;
const LOUD = 3000;
const QUIET = 20;

class FakeHost {
  tap: WakeTap | null = null;
  paused = false;
  private listeners = new Set<(s: WakeSnapshot) => void>();
  setTap(tap: WakeTap | null) { this.tap = tap; }
  setDetectionPaused(p: boolean) { this.paused = p; }
  subscribe(fn: (s: WakeSnapshot) => void) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(state: WakeSnapshot['state']) {
    const snap: WakeSnapshot = { state, modelId: 'jarvis', error: null, detection: null, droppedChunks: 0 };
    this.listeners.forEach((fn) => fn(snap));
  }
  chunk(level: number, n = 1) { for (let i = 0; i < n; i++) this.tap?.onChunk(new Float32Array(CHUNK).fill(level)); }
  wake() {
    const d: WakeDetection = { modelId: 'jarvis', phrase: 'Jarvis', at: 1, score: 0.9 };
    this.tap?.onDetection(d);
  }
}

interface Deferred<T> { promise: Promise<T>; resolve(v: T): void; reject(e: unknown): void }
function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
const tick = () => new Promise((r) => setTimeout(r, 0));

function build(opts: { draft?: string; canSubmit?: boolean; speechOn?: boolean; noBridge?: boolean; policyOk?: boolean } = {}) {
  __resetBridgeForTests();
  const host = new FakeHost();
  const timers: Array<{ fn: () => void; ms: number; live: boolean }> = [];
  let clock = 10_000;
  const state = { draft: opts.draft ?? '', canSubmit: opts.canSubmit ?? true, speechOn: opts.speechOn ?? true };
  const submits: Array<{ text: string; result: Deferred<VoiceTurnResult> }> = [];
  const speaks: Array<{ id: string; text: string; done: Deferred<void> }> = [];
  const transcribes: Array<Deferred<{ text: string }>> = [];
  const bridge: ChatBridge = {
    getDraft: () => state.draft,
    canSubmit: () => state.canSubmit,
    submit: (text) => { const result = deferred<VoiceTurnResult>(); submits.push({ text, result }); return result.promise; },
    abort: vi.fn(),
    speechOutputEnabled: () => state.speechOn,
  };
  const stopSpeaking = vi.fn();
  const timerApi = {
    setTimeout: (fn: () => void, ms: number) => { timers.push({ fn, ms, live: true }); return timers.length - 1; },
    clearTimeout: (h: unknown) => { const t = timers[h as number]; if (t) t.live = false; },
  };
  let session!: WakeCaptureSession;
  const conductor = new HandsFreeConductor({
    session: { release: () => session.release(), addTranscript: (t) => session.addTranscript(t) },
    getBridge: () => (opts.noBridge ? null : bridge),
    autoSubmitAllowed: () => opts.policyOk ?? true,
    speak: (id, text) => { const done = deferred<void>(); speaks.push({ id, text, done }); return done.promise; },
    stopSpeaking,
    host,
    now: () => clock,
    ...timerApi,
  });
  session = new WakeCaptureSession({
    host,
    transcribe: () => { const d = deferred<{ text: string }>(); transcribes.push(d); return d.promise; },
    onTranscript: conductor.handleTranscript,
    ...timerApi,
  });
  const countdown = () => timers.find((t) => t.ms === SUBMIT_COUNTDOWN_MS && t.live);
  /** Speak a request and have Whisper return `text`. */
  async function say(text: string) {
    host.wake();
    host.chunk(LOUD, 10);
    host.chunk(QUIET, 15);
    transcribes[transcribes.length - 1].resolve({ text });
    await tick();
  }
  const phase = () => handsFreePhase('listening', session.getSnapshot(), conductor.getSnapshot().phase);
  return { host, session, conductor, bridge, state, submits, speaks, transcribes, stopSpeaking, countdown, say, phase, advance: (ms: number) => { clock += ms; } };
}

beforeEach(() => __resetBridgeForTests());

describe('transcript → cancellation countdown', () => {
  it('shows the transcript with a 1.5 s window and sends nothing yet', async () => {
    const h = build();
    await h.say('Jarvis, what is the time?');
    const snap = h.conductor.getSnapshot();
    expect(snap).toMatchObject({ phase: 'pending', text: 'what is the time?', deadlineAt: 10_000 + 1500 });
    expect(h.phase()).toBe('pending');
    expect(h.submits).toHaveLength(0);
    expect(h.countdown()).toBeDefined();
    expect(h.host.paused).toBe(true); // George / the room cannot trigger a new wake
  });

  it('does not accept a second wake while the turn is in progress', async () => {
    const h = build();
    await h.say('first request');
    h.host.wake();
    expect(h.session.getSnapshot().phase).toBe('waiting');
    expect(h.conductor.getSnapshot().phase).toBe('pending');
  });
});

describe('cancellation', () => {
  it('cancel (button or Esc) during the window prevents any submission', async () => {
    const h = build();
    await h.say('delete everything');
    expect(h.conductor.cancel()).toBe(true);
    expect(h.countdown()).toBeUndefined();
    expect(h.conductor.getSnapshot()).toMatchObject({ phase: 'idle', notice: 'Cancelled. Nothing was sent.' });
    expect(h.session.getSnapshot().transcripts).toHaveLength(0);
    expect(h.host.paused).toBe(false);
    expect(h.submits).toHaveLength(0);
    expect(h.phase()).toBe('waiting');
  });

  it('cancel while the reply is generating aborts it and ignores the late result', async () => {
    const h = build();
    await h.say('tell me a story');
    h.countdown()!.fn();
    expect(h.conductor.getSnapshot().phase).toBe('thinking');
    h.conductor.cancel();
    expect(h.bridge.abort).toHaveBeenCalledTimes(1);
    h.submits[0].result.resolve({ status: 'completed', messageId: 'm1', text: 'Once upon a time' });
    await tick();
    expect(h.speaks).toHaveLength(0);
    expect(h.host.paused).toBe(false);
  });

  it('cancel while speaking stops playback and re-arms', async () => {
    const h = build();
    await h.say('hello');
    h.countdown()!.fn();
    h.submits[0].result.resolve({ status: 'completed', messageId: 'm1', text: 'Hi there' });
    await tick();
    expect(h.conductor.getSnapshot().phase).toBe('speaking');
    h.conductor.cancel();
    expect(h.stopSpeaking).toHaveBeenCalled();
    h.speaks[0].done.resolve();
    await tick();
    expect(h.conductor.getSnapshot().phase).toBe('idle');
    expect(h.host.paused).toBe(false);
  });

  it('hands-free going off ends the turn in every phase without sending or speaking', async () => {
    for (const phaseName of ['pending', 'thinking', 'speaking'] as const) {
      const h = build();
      await h.say('hello');
      if (phaseName !== 'pending') h.countdown()!.fn();
      if (phaseName === 'speaking') { h.submits[0].result.resolve({ status: 'completed', messageId: 'm', text: 'Hi' }); await tick(); }
      h.host.emit('off');
      expect(h.conductor.getSnapshot().phase).toBe('idle');
      expect(h.countdown()).toBeUndefined();
      if (phaseName === 'thinking') expect(h.bridge.abort).toHaveBeenCalled();
      if (phaseName === 'speaking') expect(h.stopSpeaking).toHaveBeenCalled();
      if (phaseName === 'pending') expect(h.submits).toHaveLength(0);
    }
  });

  it('cancel with nothing active falls through (false)', () => {
    expect(build().conductor.cancel()).toBe(false);
  });
});

describe('submission', () => {
  it('submits exactly once after the countdown, even if the timer fires again', async () => {
    const h = build();
    await h.say('what time is it');
    const t = h.countdown()!;
    t.fn();
    t.fn();
    await tick();
    expect(h.submits).toHaveLength(1);
    expect(h.submits[0].text).toBe('what time is it');
    expect(h.conductor.getSnapshot().phase).toBe('thinking');
    expect(h.host.paused).toBe(true);
  });

  it('a single wake event can never produce two submissions', async () => {
    const h = build();
    await h.say('only once');
    h.countdown()!.fn();
    h.host.wake();
    h.host.chunk(LOUD, 30);
    await tick();
    expect(h.submits).toHaveLength(1);
  });

  it('completes the loop: submit → speak → release → ready for the next wake', async () => {
    const h = build();
    await h.say('first');
    h.countdown()!.fn();
    h.submits[0].result.resolve({ status: 'completed', messageId: 'm1', text: 'Answer one' });
    await tick();
    expect(h.speaks).toEqual([expect.objectContaining({ id: 'm1', text: 'Answer one' })]);
    expect(h.host.paused).toBe(true); // still paused while George speaks
    h.speaks[0].done.resolve();
    await tick();
    expect(h.conductor.getSnapshot().phase).toBe('idle');
    expect(h.host.paused).toBe(false);
    expect(h.phase()).toBe('waiting');

    await h.say('second');
    expect(h.conductor.getSnapshot()).toMatchObject({ phase: 'pending', text: 'second' });
  });

  it('empty, silent, bracketed or wake-only transcripts are never submitted', async () => {
    for (const text of ['', '   ', '[BLANK_AUDIO]', '...', 'Jarvis']) {
      const h = build();
      await h.say(text);
      expect(h.conductor.getSnapshot().phase).toBe('idle');
      expect(h.submits).toHaveLength(0);
      expect(h.countdown()).toBeUndefined();
    }
  });

  it('a failed transcription is never submitted', async () => {
    const h = build();
    h.host.wake();
    h.host.chunk(LOUD, 10);
    h.host.chunk(QUIET, 15);
    h.transcribes[0].reject(new Error('Local speech recognition crashed'));
    await tick();
    expect(h.submits).toHaveLength(0);
  });
});

describe('typed draft protection', () => {
  it('an existing draft routes the transcript to review and starts no countdown', async () => {
    const h = build({ draft: 'half-written email' });
    await h.say('send it now');
    expect(h.conductor.getSnapshot().phase).toBe('idle');
    expect(h.countdown()).toBeUndefined();
    expect(h.session.getSnapshot().transcripts.map((t) => t.text)).toEqual(['send it now']);
    expect(h.submits).toHaveLength(0);
    expect(h.host.paused).toBe(false);
    expect(h.phase()).toBe('ready');
  });

  it('typing during the countdown cancels the auto-send and keeps the transcript for review', async () => {
    const h = build();
    await h.say('send it now');
    h.state.draft = 'wait, let me write';
    notifyDraftChanged('wait, let me write');
    expect(h.countdown()).toBeUndefined();
    expect(h.submits).toHaveLength(0);
    expect(h.session.getSnapshot().transcripts.map((t) => t.text)).toEqual(['send it now']);
    expect(h.conductor.getSnapshot().notice).toMatch(/started typing/);
    expect(h.host.paused).toBe(false);
  });

  it('whitespace-only edits do not cancel the countdown', async () => {
    const h = build();
    await h.say('go ahead');
    notifyDraftChanged('   ');
    expect(h.conductor.getSnapshot().phase).toBe('pending');
  });

  it('a draft that appears at the instant of sending is still protected', async () => {
    const h = build();
    await h.say('go ahead');
    h.state.draft = 'late typing';
    h.countdown()!.fn();
    await tick();
    expect(h.submits).toHaveLength(0);
    expect(h.session.getSnapshot().transcripts.map((t) => t.text)).toEqual(['go ahead']);
  });

  it('without server confirmation of the voice tool policy nothing is auto-sent', async () => {
    const h = build({ policyOk: false });
    await h.say('hello there');
    expect(h.conductor.getSnapshot().phase).toBe('idle');
    expect(h.submits).toHaveLength(0);
    expect(h.session.getSnapshot().transcripts.map((t) => t.text)).toEqual(['hello there']);
  });

  it('a busy chat, a missing model or a closed chat page falls back to review', async () => {
    for (const opts of [{ canSubmit: false }, { noBridge: true }]) {
      const h = build(opts);
      await h.say('hello there');
      expect(h.conductor.getSnapshot().phase).toBe('idle');
      expect(h.session.getSnapshot().transcripts.map((t) => t.text)).toEqual(['hello there']);
      expect(h.submits).toHaveLength(0);
    }
  });
});

describe('failures and speech rules', () => {
  async function through(result: VoiceTurnResult, opts: Parameters<typeof build>[0] = {}) {
    const h = build(opts);
    await h.say('question');
    h.countdown()!.fn();
    h.submits[0].result.resolve(result);
    await tick();
    return h;
  }

  it('does not speak a failed, aborted or incomplete reply, and re-arms', async () => {
    for (const status of ['failed', 'aborted'] as const) {
      const h = await through({ status });
      expect(h.speaks).toHaveLength(0);
      expect(h.conductor.getSnapshot().phase).toBe('idle');
      expect(h.host.paused).toBe(false);
    }
    const h = await through({ status: 'completed', messageId: 'm', text: '' });
    expect(h.speaks).toHaveLength(0);
  });

  it('a submit that throws is treated as a failure', async () => {
    const h = build();
    h.bridge.submit = () => Promise.reject(new Error('network'));
    await h.say('question');
    h.countdown()!.fn();
    await tick();
    expect(h.conductor.getSnapshot().phase).toBe('idle');
    expect(h.speaks).toHaveLength(0);
    expect(h.host.paused).toBe(false);
  });

  it('a rejected submission keeps the transcript for review', async () => {
    const h = await through({ status: 'rejected' });
    expect(h.session.getSnapshot().transcripts.map((t) => t.text)).toEqual(['question']);
  });

  it('respects the global voice-output setting: replies are not spoken, but the loop re-arms', async () => {
    const h = await through({ status: 'completed', messageId: 'm', text: 'An answer' }, { speechOn: false });
    expect(h.speaks).toHaveLength(0);
    expect(h.host.paused).toBe(false);
    expect(h.conductor.getSnapshot().phase).toBe('idle');
  });

  it('speaks a completed reply once, with no overlap from a second turn', async () => {
    const h = await through({ status: 'completed', messageId: 'm1', text: 'An answer' });
    expect(h.speaks).toHaveLength(1);
    h.host.wake(); // ignored: still speaking
    h.host.chunk(LOUD, 30);
    expect(h.speaks).toHaveLength(1);
    expect(h.session.getSnapshot().phase).toBe('waiting');
  });

  it('a speech failure still returns to waiting', async () => {
    const h = build();
    h.bridge.submit = async () => ({ status: 'completed', messageId: 'm', text: 'Hi' });
    await h.say('hello');
    h.countdown()!.fn();
    await tick();
    h.speaks[0].done.reject(new Error('playback blocked'));
    await tick();
    expect(h.conductor.getSnapshot().phase).toBe('idle');
    expect(h.host.paused).toBe(false);
  });
});
