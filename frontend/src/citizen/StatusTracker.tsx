import { useCallback, useEffect, useState } from 'react';
import { reportsApi } from '../api/reports';
import type { ReportStatus, ReportStatusView } from '../types';

const STEPS: { key: ReportStatus; label: string; hint: string }[] = [
  { key: 'received', label: 'Received', hint: 'Your report reached the coordination centre.' },
  { key: 'verifying', label: 'Being verified', hint: 'Checked against other reports and live data.' },
  { key: 'prioritized', label: 'Prioritized', hint: 'Ranked in the response queue.' },
  { key: 'assigned', label: 'Team assigned', hint: 'A response team has been assigned.' },
  { key: 'resolved', label: 'Resolved', hint: 'Help has reached this location.' },
];

export function trackingLink(token: string): string {
  return `${window.location.origin}${window.location.pathname}?t=${encodeURIComponent(token)}`;
}

export function StatusTracker({ token, demo }: { token: string; demo?: boolean }) {
  const [view, setView] = useState<ReportStatusView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      setView(await reportsApi.status(token));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load status');
    }
  }, [token]);

  useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [load]);

  const currentIdx = view ? STEPS.findIndex((s) => s.key === view.status) : -1;
  const done = view?.status === 'resolved';

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(trackingLink(token));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      window.prompt('Copy your private tracking link:', trackingLink(token));
    }
  };

  return (
    <section className="space-y-4" aria-live="polite">
      <div className="rounded-2xl border border-disaster-border bg-disaster-panel p-5">
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">Your report</p>
        <h2 className="mt-1 text-xl font-bold">
          {view ? STEPS[currentIdx]?.label : error ? 'Unavailable' : 'Checking…'}
        </h2>
        {view?.summary && <p className="mt-2 text-sm text-slate-300">“{view.summary}”</p>}
        {view?.note && <p className="mt-2 text-sm text-slate-400">{view.note}</p>}
        {error && <p className="mt-2 rounded bg-red-900/30 px-3 py-2 text-sm text-red-300">{error}</p>}

        <ol className="mt-5 space-y-0">
          {STEPS.map((s, i) => {
            const reached = i <= currentIdx;
            const active = i === currentIdx && !done;
            const at = view?.history.find((h) => h.status === s.key)?.at;
            return (
              <li key={s.key} className="flex gap-3">
                <div className="flex flex-col items-center">
                  <span
                    className={`mt-0.5 flex h-6 w-6 items-center justify-center rounded-full border-2 text-xs font-bold ${
                      reached
                        ? 'border-disaster-success bg-disaster-success text-slate-900'
                        : 'border-slate-600 text-slate-600'
                    } ${active ? 'animate-pulse' : ''}`}
                  >
                    {reached ? '✓' : i + 1}
                  </span>
                  {i < STEPS.length - 1 && (
                    <span className={`h-8 w-0.5 ${i < currentIdx ? 'bg-disaster-success' : 'bg-slate-700'}`} />
                  )}
                </div>
                <div className="pb-3">
                  <p className={`text-sm font-medium ${reached ? 'text-slate-100' : 'text-slate-500'}`}>
                    {s.label}
                    {at && (
                      <span className="ml-2 text-xs font-normal text-slate-500">
                        {new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                      </span>
                    )}
                  </p>
                  <p className="text-xs text-slate-500">{s.hint}</p>
                </div>
              </li>
            );
          })}
        </ol>
      </div>

      <div className="rounded-2xl border border-disaster-border bg-disaster-panel p-5">
        <p className="text-sm font-medium">Your private tracking link</p>
        <p className="mt-1 text-xs text-slate-400">
          Only people with this link can see this report's status. Bookmark it or send it to yourself.
        </p>
        <div className="mt-3 flex gap-2">
          <input
            readOnly
            value={trackingLink(token)}
            onFocus={(e) => e.currentTarget.select()}
            className="min-w-0 flex-1 rounded-lg border border-disaster-border bg-slate-900 px-3 py-2 font-mono text-xs text-slate-300"
            aria-label="Tracking link"
          />
          <button
            type="button"
            onClick={copy}
            className="rounded-lg bg-slate-700 px-4 py-2 text-sm font-medium hover:bg-slate-600"
          >
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
        {demo && (
          <p className="mt-3 rounded-lg border border-purple-400/40 bg-purple-500/10 px-3 py-2 text-xs text-purple-200">
            Demo mode: the server does not accept citizen reports yet, so this report is stored only in this
            browser and its progress is simulated.
          </p>
        )}
      </div>
    </section>
  );
}
