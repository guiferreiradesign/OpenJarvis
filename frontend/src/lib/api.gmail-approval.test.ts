import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  cancelGmailSendApproval,
  confirmGmailSendApproval,
  fetchGmailSendApproval,
} from './api';

describe('Gmail approval API actions', () => {
  afterEach(() => vi.unstubAllGlobals());

  function stubSessionStorage() {
    const values = new Map<string, string>();
    vi.stubGlobal('sessionStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    });
  }

  it('loads the server-stored approval message', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: 'opaque/id', status: 'pending', message: { to: 'a@example.com', subject: 'Subject', body: 'Body' },
    }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    stubSessionStorage();
    const item = await fetchGmailSendApproval('opaque/id', 'ui-secret');
    expect(item.message).toEqual({ to: 'a@example.com', subject: 'Subject', body: 'Body' });
    expect(fetchMock.mock.calls[0][0]).toContain('/v1/gmail/approvals/opaque%2Fid');
    expect(fetchMock.mock.calls[0][1]?.headers).toMatchObject({ 'X-OpenJarvis-Approval': 'ui-secret' });
    expect((fetchMock.mock.calls[0][1]?.headers as Record<string, string>)['X-OpenJarvis-Chat-Session']).toMatch(/^[A-Za-z0-9-]{36}$/);
  });

  it('uses distinct confirm and cancel endpoint actions', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: 'sent', message_id: 'm-1' }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: 'cancelled' }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    stubSessionStorage();
    await confirmGmailSendApproval('approval-1', 'ui-secret');
    await cancelGmailSendApproval('approval-1', 'ui-secret');
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method])).toEqual([
      ['/v1/gmail/approvals/approval-1/confirm', 'POST'],
      ['/v1/gmail/approvals/approval-1/cancel', 'POST'],
    ]);
  });
});
