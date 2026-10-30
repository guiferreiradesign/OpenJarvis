import { CAPTURE_SAMPLE_RATE } from './options';

/**
 * Encode 16 kHz mono audio, held as Float32Arrays of 16-bit PCM values (the
 * detector's chunk format), as a WAV file the local speech service accepts.
 */
export function encodeWav(segments: Float32Array[], sampleRate = CAPTURE_SAMPLE_RATE): Blob {
  const total = segments.reduce((n, s) => n + s.length, 0);
  const buffer = new ArrayBuffer(44 + total * 2);
  const view = new DataView(buffer);
  const text = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
  };
  text(0, 'RIFF');
  view.setUint32(4, 36 + total * 2, true);
  text(8, 'WAVE');
  text(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, 'data');
  view.setUint32(40, total * 2, true);
  let offset = 44;
  for (const segment of segments) {
    for (let i = 0; i < segment.length; i++) {
      view.setInt16(offset, Math.max(-32768, Math.min(32767, Math.round(segment[i]))), true);
      offset += 2;
    }
  }
  return new Blob([buffer], { type: 'audio/wav' });
}
