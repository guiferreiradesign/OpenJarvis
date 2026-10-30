/**
 * Wake-word model catalogue. The ONNX files are third-party and are never
 * tracked in Git: they live in the gitignored `frontend/public/wake-models/`
 * directory, which Vite copies next to the built app so JARVIS serves them
 * from its own origin. See `frontend/public/wake-models/README.md`.
 */
export type WakeModelId = 'jarvis' | 'hey_jarvis';

export interface WakeModelInfo {
  id: WakeModelId;
  /** Phrase shown to the user. */
  phrase: string;
  /** Classifier file inside the wake-models directory. */
  file: string;
}

export const DEFAULT_WAKE_MODEL: WakeModelId = 'jarvis';

export const WAKE_MODELS: Record<WakeModelId, WakeModelInfo> = {
  // Third-party single-word model; personal, non-commercial use only.
  jarvis: { id: 'jarvis', phrase: 'Jarvis', file: 'Jarvis_20260313_215039.onnx' },
  // Upstream openWakeWord v0.5.1 model.
  hey_jarvis: { id: 'hey_jarvis', phrase: 'Hey Jarvis', file: 'hey_jarvis_v0.1.onnx' },
};

/** Feature models shared by every classifier (openWakeWord v0.5.1). */
export const SHARED_MODEL_FILES = {
  melspectrogram: 'melspectrogram.onnx',
  embedding: 'embedding_model.onnx',
} as const;

export const WAKE_MODEL_DIR = 'wake-models';
export const ORT_RUNTIME_DIR = 'ort';
export const WAKE_WORKLET_FILE = 'wake-worklet.js';

export function isWakeModelId(value: unknown): value is WakeModelId {
  return value === 'jarvis' || value === 'hey_jarvis';
}

/** Unknown or missing values fall back to the default ("Jarvis"). */
export function resolveWakeModel(value: unknown): WakeModelId {
  return isWakeModelId(value) ? value : DEFAULT_WAKE_MODEL;
}

function baseUrl(): string {
  const base = (import.meta as { env?: { BASE_URL?: string } }).env?.BASE_URL ?? '/';
  return base.endsWith('/') ? base : `${base}/`;
}

export function wakeModelUrl(file: string): string {
  return `${baseUrl()}${WAKE_MODEL_DIR}/${file}`;
}

export function ortRuntimeUrl(): string {
  return `${baseUrl()}${ORT_RUNTIME_DIR}/`;
}

export function wakeWorkletUrl(): string {
  return `${baseUrl()}${WAKE_WORKLET_FILE}`;
}
