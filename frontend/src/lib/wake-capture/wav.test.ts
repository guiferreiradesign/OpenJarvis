import { describe, expect, it } from 'vitest';
import { encodeWav } from './wav';

async function bytes(blob: Blob) {
  return new DataView(await blob.arrayBuffer());
}

describe('encodeWav', () => {
  it('writes a 16 kHz mono 16-bit PCM header and the samples in order', async () => {
    const blob = encodeWav([Float32Array.of(1, -2, 3), Float32Array.of(40000, -40000)]);
    expect(blob.type).toBe('audio/wav');
    expect(blob.size).toBe(44 + 5 * 2);
    const v = await bytes(blob);
    const tag = (o: number) => String.fromCharCode(v.getUint8(o), v.getUint8(o + 1), v.getUint8(o + 2), v.getUint8(o + 3));
    expect([tag(0), tag(8), tag(12), tag(36)]).toEqual(['RIFF', 'WAVE', 'fmt ', 'data']);
    expect(v.getUint16(20, true)).toBe(1);
    expect(v.getUint16(22, true)).toBe(1);
    expect(v.getUint32(24, true)).toBe(16000);
    expect(v.getUint16(34, true)).toBe(16);
    expect(v.getUint32(40, true)).toBe(10);
    expect([0, 1, 2, 3, 4].map((i) => v.getInt16(44 + i * 2, true))).toEqual([1, -2, 3, 32767, -32768]);
  });
});
