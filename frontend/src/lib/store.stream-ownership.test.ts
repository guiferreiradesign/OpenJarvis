import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

class MemoryStorage {
  private store = new Map<string, string>();

  getItem(key: string): string | null {
    return this.store.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }

  removeItem(key: string): void {
    this.store.delete(key);
  }
}

beforeEach(() => {
  vi.resetModules();
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage =
    new MemoryStorage();
});

afterEach(() => {
  (globalThis as unknown as { localStorage?: MemoryStorage }).localStorage =
    undefined;
});

async function freshStore() {
  return (await import('./store')).useAppStore;
}

describe('conversation stream ownership', () => {
  it('starts a new conversation with no messages from the previous one', async () => {
    const store = await freshStore();
    const firstId = store.getState().createConversation('test-model');
    store.getState().addMessage(firstId, {
      id: 'prior-turn',
      role: 'user',
      content: 'Previous conversation only',
      timestamp: 1,
    });

    const secondId = store.getState().createConversation('test-model');

    expect(secondId).not.toBe(firstId);
    expect(store.getState().activeId).toBe(secondId);
    expect(store.getState().messages).toEqual([]);
    expect(store.getState().conversations.find((item) => item.id === firstId)?.messages).toHaveLength(1);
  });

  it('persists background stream updates without replacing the active messages', async () => {
    const store = await freshStore();
    const sourceId = store.getState().createConversation('test-model');
    store.getState().addMessage(sourceId, {
      id: 'assistant',
      role: 'assistant',
      content: '',
      timestamp: 1,
    });

    const activeId = store.getState().createConversation('test-model');
    store.getState().setStreamState({
      conversationId: sourceId,
      isStreaming: true,
      content: 'streamed response',
    });
    store.getState().updateLastAssistant(sourceId, 'streamed response');

    expect(store.getState().activeId).toBe(activeId);
    expect(store.getState().messages).toEqual([]);

    store.getState().selectConversation(sourceId);
    expect(store.getState().messages).toHaveLength(1);
    expect(store.getState().messages[0].content).toBe('streamed response');
  });

  it('keeps the stream-owning conversation until generation stops', async () => {
    const store = await freshStore();
    const sourceId = store.getState().createConversation('test-model');
    const activeId = store.getState().createConversation('test-model');
    store.getState().setStreamState({
      conversationId: sourceId,
      isStreaming: true,
    });

    store.getState().deleteConversation(sourceId);
    expect(
      store.getState().conversations.map((conversation) => conversation.id),
    ).toContain(sourceId);
    expect(store.getState().activeId).toBe(activeId);

    store.getState().resetStream();
    store.getState().deleteConversation(sourceId);
    expect(
      store.getState().conversations.map((conversation) => conversation.id),
    ).not.toContain(sourceId);
  });
});
