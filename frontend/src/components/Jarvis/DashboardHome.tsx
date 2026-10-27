import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { Activity, ArrowRight, Cpu, MessageSquarePlus, Mic, ShieldCheck, Sparkles } from 'lucide-react';
import { checkHealth, fetchServerInfo, fetchSpeechHealth, fetchTtsHealth, type SpeechHealth, type TtsHealth } from '../../lib/api';
import { useAppStore } from '../../lib/store';
import type { ServerInfo } from '../../types';
import { ConversationList } from '../Sidebar/ConversationList';
import { InputArea } from '../Chat/InputArea';
import { AgentCore } from './AgentCore';

type HealthState = 'checking' | 'available' | 'unavailable' | 'not-connected';

function ModuleStatus({ label, state, detail }: { label: string; state: HealthState; detail?: string }) {
  const value = state === 'checking' ? 'Checking' : state === 'available' ? 'Available' : state === 'not-connected' ? 'Not connected' : 'Unavailable';
  return <div className="jarvis-module-row"><span className={`module-indicator ${state}`} /><span className="module-name">{label}</span><span className={`module-value ${state}`} title={detail}>{value}</span></div>;
}

export function DashboardHome() {
  const navigate = useNavigate();
  const createConversation = useAppStore((s) => s.createConversation);
  const selectedModel = useAppStore((s) => s.selectedModel);
  const streamState = useAppStore((s) => s.streamState);
  const setCommandPaletteOpen = useAppStore((s) => s.setCommandPaletteOpen);
  const [tab, setTab] = useState<'status' | 'modules'>('status');
  const [backend, setBackend] = useState<HealthState>('checking');
  const [speech, setSpeech] = useState<SpeechHealth | null>(null);
  const [tts, setTts] = useState<TtsHealth | null>(null);
  const [serverInfo, setServerInfo] = useState<ServerInfo | null>(null);
  const [checksComplete, setChecksComplete] = useState(false);

  useEffect(() => {
    let alive = true;
    Promise.allSettled([checkHealth(), fetchSpeechHealth(), fetchTtsHealth(), fetchServerInfo()]).then(([api, stt, voice, info]) => {
      if (!alive) return;
      setBackend(api.status === 'fulfilled' && api.value ? 'available' : 'unavailable');
      setSpeech(stt.status === 'fulfilled' ? stt.value : { available: false });
      setTts(voice.status === 'fulfilled' ? voice.value : { available: false });
      setServerInfo(info.status === 'fulfilled' ? info.value : null);
      setChecksComplete(true);
    });
    return () => { alive = false; };
  }, []);

  const startNewChat = () => {
    createConversation(selectedModel);
    navigate('/chat');
  };
  const coreState = streamState.isStreaming ? (streamState.activeToolCalls.length ? 'executing' : 'thinking') : 'ready';
  const greeting = new Date().getHours() < 12 ? 'Good morning' : new Date().getHours() < 18 ? 'Good afternoon' : 'Good evening';

  return (
    <section className="jarvis-dashboard" aria-label="JARVIS Home">
      <header className="jarvis-topline">
        <div className="jarvis-brand">JARVIS <i /></div>
        <div className="jarvis-topline-caption">PERSONAL ASSISTANT <span>·</span> LOCAL INTERFACE</div>
        <button className="jarvis-model-switch" onClick={() => setCommandPaletteOpen(true)} title="Choose a model (⌘K)"><Cpu size={13} />{selectedModel || 'SELECT MODEL'} <kbd>⌘K</kbd></button>
        <div className="jarvis-local-badge"><ShieldCheck size={14} /> LOCAL SESSION</div>
      </header>

      <div className="jarvis-home-grid">
        <section className="jarvis-home-left">
          <div className="jarvis-state-label"><b /> {streamState.isStreaming ? 'AGENT ACTIVE' : backend === 'available' ? 'SYSTEM READY' : backend === 'checking' ? 'CHECKING CONNECTION' : 'SERVER OFFLINE'}</div>
          <h1>{greeting},<br /><span>Guil.</span></h1>
          <p className="jarvis-invitation">HOW CAN I HELP YOU TODAY?</p>
          <div className="jarvis-accent-rule" />

          <div className="jarvis-home-composer" aria-label="Start a real JARVIS conversation">
            <InputArea
              onMessageStart={() => navigate('/chat')}
              showMic={false}
              showResearch={false}
              placeholder="Type a request for JARVIS…"
            />
          </div>

          <section className="jarvis-panel jarvis-voice-panel">
            <div className="jarvis-panel-heading"><span>VOICE</span><span className="jarvis-outline-tag">OPTIONAL</span></div>
            <div className="jarvis-voice-line"><span className="voice-wave" aria-hidden="true" /><Mic size={18} /><span>{speech?.available ? 'Speech provider ready · voice loop not connected' : 'Microphone input is not connected to Home'}</span></div>
            <div className="jarvis-voice-providers">
              <span>MLX WHISPER <b className={speech?.available ? 'is-ready' : ''}>{speech?.available ? 'AVAILABLE' : checksComplete ? 'NOT CONNECTED' : 'CHECKING'}</b></span>
              <span>VOICEBOX <b className={tts?.available ? 'is-ready' : ''}>{tts?.available ? 'AVAILABLE' : checksComplete ? 'NOT CONNECTED' : 'CHECKING'}</b></span>
            </div>
          </section>

          <section className="jarvis-panel jarvis-recent-panel">
            <div className="jarvis-panel-heading"><span>RECENT CHATS</span><button onClick={() => navigate('/chat')}>OPEN CHAT <ArrowRight size={13} /></button></div>
            <div className="jarvis-recent-list"><ConversationList searchQuery="" /></div>
          </section>
        </section>

        <div className="jarvis-home-core"><AgentCore state={coreState} simulated={coreState === 'ready'} /></div>

        <aside className="jarvis-home-right">
          <section className="jarvis-panel jarvis-system-panel">
            <div className="jarvis-panel-heading"><span>SYSTEM</span><span className={`jarvis-outline-tag ${backend === 'available' ? 'tag-live' : ''}`}>{backend === 'checking' ? 'CHECKING' : backend === 'available' ? 'CONNECTED' : 'OFFLINE'}</span></div>
            <div className="jarvis-tabs" role="tablist" aria-label="System details">
              <button className={tab === 'status' ? 'selected' : ''} onClick={() => setTab('status')} role="tab" aria-selected={tab === 'status'}>STATUS</button>
              <button className={tab === 'modules' ? 'selected' : ''} onClick={() => setTab('modules')} role="tab" aria-selected={tab === 'modules'}>MODULES</button>
            </div>
            {tab === 'status' ? <div className="jarvis-status-content">
              <ModuleStatus label="JARVIS API" state={backend} detail="Verified through the local server health endpoint" />
              <ModuleStatus label="INTERFACE" state="available" detail="This local interface is loaded" />
              <div className="jarvis-system-note"><Activity size={14} /> Connection status is checked from the local server.</div>
            </div> : <div className="jarvis-status-content">
              <ModuleStatus label="CONFIGURED AGENT" state={serverInfo?.agent ? 'available' : checksComplete ? 'unavailable' : 'checking'} detail={serverInfo?.agent || 'No agent identity reported by the API'} />
              <ModuleStatus label="JOB DASHBOARD" state="not-connected" detail="The job manager exists; a dashboard API is not connected" />
              <ModuleStatus label="MLX WHISPER" state={speech?.available ? 'available' : checksComplete ? 'unavailable' : 'checking'} detail={speech?.reason} />
              <ModuleStatus label="VOICEBOX TTS" state={tts?.available ? 'available' : checksComplete ? 'unavailable' : 'checking'} detail={tts?.reason} />
            </div>}
          </section>

          <section className="jarvis-panel jarvis-actions-panel">
            <div className="jarvis-panel-heading"><span>QUICK ACTIONS</span><Sparkles size={15} /></div>
            <button className="jarvis-action" onClick={startNewChat}><MessageSquarePlus size={17} /><span>New conversation</span><ArrowRight size={14} /></button>
            <button className="jarvis-action disabled" disabled title="Available in a later interface increment"><Activity size={17} /><span>Analyze a file <small>COMING LATER</small></span></button>
            <button className="jarvis-action disabled" disabled title="Available in a later interface increment"><ArrowRight size={17} /><span>Browse the web <small>COMING LATER</small></span></button>
          </section>
          <div className="jarvis-honesty-note">No system measurements or job progress are shown until a live source is connected.</div>
        </aside>
      </div>
    </section>
  );
}
