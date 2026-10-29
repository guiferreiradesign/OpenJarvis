import { useEffect, useState } from 'react';
import { ChevronDown, ChevronRight, Loader2, CheckCircle2, XCircle } from 'lucide-react';
import type { ToolCallInfo } from '../../types';
import { serializeToolCallArguments } from '../../lib/tool-call';
import { cancelGmailSendApproval, confirmGmailSendApproval, fetchGmailSendApproval } from '../../lib/api';

interface Props {
  toolCall: ToolCallInfo;
}

const statusConfig = {
  running: { icon: Loader2, color: 'var(--color-accent)' },
  success: { icon: CheckCircle2, color: 'var(--color-success)' },
  error: { icon: XCircle, color: 'var(--color-error)' },
};

function previewArgs(raw: string): string {
  if (!raw) return '';
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') {
      const entries = Object.entries(parsed);
      if (entries.length === 0) return '';
      const [k, v] = entries[0];
      const valStr =
        typeof v === 'string' ? v : JSON.stringify(v);
      const trimmed = valStr.length > 40 ? `${valStr.slice(0, 40)}…` : valStr;
      return entries.length === 1 ? `${k}: ${trimmed}` : `${k}: ${trimmed}, …`;
    }
  } catch {
    /* fall through */
  }
  return raw.length > 60 ? `${raw.slice(0, 60)}…` : raw;
}

export function ToolCallCard({ toolCall }: Props) {
  const [expanded, setExpanded] = useState(false);
  const config = statusConfig[toolCall.status];
  const StatusIcon = config.icon;
  // Persisted conversations may contain the pre-fix object payload despite
  // the TypeScript contract, so normalize again at the final render boundary.
  const argumentsText = serializeToolCallArguments(toolCall.arguments);
  const preview = previewArgs(argumentsText);
  const approval = toolCall.tool === 'gmail_request_send_approval' ? parseApprovalId(toolCall.result) : null;

  return (
    <div
      className="rounded-md text-xs overflow-hidden"
      style={{
        border: '1px solid var(--color-border-subtle, var(--color-border))',
        background: 'var(--color-bg-tertiary, var(--color-bg-secondary))',
        fontFamily:
          'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
      }}
    >
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 w-full px-2.5 py-1.5 cursor-pointer text-left"
        style={{ background: 'transparent' }}
      >
        {expanded ? (
          <ChevronDown size={11} style={{ color: 'var(--color-text-tertiary)', flexShrink: 0 }} />
        ) : (
          <ChevronRight size={11} style={{ color: 'var(--color-text-tertiary)', flexShrink: 0 }} />
        )}
        <StatusIcon
          size={11}
          style={{ color: config.color, flexShrink: 0 }}
          className={toolCall.status === 'running' ? 'animate-spin' : ''}
        />
        <span
          style={{ color: 'var(--color-text)', fontWeight: 500, flexShrink: 0 }}
        >
          {toolCall.tool}
        </span>
        {preview && !expanded && (
          <span
            className="truncate"
            style={{ color: 'var(--color-text-tertiary)', fontSize: 10.5 }}
          >
            {preview}
          </span>
        )}
        <div className="flex-1" />
        {toolCall.latency != null && (
          <span
            style={{
              color: 'var(--color-text-tertiary)',
              fontSize: 10,
              flexShrink: 0,
            }}
          >
            {toolCall.latency < 1000
              ? `${Math.round(toolCall.latency)}ms`
              : `${(toolCall.latency / 1000).toFixed(1)}s`}
          </span>
        )}
      </button>
      {expanded && (
        <div
          className="px-2.5 pb-2 pt-0.5"
          style={{ borderTop: '1px solid var(--color-border-subtle, var(--color-border))' }}
        >
          {argumentsText && (
            <div className="mt-1.5">
              <div
                style={{
                  color: 'var(--color-text-tertiary)',
                  fontSize: 9.5,
                  textTransform: 'uppercase',
                  letterSpacing: '0.05em',
                  marginBottom: 3,
                }}
              >
                args
              </div>
              <pre
                className="p-1.5 rounded overflow-auto"
                style={{
                  background: 'var(--color-code-bg, rgba(0,0,0,0.2))',
                  color: 'var(--color-text-secondary)',
                  fontSize: 11,
                  lineHeight: 1.4,
                  maxHeight: 120,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-all',
                }}
              >
                {formatJson(argumentsText)}
              </pre>
            </div>
          )}
          {toolCall.result && (
            <div className="mt-1.5">
              <div
                style={{
                  color: 'var(--color-text-tertiary)',
                  fontSize: 9.5,
                  textTransform: 'uppercase',
                  letterSpacing: '0.05em',
                  marginBottom: 3,
                }}
              >
                result
              </div>
              <pre
                className="p-1.5 rounded overflow-auto"
                style={{
                  background: 'var(--color-code-bg, rgba(0,0,0,0.2))',
                  color: 'var(--color-text-secondary)',
                  fontSize: 11,
                  lineHeight: 1.4,
                  maxHeight: 180,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {toolCall.result}
              </pre>
            </div>
          )}
        </div>
      )}
      {approval && <GmailApprovalPanel approvalId={approval} />}
    </div>
  );
}

function parseApprovalId(result?: string): string | null {
  if (!result) return null;
  try {
    const parsed = JSON.parse(result);
    return typeof parsed.approval_id === 'string' ? parsed.approval_id : null;
  } catch { return null; }
}

function GmailApprovalPanel({ approvalId }: { approvalId: string }) {
  const [approval, setApproval] = useState<any>(null);
  const [approvalToken, setApprovalToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState('');
  useEffect(() => {
    const token = sessionStorage.getItem(`openjarvis-gmail-approval:${approvalId}`) || '';
    setApprovalToken(token);
    if (!token) { setError('Dashboard approval token is unavailable. Request a new approval.'); return; }
    let active = true;
    fetchGmailSendApproval(approvalId, token)
      .then((data) => {
        if (active) { setApproval(data); if (data.result?.message_id) setResult(`Sent successfully. Gmail message ID: ${data.result.message_id}`); }
      }).catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [approvalId]);
  const act = async (action: 'confirm' | 'cancel') => {
    setBusy(true); setError('');
    try {
      const data = action === 'cancel'
        ? await cancelGmailSendApproval(approvalId, approvalToken)
        : await confirmGmailSendApproval(approvalId, approvalToken);
      if (action === 'cancel') { setApproval((old: any) => ({ ...old, status: 'cancelled' })); setResult('Email cancelled.'); }
      else { setApproval((old: any) => ({ ...old, status: data.status })); setResult(data.message_id ? `Sent successfully. Gmail message ID: ${data.message_id}` : 'Gmail accepted the message.'); }
    } catch (e: any) {
      setError(e.message || 'Request failed.');
      try {
        const fresh = await fetchGmailSendApproval(approvalId, approvalToken);
        setApproval(fresh);
        if (fresh.result?.message_id) setResult(`Sent successfully. Gmail message ID: ${fresh.result.message_id}`);
      } catch { /* Preserve the actionable error returned by the send endpoint. */ }
    }
    finally { setBusy(false); }
  };
  const m = approval?.message;
  return <section className="m-3 rounded-lg p-4 text-sm" style={{ border: '1px solid var(--color-warning, #d99a2b)', background: 'var(--color-bg-secondary)' }} aria-label="Gmail send approval">
    <strong>Review email before sending</strong>
    {m && <div className="mt-3 space-y-2" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
      <div><b>Recipient:</b> {m.to}</div><div><b>Subject:</b> {m.subject}</div><div><b>Body:</b><div className="mt-1">{m.body}</div></div>
    </div>}
    <p className="mt-3">Confirming will send this exact message through Gmail.</p>
    {error && <p role="alert" className="mt-2" style={{ color: 'var(--color-error)' }}>{error}</p>}
    {result && <p role="status" className="mt-2">{result}</p>}
    {approval?.status === 'pending' && <div className="mt-3 flex gap-2">
      <button disabled={busy || !m} onClick={() => void act('confirm')} className="rounded px-3 py-2" style={{ background: 'var(--color-accent)', color: 'white' }}>{busy ? 'Working…' : 'Confirm & Send'}</button>
      <button disabled={busy} onClick={() => void act('cancel')} className="rounded px-3 py-2">Cancel</button>
    </div>}
    {approval && approval.status !== 'pending' && !result && <p className="mt-2">Approval status: {approval.status}.</p>}
  </section>;
}

function formatJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}
