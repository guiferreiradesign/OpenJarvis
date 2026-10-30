import { useEffect, useState } from 'react';
import { Mic, X } from 'lucide-react';
import { useHandsFreeStore } from '../../lib/wake-capture/store';
import { SUBMIT_COUNTDOWN_MS } from '../../lib/wake-capture/conductor';
import { applyTranscript, placeTranscript, type TranscriptChoice } from '../../lib/wake-capture/transcript';

/** Visible cancellation window before a voice request is sent. */
function SendCountdown({ text, durationMs, onCancel }: { text: string; durationMs: number; onCancel: () => void }) {
  const [started, setStarted] = useState(false);
  useEffect(() => {
    const id = window.requestAnimationFrame(() => setStarted(true));
    return () => window.cancelAnimationFrame(id);
  }, []);
  return (
    <div
      role="group"
      aria-label="Voice request about to be sent"
      className="mb-2 rounded-xl px-3 py-2 text-xs"
      style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-accent)' }}
    >
      <div className="flex items-center justify-between gap-2 mb-1" style={{ color: 'var(--color-text-secondary)' }}>
        <span className="flex items-center gap-1.5"><Mic size={12} aria-hidden="true" />Sending this voice request in {(durationMs / 1000).toFixed(1)} s</span>
        <button type="button" onClick={onCancel} className="flex items-center gap-1 rounded px-2 py-0.5 cursor-pointer"
          style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text)' }}
          aria-label="Cancel sending this voice request" title="Cancel (Esc)">
          <X size={10} aria-hidden="true" />Cancel (Esc)
        </button>
      </div>
      <p className="mb-2 text-sm" style={{ color: 'var(--color-text)' }}>{text}</p>
      <div className="h-1 rounded-full overflow-hidden" style={{ background: 'var(--color-bg-tertiary)' }}>
        <div
          className="h-full"
          style={{
            width: started ? '0%' : '100%',
            background: 'var(--color-accent)',
            transition: `width ${durationMs}ms linear`,
          }}
        />
      </div>
    </div>
  );
}

/**
 * Hands-free transcripts meet the chat input here. An empty input receives the
 * text directly; a non-empty one is never touched until the user chooses
 * Insert (append) or Replace. Nothing is ever sent from here.
 */
export function WakeTranscriptReview({
  draft,
  onChange,
}: {
  draft: string;
  onChange: (next: string) => void;
}) {
  const transcripts = useHandsFreeStore((s) => s.capture.transcripts);
  const dismiss = useHandsFreeStore((s) => s.dismissTranscript);
  const turn = useHandsFreeStore((s) => s.turn);
  const cancel = useHandsFreeStore((s) => s.cancel);
  const current = transcripts[0];
  const direct = current !== undefined && placeTranscript(draft) === 'direct';

  useEffect(() => {
    if (!current || !direct) return;
    onChange(applyTranscript(draft, current.text, 'replace'));
    dismiss(current.id);
  }, [current, direct, draft, onChange, dismiss]);

  if (turn.phase === 'pending' && turn.deadlineAt !== null) {
    return <SendCountdown text={turn.text} durationMs={SUBMIT_COUNTDOWN_MS} onCancel={cancel} />;
  }
  if (!current || direct) return null;

  const choose = (choice: TranscriptChoice) => {
    onChange(applyTranscript(draft, current.text, choice));
    dismiss(current.id);
  };

  return (
    <div
      role="group"
      aria-label="Hands-free transcript"
      className="mb-2 rounded-xl px-3 py-2 text-xs"
      style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-accent)' }}
    >
      <div className="flex items-center gap-1.5 mb-1" style={{ color: 'var(--color-text-secondary)' }}>
        <Mic size={12} aria-hidden="true" />
        Hands-free transcript — your current text was left untouched
        {transcripts.length > 1 ? ` (+${transcripts.length - 1} more)` : ''}
      </div>
      <p className="mb-2 text-sm" style={{ color: 'var(--color-text)' }}>{current.text}</p>
      <div className="flex gap-2">
        <button type="button" onClick={() => choose('insert')} className="rounded px-2 py-1 cursor-pointer"
          style={{ background: 'var(--color-accent)', color: 'white' }}>
          Add to my text
        </button>
        <button type="button" onClick={() => choose('replace')} className="rounded px-2 py-1 cursor-pointer"
          style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text)' }}>
          Replace my text
        </button>
        <button type="button" onClick={() => dismiss(current.id)} className="rounded px-2 py-1 cursor-pointer"
          style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text)' }}>
          Discard
        </button>
      </div>
    </div>
  );
}
