import { describe, expect, it, vi } from 'vitest';
import {
  WakeController, type WakeAudioContext, type WakeDeps, type WakeWorkletNode,
} from '../wake/controller';
import { CHUNK_SAMPLES, EMBEDDING_DIM, MEL_BINS, type WakePipelineModels } from '../wake/pipeline';
import { WakeCaptureSession } from './session';

class FakeTrack {
  stopped = false;
  private handlers: Record<string, Array<() => void>> = {};
  addEventListener(type: string, fn: () => void) { (this.handlers[type] ??= []).push(fn); }
  stop() { this.stopped = true; }
  fire(type: string) { (this.handlers[type] ?? []).forEach((fn) => fn()); }
}
class FakeStream {
  tracks = [new FakeTrack()];
  getTracks() { return this.tracks; }
  getAudioTracks() { return this.tracks; }
}

/** Real WakeController + real WakeCaptureSession, fake audio graph, models and transcriber. */
function build() {
  const stream = new FakeStream();
  const score = { value: 0 };
  const models: WakePipelineModels = {
    mel: { run: async (_d, dims) => new Float32Array(Math.ceil(dims[1] / 160 - 3) * MEL_BINS) },
    embedding: { run: async () => new Float32Array(EMBEDDING_DIM) },
    classifier: { run: async () => Float32Array.of(score.value) },
    classifierFrames: 16,
  };
  const node: WakeWorkletNode = { port: { onmessage: null }, onprocessorerror: null, connect: vi.fn(), disconnect: vi.fn() };
  const ctx = {
    sampleRate: 16000, state: 'running',
    audioWorklet: { addModule: vi.fn(async () => {}) },
    createMediaStreamSource: vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() })),
    resume: vi.fn(async () => {}),
    close: vi.fn(async () => {}),
  } satisfies WakeAudioContext;
  let clock = 1_000;
  const deps = {
    getUserMedia: vi.fn(async () => stream as unknown as MediaStream),
    createAudioContext: vi.fn(() => ctx),
    createWorkletNode: vi.fn(() => node),
    loadModels: vi.fn(async () => models),
    workletUrl: '/wake-worklet.js',
    now: () => clock,
    setTimeout: () => 0,
    clearTimeout: () => {},
  } satisfies WakeDeps;
  const controller = new WakeController(deps);

  const requests: Array<{ blob: Blob; signal: AbortSignal; resolve(v: { text: string }): void }> = [];
  const session = new WakeCaptureSession({
    host: controller,
    transcribe: (blob, _name, signal) => new Promise((resolve) => { requests.push({ blob, signal, resolve }); }),
    setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
  });
  const flush = () => new Promise((r) => setTimeout(r, 0));
  async function feed(level: number, n: number, wakeScore = 0) {
    for (let i = 0; i < n; i++) {
      clock += 80;
      score.value = wakeScore;
      node.port.onmessage?.({ data: new Float32Array(CHUNK_SAMPLES).fill(level) });
      await flush();
    }
  }
  return { controller, session, stream, node, ctx, requests, feed, flush, score };
}

describe('detector + capture together', () => {
  it('goes from a real detection to a transcript, then re-arms the detector', async () => {
    const h = build();
    await h.controller.start('jarvis');
    await h.feed(20, 12); // warm-up and background
    expect(h.session.getSnapshot().phase).toBe('waiting');

    await h.feed(3000, 1, 0.9); // the wake phrase is scored high
    expect(h.session.getSnapshot().phase).toBe('listening');

    await h.feed(3000, 10); // request
    await h.feed(20, 15); // silence ends it
    expect(h.session.getSnapshot().phase).toBe('transcribing');
    expect(h.requests).toHaveLength(1);

    h.requests[0].resolve({ text: 'Jarvis, hello there' });
    await h.flush();
    expect(h.session.getSnapshot().transcripts.map((t) => t.text)).toEqual(['hello there']);

    // Detector is armed again and can trigger a second request.
    await h.feed(20, 12);
    await h.feed(3000, 1, 0.9);
    expect(h.session.getSnapshot().phase).toBe('listening');
    await h.controller.stop();
  });

  it('does not score audio (so cannot re-trigger) while a request is being captured', async () => {
    const h = build();
    await h.controller.start('jarvis');
    await h.feed(20, 12);
    await h.feed(3000, 1, 0.9);
    const before = h.controller.getSnapshot().detection?.at;
    await h.feed(3000, 8, 0.99); // the phrase "heard" again mid-request
    expect(h.controller.getSnapshot().detection?.at).toBe(before);
    expect(h.session.getSnapshot().phase).toBe('listening');
    await h.controller.stop();
  });

  it('turning it off mid-capture releases the mic, worklet and context and sends nothing', async () => {
    const h = build();
    await h.controller.start('jarvis');
    await h.feed(20, 12);
    await h.feed(3000, 1, 0.9);
    await h.feed(3000, 5);
    await h.controller.stop();
    expect(h.stream.tracks[0].stopped).toBe(true);
    expect(h.node.disconnect).toHaveBeenCalled();
    expect(h.ctx.close).toHaveBeenCalled();
    expect(h.node.port.onmessage).toBeNull();
    expect(h.session.getSnapshot().phase).toBe('waiting');
    expect(h.requests).toHaveLength(0);
  });

  it('turning it off during transcription aborts the request', async () => {
    const h = build();
    await h.controller.start('jarvis');
    await h.feed(20, 12);
    await h.feed(3000, 1, 0.9);
    await h.feed(3000, 10);
    await h.feed(20, 15);
    expect(h.session.getSnapshot().phase).toBe('transcribing');
    await h.controller.stop();
    expect(h.requests[0].signal.aborted).toBe(true);
    expect(h.stream.tracks[0].stopped).toBe(true);
    h.requests[0].resolve({ text: 'late' });
    await h.flush();
    expect(h.session.getSnapshot().transcripts).toHaveLength(0);
  });

  it('a microphone that ends mid-request drops the capture and surfaces the detector error', async () => {
    const h = build();
    await h.controller.start('jarvis');
    await h.feed(20, 12);
    await h.feed(3000, 1, 0.9);
    h.stream.tracks[0].fire('ended');
    await h.flush();
    expect(h.controller.getSnapshot().state).toBe('error');
    expect(h.session.getSnapshot().phase).toBe('waiting');
    expect(h.requests).toHaveLength(0);
  });

  it('a throwing tap cannot break detection', async () => {
    const h = build();
    h.controller.setTap({ onChunk: () => { throw new Error('boom'); }, onDetection: () => { throw new Error('boom'); } });
    await h.controller.start('jarvis');
    await h.feed(20, 12);
    await h.feed(3000, 1, 0.9);
    expect(h.controller.getSnapshot().state).toBe('detected');
    await h.controller.stop();
  });
});
