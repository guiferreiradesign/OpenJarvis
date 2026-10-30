import type { WakeSnapshot } from '../wake/controller';
import { getChatBridge, onDraftChanged, type ChatBridge, type VoiceTurnResult } from './bridge';

export type TurnPhase = 'idle' | 'pending' | 'thinking' | 'speaking';

export interface TurnSnapshot {
  phase: TurnPhase;
  /** The transcript waiting to be sent (pending) or being answered. */
  text: string;
  /** When the cancellation window closes (pending only). */
  deadlineAt: number | null;
  /** Last thing that ended a turn without a spoken reply, for the status line. */
  notice: string | null;
}

export const SUBMIT_COUNTDOWN_MS = 1500;

export interface ConductorDeps {
  /** What the capture session exposes to a conductor. */
  session: {
    release(): void;
    addTranscript(text: string): void;
  };
  getBridge?(): ChatBridge | null;
  /** False when the server cannot be confirmed to restrict voice turns. */
  autoSubmitAllowed?(): boolean;
  /** Speak a finished reply through the shared playback queue; resolves when it ends. */
  speak(messageId: string, text: string): Promise<void>;
  stopSpeaking(): void;
  host: { subscribe(fn: (snapshot: WakeSnapshot) => void): () => void };
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
  countdownMs?: number;
}

const IDLE: TurnSnapshot = { phase: 'idle', text: '', deadlineAt: null, notice: null };

/**
 * One hands-free turn after a transcript: a visible cancellation window, a
 * single submission through the chat bridge, then the spoken reply, then the
 * detector is released. Exactly one turn can be active; a turn counter fences
 * every asynchronous step so a cancelled or superseded turn can never submit,
 * speak or release again.
 */
export class HandsFreeConductor {
  private snapshot: TurnSnapshot = IDLE;
  private listeners = new Set<(s: TurnSnapshot) => void>();
  private turn = 0;
  private timer: unknown = null;
  private stopDraftWatch: (() => void) | null = null;
  private readonly unsubscribeHost: () => void;

  constructor(private readonly deps: ConductorDeps) {
    // Losing the detector (Off, microphone ended, worklet failure) ends the turn.
    this.unsubscribeHost = deps.host.subscribe((snap) => {
      if (snap.state === 'off' || snap.state === 'error') this.reset();
    });
  }

  getSnapshot = (): TurnSnapshot => this.snapshot;

  subscribe = (fn: (s: TurnSnapshot) => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  private set(patch: Partial<TurnSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    this.listeners.forEach((fn) => fn(this.snapshot));
  }

  private bridge(): ChatBridge | null {
    return (this.deps.getBridge ?? getChatBridge)();
  }

  /**
   * Offered by the capture session. Returns true when this turn takes the
   * transcript. It declines — sending the text to review instead — whenever
   * submitting could clobber the user's work: a draft exists, a reply is
   * already streaming, no model is selected, or the chat is not open.
   */
  handleTranscript = (text: string): boolean => {
    if (this.snapshot.phase !== 'idle') return false;
    const bridge = this.bridge();
    if (this.deps.autoSubmitAllowed && !this.deps.autoSubmitAllowed()) return false;
    if (!bridge || !bridge.canSubmit() || bridge.getDraft().trim() !== '') return false;

    const turn = ++this.turn;
    const ms = this.deps.countdownMs ?? SUBMIT_COUNTDOWN_MS;
    this.set({ phase: 'pending', text, deadlineAt: this.deps.now() + ms, notice: null });
    this.timer = this.deps.setTimeout(() => void this.fire(turn), ms);
    // Typing during the window means the user is working: never auto-send over it.
    this.stopDraftWatch = onDraftChanged((draft) => {
      if (turn === this.turn && this.snapshot.phase === 'pending' && draft.trim() !== '') this.divert(turn);
    });
    return true;
  };

  private clearPending(): void {
    if (this.timer !== null) {
      this.deps.clearTimeout(this.timer);
      this.timer = null;
    }
    this.stopDraftWatch?.();
    this.stopDraftWatch = null;
  }

  private finish(notice: string | null): void {
    this.clearPending();
    this.set({ phase: 'idle', text: '', deadlineAt: null, notice });
    this.deps.session.release();
  }

  /** New text appeared during the window: hand the transcript to review instead. */
  private divert(turn: number): void {
    if (turn !== this.turn || this.snapshot.phase !== 'pending') return;
    const text = this.snapshot.text;
    this.turn += 1;
    this.deps.session.addTranscript(text);
    this.finish('You started typing, so the voice transcript was not sent. Review it below.');
  }

  private async fire(turn: number): Promise<void> {
    if (turn !== this.turn || this.snapshot.phase !== 'pending') return;
    const text = this.snapshot.text;
    this.clearPending();
    const bridge = this.bridge();
    // Re-check at the moment of sending: the draft or chat state may have changed.
    if (
      (this.deps.autoSubmitAllowed && !this.deps.autoSubmitAllowed())
      || !bridge || !bridge.canSubmit() || bridge.getDraft().trim() !== ''
    ) {
      this.turn += 1;
      this.deps.session.addTranscript(text);
      this.finish('Could not send the voice request, so it was kept for review.');
      return;
    }
    this.set({ phase: 'thinking', deadlineAt: null });

    let result: VoiceTurnResult;
    try {
      result = await bridge.submit(text);
    } catch {
      result = { status: 'failed' };
    }
    if (turn !== this.turn) return; // cancelled or reset while thinking

    if (result.status === 'rejected') {
      this.turn += 1;
      this.deps.session.addTranscript(text);
      this.finish('The chat could not take the voice request, so it was kept for review.');
      return;
    }
    if (result.status !== 'completed' || !result.messageId || !result.text) {
      this.turn += 1;
      this.finish(result.status === 'aborted' ? null : 'The reply did not complete, so nothing was spoken.');
      return;
    }
    if (!bridge.speechOutputEnabled()) {
      this.turn += 1;
      this.finish(null);
      return;
    }

    this.set({ phase: 'speaking' });
    try {
      await this.deps.speak(result.messageId, result.text);
    } catch {
      /* playback problems are shown by the voice-output UI; we still re-arm */
    }
    if (turn !== this.turn) return;
    this.turn += 1;
    this.finish(null);
  }

  /** User cancel (button or Esc). Returns false when no turn is active. */
  cancel(): boolean {
    const { phase } = this.snapshot;
    if (phase === 'idle') return false;
    this.turn += 1; // fences every pending async step
    if (phase === 'thinking') this.bridge()?.abort();
    if (phase === 'speaking') this.deps.stopSpeaking();
    this.finish(phase === 'pending' ? 'Cancelled. Nothing was sent.' : null);
    return true;
  }

  /** Hands-free turned off or the detector died: end everything, do not re-arm. */
  reset(): void {
    if (this.snapshot.phase === 'idle') return;
    const { phase } = this.snapshot;
    this.turn += 1;
    if (phase === 'thinking') this.bridge()?.abort();
    if (phase === 'speaking') this.deps.stopSpeaking();
    this.clearPending();
    this.set({ ...IDLE });
  }

  dismissNotice(): void {
    if (this.snapshot.notice !== null) this.set({ notice: null });
  }

  dispose(): void {
    this.reset();
    this.unsubscribeHost();
  }
}
