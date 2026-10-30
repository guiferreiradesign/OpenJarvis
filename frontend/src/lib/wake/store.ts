import { create } from 'zustand';
import { WakeController, type WakeDeps, type WakeSnapshot } from './controller';
import { resolveWakeModel, wakeWorkletUrl, type WakeModelId } from './models';

function realDeps(): WakeDeps {
  return {
    getUserMedia: (constraints) => navigator.mediaDevices.getUserMedia(constraints),
    createAudioContext: () => {
      const Ctor = window.AudioContext
        ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      return new Ctor({ sampleRate: 16000 }) as unknown as ReturnType<WakeDeps['createAudioContext']>;
    },
    createWorkletNode: (ctx) =>
      new AudioWorkletNode(ctx as unknown as AudioContext, 'wake-capture', {
        numberOfInputs: 1,
        numberOfOutputs: 0,
        channelCount: 1,
        processorOptions: { targetRate: 16000 },
      }) as unknown as ReturnType<WakeDeps['createWorkletNode']>,
    // Imported lazily so the runtime and models load only once wake detection is on.
    loadModels: async (modelId) => (await import('./ort-session')).loadWakeModels(modelId),
    workletUrl: wakeWorkletUrl(),
    now: () => Date.now(),
    setTimeout: (fn, ms) => window.setTimeout(fn, ms),
    clearTimeout: (handle) => window.clearTimeout(handle as number),
  };
}

let controller: WakeController | null = null;

/** Created on first use, so merely loading the app never touches audio. */
export function getWakeController(): WakeController {
  if (!controller) {
    controller = new WakeController(realDeps());
    if (typeof window !== 'undefined') {
      // Release the microphone if the page is closed or navigated away.
      window.addEventListener('pagehide', () => void controller?.stop());
    }
  }
  return controller;
}

interface WakeStore extends WakeSnapshot {
  start: (modelId: unknown) => Promise<void>;
  stop: () => Promise<void>;
}

let subscribed = false;

const OFF: WakeSnapshot = { state: 'off', modelId: null, error: null, detection: null, droppedChunks: 0 };

export const useWakeStore = create<WakeStore>((set) => ({
  ...OFF,
  start: async (modelId) => {
    const c = getWakeController();
    if (!subscribed) {
      subscribed = true;
      c.subscribe((snap) => set(snap));
    }
    await c.start(resolveWakeModel(modelId) as WakeModelId);
  },
  stop: async () => {
    if (controller) await controller.stop();
    set({ ...OFF });
  },
}));
