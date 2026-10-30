import { describe, expect, it } from 'vitest';
import workletRaw from '../../../public/wake-worklet.js?raw';
import viteConfigRaw from '../../../vite.config.ts?raw';
import gitignoreRaw from '../../../../.gitignore?raw';
import packageRaw from '../../../package.json?raw';

const libFiles = import.meta.glob('./*.ts', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;
const componentFiles = import.meta.glob('../../components/Wake/*.tsx', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;

const wakeSources = [
  ...Object.entries(libFiles),
  ...Object.entries(componentFiles),
  ['public/wake-worklet.js', workletRaw],
]
  .filter(([file]) => !/\.test\./.test(file))
  .map(([file, text]) => ({ file, text }));

const stripComments = (t: string) => t.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

describe('wake detection is detector-only', () => {
  it('imports nothing from chat, API, speech, TTS, streaming or tool code', () => {
    const banned = /from ['"](?:\.\.\/)+(?:lib\/)?(?:store|api|sse|tts|speech-input|message-text)['"]|hooks\/use(?:Speech|Tts)|InputArea|ChatArea/;
    for (const { file, text } of wakeSources) {
      expect(banned.test(stripComments(text)), file).toBe(false);
    }
  });

  it('never references transcription, synthesis, chat submission, tools or recording', () => {
    const banned = /transcribe|synthesi[sz]e|sendMessage|\/v1\/|openrouter|MediaRecorder|localStorage|sessionStorage|indexedDB|XMLHttpRequest|WebSocket|sendBeacon|EventSource/i;
    for (const { file, text } of wakeSources) {
      expect(banned.test(stripComments(text)), file).toBe(false);
    }
  });

  it('only the model loader fetches, and only same-origin wake-model files', () => {
    const fetchers = wakeSources.filter(({ text }) => /\bfetch\(/.test(stripComments(text)));
    expect(fetchers.map((f) => f.file)).toEqual(['./ort-session.ts']);
    for (const { file, text } of wakeSources) {
      expect(/https?:\/\//.test(stripComments(text)), file).toBe(false);
    }
  });
});

describe('model files stay out of Git and the PWA precache', () => {
  it('gitignores everything in public/wake-models except its README', () => {
    expect(gitignoreRaw).toContain('frontend/public/wake-models/*');
    expect(gitignoreRaw).toContain('!frontend/public/wake-models/README.md');
  });

  it('excludes models, the runtime and wasm from the service-worker precache', () => {
    const ignores = viteConfigRaw.match(/globIgnores:\s*\[([^\]]*)\]/)?.[1] ?? '';
    for (const pattern of ['wake-models/**', 'ort/**', '**/*.onnx', '**/*.wasm']) {
      expect(ignores).toContain(pattern);
    }
  });

  it('copies the runtime from node_modules at build time instead of tracking it', () => {
    expect(viteConfigRaw).toContain("node_modules/onnxruntime-web/dist");
  });

  it('pins onnxruntime-web to an exact version', () => {
    const pkg = JSON.parse(packageRaw);
    expect(pkg.dependencies['onnxruntime-web']).toMatch(/^\d+\.\d+\.\d+$/);
  });
});
