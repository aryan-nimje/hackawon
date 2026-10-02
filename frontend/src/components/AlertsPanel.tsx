import { useState } from 'react';
import { escapeHtml } from '../api/client';
import type { AlertDraft } from '../types';

interface AlertsPanelProps {
  alerts: AlertDraft[];
  loading: boolean;
  error: string | null;
}

const audienceLabel: Record<AlertDraft['audience'], string> = {
  public: 'Public',
  field_teams: 'Field Teams',
  hospitals: 'Hospitals',
};

export function AlertsPanel({ alerts, loading, error }: AlertsPanelProps) {
  const [copied, setCopied] = useState<string | null>(null);

  const copy = async (alert: AlertDraft) => {
    const text = `${alert.title}\n\n${alert.body}`;
    await navigator.clipboard.writeText(text);
    setCopied(alert.audience);
    setTimeout(() => setCopied(null), 2000);
  };

  return (
    <div className="rounded-xl border border-disaster-border bg-disaster-panel p-4">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">
        Draft Alerts
      </h2>
      <p className="mb-3 text-xs text-slate-500">
        Copy only — nothing is actually sent. Human coordinator must approve before any real
        dispatch.
      </p>

      {error && <p className="text-sm text-red-300">{error}</p>}
      {loading && alerts.length === 0 && (
        <p className="text-sm text-slate-500">Waiting for communication agent…</p>
      )}
      {!loading && alerts.length === 0 && (
        <p className="text-sm text-slate-500">No alert drafts yet.</p>
      )}

      <div className="space-y-3">
        {alerts.map((alert) => (
          <div
            key={alert.audience}
            className="rounded-lg border border-disaster-border bg-slate-800/40 p-3 text-sm"
          >
            <div className="mb-1 flex items-center justify-between">
              <span className="text-xs font-semibold uppercase text-disaster-accent">
                {audienceLabel[alert.audience]}
              </span>
              <button
                type="button"
                onClick={() => copy(alert)}
                className="rounded bg-slate-700 px-2 py-0.5 text-xs hover:bg-slate-600"
              >
                {copied === alert.audience ? 'Copied!' : 'Copy'}
              </button>
            </div>
            <p className="font-medium text-slate-200">{escapeHtml(alert.title)}</p>
            <p className="mt-1 whitespace-pre-wrap text-slate-400">{escapeHtml(alert.body)}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
