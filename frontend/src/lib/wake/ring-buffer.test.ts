import { describe, expect, it } from 'vitest';
import { RingBuffer } from './ring-buffer';

describe('RingBuffer', () => {
  it('returns samples oldest-first and fewer than asked when not full', () => {
    const rb = new RingBuffer(8);
    rb.push([1, 2, 3]);
    expect(Array.from(rb.tail(5))).toEqual([1, 2, 3]);
    expect(rb.length).toBe(3);
  });

  it('wraps around and overwrites the oldest samples', () => {
    const rb = new RingBuffer(4);
    rb.push([1, 2, 3]);
    rb.push([4, 5, 6]);
    expect(Array.from(rb.tail(4))).toEqual([3, 4, 5, 6]);
    expect(Array.from(rb.tail(2))).toEqual([5, 6]);
    expect(rb.length).toBe(4);
  });

  it('stays bounded no matter how much audio is pushed', () => {
    const rb = new RingBuffer(32000);
    const chunk = new Float32Array(1280).fill(7);
    for (let i = 0; i < 2000; i++) rb.push(chunk); // ~2.7 minutes of audio
    expect(rb.length).toBe(32000);
    expect(rb.tail(10 ** 9).length).toBe(32000);
  });

  it('keeps only the last `capacity` samples of an oversized block', () => {
    const rb = new RingBuffer(3);
    rb.push([1, 2, 3, 4, 5]);
    expect(Array.from(rb.tail(3))).toEqual([3, 4, 5]);
  });

  it('clear() forgets and zeroes the stored audio', () => {
    const rb = new RingBuffer(4);
    rb.push([9, 9, 9, 9]);
    rb.clear();
    expect(rb.length).toBe(0);
    expect(rb.tail(4).length).toBe(0);
    rb.push([1]);
    expect(Array.from(rb.tail(4))).toEqual([1]);
  });

  it('rejects an invalid capacity', () => {
    expect(() => new RingBuffer(0)).toThrow();
  });
});
