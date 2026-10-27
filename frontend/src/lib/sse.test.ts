import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const fetchMock = vi.fn<typeof fetch>();
const originalFetch = globalThis.fetch;

beforeEach(() => {
  vi.resetModules();
  fetchMock.mockReset();
  globalThis.fetch = fetchMock;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe('streamChat', () => {
  it('sends the full active transcript to the existing chat endpoint', async () => {
    fetchMock.mockResolvedValue(new Response('event: message\ndata: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n'));
    const { streamChat } = await import('./sse');
    const messages = [
      { role: 'user', content: 'Remember this value: blue.' },
      { role: 'assistant', content: 'I will remember blue.' },
      { role: 'user', content: 'What value did I give you?' },
    ];
    const events = [];
    for await (const event of streamChat({ model: 'local-model', messages, stream: true })) events.push(event);

    expect(fetchMock).toHaveBeenCalledWith('/v1/chat/completions', expect.objectContaining({ method: 'POST' }));
    const request = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(request.messages).toEqual(messages);
    expect(request.stream).toBe(true);
    expect(events).toEqual([{ event: 'message', data: '{"choices":[{"delta":{"content":"Hello"}}]}' }]);
  });

  it('surfaces server failures so the chat view can recover', async () => {
    fetchMock.mockResolvedValue(new Response('unavailable', { status: 503 }));
    const { streamChat } = await import('./sse');
    const consume = async () => {
      for await (const _event of streamChat({ model: 'local-model', messages: [{ role: 'user', content: 'Hi' }], stream: true })) { /* consume */ }
    };
    await expect(consume()).rejects.toThrow('Chat request failed: 503');
  });
});
