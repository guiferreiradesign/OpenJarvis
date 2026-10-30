import { RingBuffer } from './ring-buffer';

/**
 * Streaming openWakeWord feature pipeline, ported from the upstream v0.5.1
 * Python (`AudioFeatures` + `Model.predict`) and validated in the Phase 4.4.0
 * spike. Audio arrives as 80 ms chunks of 16 kHz mono samples holding 16-bit
 * PCM values (not normalised to +-1).
 */
export const SAMPLE_RATE = 16000;
export const CHUNK_SAMPLES = 1280; // 80 ms
export const MEL_CONTEXT_SAMPLES = 160 * 3; // extra history upstream prepends per chunk
export const MEL_BINS = 32;
export const EMBEDDING_WINDOW = 76; // mel frames per embedding
export const EMBEDDING_DIM = 96;
/** Upstream zeroes the first scores while its buffers fill. */
export const WARMUP_CHUNKS = 5;
/** About two seconds of audio: the only raw audio the detector ever holds. */
export const RING_SAMPLES = SAMPLE_RATE * 2;

const MEL_BUFFER_FRAMES = 970;
const FEATURE_BUFFER_FRAMES = 120;

/** One loaded model, reduced to what the pipeline needs. */
export interface TensorRunner {
  run(data: Float32Array, dims: number[]): Promise<Float32Array>;
  release?(): Promise<void> | void;
}

export interface WakePipelineModels {
  mel: TensorRunner;
  embedding: TensorRunner;
  classifier: TensorRunner;
  /** Embedding frames the classifier consumes (16 for the shipped models). */
  classifierFrames: number;
}

export class WakePipeline {
  private readonly ring: RingBuffer;
  private melFrames: Float32Array[] = [];
  private features: Float32Array[] = [];
  private chunks = 0;

  constructor(private readonly models: WakePipelineModels, ring?: RingBuffer) {
    this.ring = ring ?? new RingBuffer(RING_SAMPLES);
    this.reset();
  }

  /** Samples of raw audio currently buffered (bounded by the ring size). */
  get bufferedSamples(): number {
    return this.ring.length;
  }

  reset(): void {
    this.ring.clear();
    this.melFrames = Array.from({ length: EMBEDDING_WINDOW }, () =>
      new Float32Array(MEL_BINS).fill(1),
    );
    this.features = Array.from({ length: FEATURE_BUFFER_FRAMES }, () =>
      new Float32Array(EMBEDDING_DIM),
    );
    this.chunks = 0;
  }

  /** Feed one 80 ms chunk; resolves to the wake score for this frame (0..1). */
  async process(chunk: ArrayLike<number>): Promise<number> {
    if (chunk.length !== CHUNK_SAMPLES) {
      throw new Error(`Expected ${CHUNK_SAMPLES} samples per chunk, got ${chunk.length}`);
    }
    this.ring.push(chunk);

    const window = this.ring.tail(CHUNK_SAMPLES + MEL_CONTEXT_SAMPLES);
    const mel = await this.models.mel.run(window, [1, window.length]);
    const frames = Math.floor(mel.length / MEL_BINS);
    for (let f = 0; f < frames; f++) {
      const row = new Float32Array(MEL_BINS);
      for (let b = 0; b < MEL_BINS; b++) row[b] = mel[f * MEL_BINS + b] / 10 + 2;
      this.melFrames.push(row);
    }
    if (this.melFrames.length > MEL_BUFFER_FRAMES) {
      this.melFrames.splice(0, this.melFrames.length - MEL_BUFFER_FRAMES);
    }

    const flat = new Float32Array(EMBEDDING_WINDOW * MEL_BINS);
    this.melFrames.slice(-EMBEDDING_WINDOW).forEach((row, i) => flat.set(row, i * MEL_BINS));
    const embedding = await this.models.embedding.run(flat, [1, EMBEDDING_WINDOW, MEL_BINS, 1]);
    this.features.push(Float32Array.from(embedding.subarray(0, EMBEDDING_DIM)));
    if (this.features.length > FEATURE_BUFFER_FRAMES) this.features.shift();

    const n = this.models.classifierFrames;
    const input = new Float32Array(n * EMBEDDING_DIM);
    this.features.slice(-n).forEach((row, i) => input.set(row, i * EMBEDDING_DIM));
    const out = await this.models.classifier.run(input, [1, n, EMBEDDING_DIM]);

    this.chunks += 1;
    return this.chunks <= WARMUP_CHUNKS ? 0 : out[0];
  }

  async release(): Promise<void> {
    this.ring.clear();
    await Promise.allSettled(
      [this.models.mel, this.models.embedding, this.models.classifier].map(
        async (m) => m.release?.(),
      ),
    );
  }
}
