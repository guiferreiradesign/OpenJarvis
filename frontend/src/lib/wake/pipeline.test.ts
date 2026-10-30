import { describe, expect, it } from 'vitest';
import {
  CHUNK_SAMPLES, EMBEDDING_DIM, MEL_BINS, RING_SAMPLES, WARMUP_CHUNKS, WakePipeline,
  type TensorRunner, type WakePipelineModels,
} from './pipeline';

interface Call { data: Float32Array; dims: number[] }

function recorder(produce: (data: Float32Array, dims: number[], n: number) => Float32Array) {
  const calls: Call[] = [];
  const runner: TensorRunner = {
    async run(data, dims) {
      calls.push({ data: Float32Array.from(data), dims: [...dims] });
      return produce(data, dims, calls.length);
    },
  };
  return { runner, calls };
}

function makeModels(opts: { score?: (n: number) => number; classifierFrames?: number } = {}) {
  // Mirrors the real mel model: ceil(samples / 160 - 3) frames of 32 bins.
  const mel = recorder((_d, dims) => new Float32Array(Math.ceil(dims[1] / 160 - 3) * MEL_BINS).fill(20));
  const emb = recorder(() => new Float32Array(EMBEDDING_DIM).fill(0.5));
  const clf = recorder((_d, _dims, n) => Float32Array.of((opts.score ?? (() => 0.9))(n)));
  const models: WakePipelineModels = {
    mel: mel.runner, embedding: emb.runner, classifier: clf.runner,
    classifierFrames: opts.classifierFrames ?? 16,
  };
  return { models, mel, emb, clf };
}

const chunk = (v = 100) => new Float32Array(CHUNK_SAMPLES).fill(v);

describe('WakePipeline tensor handling', () => {
  it('feeds the mel model only the available audio at first, then 1280 + 480 samples', async () => {
    const { models, mel } = makeModels();
    const p = new WakePipeline(models);
    await p.process(chunk());
    await p.process(chunk());
    expect(mel.calls[0].dims).toEqual([1, 1280]);
    expect(mel.calls[1].dims).toEqual([1, 1760]);
    expect(mel.calls[1].data.length).toBe(1760);
  });

  it('passes 16-bit-scaled sample values through unchanged', async () => {
    const { models, mel } = makeModels();
    await new WakePipeline(models).process(chunk(12345));
    expect(mel.calls[0].data[0]).toBe(12345);
  });

  it('rescales mel output with x/10+2 and builds a 76x32 embedding window', async () => {
    const { models, emb } = makeModels();
    const p = new WakePipeline(models);
    await p.process(chunk());
    await p.process(chunk());
    const call = emb.calls[1];
    expect(call.dims).toEqual([1, 76, 32, 1]);
    expect(call.data.length).toBe(76 * 32);
    // Newest frames are 20/10+2 = 4; older ones are the initial ones-buffer.
    expect(call.data[call.data.length - 1]).toBe(4);
    expect(call.data[0]).toBe(1);
  });

  it('gives the classifier the last N 96-dim embeddings as [1, N, 96]', async () => {
    const { models, clf } = makeModels({ classifierFrames: 16 });
    await new WakePipeline(models).process(chunk());
    expect(clf.calls[0].dims).toEqual([1, 16, 96]);
    expect(clf.calls[0].data.length).toBe(16 * 96);
    expect(clf.calls[0].data[clf.calls[0].data.length - 1]).toBe(0.5); // newest embedding last
  });

  it('adapts to a classifier that wants a different frame count', async () => {
    const { models, clf } = makeModels({ classifierFrames: 8 });
    await new WakePipeline(models).process(chunk());
    expect(clf.calls[0].dims).toEqual([1, 8, 96]);
  });

  it('reports 0 during warm-up and the classifier score afterwards', async () => {
    const { models } = makeModels({ score: () => 0.9 });
    const p = new WakePipeline(models);
    const scores: number[] = [];
    for (let i = 0; i < WARMUP_CHUNKS + 2; i++) scores.push(await p.process(chunk()));
    expect(scores.slice(0, WARMUP_CHUNKS)).toEqual(new Array(WARMUP_CHUNKS).fill(0));
    scores.slice(WARMUP_CHUNKS).forEach((v) => expect(v).toBeCloseTo(0.9, 5));
  });

  it('rejects chunks that are not exactly 80 ms', async () => {
    const { models } = makeModels();
    await expect(new WakePipeline(models).process(new Float32Array(1000))).rejects.toThrow(/1280/);
  });

  it('keeps raw audio bounded to the two-second ring', async () => {
    const { models } = makeModels();
    const p = new WakePipeline(models);
    for (let i = 0; i < 120; i++) await p.process(chunk()); // ~9.6 s
    expect(p.bufferedSamples).toBe(RING_SAMPLES);
    expect(RING_SAMPLES).toBe(32000);
  });

  it('release() clears audio and releases every model', async () => {
    const released: string[] = [];
    const { models } = makeModels();
    for (const k of ['mel', 'embedding', 'classifier'] as const) {
      models[k].release = () => { released.push(k); };
    }
    const p = new WakePipeline(models);
    await p.process(chunk());
    await p.release();
    expect(p.bufferedSamples).toBe(0);
    expect(released.sort()).toEqual(['classifier', 'embedding', 'mel']);
  });
});
