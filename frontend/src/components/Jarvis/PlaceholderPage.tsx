import { useLocation } from 'react-router';
import { Construction } from 'lucide-react';

export function PlaceholderPage({ title }: { title: string }) {
  const location = useLocation();
  return (
    <section className="jarvis-placeholder">
      <div className="jarvis-eyebrow">JARVIS // {title.toUpperCase()}</div>
      <Construction size={28} />
      <h1>{title}</h1>
      <p>This area is reserved for a later JARVIS interface increment.</p>
      <small>Preview route: {location.pathname}</small>
    </section>
  );
}
