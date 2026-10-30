import { useEffect, useRef } from 'react';
import { Ear, Mic, Loader2, Square, Volume2, X } from 'lucide-react';
import { toast } from 'sonner';
import { useWakeStore } from '../../lib/wake/store';
import { useHandsFreeStore } from '../../lib/wake-capture/store';
import { CANCELLABLE_PHASES, HANDS_FREE_LABEL } from '../../lib/wake-capture/phase';

/**
 * Shown only while hands-free mode is on, so continuous listening is never
 * hidden. A wake phrase starts one request: capture, local transcription, a
 * short cancellation window, then the normal chat turn and a spoken reply.
 */
export function WakeIndicator() {
  const phase = useHandsFreeStore((s) => s.phase);
  const capture = useHandsFreeStore((s) => s.capture);
  const notice = useHandsFreeStore((s) => s.turn.notice);
  const autoSubmit = useHandsFreeStore((s) => s.autoSubmit);
  const stop = useHandsFreeStore((s) => s.stop);
  const cancel = useHandsFreeStore((s) => s.cancel);
  const dismissOutcome = useHandsFreeStore((s) => s.dismissOutcome);
  const wakeError = useWakeStore((s) => s.error);
  const detection = useWakeStore((s) => s.detection);
  const lastAt = useRef<number | null>(null);

  useEffect(() => {
    if (!detection || detection.at === lastAt.current) return;
    lastAt.current = detection.at;
    toast(`Wake word detected: “${detection.phrase}”`, {
      description: 'Speak your request. Press Esc to cancel.',
      duration: 3000,
    });
  }, [detection]);

  // Esc cancels whatever is active: capture, transcription, the send countdown,
  // the reply being generated, or the reply being spoken.
  const capturing = CANCELLABLE_PHASES.has(phase);
  useEffect(() => {
    if (!capturing) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') cancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [capturing, cancel]);

  // Release the microphone if this component (the whole app shell) unmounts.
  useEffect(() => () => void useHandsFreeStore.getState().stop(), []);

  if (phase === 'off') return null;

  const isError = phase === 'error';
  const color = isError
    ? 'var(--color-error)'
    : phase === 'listening'
      ? 'var(--color-success)'
      : 'var(--color-accent)';
  const Icon = phase === 'transcribing' || phase === 'thinking'
    ? Loader2
    : phase === 'listening' ? Mic : phase === 'speaking' ? Volume2 : Ear;
  const detail = isError ? (wakeError ?? capture.error) : null;

  return (
    <div
      className="fixed top-2 right-14 z-40 flex items-center gap-2 rounded-full px-3 py-1 text-xs"
      style={{
        background: 'var(--color-bg-secondary)',
        border: `1px solid ${color}`,
        color: 'var(--color-text)',
      }}
      role="status"
      aria-live="polite"
    >
      <Icon
        size={14}
        style={{ color }}
        className={phase === 'transcribing' || phase === 'thinking' ? 'animate-spin' : undefined}
        aria-hidden="true"
      />
      <span>Hands-free: {HANDS_FREE_LABEL[phase]}</span>
      {phase === 'ready' && (
        <span style={{ color: 'var(--color-text-secondary)' }}>review it in the chat box</span>
      )}
      {detail && <span style={{ color: 'var(--color-text-secondary)' }}>{detail}</span>}
      {!autoSubmit && phase !== 'initialising' && (
        <span style={{ color: 'var(--color-error)' }}>
          Auto-send disabled: this JARVIS server does not report the voice safety policy. Restart it.
        </span>
      )}
      {notice && phase === 'waiting' && (
        <span style={{ color: 'var(--color-text-secondary)' }}>{notice}</span>
      )}
      {capturing && (
        <button
          onClick={cancel}
          className="flex items-center gap-1 rounded px-1.5 py-0.5 cursor-pointer"
          style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text)' }}
          aria-label="Cancel this request"
          title="Cancel this request (Esc)"
        >
          <X size={10} aria-hidden="true" />
          Cancel (Esc)
        </button>
      )}
      {(phase === 'no_speech' || (phase === 'error' && capture.outcome === 'error') || (notice && phase === 'waiting')) && (
        <button
          onClick={dismissOutcome}
          className="rounded px-1.5 py-0.5 cursor-pointer"
          style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text)' }}
          aria-label="Dismiss"
        >
          Dismiss
        </button>
      )}
      <button
        onClick={() => void stop()}
        className="flex items-center gap-1 rounded px-1.5 py-0.5 cursor-pointer"
        style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text)' }}
        aria-label="Turn hands-free mode off"
        title="Turn hands-free mode off and release the microphone"
      >
        <Square size={10} aria-hidden="true" />
        Off
      </button>
    </div>
  );
}
