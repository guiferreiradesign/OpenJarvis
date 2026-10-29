import { describe, expect, it } from 'vitest';
import {
  MAX_RECORDING_MS,
  NO_SPEECH_MESSAGE,
  describeMicError,
  describeTranscriptionError,
  pickRecorderMimeType,
  recordingFilename,
  speechStatusText,
} from './speech-input';

const named = (name: string) => Object.assign(new Error('x'), { name });

describe('recorder format', () => {
  it('prefers WebM/Opus, then falls back to what the browser supports', () => {
    expect(pickRecorderMimeType(() => true)).toBe('audio/webm;codecs=opus');
    expect(pickRecorderMimeType((t) => t === 'audio/mp4')).toBe('audio/mp4');
    expect(pickRecorderMimeType(() => false)).toBe('');
  });

  it('names the upload by container', () => {
    expect(recordingFilename('audio/webm;codecs=opus')).toBe('recording.webm');
    expect(recordingFilename('audio/ogg;codecs=opus')).toBe('recording.ogg');
    expect(recordingFilename('audio/mp4')).toBe('recording.mp4');
    expect(recordingFilename('')).toBe('recording.webm');
  });

  it('stops before the 120 s server limit', () => {
    expect(MAX_RECORDING_MS).toBeLessThan(120_000);
  });
});

describe('microphone errors', () => {
  it.each([
    ['NotAllowedError', 'blocked'],
    ['SecurityError', 'blocked'],
    ['NotFoundError', 'No microphone'],
    ['NotReadableError', 'busy'],
    ['Whatever', 'Could not start'],
  ])('%s -> readable message', (name, fragment) => {
    expect(describeMicError(named(name))).toContain(fragment);
  });

  it('survives non-errors', () => {
    expect(describeMicError(undefined)).toContain('Could not start');
  });
});

describe('transcription errors', () => {
  it('maps silent and empty audio to one friendly message', () => {
    expect(describeTranscriptionError('The recording is empty')).toBe(NO_SPEECH_MESSAGE);
    expect(describeTranscriptionError('The recording contains no audio')).toBe(NO_SPEECH_MESSAGE);
  });

  it('explains unsupported formats and an unavailable local recogniser', () => {
    expect(describeTranscriptionError('Unsupported audio format; send a WAV')).toContain(
      'not supported',
    );
    const text = describeTranscriptionError(
      'No local speech-to-text backend is available. Cloud speech is never used',
    );
    expect(text).toContain('not available');
    expect(text).toContain('Nothing was sent');
  });

  it('keeps other server messages and handles an empty one', () => {
    expect(describeTranscriptionError('Converting the recording took longer than 30 s')).toContain(
      'too long',
    );
    expect(describeTranscriptionError('Speech transcription failed: boom')).toBe(
      'Speech transcription failed: boom',
    );
    expect(describeTranscriptionError('')).toBe('Transcription failed.');
  });
});

describe('status line', () => {
  it('shows recording, processing, error and success in that priority', () => {
    expect(speechStatusText('recording', 'e', 'n')?.kind).toBe('recording');
    expect(speechStatusText('transcribing', 'e', 'n')?.kind).toBe('processing');
    expect(speechStatusText('idle', 'bad', 'ok')).toEqual({ kind: 'error', text: 'bad' });
    expect(speechStatusText('idle', null, 'ok')).toEqual({ kind: 'success', text: 'ok' });
    expect(speechStatusText('idle', null, null)).toBeNull();
  });

  it('tells the user how to stop and cancel', () => {
    const text = speechStatusText('recording', null, null)?.text ?? '';
    expect(text).toContain('Esc');
  });
});
