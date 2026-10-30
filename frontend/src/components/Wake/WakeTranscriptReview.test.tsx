import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  transcripts: [] as Array<{ id: number; text: string }>,
  turn: { phase: 'idle', text: '', deadlineAt: null as number | null, notice: null },
  dismiss: vi.fn(),
  cancel: vi.fn(),
}));
vi.mock('../../lib/wake-capture/store', () => ({
  useHandsFreeStore: (selector: (s: unknown) => unknown) =>
    selector({ capture: { transcripts: mocks.transcripts }, dismissTranscript: mocks.dismiss, turn: mocks.turn, cancel: mocks.cancel }),
}));

import { WakeTranscriptReview } from './WakeTranscriptReview';

const render = (draft: string, onChange = vi.fn()) =>
  renderToStaticMarkup(<WakeTranscriptReview draft={draft} onChange={onChange} />);

beforeEach(() => {
  mocks.transcripts = [];
  mocks.turn = { phase: 'idle', text: '', deadlineAt: null, notice: null };
  vi.clearAllMocks();
});

describe('WakeTranscriptReview', () => {
  it('renders nothing when there is no transcript', () => {
    expect(render('my draft')).toBe('');
  });

  it('does not show a card (and leaves placement to the empty input) when the draft is empty', () => {
    mocks.transcripts = [{ id: 1, text: 'what is the time' }];
    expect(render('')).toBe('');
  });

  it('shows the transcript separately and offers insert, replace and discard when a draft exists', () => {
    mocks.transcripts = [{ id: 1, text: 'what is the time' }, { id: 2, text: 'another' }];
    const html = render('half-written email');
    expect(html).toContain('what is the time');
    expect(html).toContain('current text was left untouched');
    expect(html).toContain('Add to my text');
    expect(html).toContain('Replace my text');
    expect(html).toContain('Discard');
    expect(html).toContain('+1 more');
  });

  it('shows the transcript and a visible cancel control during the send countdown', () => {
    mocks.turn = { phase: 'pending', text: 'turn off the lights', deadlineAt: 1500, notice: null };
    const html = render('');
    expect(html).toContain('turn off the lights');
    expect(html).toContain('Sending this voice request in 1.5 s');
    expect(html).toContain('Cancel sending this voice request');
  });

  it('never calls onChange or dismiss while merely rendering over an existing draft', () => {
    mocks.transcripts = [{ id: 1, text: 'hello' }];
    const onChange = vi.fn();
    render('keep this', onChange);
    expect(onChange).not.toHaveBeenCalled();
    expect(mocks.dismiss).not.toHaveBeenCalled();
  });
});
