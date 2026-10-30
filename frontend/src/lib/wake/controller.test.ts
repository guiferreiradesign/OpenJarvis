import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DETECTED_DISPLAY_MS, WakeController, describeWakeError,
  type WakeAudioContext, type WakeDeps, type WakeSnapshot, type WakeWorkletNode,
} from './controller';
import { CHUNK_SAMPLES, EMBEDDING_DIM, MEL_BINS, type WakePipelineModels } from './pipeline';

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

function build(opts: { score?: () => number; failClassifier?: boolean } = {}) {
  const stream = new FakeStream();
  const released: string[] = [];
  const state = { score: 0.9 };
  let clfCalls = 0;
  const runner = (name: string, run: WakePipelineModels['mel']['run']) => ({ run, release: () => { released.push(name); } });
  const models: WakePipelineModels = {
    mel: runner('mel', async (_d, dims) => new Float32Array(Math.ceil(dims[1] / 160 - 3) * MEL_BINS)),
    embedding: runner('embedding', async () => new Float32Array(EMBEDDING_DIM)),
    classifier: runner('classifier', async () => {
      clfCalls += 1;
      if (opts.failClassifier) throw new Error('onnx exploded');
      return Float32Array.of(opts.score ? opts.score() : state.score);
    }),
    classifierFrames: 16,
  };
  const node: WakeWorkletNode = {
    port: { onmessage: null }, onprocessorerror: null,
    connect: vi.fn(), disconnect: vi.fn(),
  };
  const ctx = {
    sampleRate: 16000, state: 'running',
    audioWorklet: { addModule: vi.fn(async () => {}) },
    createMediaStreamSource: vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() })),
    resume: vi.fn(async () => {}),
    close: vi.fn(async () => {}),
  } satisfies WakeAudioContext;
  const timers: Array<{ fn: () => void; ms: number; id: number; live: boolean }> = [];
  let clock = 1_000;
  const deps = {
    getUserMedia: vi.fn(async () => stream as unknown as MediaStream),
    createAudioContext: vi.fn(() => ctx),
    createWorkletNode: vi.fn(() => node),
    loadModels: vi.fn(async () => models),
    workletUrl: '/wake-worklet.js',
    now: () => clock,
    setTimeout: (fn: () => void, ms: number) => { const t = { fn, ms, id: timers.length, live: true }; timers.push(t); return t.id; },
    clearTimeout: (id: unknown) => { const t = timers[id as number]; if (t) t.live = false; },
  } satisfies WakeDeps;
  return {
    deps, stream, node, ctx, models, released, state, timers,
    advance: (ms: number) => { clock += ms; },
    runTimers: () => timers.filter((t) => t.live).forEach((t) => { t.live = false; t.fn(); }),
    clfCalls: () => clfCalls,
  };
}

const flush = () => new Promise((r) => setTimeout(r, 0));
async function feed(h: ReturnType<typeof build>, n: number, step = 80) {
  for (let i = 0; i < n; i++) {
    h.advance(step);
    h.node.port.onmessage?.({ data: new Float32Array(CHUNK_SAMPLES) });
    await flush();
  }
}
async function started(h = build(), modelId: 'jarvis' | 'hey_jarvis' = 'jarvis') {
  const c = new WakeController(h.deps);
  await c.start(modelId);
  return { c, h };
}

describe('WakeController lifecycle', () => {
  it('does nothing on construction: no microphone, no models, no audio context', () => {
    const h = build();
    const c = new WakeController(h.deps);
    expect(c.getSnapshot().state).toBe('off');
    expect(h.deps.getUserMedia).not.toHaveBeenCalled();
    expect(h.deps.loadModels).not.toHaveBeenCalled();
    expect(h.deps.createAudioContext).not.toHaveBeenCalled();
  });

  it('start() opens the mic, lazily loads the chosen model and listens', async () => {
    const { c, h } = await started();
    expect(c.getSnapshot()).toMatchObject({ state: 'listening', modelId: 'jarvis', error: null });
    expect(h.deps.getUserMedia).toHaveBeenCalledTimes(1);
    expect(h.deps.getUserMedia).toHaveBeenCalledWith(expect.objectContaining({
      audio: expect.objectContaining({ echoCancellation: true }),
    }));
    expect(h.deps.loadModels).toHaveBeenCalledWith('jarvis');
    expect(h.ctx.audioWorklet.addModule).toHaveBeenCalledWith('/wake-worklet.js');
  });

  it('loads the upstream model when Hey Jarvis is selected', async () => {
    const { c, h } = await started(build(), 'hey_jarvis');
    expect(h.deps.loadModels).toHaveBeenCalledWith('hey_jarvis');
    await feed(h, 8);
    expect(c.getSnapshot().detection?.phrase).toBe('Hey Jarvis');
  });

  it('ignores a second start() while already listening', async () => {
    const { c, h } = await started();
    await c.start('jarvis');
    expect(h.deps.getUserMedia).toHaveBeenCalledTimes(1);
  });

  it('permission denial becomes an error and never loads models', async () => {
    const h = build();
    h.deps.getUserMedia.mockRejectedValueOnce(new DOMException('no', 'NotAllowedError'));
    const c = new WakeController(h.deps);
    await c.start('jarvis');
    expect(c.getSnapshot().state).toBe('error');
    expect(c.getSnapshot().error).toMatch(/blocked/i);
    expect(h.deps.loadModels).not.toHaveBeenCalled();
    expect(h.ctx.close).toHaveBeenCalled();
  });

  it('can be started again after an error', async () => {
    const h = build();
    h.deps.getUserMedia.mockRejectedValueOnce(new DOMException('no', 'NotFoundError'));
    const c = new WakeController(h.deps);
    await c.start('jarvis');
    expect(c.getSnapshot().state).toBe('error');
    await c.start('jarvis');
    expect(c.getSnapshot().state).toBe('listening');
  });

  it('an ended microphone track stops everything and reports the error', async () => {
    const { c, h } = await started();
    h.stream.tracks[0].fire('ended');
    await flush();
    expect(c.getSnapshot().state).toBe('error');
    expect(c.getSnapshot().error).toMatch(/microphone/i);
    expect(h.stream.tracks[0].stopped).toBe(true);
    expect(h.ctx.close).toHaveBeenCalled();
    expect(h.released.sort()).toEqual(['classifier', 'embedding', 'mel']);
  });

  it('AudioWorklet load failure is reported and cleaned up', async () => {
    const h = build();
    h.ctx.audioWorklet.addModule.mockRejectedValueOnce(new Error('worklet blocked'));
    const c = new WakeController(h.deps);
    await c.start('jarvis');
    expect(c.getSnapshot()).toMatchObject({ state: 'error', error: 'worklet blocked' });
    expect(h.stream.tracks[0].stopped).toBe(true);
    expect(h.released.length).toBe(3);
  });

  it('a worklet processor error is reported and cleaned up', async () => {
    const { c, h } = await started();
    h.node.onprocessorerror?.({});
    await flush();
    expect(c.getSnapshot().state).toBe('error');
    expect(h.stream.tracks[0].stopped).toBe(true);
  });

  it('a model-loading failure stops the microphone and surfaces the message', async () => {
    const h = build();
    h.deps.loadModels.mockRejectedValueOnce(new Error('Wake-word model x.onnx is missing (HTTP 404).'));
    const c = new WakeController(h.deps);
    await c.start('jarvis');
    expect(c.getSnapshot()).toMatchObject({ state: 'error' });
    expect(c.getSnapshot().error).toMatch(/missing/);
    expect(h.stream.tracks[0].stopped).toBe(true);
  });

  it('an ONNX inference error is reported and cleaned up', async () => {
    const { c, h } = await started(build({ failClassifier: true }));
    await feed(h, 2);
    expect(c.getSnapshot().state).toBe('error');
    expect(c.getSnapshot().error).toMatch(/onnx exploded/);
    expect(h.stream.tracks[0].stopped).toBe(true);
    expect(h.ctx.close).toHaveBeenCalled();
  });

  it('stop() releases the tracks, context, graph and models, and ignores later audio', async () => {
    const { c, h } = await started();
    const handler = h.node.port.onmessage;
    await c.stop();
    expect(c.getSnapshot()).toMatchObject({ state: 'off', modelId: null, detection: null });
    expect(h.stream.tracks[0].stopped).toBe(true);
    expect(h.ctx.close).toHaveBeenCalledTimes(1);
    expect(h.node.disconnect).toHaveBeenCalled();
    expect(h.node.port.onmessage).toBeNull();
    expect(h.released.sort()).toEqual(['classifier', 'embedding', 'mel']);
    const before = h.clfCalls();
    handler?.({ data: new Float32Array(CHUNK_SAMPLES) });
    await flush();
    expect(h.clfCalls()).toBe(before);
  });

  it('stop() while still waiting for the microphone leaves nothing open', async () => {
    const h = build();
    let resolve!: (s: MediaStream) => void;
    h.deps.getUserMedia.mockImplementationOnce(() => new Promise<MediaStream>((r) => { resolve = r; }));
    const c = new WakeController(h.deps);
    const starting = c.start('jarvis');
    await c.stop();
    resolve(h.stream as unknown as MediaStream);
    await starting;
    expect(c.getSnapshot().state).toBe('off');
    expect(h.stream.tracks[0].stopped).toBe(true);
    expect(h.deps.loadModels).not.toHaveBeenCalled();
  });

  it('stop() while models are loading releases them when they arrive', async () => {
    const h = build();
    let resolve!: (m: WakePipelineModels) => void;
    h.deps.loadModels.mockImplementationOnce(() => new Promise((r) => { resolve = r; }));
    const c = new WakeController(h.deps);
    const starting = c.start('jarvis');
    await flush();
    await c.stop();
    resolve(h.models);
    await starting;
    expect(c.getSnapshot().state).toBe('off');
    expect(h.released.sort()).toEqual(['classifier', 'embedding', 'mel']);
  });
});

describe('WakeController detection', () => {
  let h: ReturnType<typeof build>;
  let c: WakeController;
  const events: WakeSnapshot[] = [];
  beforeEach(async () => {
    events.length = 0;
    h = build();
    c = new WakeController(h.deps);
    c.subscribe((s) => events.push(s));
    await c.start('jarvis');
  });
  afterEach(async () => { await c.stop(); });

  it('stays quiet below the threshold', async () => {
    h.state.score = 0.2;
    await feed(h, 12);
    expect(c.getSnapshot().state).toBe('listening');
    expect(c.getSnapshot().detection).toBeNull();
  });

  it('reports the phrase once, debounced, and keeps listening', async () => {
    await feed(h, 12); // score stays at 0.9 after warm-up
    const detectedEvents = events.filter((e) => e.state === 'detected');
    expect(detectedEvents).toHaveLength(1);
    expect(c.getSnapshot().detection).toMatchObject({ modelId: 'jarvis', phrase: 'Jarvis' });
    h.runTimers(); // detected indicator times out
    expect(c.getSnapshot().state).toBe('listening');
  });

  it('can fire again after the cooldown once the score has dropped', async () => {
    await feed(h, 10);
    h.state.score = 0.05;
    await feed(h, 30, 100); // > 2 s of quiet
    h.state.score = 0.9;
    await feed(h, 3);
    expect(events.filter((e) => e.state === 'detected')).toHaveLength(2);
  });

  it('shows the detected state for a bounded time', () => {
    expect(DETECTED_DISPLAY_MS).toBeLessThanOrEqual(3000);
  });

  it('detection alone never changes anything but its own state', async () => {
    await feed(h, 10);
    expect(Object.keys(c.getSnapshot()).sort()).toEqual(
      ['detection', 'droppedChunks', 'error', 'modelId', 'state'],
    );
  });
});

describe('describeWakeError', () => {
  it('maps microphone errors to readable text', () => {
    expect(describeWakeError(new DOMException('x', 'NotAllowedError'))).toMatch(/blocked/);
    expect(describeWakeError(new DOMException('x', 'NotFoundError'))).toMatch(/No microphone/);
    expect(describeWakeError(new DOMException('x', 'NotReadableError'))).toMatch(/busy/);
  });
});
