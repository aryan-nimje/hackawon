import { CITY } from '../lib/layers';
import { useState } from 'react';
import { loadTokens, rememberToken, type SubmitResult } from '../api/reports';
import { ReportForm } from './ReportForm';
import { StatusTracker } from './StatusTracker';

function tokenFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get('t');
}

export default function CitizenApp() {
  const [token, setToken] = useState<string | null>(tokenFromUrl);
  const [demo, setDemo] = useState(() => (tokenFromUrl() ?? '').startsWith('demo-'));
  const previous = loadTokens().filter((t) => t !== token);

  const handleSubmitted = (r: SubmitResult) => {
    rememberToken(r.token);
    setDemo(r.demo);
    setToken(r.token);
    window.history.pushState(null, '', `?t=${encodeURIComponent(r.token)}`);
    window.scrollTo({ top: 0 });
  };

  const newReport = () => {
    setToken(null);
    window.history.pushState(null, '', window.location.pathname);
  };

  const openPrevious = (t: string) => {
    setDemo(t.startsWith('demo-'));
    setToken(t);
    window.history.pushState(null, '', `?t=${encodeURIComponent(t)}`);
  };

  return (
    <div className="mx-auto min-h-screen max-w-xl px-4 pb-16 pt-5">
      <header className="mb-4">
        <h1 className="text-2xl font-bold">{token ? 'Report status' : 'Report an emergency'}</h1>
        <p className="text-sm text-slate-400">Flood response · {CITY.name.replace(/\s*\(.*\)/, '')} (simulated scenario)</p>
      </header>

      <div className="mb-5 rounded-xl border border-red-500/50 bg-red-500/10 px-4 py-3 text-sm text-red-100" role="note">
        <strong>In immediate danger? Call 911 (or your local emergency number) first.</strong>
        <br />
        This is a decision-support prototype. Nothing you submit here is sent to real rescue teams.
      </div>

      {token ? (
        <>
          <StatusTracker token={token} demo={demo} />
          <button
            type="button"
            onClick={newReport}
            className="mt-5 min-h-11 w-full rounded-xl border border-disaster-border bg-slate-800 text-sm font-medium hover:bg-slate-700"
          >
            Report another emergency
          </button>
        </>
      ) : (
        <>
          <ReportForm onSubmitted={handleSubmitted} />
          {previous.length > 0 && (
            <section className="mt-8">
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-400">Your earlier reports</h2>
              <ul className="space-y-2">
                {previous.map((t) => (
                  <li key={t}>
                    <button
                      type="button"
                      onClick={() => openPrevious(t)}
                      className="w-full rounded-lg border border-disaster-border bg-disaster-panel px-3 py-2 text-left font-mono text-xs text-slate-300 hover:bg-slate-800"
                    >
                      {t}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}
