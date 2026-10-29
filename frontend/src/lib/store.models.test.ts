import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ModelInfo } from '../types';

class MemoryStorage {
  private store = new Map<string, string>();

  getItem(key: string): string | null {
    return this.store.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

const model = (id: string): ModelInfo => ({
  id,
  object: 'model',
  created: 0,
  owned_by: 'openjarvis',
});

beforeEach(() => {
  vi.resetModules();
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage =
    new MemoryStorage();
});

afterEach(() => {
  (globalThis as unknown as { localStorage?: MemoryStorage }).localStorage =
    undefined;
});

describe('setModels', () => {
  it('does not select an embedding-only model', async () => {
    const { useAppStore } = await import('./store');

    useAppStore.getState().setModels([model('nomic-embed-text')]);

    expect(useAppStore.getState().selectedModel).toBe('');
  });

  it('clears a missing selection when no chat fallback exists', async () => {
    const { useAppStore } = await import('./store');
    useAppStore.getState().setSelectedModel('deleted-chat-model');

    useAppStore.getState().setModels([model('nomic-embed-text')]);

    expect(useAppStore.getState().selectedModel).toBe('');
  });

  it('replaces an embedding selection with an available chat model', async () => {
    const { useAppStore } = await import('./store');
    useAppStore.getState().setSelectedModel('all-minilm:latest');

    useAppStore.getState().setModels([
      model('all-minilm:latest'),
      model('qwen3.5:4b'),
    ]);

    expect(useAppStore.getState().selectedModel).toBe('qwen3.5:4b');
  });

  it('initializes from the configured model when it is absent from the model list', async () => {
    const { useAppStore } = await import('./store');

    useAppStore.getState().initializeModels(
      [model('qwen3.5:4b-q4_K_M')],
      {
        model: 'openrouter/cohere/north-mini-code:free',
        agent: 'orchestrator',
        engine: 'multi',
      },
    );

    expect(useAppStore.getState().models.map((m) => m.id)).toEqual([
      'qwen3.5:4b-q4_K_M',
    ]);
    expect(useAppStore.getState().serverInfo?.model).toBe(
      'openrouter/cohere/north-mini-code:free',
    );
    expect(useAppStore.getState().selectedModel).toBe(
      'openrouter/cohere/north-mini-code:free',
    );
  });

  it('preserves a local model chosen after configured-model initialization', async () => {
    const { useAppStore } = await import('./store');

    useAppStore.getState().initializeModels(
      [model('qwen3.5:4b-q4_K_M')],
      {
        model: 'openrouter/cohere/north-mini-code:free',
        agent: 'orchestrator',
        engine: 'multi',
      },
    );
    useAppStore.getState().setSelectedModel('qwen3.5:4b-q4_K_M');

    // Model refreshes must not reset a valid picker selection to server default.
    useAppStore.getState().setModels([model('qwen3.5:4b-q4_K_M')]);

    expect(useAppStore.getState().selectedModel).toBe('qwen3.5:4b-q4_K_M');
  });
});
