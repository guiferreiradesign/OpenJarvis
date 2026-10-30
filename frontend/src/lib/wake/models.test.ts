import { describe, expect, it } from 'vitest';
import {
  DEFAULT_WAKE_MODEL, SHARED_MODEL_FILES, WAKE_MODELS, resolveWakeModel,
  wakeModelUrl, wakeWorkletUrl,
} from './models';

describe('wake model selection', () => {
  it('defaults to the single-word Jarvis model', () => {
    expect(DEFAULT_WAKE_MODEL).toBe('jarvis');
    expect(WAKE_MODELS[DEFAULT_WAKE_MODEL].phrase).toBe('Jarvis');
    expect(WAKE_MODELS.jarvis.file).toBe('Jarvis_20260313_215039.onnx');
  });

  it('offers Hey Jarvis as the alternative, using the upstream v0.5.1 file', () => {
    expect(WAKE_MODELS.hey_jarvis.phrase).toBe('Hey Jarvis');
    expect(WAKE_MODELS.hey_jarvis.file).toBe('hey_jarvis_v0.1.onnx');
    expect(Object.keys(WAKE_MODELS).sort()).toEqual(['hey_jarvis', 'jarvis']);
  });

  it('accepts both ids and falls back to the default for anything else', () => {
    expect(resolveWakeModel('jarvis')).toBe('jarvis');
    expect(resolveWakeModel('hey_jarvis')).toBe('hey_jarvis');
    expect(resolveWakeModel('alexa')).toBe('jarvis');
    expect(resolveWakeModel(undefined)).toBe('jarvis');
  });

  it('shares the same mel and embedding models across both phrases', () => {
    expect(SHARED_MODEL_FILES).toEqual({
      melspectrogram: 'melspectrogram.onnx',
      embedding: 'embedding_model.onnx',
    });
  });

  it('serves everything from JARVIS\'s own origin (relative URLs)', () => {
    expect(wakeModelUrl('x.onnx')).toBe('/wake-models/x.onnx');
    expect(wakeWorkletUrl()).toBe('/wake-worklet.js');
    for (const url of [wakeModelUrl('a.onnx'), wakeWorkletUrl()]) expect(url).not.toMatch(/^https?:/);
  });
});
