import { describe, expect, it } from 'vitest';
import { Endpointer, chunkRms, estimateNoiseFloor } from './endpointer';
import { DEFAULT_CAPTURE_OPTIONS, MAX_PRE_ROLL_MS, resolveCaptureOptions } from './options';

const LOUD = 3000;
const QUIET = 20;
const ms = (n: number) => n / 80;

function run(ep: Endpointer, rms: number, ms_: number) {
  let verdict = 'continue';
  for (let i = 0; i < ms(ms_); i++) {
    verdict = ep.update(rms);
    if (verdict !== 'continue') return verdict;
  }
  return verdict;
}

describe('Endpointer', () => {
  it('ends about 1.2 s after speech stops', () => {
    const ep = new Endpointer(DEFAULT_CAPTURE_OPTIONS);
    expect(run(ep, LOUD, 800)).toBe('continue');
    expect(ep.speechStarted).toBe(true);
    expect(run(ep, QUIET, 1120)).toBe('continue');
    expect(ep.update(QUIET)).toBe('end');
  });

  it('keeps going through a short pause inside a sentence', () => {
    const ep = new Endpointer(DEFAULT_CAPTURE_OPTIONS);
    run(ep, LOUD, 800);
    run(ep, QUIET, 640);
    expect(run(ep, LOUD, 800)).toBe('continue');
    expect(run(ep, QUIET, 1120)).toBe('continue');
    expect(ep.update(QUIET)).toBe('end');
  });

  it('reports no speech after 6 s of silence', () => {
    const ep = new Endpointer(DEFAULT_CAPTURE_OPTIONS);
    expect(run(ep, QUIET, 5920)).toBe('continue');
    expect(ep.update(QUIET)).toBe('no_speech');
  });

  it('ignores a brief click shorter than the minimum speech run', () => {
    const ep = new Endpointer(DEFAULT_CAPTURE_OPTIONS);
    ep.update(LOUD);
    ep.update(LOUD);
    expect(ep.speechStarted).toBe(false);
    expect(run(ep, QUIET, 6000)).toBe('no_speech');
  });

  it('stops at the 20 s hard limit even if the user keeps talking', () => {
    const ep = new Endpointer(DEFAULT_CAPTURE_OPTIONS);
    expect(run(ep, LOUD, 19_920)).toBe('continue');
    expect(ep.update(LOUD)).toBe('end');
  });

  it('adapts to a noisy room instead of treating the noise as speech', () => {
    const ep = new Endpointer(DEFAULT_CAPTURE_OPTIONS, 600);
    expect(ep.voiceThreshold).toBe(1800);
    expect(run(ep, 900, 6000)).toBe('no_speech');
  });
});

describe('audio helpers and options', () => {
  it('computes RMS and the quietest block', () => {
    expect(chunkRms(new Float32Array(0))).toBe(0);
    expect(chunkRms(Float32Array.of(3, -3, 3, -3))).toBeCloseTo(3);
    const samples = new Float32Array(8);
    samples.fill(10, 0, 4);
    samples.fill(2, 4, 8);
    expect(estimateNoiseFloor(samples, 4)).toBeCloseTo(2);
    expect(estimateNoiseFloor(new Float32Array(2), 4)).toBe(0);
  });

  it('caps the pre-roll at two seconds', () => {
    expect(resolveCaptureOptions({ preRollMs: 9000 }).preRollMs).toBe(MAX_PRE_ROLL_MS);
    expect(resolveCaptureOptions({ preRollMs: -5 }).preRollMs).toBe(0);
    expect(resolveCaptureOptions({ endSilenceMs: 900 }).endSilenceMs).toBe(900);
  });
});
