import { create } from 'zustand';
import { fetchVoiceInputPolicy, transcribeAudio } from '../api';
import { stripThinkTags } from '../message-text';
import { isStreamNotice, useTtsStore } from '../tts';
import { getWakeController, useWakeStore } from '../wake/store';
import { markVoiceReply } from './bridge';
import { HandsFreeConductor, type TurnSnapshot } from './conductor';
import { handsFreePhase, type HandsFreePhase } from './phase';
import { WakeCaptureSession, type CaptureSnapshot } from './session';

/**
 * Speak a finished voice-turn reply through the shared playback queue and
 * resolve once it has stopped. Nothing here synthesises on its own: it is the
 * same store the per-message Speak button uses, so replies never overlap.
 */
export async function speakAndWait(messageId: string, rawText: string): Promise<void> {
  const text = stripThinkTags(rawText).trim();
  if (!text || isStreamNotice(text)) return;
  const tts = useTtsStore.getState();
  await tts.ensureHealth();
  if (useTtsStore.getState().available !== true) return;
  markVoiceReply(messageId);
  tts.markAutoSpoken(messageId);
  await useTtsStore.getState().speak(messageId, text);
  // speak() resolves once playback has started (or failed). Wait for it to end,
  // or for someone else to take over the player.
  await new Promise<void>((resolve) => {
    const done = () => {
      const s = useTtsStore.getState();
      return s.state === 'idle' || s.speakingId !== messageId;
    };
    if (done()) return resolve();
    const unsubscribe = useTtsStore.subscribe(() => {
      if (done()) {
        unsubscribe();
        resolve();
      }
    });
  });
}

// Auto-submit is allowed only after the server confirms it restricts voice turns.
let voicePolicyConfirmed = false;
let session: WakeCaptureSession | null = null;
let conductor: HandsFreeConductor | null = null;

/** Created on first use, so loading the app never touches audio. */
export function getHandsFree(): { session: WakeCaptureSession; conductor: HandsFreeConductor } {
  if (!session || !conductor) {
    const host = getWakeController();
    const timers = {
      setTimeout: (fn: () => void, ms: number) => window.setTimeout(fn, ms),
      clearTimeout: (handle: unknown) => window.clearTimeout(handle as number),
    };
    let created: WakeCaptureSession | null = null;
    const c = new HandsFreeConductor({
      session: {
        release: () => created?.release(),
        addTranscript: (text) => created?.addTranscript(text),
      },
      autoSubmitAllowed: () => voicePolicyConfirmed,
      speak: speakAndWait,
      stopSpeaking: () => useTtsStore.getState().stop(),
      host,
      now: () => Date.now(),
      ...timers,
    });
    created = new WakeCaptureSession({
      host,
      transcribe: (audio, filename, signal) => transcribeAudio(audio, filename, signal),
      onTranscript: c.handleTranscript,
      ...timers,
    });
    session = created;
    conductor = c;
  }
  return { session, conductor };
}

export function getCaptureSession(): WakeCaptureSession {
  return getHandsFree().session;
}

const IDLE: CaptureSnapshot = { phase: 'waiting', outcome: 'none', error: null, transcripts: [] };
const IDLE_TURN: TurnSnapshot = { phase: 'idle', text: '', deadlineAt: null, notice: null };

interface HandsFreeStore {
  capture: CaptureSnapshot;
  turn: TurnSnapshot;
  phase: HandsFreePhase;
  /** False when the server did not confirm the voice tool policy (review-only mode). */
  autoSubmit: boolean;
  start: (modelId: unknown) => Promise<void>;
  stop: () => Promise<void>;
  /** Cancel whatever is active: capture, transcription, countdown, reply or speech. */
  cancel: () => void;
  dismissTranscript: (id: number) => void;
  dismissOutcome: () => void;
}

let wired = false;

export const useHandsFreeStore = create<HandsFreeStore>((set) => {
  const recompute = () => {
    const capture = session?.getSnapshot() ?? IDLE;
    const turn = conductor?.getSnapshot() ?? IDLE_TURN;
    set({ capture, turn, phase: handsFreePhase(useWakeStore.getState().state, capture, turn.phase) });
  };
  const wire = () => {
    const hf = getHandsFree();
    if (wired) return hf;
    wired = true;
    hf.session.subscribe(recompute);
    hf.conductor.subscribe(recompute);
    useWakeStore.subscribe(recompute);
    return hf;
  };
  return {
    capture: IDLE,
    turn: IDLE_TURN,
    phase: 'off',
    autoSubmit: false,
    start: async (modelId) => {
      wire();
      voicePolicyConfirmed = await fetchVoiceInputPolicy();
      set({ autoSubmit: voicePolicyConfirmed });
      await useWakeStore.getState().start(modelId);
      recompute();
    },
    stop: async () => {
      conductor?.reset();
      session?.reset();
      await useWakeStore.getState().stop();
      recompute();
    },
    cancel: () => {
      if (!conductor?.cancel()) session?.cancel();
    },
    dismissTranscript: (id) => session?.dismissTranscript(id),
    dismissOutcome: () => {
      session?.dismissOutcome();
      conductor?.dismissNotice();
    },
  };
});
