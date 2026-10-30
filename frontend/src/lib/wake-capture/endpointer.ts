import { CHUNK_MS, type CaptureOptions } from './options';

/** RMS of a chunk holding 16-bit PCM values. */
export function chunkRms(chunk: ArrayLike<number>): number {
  if (chunk.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
  return Math.sqrt(sum / chunk.length);
}

/** Quietest block of the given audio, used as the starting noise floor. */
export function estimateNoiseFloor(samples: Float32Array, blockSize: number): number {
  let floor = Number.POSITIVE_INFINITY;
  for (let i = 0; i + blockSize <= samples.length; i += blockSize) {
    floor = Math.min(floor, chunkRms(samples.subarray(i, i + blockSize)));
  }
  return Number.isFinite(floor) ? floor : 0;
}

export type EndpointVerdict = 'continue' | 'end' | 'no_speech';

/**
 * Energy-based endpointing driven by chunk count, so timing is deterministic
 * and needs no timers. Speech starts after a few consecutive voiced chunks and
 * ends after a stretch of silence; waiting too long for speech, or running past
 * the hard limit, also ends the request.
 */
export class Endpointer {
  private elapsedMs = 0;
  private voicedRun = 0;
  private silentMs = 0;
  private floor: number;
  private started = false;

  constructor(private readonly options: CaptureOptions, initialNoiseFloor = 0) {
    this.floor = initialNoiseFloor;
  }

  get speechStarted(): boolean {
    return this.started;
  }

  get voiceThreshold(): number {
    return Math.max(this.options.minVoiceRms, this.floor * this.options.noiseFloorFactor);
  }

  update(rms: number): EndpointVerdict {
    this.elapsedMs += CHUNK_MS;
    const voiced = rms >= this.voiceThreshold;

    if (voiced) {
      this.voicedRun += 1;
      this.silentMs = 0;
      if (this.voicedRun >= this.options.minSpeechChunks) this.started = true;
    } else {
      this.voicedRun = 0;
      this.floor = this.floor * 0.9 + rms * 0.1;
      if (this.started) this.silentMs += CHUNK_MS;
    }

    if (this.started && this.silentMs >= this.options.endSilenceMs) return 'end';
    if (this.elapsedMs >= this.options.maxRequestMs) return this.started ? 'end' : 'no_speech';
    if (!this.started && this.elapsedMs >= this.options.noSpeechTimeoutMs) return 'no_speech';
    return 'continue';
  }
}
