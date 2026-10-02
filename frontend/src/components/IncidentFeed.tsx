import { useState } from 'react';
import { escapeHtml } from '../api/client';
import type { Incident } from '../types';

interface IncidentFeedProps {
  incidents: Incident[];
  loading: boolean;
  error: string | null;
}

function credibilityBadge(score: number, flagged: boolean) {
  if (flagged) return 'bg-red-900/50 text-red-300 border-red-700';
  if (score >= 0.7) return 'bg-green-900/40 text-green-300 border-green-700';
  if (score >= 0.45) return 'bg-yellow-900/40 text-yellow-300 border-yellow-700';
  return 'bg-red-900/50 text-red-300 border-red-700';
}

export function IncidentFeed({ incidents, loading, error }: IncidentFeedProps) {
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <div className="rounded-xl border border-disaster-border bg-disaster-panel p-4">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">
        Incident Feed
      </h2>

      {error && (
        <p className="rounded bg-red-900/30 px-2 py-1 text-sm text-red-300">{error}</p>
      )}

      {loading && incidents.length === 0 && (
        <p className="text-sm text-slate-500">Loading incidents…</p>
      )}

      {!loading && incidents.length === 0 && (
        <p className="text-sm text-slate-500">No incidents yet.</p>
      )}

      <ul className="max-h-96 space-y-2 overflow-y-auto">
        {incidents.map((inc) => {
          const ver = inc.verification;
          const flagged = ver?.flagged ?? false;
          const score = ver?.credibility ?? null;
          return (
            <li
              key={inc.id}
              className={`rounded-lg border px-3 py-2 text-sm ${
                flagged
                  ? 'border-red-700/60 bg-red-950/20'
                  : 'border-disaster-border bg-slate-800/40'
              }`}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs text-slate-500">{inc.id}</span>
                <span className="rounded bg-slate-700 px-1.5 py-0.5 text-xs uppercase">
                  {inc.urgency}
                </span>
                <span className="rounded bg-slate-700 px-1.5 py-0.5 text-xs">{inc.need_type}</span>
                {score != null && (
                  <span
                    className={`rounded border px-1.5 py-0.5 text-xs ${credibilityBadge(score, flagged)}`}
                  >
                    Credibility {(score * 100).toFixed(0)}%
                    {flagged ? ' — REVIEW' : ''}
                  </span>
                )}
              </div>
              <p className="mt-1 text-slate-200">{escapeHtml(inc.text)}</p>
              <p className="text-xs text-slate-500">{escapeHtml(inc.location)}</p>
              {ver && (
                <button
                  type="button"
                  className="mt-1 text-xs text-disaster-accent hover:underline"
                  onClick={() => setExpanded(expanded === inc.id ? null : inc.id)}
                >
                  {expanded === inc.id ? 'Hide reasoning' : 'Show agent reasoning'}
                </button>
              )}
              {expanded === inc.id && ver && (
                <ul className="mt-1 list-inside list-disc text-xs text-slate-400">
                  {ver.reasons.map((r, i) => (
                    <li key={i}>{escapeHtml(r)}</li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
