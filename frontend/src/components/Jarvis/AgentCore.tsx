export type AgentCoreState = 'ready' | 'listening' | 'thinking' | 'speaking' | 'executing';

// Keep renderer tuning explicit for the later Bloom renderer. These values
// are design defaults; this CSS/SVG core does not emulate WebGL exposure.
export const DEFAULT_BLOOM_SETTINGS = {
  strength: 0.30,
  radius: 0.75,
  exposure: 1.20,
  quality: 'Balanced',
  bloom: true,
  parallax: true,
} as const;

export function AgentCore({ state = 'ready', simulated = false }: { state?: AgentCoreState; simulated?: boolean }) {
  return (
    <section className={`jarvis-core-stage state-${state}`} aria-label={`JARVIS core: ${state}`}>
      <div className="jarvis-core-label">CORE <span>//</span> {state.toUpperCase()} {simulated && <em>· VISUAL PREVIEW</em>}</div>
      <div className="jarvis-core" aria-hidden="true">
        <div className="core-grid" />
        <div className="core-halo" />
        <div className="core-orbit orbit-a"><i /><i /><i /></div>
        <div className="core-orbit orbit-b"><i /><i /></div>
        <div className="core-orbit orbit-c"><i /><i /><i /><i /></div>
        <div className="core-orbit orbit-d"><i /><i /></div>
        <div className="core-disc"><div className="core-disc-inner" /><div className="core-disc-light" /></div>
        <div className="core-crosshair crosshair-x" /><div className="core-crosshair crosshair-y" />
      </div>
      <div className="core-platform" aria-hidden="true"><span /><span /><span /><span /></div>
      <div className="core-state-caption"><b />{state === 'thinking' ? 'AGENT ACTIVE' : 'SYSTEM STANDBY'}</div>
    </section>
  );
}
