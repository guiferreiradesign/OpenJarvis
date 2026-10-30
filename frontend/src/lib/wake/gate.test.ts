import { describe, expect, it } from 'vitest';
import { WakeGate } from './gate';

describe('WakeGate', () => {
  it('fires only at or above the threshold', () => {
    const g = new WakeGate();
    expect(g.update(0.49, 0)).toBe(false);
    expect(g.update(0.5, 80)).toBe(true);
  });

  it('turns consecutive high frames into one detection', () => {
    const g = new WakeGate();
    const fired = [0.6, 0.9, 0.95, 0.8, 0.7].map((s, i) => g.update(s, i * 80));
    expect(fired.filter(Boolean)).toHaveLength(1);
  });

  it('does not re-arm before the cooldown even if the score drops', () => {
    const g = new WakeGate({ cooldownMs: 2000 });
    expect(g.update(0.9, 0)).toBe(true);
    expect(g.update(0.0, 500)).toBe(false); // drops, but too soon
    expect(g.update(0.9, 900)).toBe(false);
  });

  it('does not re-arm after the cooldown while the score stays high', () => {
    const g = new WakeGate({ cooldownMs: 2000 });
    g.update(0.9, 0);
    expect(g.update(0.9, 3000)).toBe(false);
  });

  it('fires again after cooldown once the score has dropped', () => {
    const g = new WakeGate({ cooldownMs: 2000 });
    expect(g.update(0.9, 0)).toBe(true);
    expect(g.update(0.1, 2500)).toBe(false); // re-arms
    expect(g.update(0.9, 2600)).toBe(true);
  });

  it('reset() re-arms immediately', () => {
    const g = new WakeGate();
    g.update(0.9, 0);
    g.reset();
    expect(g.update(0.9, 10)).toBe(true);
  });
});
