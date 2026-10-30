import type { WakeState } from '../wake/controller';
import type { CaptureSnapshot } from './session';
import type { TurnPhase } from './conductor';

export type HandsFreePhase =
  | 'off' | 'initialising' | 'waiting' | 'listening'
  | 'transcribing' | 'pending' | 'thinking' | 'speaking'
  | 'ready' | 'no_speech' | 'error';

export const HANDS_FREE_LABEL: Record<HandsFreePhase, string> = {
  off: 'Off',
  initialising: 'Starting…',
  waiting: 'Waiting for wake word',
  listening: 'Listening',
  transcribing: 'Transcribing locally…',
  pending: 'Sending soon',
  thinking: 'Thinking…',
  speaking: 'Speaking',
  ready: 'Transcript ready',
  no_speech: 'No speech detected',
  error: 'Error',
};

/** Phases in which Cancel / Esc has something to cancel. */
export const CANCELLABLE_PHASES: ReadonlySet<HandsFreePhase> = new Set([
  'listening', 'transcribing', 'pending', 'thinking', 'speaking',
]);

/** One user-facing state from the detector, the capture session and the turn. */
export function handsFreePhase(
  wake: WakeState,
  capture: CaptureSnapshot,
  turn: TurnPhase = 'idle',
): HandsFreePhase {
  if (wake === 'off') return 'off';
  if (wake === 'error') return 'error';
  if (wake === 'initialising') return 'initialising';
  if (capture.phase === 'listening') return 'listening';
  if (capture.phase === 'transcribing') return 'transcribing';
  if (turn !== 'idle') return turn;
  if (capture.outcome === 'error') return 'error';
  if (capture.outcome === 'no_speech') return 'no_speech';
  if (capture.transcripts.length > 0) return 'ready';
  return 'waiting';
}
