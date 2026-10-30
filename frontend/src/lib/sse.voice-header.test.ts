import { afterEach, describe, expect, it, vi } from 'vitest';
import { streamChat } from './sse';

function capture() {
  const fetchMock = vi.fn(async () => new Response('data: [DONE]\n\n', { status: 200 }));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}
async function run(options?: { inputMode?: 'voice' }) {
  const fetchMock = capture();
  for await (const ev of streamChat({ model: 'm', messages: [{ role: 'user', content: 'hi' }], stream: true }, undefined, options)) void ev;
  const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
  return new Headers(init.headers as HeadersInit);
}
afterEach(() => vi.unstubAllGlobals());

describe('chat request input-mode header', () => {
  it('is added for voice turns', async () => {
    expect((await run({ inputMode: 'voice' })).get('X-OpenJarvis-Input-Mode')).toBe('voice');
  });
  it('is absent for typed turns, which keep their existing behaviour', async () => {
    expect((await run()).get('X-OpenJarvis-Input-Mode')).toBeNull();
    expect((await run({})).get('X-OpenJarvis-Input-Mode')).toBeNull();
  });
});
