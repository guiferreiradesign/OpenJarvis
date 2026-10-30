import type { TensorRunner, WakePipelineModels } from './pipeline';
import {
  SHARED_MODEL_FILES,
  WAKE_MODELS,
  type WakeModelId,
  ortRuntimeUrl,
  wakeModelUrl,
} from './models';

type Ort = typeof import('onnxruntime-web/wasm');

async function fetchModel(file: string): Promise<Uint8Array> {
  const url = wakeModelUrl(file);
  let res: Response;
  try {
    res = await fetch(url);
  } catch {
    throw new Error(`Could not load wake-word model ${file} from JARVIS`);
  }
  if (!res.ok) {
    throw new Error(
      `Wake-word model ${file} is missing (HTTP ${res.status}). `
      + 'Put it in frontend/public/wake-models/ and rebuild; see the README there.',
    );
  }
  return new Uint8Array(await res.arrayBuffer());
}

async function toRunner(ort: Ort, bytes: Uint8Array): Promise<{ runner: TensorRunner; frames: number | null }> {
  const session = await ort.InferenceSession.create(bytes, {
    executionProviders: ['wasm'],
    graphOptimizationLevel: 'all',
  });
  const meta = session.inputMetadata[0] as { shape?: ReadonlyArray<number | string> } | undefined;
  const second = meta?.shape?.[1];
  return {
    frames: typeof second === 'number' ? second : null,
    runner: {
      async run(data, dims) {
        const out = await session.run({
          [session.inputNames[0]]: new ort.Tensor('float32', data, dims),
        });
        return Float32Array.from(out[session.outputNames[0]].data as Float32Array);
      },
      release: () => session.release(),
    },
  };
}

/**
 * Lazily loads ONNX Runtime Web (WebAssembly, single thread) and the three
 * models from JARVIS's own origin. Nothing is fetched from a third party.
 */
export async function loadWakeModels(modelId: WakeModelId): Promise<WakePipelineModels> {
  const ort = await import('onnxruntime-web/wasm');
  ort.env.wasm.wasmPaths = ortRuntimeUrl();
  ort.env.wasm.numThreads = 1;
  ort.env.wasm.proxy = false;

  const loaded: TensorRunner[] = [];
  try {
    const [melBytes, embBytes, clfBytes] = await Promise.all([
      fetchModel(SHARED_MODEL_FILES.melspectrogram),
      fetchModel(SHARED_MODEL_FILES.embedding),
      fetchModel(WAKE_MODELS[modelId].file),
    ]);
    const mel = await toRunner(ort, melBytes);
    loaded.push(mel.runner);
    const embedding = await toRunner(ort, embBytes);
    loaded.push(embedding.runner);
    const classifier = await toRunner(ort, clfBytes);
    loaded.push(classifier.runner);
    if (classifier.frames === null) {
      throw new Error(`Wake-word model ${WAKE_MODELS[modelId].file} has an unexpected input shape`);
    }
    return {
      mel: mel.runner,
      embedding: embedding.runner,
      classifier: classifier.runner,
      classifierFrames: classifier.frames,
    };
  } catch (err) {
    await Promise.allSettled(loaded.map(async (r) => r.release?.()));
    throw err;
  }
}
