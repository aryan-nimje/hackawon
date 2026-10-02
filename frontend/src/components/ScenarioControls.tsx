interface ScenarioControlsProps {
  onStart: (speed: number, failures: string[]) => void;
  loading: boolean;
  runId: string | null;
}

export function ScenarioControls({ onStart, loading, runId }: ScenarioControlsProps) {
  return (
    <div className="rounded-xl border border-disaster-border bg-disaster-panel p-4">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">
        Scenario Controls
      </h2>
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm">
          Replay speed
          <select
            id="replay-speed"
            className="ml-2 rounded border border-disaster-border bg-slate-800 px-2 py-1"
            defaultValue="5"
          >
            <option value="1">1x</option>
            <option value="5">5x</option>
            <option value="10">10x</option>
          </select>
        </label>
        <label className="text-sm">
          Simulate failure
          <select
            id="simulate-failure"
            className="ml-2 rounded border border-disaster-border bg-slate-800 px-2 py-1"
            defaultValue=""
          >
            <option value="">None</option>
            <option value="rescue">Rescue</option>
            <option value="medical">Medical</option>
            <option value="logistics">Logistics</option>
          </select>
        </label>
        <button
          type="button"
          disabled={loading}
          onClick={() => {
            const speed = Number(
              (document.getElementById('replay-speed') as HTMLSelectElement).value,
            );
            const failure = (document.getElementById('simulate-failure') as HTMLSelectElement)
              .value;
            onStart(speed, failure ? [failure] : []);
          }}
          className="rounded-lg bg-disaster-accent px-4 py-2 text-sm font-medium text-slate-900 hover:bg-sky-300 disabled:opacity-50"
        >
          {loading ? 'Starting…' : 'Start Scenario'}
        </button>
      </div>
      {runId && (
        <p className="mt-2 truncate text-xs text-slate-500">
          Run ID: <span className="font-mono text-slate-400">{runId}</span>
        </p>
      )}
    </div>
  );
}
