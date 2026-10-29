import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../hooks/useTts', () => ({ useTts: vi.fn() }));

import { useTts } from '../../hooks/useTts';
import { SpeakMessageButton } from './SpeakMessageButton';

describe('read-aloud feedback', () => {
  it('shows a playback error beside the message that failed', () => {
    vi.mocked(useTts).mockReturnValue({
      available: true,
      state: 'idle',
      speakingId: null,
      error: 'Playback failed',
      errorId: 'failed-message',
      speak: vi.fn(async () => {}),
      stop: vi.fn(),
      isLoading: false,
      isSpeaking: false,
    });

    const failed = renderToStaticMarkup(
      <SpeakMessageButton messageId="failed-message" content="Hello" />,
    );
    const other = renderToStaticMarkup(
      <SpeakMessageButton messageId="other-message" content="Hello" />,
    );

    expect(failed).toContain('role="alert"');
    expect(failed).toContain('Playback failed');
    expect(other).not.toContain('Playback failed');
  });

  const base = {
    available: true,
    state: 'idle' as const,
    speakingId: null,
    error: null,
    errorId: null,
    speak: vi.fn(async () => {}),
    stop: vi.fn(),
    isLoading: false,
    isSpeaking: false,
  };

  it('offers an accessible Speak response control without the voice-output setting', () => {
    vi.mocked(useTts).mockReturnValue(base);
    const html = renderToStaticMarkup(<SpeakMessageButton messageId="m1" content="Hello" />);
    expect(html).toContain('aria-label="Speak response"');
  });

  it('hides the control when no TTS backend is available', () => {
    vi.mocked(useTts).mockReturnValue({ ...base, available: false });
    expect(renderToStaticMarkup(<SpeakMessageButton messageId="m1" content="Hello" />)).toBe('');
  });

  it('shows loading and playing states only on the owning message', () => {
    vi.mocked(useTts).mockReturnValue({ ...base, state: 'loading', speakingId: 'm1', isLoading: true });
    const loading = renderToStaticMarkup(<SpeakMessageButton messageId="m1" content="Hi" />);
    const other = renderToStaticMarkup(<SpeakMessageButton messageId="m2" content="Hi" />);
    expect(loading).toContain('aria-busy="true"');
    expect(loading).toContain('aria-label="Stop speaking"');
    expect(other).toContain('aria-label="Speak response"');

    vi.mocked(useTts).mockReturnValue({ ...base, state: 'speaking', speakingId: 'm1', isSpeaking: true });
    expect(renderToStaticMarkup(<SpeakMessageButton messageId="m1" content="Hi" />)).toContain(
      'aria-label="Stop speaking"',
    );
  });
});
