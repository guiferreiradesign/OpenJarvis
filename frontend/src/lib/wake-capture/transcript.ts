/**
 * Leading wake-phrase removal and usability checks for transcripts.
 *
 * The phrase is stripped only when it is the very first thing said ("Jarvis,
 * what's the time", "Hey Jarvis. Open ..."), is a whole word, and is followed by
 * punctuation, whitespace or the end. Possessives ("Jarvis's"), longer words
 * ("Jarvisual") and the name appearing later in the request are left alone.
 */
const LEADING_WAKE =
  /^\s*(?:(?:hey|hi|ok|okay)[\s,.!]+)?jarvis(?=[\s,.!?;:—–]|$)[\s,.!?;:—–]*/i;

export function stripWakePhrase(text: string): string {
  const stripped = text.replace(LEADING_WAKE, '');
  return stripped.trim();
}

/** Whisper emits things like "[BLANK_AUDIO]" or "(silence)" for silence. */
const BRACKETED_ONLY = /^(?:[\[(*♪].*[\])*♪]\s*)+$/;

/** True when the text can plausibly be a spoken request. */
export function isUsableTranscript(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed) return false;
  if (!/[\p{L}\p{N}]/u.test(trimmed)) return false;
  if (BRACKETED_ONLY.test(trimmed)) return false;
  return true;
}

/** Wake-phrase removal followed by validation; null when unusable. */
export function cleanTranscript(raw: string): string | null {
  const trimmed = (raw || '').trim();
  if (!isUsableTranscript(trimmed)) return null;
  const stripped = stripWakePhrase(trimmed);
  return isUsableTranscript(stripped) ? stripped : null;
}

export type TranscriptChoice = 'insert' | 'replace';

/** How a finished transcript meets the chat input. */
export function placeTranscript(draft: string): 'direct' | 'review' {
  return draft.trim() === '' ? 'direct' : 'review';
}

/** Combine a transcript with the existing draft; nothing is ever dropped silently. */
export function applyTranscript(draft: string, transcript: string, choice: TranscriptChoice): string {
  if (draft.trim() === '' || choice === 'replace') return transcript;
  return `${draft.replace(/\s+$/, '')} ${transcript}`;
}
