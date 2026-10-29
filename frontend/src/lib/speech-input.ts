// Pure helpers for browser push-to-talk: recorder format choice, readable
// errors, and the status line. Kept free of React/DOM so they are unit-tested.

export type SpeechInputState = 'idle' | 'recording' | 'transcribing';

// The local server accepts WAV, WebM, Ogg and MP4 and converts them (FFmpeg).
const RECORDER_MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
] as const;

// The server refuses recordings over 120 s; stop a little earlier.
export const MAX_RECORDING_MS = 60_000;

export function pickRecorderMimeType(isSupported: (type: string) => boolean): string {
  return RECORDER_MIME_TYPES.find((type) => isSupported(type)) ?? '';
}

export function recordingFilename(mimeType: string): string {
  if (mimeType.includes('ogg')) return 'recording.ogg';
  if (mimeType.includes('mp4')) return 'recording.mp4';
  return 'recording.webm';
}

export function describeMicError(err: unknown): string {
  const name = err instanceof DOMException || err instanceof Error ? err.name : '';
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'Microphone access is blocked. Allow it for this site in your browser, then click the microphone again.';
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'No microphone was found. Connect one and try again.';
    case 'NotReadableError':
    case 'AbortError':
      return 'The microphone is busy or unavailable. Close other apps using it and try again.';
    default:
      return 'Could not start recording from the microphone.';
  }
}

export const NO_SPEECH_MESSAGE = 'No speech was heard. Try again, closer to the microphone.';

export function describeTranscriptionError(message: string): string {
  const text = (message || '').trim();
  const lower = text.toLowerCase();
  if (!text) return 'Transcription failed.';
  if (lower.includes('no audio') || lower.includes('empty') || lower.includes('no speech')) {
    return NO_SPEECH_MESSAGE;
  }
  if (lower.includes('unsupported audio format')) {
    return "This browser's recording format is not supported by the local speech service.";
  }
  if (lower.includes('larger than') || lower.includes('longer than')) {
    return `The recording is too long. ${text}`;
  }
  if (lower.includes('no local speech-to-text')) {
    return 'Local speech recognition is not available. Nothing was sent anywhere.';
  }
  return text;
}

export const TRANSCRIPT_ADDED_MESSAGE = 'Transcript added. Review it, then press Send.';

export function speechStatusText(
  state: SpeechInputState,
  error: string | null,
  notice: string | null,
): { kind: 'recording' | 'processing' | 'error' | 'success'; text: string } | null {
  if (state === 'recording') {
    return { kind: 'recording', text: 'Recording… click the microphone to stop, Esc to cancel.' };
  }
  if (state === 'transcribing') {
    return { kind: 'processing', text: 'Transcribing locally…' };
  }
  if (error) return { kind: 'error', text: error };
  if (notice) return { kind: 'success', text: notice };
  return null;
}
