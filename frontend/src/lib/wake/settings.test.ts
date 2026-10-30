import { beforeEach, describe, expect, it, vi } from 'vitest';

class MemoryStorage {
  private store = new Map<string, string>();
  getItem(k: string) { return this.store.get(k) ?? null; }
  setItem(k: string, v: string) { this.store.set(k, String(v)); }
}

beforeEach(() => {
  vi.resetModules();
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage = new MemoryStorage();
});

describe('wake settings', () => {
  it('defaults to the single-word Jarvis model', async () => {
    const { useAppStore } = await import('../store');
    expect(useAppStore.getState().settings.wakeModel).toBe('jarvis');
  });

  it('lets the alternative phrase be selected without touching other voice settings', async () => {
    const { useAppStore } = await import('../store');
    const before = useAppStore.getState().settings;
    useAppStore.getState().updateSettings({ wakeModel: 'hey_jarvis' });
    const after = useAppStore.getState().settings;
    expect(after.wakeModel).toBe('hey_jarvis');
    expect(after.speechEnabled).toBe(before.speechEnabled);
    expect(after.voiceOutputEnabled).toBe(before.voiceOutputEnabled);
    expect(after.voiceAutoplay).toBe(before.voiceAutoplay);
  });

  it('has no persisted "wake detection enabled" flag: it is always off after a reload', async () => {
    const { useAppStore } = await import('../store');
    expect(Object.keys(useAppStore.getState().settings).filter((k) => /wake/i.test(k))).toEqual(['wakeModel']);
  });
});
