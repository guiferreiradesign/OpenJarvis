import { WakeGate, type WakeGateOptions } from './gate';
import { CHUNK_SAMPLES, WakePipeline, type WakePipelineModels } from './pipeline';
import { WAKE_MODELS, type WakeModelId } from './models';

/**
 * Wake-word controller. It owns the microphone, scores audio locally and
 * reports detections. It never records a request, never calls a speech,
 * reasoning or tool endpoint, and never touches the chat. Request capture
 * (Phase 4.4b) lives outside this directory and plugs in through the narrow
 * WakeTap seam below, so the microphone keeps exactly one owner.
 */
export type WakeState = 'off' | 'initialising' | 'listening' | 'detected' | 'error';

export interface WakeDetection {
  modelId: WakeModelId;
  phrase: string;
  at: number;
  score: number;
}

export interface WakeSnapshot {
  state: WakeState;
  modelId: WakeModelId | null;
  error: string | null;
  detection: WakeDetection | null;
  /** Chunks dropped because inference could not keep up. */
  droppedChunks: number;
}

export const WAKE_STATE_LABEL: Record<WakeState, string> = {
  off: 'Off',
  initialising: 'Initialising…',
  listening: 'Listening',
  detected: 'Wake word detected',
  error: 'Error',
};

/**
 * Seam for request capture. It sees every 80 ms chunk as it arrives (16 kHz
 * mono, 16-bit PCM values in a Float32Array) and every detection. The
 * controller keeps no reference to a chunk after handing it over.
 */
export interface WakeTap {
  onChunk(chunk: Float32Array): void;
  onDetection(detection: WakeDetection): void;
}

/** Minimal audio-node surface the controller relies on (real or fake). */
export interface WakeWorkletNode {
  port: { onmessage: ((ev: { data: unknown }) => void) | null };
  onprocessorerror: ((ev: unknown) => void) | null;
  connect(dest: unknown): unknown;
  disconnect(): void;
}

export interface WakeAudioContext {
  readonly sampleRate: number;
  readonly state: string;
  readonly audioWorklet: { addModule(url: string): Promise<void> };
  createMediaStreamSource(stream: MediaStream): { connect(dest: unknown): unknown; disconnect(): void };
  resume(): Promise<void>;
  close(): Promise<void>;
}

export interface WakeDeps {
  getUserMedia(constraints: MediaStreamConstraints): Promise<MediaStream>;
  /** Called synchronously from start() so it stays inside the user's click. */
  createAudioContext(): WakeAudioContext;
  createWorkletNode(ctx: WakeAudioContext): WakeWorkletNode;
  loadModels(modelId: WakeModelId): Promise<WakePipelineModels>;
  workletUrl: string;
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const DETECTED_DISPLAY_MS = 1500;
const MAX_PENDING_CHUNKS = 10; // 0.8 s; beyond this the oldest chunk is dropped

export const MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    channelCount: 1,
    echoCancellation: true, // lets George's own voice be cancelled when playing
    noiseSuppression: false,
    autoGainControl: false,
  },
};

export function describeWakeError(err: unknown): string {
  const name = err instanceof DOMException || err instanceof Error ? err.name : '';
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'Microphone access was blocked. Allow it for this site in your browser and try again.';
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'No microphone was found.';
    case 'NotReadableError':
    case 'AbortError':
      return 'The microphone is busy or unavailable. Close other apps using it and try again.';
    default:
      return err instanceof Error && err.message ? err.message : 'Wake detection failed.';
  }
}

type Listener = (snapshot: WakeSnapshot) => void;

export class WakeController {
  private snapshot: WakeSnapshot = {
    state: 'off', modelId: null, error: null, detection: null, droppedChunks: 0,
  };
  private listeners = new Set<Listener>();
  private token = 0;
  private ctx: WakeAudioContext | null = null;
  private stream: MediaStream | null = null;
  private source: { disconnect(): void } | null = null;
  private node: WakeWorkletNode | null = null;
  private pipeline: WakePipeline | null = null;
  private models: WakePipelineModels | null = null;
  private gate: WakeGate;
  private pending: Float32Array[] = [];
  private draining = false;
  private detectedTimer: unknown = null;
  private tap: WakeTap | null = null;
  private paused = false;

  constructor(private readonly deps: WakeDeps, gateOptions: Partial<WakeGateOptions> = {}) {
    this.gate = new WakeGate(gateOptions);
  }

  getSnapshot = (): WakeSnapshot => this.snapshot;

  subscribe = (fn: Listener): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  /** Attach (or detach with null) the request-capture tap. */
  setTap(tap: WakeTap | null): void {
    this.tap = tap;
  }

  /**
   * Pause or resume scoring while a request is captured. Chunks still reach the
   * tap. Pausing drops queued chunks; resuming restarts the pipeline and gate
   * from a clean state so the detector re-arms without stale features.
   */
  setDetectionPaused(paused: boolean): void {
    if (paused === this.paused) return;
    this.paused = paused;
    this.pending = [];
    if (paused) {
      if (this.detectedTimer !== null) {
        this.deps.clearTimeout(this.detectedTimer);
        this.detectedTimer = null;
      }
      if (this.snapshot.state === 'detected') this.set({ state: 'listening' });
    } else {
      this.pipeline?.reset();
      this.gate.reset();
    }
  }

  private set(patch: Partial<WakeSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    this.listeners.forEach((fn) => fn(this.snapshot));
  }

  /**
   * Switch wake detection on. Must be called from a user action: it is the
   * only place the microphone is opened.
   */
  async start(modelId: WakeModelId): Promise<void> {
    if (this.snapshot.state !== 'off' && this.snapshot.state !== 'error') return;
    // fail() and stop() have already released everything, so there is nothing to
    // clean up here. Taking the token first means a quick Off right after On wins.
    const token = ++this.token;
    this.paused = false;
    this.set({ state: 'initialising', modelId, error: null, detection: null, droppedChunks: 0 });

    try {
      // Created synchronously so it counts as part of the click that got us here.
      const ctx = this.deps.createAudioContext();
      this.ctx = ctx;

      const stream = await this.deps.getUserMedia(MIC_CONSTRAINTS);
      if (token !== this.token) { stream.getTracks().forEach((t) => t.stop()); return; }
      this.stream = stream;
      stream.getAudioTracks().forEach((track) => {
        track.addEventListener('ended', () => {
          if (token === this.token) void this.fail(token, 'The microphone was disconnected or stopped.');
        });
      });

      const models = await this.deps.loadModels(modelId);
      if (token !== this.token) { await releaseModels(models); return; }
      this.models = models;
      this.pipeline = new WakePipeline(models);
      this.gate.reset();

      await ctx.audioWorklet.addModule(this.deps.workletUrl);
      if (token !== this.token) return;
      if (ctx.state === 'suspended') await ctx.resume();

      const node = this.deps.createWorkletNode(ctx);
      node.port.onmessage = (ev) => this.onChunk(token, ev.data);
      node.onprocessorerror = () => void this.fail(token, 'Audio processing stopped unexpectedly.');
      const source = ctx.createMediaStreamSource(stream);
      source.connect(node);
      this.node = node;
      this.source = source;

      this.set({ state: 'listening' });
    } catch (err) {
      if (token === this.token) await this.fail(token, describeWakeError(err));
    }
  }

  /** Immediate Off: stops the tracks, closes the context, frees the models. */
  async stop(): Promise<void> {
    this.token += 1;
    await this.cleanup();
    this.set({ state: 'off', modelId: null, error: null, detection: null });
  }

  private async fail(token: number, message: string): Promise<void> {
    if (token !== this.token) return;
    this.token += 1;
    await this.cleanup();
    this.set({ state: 'error', error: message, detection: null });
  }

  private onChunk(token: number, data: unknown): void {
    if (token !== this.token || !(data instanceof Float32Array) || data.length !== CHUNK_SAMPLES) return;
    if (this.tap) {
      try { this.tap.onChunk(data); } catch { /* a capture bug must never stop the detector */ }
    }
    if (this.paused) return;
    if (this.pending.length >= MAX_PENDING_CHUNKS) {
      this.pending.shift();
      this.set({ droppedChunks: this.snapshot.droppedChunks + 1 });
    }
    this.pending.push(data);
    if (!this.draining) void this.drain(token);
  }

  private async drain(token: number): Promise<void> {
    this.draining = true;
    try {
      while (this.pending.length > 0 && token === this.token && this.pipeline && !this.paused) {
        const chunk = this.pending.shift() as Float32Array;
        const score = await this.pipeline.process(chunk);
        if (token !== this.token) return;
        if (this.gate.update(score, this.deps.now())) this.onDetected(token, score);
      }
    } catch (err) {
      await this.fail(token, `Wake detection failed: ${describeWakeError(err)}`);
    } finally {
      this.draining = false;
    }
  }

  private onDetected(token: number, score: number): void {
    const modelId = this.snapshot.modelId;
    if (!modelId) return;
    const detection: WakeDetection = { modelId, phrase: WAKE_MODELS[modelId].phrase, at: this.deps.now(), score };
    this.set({ state: 'detected', detection });
    if (this.detectedTimer !== null) this.deps.clearTimeout(this.detectedTimer);
    this.detectedTimer = this.deps.setTimeout(() => {
      this.detectedTimer = null;
      if (token === this.token && this.snapshot.state === 'detected') this.set({ state: 'listening' });
    }, DETECTED_DISPLAY_MS);
    if (this.tap) {
      try { this.tap.onDetection(detection); } catch { /* see onChunk */ }
    }
  }

  private async cleanup(): Promise<void> {
    if (this.detectedTimer !== null) {
      this.deps.clearTimeout(this.detectedTimer);
      this.detectedTimer = null;
    }
    this.pending = [];
    this.paused = false;
    if (this.node) {
      this.node.port.onmessage = null;
      this.node.onprocessorerror = null;
      try { this.node.disconnect(); } catch { /* already disconnected */ }
      this.node = null;
    }
    if (this.source) {
      try { this.source.disconnect(); } catch { /* already disconnected */ }
      this.source = null;
    }
    if (this.stream) {
      this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
    }
    const ctx = this.ctx;
    this.ctx = null;
    if (ctx) {
      try { await ctx.close(); } catch { /* already closed */ }
    }
    const pipeline = this.pipeline;
    const models = this.models;
    this.pipeline = null;
    this.models = null;
    if (pipeline) await pipeline.release();
    else if (models) await releaseModels(models);
    this.gate.reset();
  }
}

async function releaseModels(models: WakePipelineModels): Promise<void> {
  await Promise.allSettled([models.mel, models.embedding, models.classifier].map(async (m) => m.release?.()));
}
