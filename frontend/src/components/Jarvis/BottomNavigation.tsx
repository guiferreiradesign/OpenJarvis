import { NavLink } from 'react-router';
import { Home, MessageSquare, FolderKanban, Wrench, Settings2 } from 'lucide-react';

const items = [
  { to: '/', label: 'Home', Icon: Home, end: true },
  { to: '/chat', label: 'Chat', Icon: MessageSquare },
  { to: '/projects', label: 'Projects', Icon: FolderKanban },
  { to: '/tools', label: 'Tools', Icon: Wrench },
  { to: '/settings', label: 'Settings', Icon: Settings2 },
];

export function BottomNavigation() {
  return (
    <nav className="jarvis-bottom-nav" aria-label="Main navigation">
      {items.map(({ to, label, Icon, end }) => (
        <NavLink key={to} to={to} end={end} className={({ isActive }) => `jarvis-nav-item${isActive ? ' active' : ''}`}>
          <Icon size={19} strokeWidth={1.7} />
          <span>{label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
