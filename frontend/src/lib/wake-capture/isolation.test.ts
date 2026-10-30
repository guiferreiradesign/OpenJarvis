import { describe, expect, it } from 'vitest';

const files = import.meta.glob('./*.ts', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;
const sources = Object.entries(files)
  .filter(([file]) => !/\.test\./.test(file))
  .map(([file, text]) => ({ file, text }));
const stripComments = (t: string) => t.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const importedNames = (code: string, from: string) =>
  [...code.matchAll(new RegExp(`import \\{([^}]*)\\} from ['"]${from.replace(/[./]/g, '\\$&')}['"]`, 'g'))]
    .flatMap((m) => m[1].split(',').map((n) => n.trim()))
    .sort();

describe('hands-free reaches other systems only through narrow, named doors', () => {
  it('only store.ts touches the API, and only to transcribe and confirm the voice policy', () => {
    for (const { file, text } of sources) {
      const code = stripComments(text);
      expect(/from ['"]\.\.\/api['"]/.test(code), file).toBe(file === './store.ts');
    }
    const store = stripComments(files['./store.ts']);
    expect(importedNames(store, '../api')).toEqual(['fetchVoiceInputPolicy', 'transcribeAudio']);
  });

  it('only store.ts uses the shared speech queue, and only to speak, stop and mark replies', () => {
    for (const { file, text } of sources) {
      expect(/from ['"]\.\.\/tts['"]/.test(stripComments(text)), file).toBe(file === './store.ts');
    }
    expect(importedNames(stripComments(files['./store.ts']), '../tts')).toEqual(['isStreamNotice', 'useTtsStore']);
  });

  it('imports nothing from the app store, streaming, components, hooks or connectors', () => {
    const banned = /from ['"](?:\.\.\/)+(?:lib\/)?(?:store|sse|connectors-api|supabase)['"]|hooks\/use|components\//;
    for (const { file, text } of sources) expect(banned.test(stripComments(text)), file).toBe(false);
  });

  it('submits only through the chat bridge, from the conductor', () => {
    for (const { file, text } of sources) {
      const code = stripComments(text);
      const callsSubmit = /\.submit\(/.test(code);
      expect(callsSubmit, file).toBe(file === './conductor.ts');
      expect(/streamChat|sendMessage|addMessage|createConversation/.test(code), file).toBe(false);
    }
  });

  it('never references models, synthesis endpoints, tools, approvals or raw network and storage', () => {
    const banned = /fetch\(|\/v1\/|openrouter|synthesi[sz]e|approval|gmail|tool_call|executeTool|MediaRecorder|getUserMedia|localStorage|sessionStorage|indexedDB|XMLHttpRequest|WebSocket|sendBeacon|EventSource|https?:\/\//i;
    for (const { file, text } of sources) expect(banned.test(stripComments(text)), file).toBe(false);
  });

  it('never opens a microphone of its own', () => {
    for (const { file, text } of sources) expect(/getUserMedia|AudioContext/.test(stripComments(text)), file).toBe(false);
  });
});

describe('the chat input is the single pipeline for typed and voice turns', () => {
  const inputArea = import.meta.glob('../../components/Chat/InputArea.tsx', { query: '?raw', import: 'default', eager: true });
  const source = stripComments(Object.values(inputArea)[0] as string);

  it('has exactly one streaming chat call, inside runTurn', () => {
    expect(source.match(/streamChat\(/g)).toHaveLength(1);
    const runTurn = source.slice(source.indexOf('const runTurn'), source.indexOf('const sendMessage'));
    expect(runTurn).toContain('streamChat(');
  });

  it('typed sending and the voice bridge both call runTurn', () => {
    expect(source).toContain("runTurn(content, 'typed')");
    expect(source).toContain("runTurn(text, 'voice')");
  });

  it('adds the voice header only for voice turns and never uses deep research for them', () => {
    expect(source).toContain("origin === 'voice' ? { inputMode: 'voice' } : {}");
    expect(source).toContain("deepResearch && origin === 'typed'");
  });

  it('does not touch the input text for voice turns', () => {
    const runTurn = source.slice(source.indexOf('const runTurn'), source.indexOf('const sendMessage'));
    expect(runTurn).not.toContain('setInput');
  });
});
