/** Chunks arrive every 80 ms (1280 samples at 16 kHz). */
export const CHUNK_MS = 80;
export const CAPTURE_SAMPLE_RATE = 16000;

/** Tunable timing for wake-triggered request capture. */
export interface CaptureOptions {
  /** Audio kept from before the detection fires. Hard-capped at MAX_PRE_ROLL_MS. */
  preRollMs: number;
  /** Silence after speech that ends the request. */
  endSilenceMs: number;
  /** How long to wait for speech to begin after the wake phrase. */
  noSpeechTimeoutMs: number;
  /** Hard limit for the request itself, measured from the detection. */
  maxRequestMs: number;
  /** Consecutive voiced chunks needed before speech counts as started. */
  minSpeechChunks: number;
  /** Absolute floor for the voice threshold (RMS on the 16-bit scale). */
  minVoiceRms: number;
  /** Voice threshold as a multiple of the tracked noise floor. */
  noiseFloorFactor: number;
  /** Give up on a transcription that takes longer than this. */
  transcribeTimeoutMs: number;
}

export const MAX_PRE_ROLL_MS = 2000;

export const DEFAULT_CAPTURE_OPTIONS: CaptureOptions = {
  preRollMs: 1200,
  endSilenceMs: 1200,
  noSpeechTimeoutMs: 6000,
  maxRequestMs: 20_000,
  minSpeechChunks: 3,
  minVoiceRms: 400,
  noiseFloorFactor: 3,
  transcribeTimeoutMs: 60_000,
};

export function resolveCaptureOptions(overrides: Partial<CaptureOptions> = {}): CaptureOptions {
  const merged = { ...DEFAULT_CAPTURE_OPTIONS, ...overrides };
  merged.preRollMs = Math.min(Math.max(0, merged.preRollMs), MAX_PRE_ROLL_MS);
  return merged;
}
