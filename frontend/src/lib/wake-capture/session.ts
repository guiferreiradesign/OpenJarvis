import { RingBuffer } from '../wake/ring-buffer';
import type { WakeDetection, WakeSnapshot, WakeTap } from '../wake/controller';
import { Endpointer, chunkRms, estimateNoiseFloor } from './endpointer';
import {
  CAPTURE_SAMPLE_RATE, CHUNK_MS, resolveCaptureOptions, type CaptureOptions,
} from './options';
import { cleanTranscript } from './transcript';
import { encodeWav } from './wav';
import { NO_SPEECH_MESSAGE, describeTranscriptionError } from '../speech-input';

export type CapturePhase = 'waiting' | 'listening' | 'transcribing';
export type CaptureOutcome = 'none' | 'no_speech' | 'error';

export interface PendingTranscript {
  id: number;
  text: string;
}

export interface CaptureSnapshot {
  phase: CapturePhase;
  /** Result of the most recent request when nothing is pending for it. */
  outcome: CaptureOutcome;
  error: string | null;
  /** Transcripts waiting for the user to insert or discard them. */
  transcripts: PendingTranscript[];
}

/** What the session needs from the wake controller. */
export interface CaptureHost {
  setTap(tap: WakeTap | null): void;
  setDetectionPaused(paused: boolean): void;
  subscribe(fn: (snapshot: WakeSnapshot) => void): () => void;
}

export interface CaptureDeps {
  host: CaptureHost;
  /**
   * Offer a finished, cleaned transcript to a conductor. Returning true takes
   * ownership: the session keeps the detector paused until release() and does
   * not add the text to the review list. Without it (or on false) the
   * transcript goes to review as before.
   */
  onTranscript?(text: string): boolean;
  /** Sends the finished request audio to the local transcription endpoint. */
  transcribe(audio: Blob, filename: string, signal: AbortSignal): Promise<{ text: string }>;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

const MAX_PENDING = 3;
const CHUNK_SAMPLES = (CAPTURE_SAMPLE_RATE * CHUNK_MS) / 1000;

/**
 * Turns a wake detection into one captured request. While waiting it keeps
 * only a bounded pre-roll ring in memory. After a detection it records until
 * the endpointer says the request is over, sends that single clip to the local
 * transcription endpoint, and hands back a transcript for review. It never
 * sends anything to chat, a model, speech synthesis or a tool.
 */
export class WakeCaptureSession {
  private snapshot: CaptureSnapshot = { phase: 'waiting', outcome: 'none', error: null, transcripts: [] };
  private listeners = new Set<(s: CaptureSnapshot) => void>();
  private readonly options: CaptureOptions;
  private preRoll: RingBuffer;
  private captured: Float32Array[] = [];
  private endpointer: Endpointer | null = null;
  private abort: AbortController | null = null;
  private timeoutHandle: unknown = null;
  private timedOut = false;
  private run = 0;
  private nextId = 1;
  private unsubscribe: (() => void) | null = null;
  /** True while a conductor owns the detector pause (submit / think / speak). */
  private held = false;

  constructor(private readonly deps: CaptureDeps, options: Partial<CaptureOptions> = {}) {
    this.options = resolveCaptureOptions(options);
    this.preRoll = new RingBuffer(Math.max(CHUNK_SAMPLES, this.preRollSamples()));
    deps.host.setTap({
      onChunk: (chunk) => this.onChunk(chunk),
      onDetection: (d) => this.onDetection(d),
    });
    // Losing the detector (switched off, mic ended, worklet failure) ends any capture.
    this.unsubscribe = deps.host.subscribe((snap) => {
      if (snap.state === 'off' || snap.state === 'error') this.reset();
    });
  }

  getSnapshot = (): CaptureSnapshot => this.snapshot;

  subscribe = (fn: (s: CaptureSnapshot) => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  private set(patch: Partial<CaptureSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    this.listeners.forEach((fn) => fn(this.snapshot));
  }

  private preRollSamples(): number {
    return Math.round((this.options.preRollMs / 1000) * CAPTURE_SAMPLE_RATE);
  }

  private onChunk(chunk: Float32Array): void {
    if (this.held) return;
    const { phase } = this.snapshot;
    if (phase === 'waiting') {
      this.preRoll.push(chunk);
      return;
    }
    if (phase !== 'listening' || !this.endpointer) return;

    this.captured.push(chunk);
    const verdict = this.endpointer.update(chunkRms(chunk));
    if (verdict === 'end') void this.finish(this.run);
    else if (verdict === 'no_speech') this.endWithOutcome('no_speech', null);
  }

  private onDetection(_detection: WakeDetection): void {
    if (this.held || this.snapshot.phase !== 'waiting') return;
    this.run += 1;
    const preRoll = this.preRoll.tail(this.preRollSamples());
    this.preRoll.clear();
    this.captured = preRoll.length > 0 ? [preRoll] : [];
    // The pre-roll usually holds the wake phrase itself, so its quietest block can be
    // speech-level; cap it so that cannot raise the voice threshold out of reach.
    const floor = Math.min(estimateNoiseFloor(preRoll, CHUNK_SAMPLES), this.options.minVoiceRms);
    this.endpointer = new Endpointer(this.options, floor);
    this.deps.host.setDetectionPaused(true);
    this.set({ phase: 'listening', outcome: 'none', error: null });
  }

  /** Stop the request now. Nothing is uploaded or inserted. */
  cancel(): void {
    if (this.snapshot.phase === 'waiting') return;
    this.run += 1;
    this.abortTranscription();
    this.clearAudio();
    this.rearm();
    this.set({ phase: 'waiting', outcome: 'none', error: null });
  }

  /** Detector went away: drop audio and any in-flight request. Transcripts are text and stay. */
  reset(): void {
    this.held = false;
    this.run += 1;
    this.abortTranscription();
    this.clearAudio();
    this.preRoll.clear();
    if (this.snapshot.phase !== 'waiting' || this.snapshot.outcome !== 'none') {
      this.set({ phase: 'waiting', outcome: 'none', error: null });
    }
  }

  /** Detach from the host and release everything. */
  dispose(): void {
    this.reset();
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.deps.host.setTap(null);
  }

  /** Give the detector back after a conductor finished with a transcript. */
  release(): void {
    if (!this.held) return;
    this.held = false;
    this.rearm();
  }

  /** Put a transcript in the review list (never submitted automatically). */
  addTranscript(text: string): void {
    const transcripts = [...this.snapshot.transcripts, { id: this.nextId++, text }].slice(-MAX_PENDING);
    this.set({ transcripts });
  }

  dismissTranscript(id: number): void {
    this.set({ transcripts: this.snapshot.transcripts.filter((t) => t.id !== id) });
  }

  dismissOutcome(): void {
    if (this.snapshot.outcome !== 'none') this.set({ outcome: 'none', error: null });
  }

  private clearAudio(): void {
    for (const segment of this.captured) segment.fill(0);
    this.captured = [];
    this.endpointer = null;
  }

  private abortTranscription(): void {
    if (this.timeoutHandle !== null) {
      this.deps.clearTimeout(this.timeoutHandle);
      this.timeoutHandle = null;
    }
    this.abort?.abort();
    this.abort = null;
  }

  private rearm(): void {
    this.preRoll.clear();
    this.deps.host.setDetectionPaused(false);
  }

  private endWithOutcome(outcome: 'no_speech' | 'error', error: string | null): void {
    this.abortTranscription();
    this.clearAudio();
    this.rearm();
    this.set({ phase: 'waiting', outcome, error });
  }

  private async finish(run: number): Promise<void> {
    if (run !== this.run || this.snapshot.phase !== 'listening') return;
    const speech = this.endpointer?.speechStarted ?? false;
    if (!speech) {
      this.endWithOutcome('no_speech', null);
      return;
    }
    const audio = encodeWav(this.captured);
    this.clearAudio();
    this.set({ phase: 'transcribing' });

    const controller = new AbortController();
    this.abort = controller;
    this.timedOut = false;
    this.timeoutHandle = this.deps.setTimeout(() => {
      this.timedOut = true;
      controller.abort();
    }, this.options.transcribeTimeoutMs);

    try {
      const result = await this.deps.transcribe(audio, 'request.wav', controller.signal);
      if (run !== this.run) return;
      const text = cleanTranscript(result?.text ?? '');
      if (text === null) {
        this.endWithOutcome('no_speech', null);
        return;
      }
      this.abortTranscription();
      if (this.deps.onTranscript?.(text)) {
        // A conductor now owns the turn; the detector stays paused until release().
        this.held = true;
        this.set({ phase: 'waiting', outcome: 'none', error: null });
        return;
      }
      this.rearm();
      const transcripts = [...this.snapshot.transcripts, { id: this.nextId++, text }].slice(-MAX_PENDING);
      this.set({ phase: 'waiting', outcome: 'none', error: null, transcripts });
    } catch (err) {
      if (run !== this.run) return;
      if (this.timedOut) {
        this.endWithOutcome('error', 'Transcription timed out. Try a shorter request.');
        return;
      }
      if (err instanceof DOMException && err.name === 'AbortError') return;
      const message = describeTranscriptionError(err instanceof Error ? err.message : '');
      if (message === NO_SPEECH_MESSAGE) this.endWithOutcome('no_speech', null);
      else this.endWithOutcome('error', message);
    }
  }
}
