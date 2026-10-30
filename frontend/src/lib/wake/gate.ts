export interface WakeGateOptions {
  /** Score at or above which a frame counts as a wake phrase. */
  threshold: number;
  /** Minimum time between two detections. */
  cooldownMs: number;
  /** The score must fall below this before the gate re-arms. */
  releaseThreshold: number;
}

export const DEFAULT_GATE_OPTIONS: WakeGateOptions = {
  threshold: 0.5,
  cooldownMs: 2000,
  releaseThreshold: 0.3,
};

/**
 * Turns a stream of per-frame scores into discrete detections. After firing it
 * stays disarmed until both the cooldown has passed and the score has dropped
 * back down, so one spoken phrase (several consecutive high frames) yields one
 * event.
 */
export class WakeGate {
  private armed = true;
  private lastFiredAt = Number.NEGATIVE_INFINITY;
  private readonly options: WakeGateOptions;

  constructor(options: Partial<WakeGateOptions> = {}) {
    this.options = { ...DEFAULT_GATE_OPTIONS, ...options };
  }

  /** Returns true exactly when this frame should raise a detection. */
  update(score: number, nowMs: number): boolean {
    if (!this.armed) {
      const cooledDown = nowMs - this.lastFiredAt >= this.options.cooldownMs;
      if (cooledDown && score < this.options.releaseThreshold) this.armed = true;
      return false;
    }
    if (score >= this.options.threshold) {
      this.armed = false;
      this.lastFiredAt = nowMs;
      return true;
    }
    return false;
  }

  reset(): void {
    this.armed = true;
    this.lastFiredAt = Number.NEGATIVE_INFINITY;
  }
}
