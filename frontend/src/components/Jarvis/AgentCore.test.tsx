import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { AgentCore, DEFAULT_BLOOM_SETTINGS } from './AgentCore';

describe('AgentCore visual state contract', () => {
  it('exposes the five replaceable states and marks simulated preview states', () => {
    const states = ['ready', 'listening', 'thinking', 'speaking', 'executing'] as const;
    for (const state of states) {
      const markup = renderToStaticMarkup(<AgentCore state={state} simulated={state === 'ready'} />);
      expect(markup).toContain(`state-${state}`);
    }
    expect(renderToStaticMarkup(<AgentCore simulated />)).toContain('VISUAL PREVIEW');
  });

  it('keeps the approved Bloom renderer defaults explicit', () => {
    expect(DEFAULT_BLOOM_SETTINGS).toEqual({
      strength: 0.30,
      radius: 0.75,
      exposure: 1.20,
      quality: 'Balanced',
      bloom: true,
      parallax: true,
    });
  });
});
