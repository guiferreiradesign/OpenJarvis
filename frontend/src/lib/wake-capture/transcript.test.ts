import { describe, expect, it } from 'vitest';
import { applyTranscript, cleanTranscript, placeTranscript, stripWakePhrase } from './transcript';

describe('stripWakePhrase', () => {
  it.each([
    ['Jarvis, what is the time?', 'what is the time?'],
    ['Jarvis. Open my calendar', 'Open my calendar'],
    ['Hey Jarvis, what is the time?', 'what is the time?'],
    ['hey, jarvis what is the time', 'what is the time'],
    ['  JARVIS what is the time', 'what is the time'],
  ])('removes a clear leading phrase: %s', (input, expected) => {
    expect(stripWakePhrase(input)).toBe(expected);
  });

  it.each([
    "What does Jarvis do?",
    "Tell Jarvis to wait",
    "Jarvis's schedule today",
    'Jarvisual studio setup',
    'Heyjarvis is odd',
    'Open the Jarvis folder',
  ])('leaves other words alone: %s', (input) => {
    expect(stripWakePhrase(input)).toBe(input);
  });

  it('removes only one leading occurrence', () => {
    expect(stripWakePhrase('Jarvis, Jarvis is a name')).toBe('Jarvis is a name');
  });
});

describe('cleanTranscript', () => {
  it('returns null for empty, punctuation-only and bracketed silence markers', () => {
    for (const t of ['', '   ', '...', '?!', '[BLANK_AUDIO]', '(silence)', '[ Music ]']) {
      expect(cleanTranscript(t)).toBeNull();
    }
  });

  it('returns null when only the wake phrase was heard', () => {
    expect(cleanTranscript('Jarvis.')).toBeNull();
    expect(cleanTranscript('Hey Jarvis')).toBeNull();
  });

  it('returns the request without the phrase', () => {
    expect(cleanTranscript(' Jarvis, what is the weather? ')).toBe('what is the weather?');
  });

  it('keeps a request that merely mentions brackets mid-text', () => {
    expect(cleanTranscript('Set a timer (ten minutes)')).toBe('Set a timer (ten minutes)');
  });
});

describe('draft protection helpers', () => {
  it('goes straight to the input only when the draft is empty', () => {
    expect(placeTranscript('')).toBe('direct');
    expect(placeTranscript('  \n')).toBe('direct');
    expect(placeTranscript('half-written email')).toBe('review');
  });

  it('inserts after the draft or replaces it, and never drops text otherwise', () => {
    expect(applyTranscript('Hello', 'world', 'insert')).toBe('Hello world');
    expect(applyTranscript('Hello  \n', 'world', 'insert')).toBe('Hello world');
    expect(applyTranscript('Hello', 'world', 'replace')).toBe('world');
    expect(applyTranscript('', 'world', 'insert')).toBe('world');
  });
});
