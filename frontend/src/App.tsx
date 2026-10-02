import { useCallback, useEffect, useState } from 'react';
import { api } from './api/client';
import { AgentTimeline } from './components/AgentTimeline';
import { AlertsPanel } from './components/AlertsPanel';
import { Banner } from './components/Banner';
import { DisasterMap } from './components/DisasterMap';
import { DraftPlan } from './components/DraftPlan';
import { IncidentFeed } from './components/IncidentFeed';
import { ReviewPanel } from './components/ReviewPanel';
import { ScenarioControls } from './components/ScenarioControls';
import { useHealth, useRun } from './hooks/useRun';
import { useSSE } from './hooks/useSSE';

export default function App() {
  const [runId, setRunId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [incidents, setIncidents] = useState<Awaited<ReturnType<typeof api.getIncidents>>>([]);
  const [incidentsError, setIncidentsError] = useState<string | null>(null);

  const { health, error: healthError } = useHealth();
  const { run, loading: runLoading, error: runError, refresh } = useRun(runId);
  const { events, connected, error: sseError } = useSSE(runId);

  const loadIncidents = useCallback(async (id: string) => {
    try {
      const data = await api.getIncidents(id);
      setIncidents(data);
      setIncidentsError(null);
    } catch (e) {
      setIncidentsError(e instanceof Error ? e.message : 'Failed to load incidents');
    }
  }, []);

  const handleStart = async (speed: number, failures: string[]) => {
    setStarting(true);
    setStartError(null);
    try {
      const res = await api.startScenario(speed, failures);
      setRunId(res.run_id);
      setTimeout(() => loadIncidents(res.run_id), 2000);
    } catch (e) {
      setStartError(e instanceof Error ? e.message : 'Failed to start scenario');
    } finally {
      setStarting(false);
    }
  };

  const handleReview = async (
    actions: { item_id: string; action: string; edited_content?: string }[],
  ) => {
    if (!runId) return;
    await api.reviewPlan(runId, actions);
    await refresh();
  };

  useEffect(() => {
    if (runId && run) {
      loadIncidents(runId);
    }
  }, [runId, run?.incidents.length, loadIncidents, run]);

  const backendDown = !!healthError;
  const mockMode = health?.mock_mode ?? true;

  return (
    <div className="min-h-screen p-4 md:p-6">
      <header className="mb-6">
        <h1 className="text-2xl font-bold text-slate-100">
          AI Multi-Agent Disaster Relief Coordinator
        </h1>
        <p className="text-sm text-slate-400">
          Supervisor-orchestrated decision support for Houston flood scenario (simulated)
        </p>
        <div className="mt-3">
          <Banner mockMode={mockMode} />
        </div>
        {backendDown && (
          <p className="mt-2 rounded bg-red-900/40 px-3 py-2 text-sm text-red-300">
            Backend unavailable — start the API server at http://localhost:8742. {healthError}
          </p>
        )}
      </header>

      <div className="mb-4">
        <ScenarioControls
          onStart={handleStart}
          loading={starting}
          runId={runId}
        />
        {startError && <p className="mt-2 text-sm text-red-300">{startError}</p>}
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <DisasterMap
          run={run}
          incidents={incidents.length ? incidents : run?.incidents ?? []}
          loading={runLoading}
          error={runError ?? incidentsError}
        />
        <AgentTimeline
          events={events.length ? events : run?.activity_log ?? []}
          connected={connected}
          error={sseError}
          loading={runLoading && !run}
        />
        <IncidentFeed
          incidents={incidents.length ? incidents : run?.incidents ?? []}
          loading={runLoading}
          error={incidentsError ?? runError}
        />
        <DraftPlan plan={run?.plan} loading={runLoading} error={runError} />
        <ReviewPanel plan={run?.plan} onReview={handleReview} loading={runLoading} />
        <AlertsPanel alerts={run?.alerts ?? []} loading={runLoading} error={runError} />
      </div>
    </div>
  );
}
