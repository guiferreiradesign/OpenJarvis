import { ChatArea } from '../components/Chat/ChatArea';
import { SystemPanel } from '../components/Chat/SystemPanel';
import { useAppStore } from '../lib/store';
import { ConversationList } from '../components/Sidebar/ConversationList';
import { Plus, Radio } from 'lucide-react';
import { useNavigate } from 'react-router';

export function ChatPage() {
  const systemPanelOpen = useAppStore((s) => s.systemPanelOpen);
  const createConversation = useAppStore((s) => s.createConversation);
  const selectedModel = useAppStore((s) => s.selectedModel);
  const setCommandPaletteOpen = useAppStore((s) => s.setCommandPaletteOpen);
  const navigate = useNavigate();

  return (
    <div className="jarvis-chat-page">
      <aside className="jarvis-chat-history">
        <div className="jarvis-chat-history-head">
          <div><span className="jarvis-eyebrow">JARVIS // CHAT</span><strong>Conversations</strong></div>
          <button className="jarvis-icon-button" aria-label="Start new conversation" onClick={() => createConversation(selectedModel)}><Plus size={17} /></button>
        </div>
        <div className="jarvis-chat-history-list"><ConversationList searchQuery="" /></div>
        <button className="jarvis-chat-model" onClick={() => setCommandPaletteOpen(true)} title="Choose model (⌘K)"><Radio size={14} /><span>{selectedModel || 'Select a model'}</span></button>
      </aside>
      <section className="jarvis-chat-main">
        <header className="jarvis-chat-toolbar">
          <span className="jarvis-eyebrow">LIVE CONVERSATION</span>
          <button onClick={() => navigate('/')} className="jarvis-text-button">HOME</button>
        </header>
        <ChatArea />
      </section>
      {systemPanelOpen && <SystemPanel />}
    </div>
  );
}
