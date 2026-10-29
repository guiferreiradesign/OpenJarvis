import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { MicButton } from './MicButton';

const render = (props: Partial<Parameters<typeof MicButton>[0]>) =>
  renderToStaticMarkup(<MicButton state="idle" onClick={vi.fn()} {...props} />);

describe('MicButton', () => {
  it('is an enabled, labelled button when idle', () => {
    const html = render({});
    expect(html).toContain('aria-label="Voice input"');
    expect(html).toContain('aria-pressed="false"');
    expect(html).not.toContain('disabled');
  });

  it('shows the recording state and offers to stop', () => {
    const html = render({ state: 'recording' });
    expect(html).toContain('aria-label="Stop recording"');
    expect(html).toContain('aria-pressed="true"');
  });

  it('is disabled while transcribing', () => {
    const html = render({ state: 'transcribing' });
    expect(html).toContain('aria-label="Transcribing..."');
    expect(html).toContain('disabled');
  });

  it('explains why it is disabled', () => {
    expect(render({ disabled: true, reason: 'not-enabled' })).toContain('Enable in Settings');
    expect(render({ disabled: true, reason: 'no-backend' })).toContain(
      'Local speech recognition is not available',
    );
  });
});
