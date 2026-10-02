import type { ActivityEvent, AgentStatus } from '../types';

interface AgentTimelineProps {
  events: ActivityEvent[];
  connected: boolean;
  error: string | null;
  loading: boolean;
}

const statusColor: Record<AgentStatus, string> = {
  pending: 'bg-slate-500',
  running: 'bg-disaster-accent animate-pulse',
  completed: 'bg-disaster-success',
  failed: 'bg-disaster-danger',
  skipped: 'bg-slate-600',
};

export function AgentTimeline({ events, connected, error, loading }: AgentTimelineProps) {
  return (
    <div className="rounded-xl border border-disaster-border bg-disaster-panel p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">
          Agent Activity Timeline
        </h2>
        <span
          className={`text-xs ${connected ? 'text-disaster-success' : 'text-slate-500'}`}
        >
          {connected ? '● Live' : '○ Offline'}
        </span>
      </div>

      {error && (
        <p className="mb-2 rounded bg-red-900/30 px-2 py-1 text-xs text-red-300">{error}</p>
      )}

      {loading && events.length === 0 && (
        <p className="text-sm text-slate-500">Waiting for agent activity…</p>
      )}

      {!loading && events.length === 0 && (
        <p className="text-sm text-slate-500">
          No activity yet. Start a scenario to watch agents orchestrate.
        </p>
      )}

      <ul className="max-h-72 space-y-2 overflow-y-auto">
        {events.map((ev, i) => (
          <li
            key={`${ev.agent}-${ev.timestamp}-${i}`}
            className="flex items-start gap-2 rounded-lg bg-slate-800/60 px-3 py-2 text-sm"
          >
            <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${statusColor[ev.status]}`} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium capitalize text-slate-200">{ev.agent}</span>
                <span className="text-xs uppercase text-slate-500">{ev.status}</span>
                {ev.duration_ms != null && (
                  <span className="text-xs text-slate-500">{ev.duration_ms}ms</span>
                )}
              </div>
              <p className="text-slate-400">{ev.summary}</p>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
