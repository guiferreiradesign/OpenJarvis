import { useEffect } from 'react';
import { Volume2, Square, Loader2 } from 'lucide-react';
import { useTts } from '../../hooks/useTts';
import { useTtsStore } from '../../lib/tts';

interface Props {
  messageId: string;
  content: string;
}

/**
 * Speaks one assistant message through the server's TTS backend, only on click.
 * Automatic playback is a separate opt-in (Settings), not controlled here.
 */
export function SpeakMessageButton({ messageId, content }: Props) {
  const { speak, stop, speakingId, available, state, error, errorId } = useTts();

  // Playback is global; if this message goes away mid-utterance (conversation
  // switch, deletion) release the audio rather than leave it playing unowned.
  useEffect(
    () => () => {
      const tts = useTtsStore.getState();
      if (tts.speakingId === messageId) tts.stop();
    },
    [messageId],
  );

  if (!available) return null;

  // Playback is global, so this button reflects the shared utterance only when
  // that utterance is this message.
  const isMine = speakingId === messageId;
  const busy = isMine && (state === 'loading' || state === 'speaking');
  const messageError = errorId === messageId ? error : null;

  return (
    <>
      <button
        onClick={() => (busy ? stop() : speak(messageId, content))}
        className="p-1 rounded opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity cursor-pointer"
        style={{
          color: busy ? 'var(--color-accent)' : 'var(--color-text-tertiary)',
          opacity: busy ? 1 : undefined,
        }}
        title={
          messageError ||
          (isMine && state === 'loading'
            ? 'Synthesizing...'
            : isMine && state === 'speaking'
              ? 'Stop speaking'
              : 'Speak response')
        }
        aria-label={busy ? 'Stop speaking' : 'Speak response'}
        aria-busy={isMine && state === 'loading'}
      >
        {isMine && state === 'loading' ? (
          <Loader2 size={14} className="animate-spin" />
        ) : busy ? (
          <Square size={14} />
        ) : (
          <Volume2 size={14} />
        )}
      </button>
      {messageError && (
        <span role="alert" className="text-xs" style={{ color: 'var(--color-error)' }}>
          {messageError}
        </span>
      )}
    </>
  );
}
