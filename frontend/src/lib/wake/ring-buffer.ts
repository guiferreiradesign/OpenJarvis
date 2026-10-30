/**
 * Fixed-size circular sample buffer. The wake detector keeps only the last
 * couple of seconds of microphone audio here, overwriting the oldest samples;
 * nothing else retains raw audio, and nothing is written to disk.
 */
export class RingBuffer {
  private readonly data: Float32Array;
  private writePos = 0;
  private filled = 0;

  constructor(readonly capacity: number) {
    if (!Number.isInteger(capacity) || capacity <= 0) {
      throw new Error('RingBuffer capacity must be a positive integer');
    }
    this.data = new Float32Array(capacity);
  }

  /** Number of valid samples currently held (never more than `capacity`). */
  get length(): number {
    return this.filled;
  }

  push(samples: ArrayLike<number>): void {
    // A block larger than the buffer can only leave its last `capacity` samples.
    const start = Math.max(0, samples.length - this.capacity);
    for (let i = start; i < samples.length; i++) {
      this.data[this.writePos] = samples[i];
      this.writePos = (this.writePos + 1) % this.capacity;
    }
    this.filled = Math.min(this.capacity, this.filled + (samples.length - start));
  }

  /** The most recent `n` samples (or fewer, if fewer exist), oldest first. */
  tail(n: number): Float32Array {
    const count = Math.min(Math.max(0, n), this.filled);
    const out = new Float32Array(count);
    let pos = (this.writePos - count + this.capacity) % this.capacity;
    for (let i = 0; i < count; i++) {
      out[i] = this.data[pos];
      pos = (pos + 1) % this.capacity;
    }
    return out;
  }

  /** Forget everything and overwrite the stored samples with zeros. */
  clear(): void {
    this.data.fill(0);
    this.writePos = 0;
    this.filled = 0;
  }
}
