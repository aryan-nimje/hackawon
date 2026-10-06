import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { post } from '../shared/bus';
import { countIncidentStatuses, incidentStatusOf, isTrackedIncident, mergeIncidents } from '../shared/incidents';
import { CITY, loadCity } from '../lib/layers';
import { useCityVersion } from '../lib/useCity';
import { useLive } from '../shared/live';
import { WorldMap } from '../shared/map/WorldMap';
import type { ReplanAction, ReplanResult, SimBusEvent } from '../shared/types';
import { Banner, Toasts, type ToastMsg } from '../shared/ui';
import { AlertDrafts, ExecutionTracker, ExternalFeed, HospitalCapacity, LiveFeed, PlanReview, useSignals } from './panels';

export default function AuthorityApp() {
  const [toasts, setToasts] = useState<ToastMsg[]>([]);
  const tid = useRef(0);
  const toast = useCallback((text: string, tone: ToastMsg['tone'] = 'info') => {
    const id = ++tid.current;
    setToasts((t) => [...t.slice(-3), { id, text, tone }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000);
  }, []);

  const live = useLive({
    onSimEvent: (e: SimBusEvent) => {
      if (['route_blocked', 'bridge_collapsed', 'site_inaccessible', 'vehicle_failed', 'incident_escalated', 'region_expanded', 'hospital_full', 'patient_diverted'].includes(e.kind)) {
        toast(e.text, e.kind === 'incident_escalated' || e.kind === 'patient_diverted' ? 'warn' : 'error');
      }
    },
    onReport: (r) => toast(`New citizen report: ${r.submission.text.slice(0, 80)}`, 'warn'),
  });
  const { payload, world, reports, connected, activeRunId, runCity } = live;
  useCityVersion(); // re-render panels when the city's layers change

  // Authority is read-only: it shows the city the active run plans for. The backend sends it with `run.active`,
  // whether the run came from the simulator or from real reports.
  useEffect(() => {
    if (!runCity || runCity === CITY.slug) return;
    void loadCity(runCity).then((r) => { if (!r.ok) toast(`Could not load ${runCity}: ${r.error}`, 'error'); });
  }, [runCity, toast]);
  const plan = payload?.plan ?? null;
  // external signals (live news, alerts) and simulated evidence; re-read when the plan payload changes (credibility moved)
  const { signals, error: signalsError } = useSignals(payload);
  const simulationRunning = world.run_id != null && world.run_id === activeRunId && world.ts > 0;

  const incidents = useMemo(
    () => mergeIncidents({ backend: payload?.incidents ?? [], reports, sim: world.sim_incidents, override: world.urgency_override }),
    [payload, reports, world.sim_incidents, world.urgency_override],
  );
  const approvedKeys = useMemo(
    () => new Set((plan?.items ?? []).filter((i) => i.status === 'approved' || i.status === 'edited').map((i) => i.id)),
    [plan],
  );

  const [selected, setSelected] = useState<string | null>(null);
  // a new active run starts with a clean selection
  useEffect(() => { setSelected(null); setFocus(null); }, [activeRunId]);
  const [focus, setFocus] = useState<{ lat: number; lng: number; key: number } | null>(null);
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => { const id = setInterval(() => setClock(new Date()), 1000); return () => clearInterval(id); }, []);

  const runId = activeRunId ?? payload?.run_id ?? plan?.run_id ?? null;
  const review = async (actions: { item_id: string; action: string; edited_content?: string }[]) => {
    if (!runId) throw new Error('No active run');
    await post(`/plan/${runId}/review`, { actions });
  };
  const replan = async (item_id: string, action: ReplanAction) => {
    if (!runId) throw new Error('No active run');
    const res = await post<ReplanResult>(`/plan/${runId}/replan`, { item_id, action });
    if (action === 'hold') toast('Holding position.', 'info');
    else if (res.status === 'rerouted' && res.route) toast(`Re-routed on a clean route: ${res.route.distance_km} km, ~${res.route.duration_min} min.`, 'info');
    else if (res.status === 'no_clean_detour') toast('No clean detour found. The vehicle is waiting for Authority.', 'error');
    else toast(`Re-route not applied${res.message ? `: ${res.message}` : ''}.`, 'warn');
  };

  // a resolved or expired incident is no longer a live critical
  const critical = incidents.filter((i) => i.urgency === 'critical' && i.source !== 'weather' && i.source !== 'news'
    && !(isTrackedIncident(i) && ['resolved', 'expired'].includes(incidentStatusOf(world.incident_status, i.id)))).length;
  const counts = useMemo(() => countIncidentStatuses(incidents, world.incident_status), [incidents, world.incident_status]);
  const activeDis = world.disruptions.filter((d) => d.status === 'active').length;

  return (
    <div className="flex min-h-screen flex-col gap-3 p-3 lg:h-screen lg:p-4">
      <Toasts items={toasts} onDismiss={(id) => setToasts((t) => t.filter((x) => x.id !== id))} />

      <header className="flex flex-wrap items-center gap-3 rounded-2xl border border-[#e8e4dc] bg-white px-4 py-2.5 shadow-sm">
        <div>
          <h1 className="text-base font-bold">Authority command</h1>
          <p className="text-[11px] text-[#6b6b6b]">Decision-support dashboard{runId && <> · run <span className="font-mono">{runId.slice(0, 8)}</span></>}</p>
        </div>
        <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${connected ? 'bg-[#e3eeee] text-[#1f4f52]' : 'bg-[#fbe9e7] text-[#8c1d17]'}`}>
          {connected ? '● Connected' : '○ Reconnecting…'}
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-2 text-xs">
          <Stat label="Critical" value={critical} bad={critical > 0} />
          <Stat label="Open" value={counts.open} />
          <Stat label="Resolved" value={counts.resolved} />
          <Stat label="Expired" value={counts.expired} bad={counts.expired > 0} />
          <Stat label="Active teams" value={world.vehicles.length} />
          <Stat label="Disruptions" value={activeDis} bad={activeDis > 0} />
          <span className="font-mono text-[#555]">{clock.toLocaleTimeString()}</span>
        </div>
      </header>

      {!connected && <Banner tone="error">Cannot reach the live stream. Check VITE_API_BASE_URL and that the backend is running. Retrying automatically.</Banner>}

      <div className="grid min-h-0 flex-1 gap-3 lg:grid-cols-[20rem_1fr_24rem]">
        <div className="flex min-h-[20rem] min-w-0 flex-col gap-3 lg:min-h-0">
          <ExternalFeed signals={signals} error={signalsError} incidents={incidents} simulationRunning={simulationRunning} />
          <LiveFeed incidents={incidents} escalated={world.urgency_override} selectedId={selected} incidentStatus={world.incident_status} simTimeS={world.sim_time_s}
            onSelect={(i) => { setSelected(i.id); setFocus({ lat: i.lat, lng: i.lng, key: Date.now() }); }} />
        </div>

        <div className="min-h-[26rem] min-w-0 rounded-2xl border border-[#e8e4dc] bg-white p-3 shadow-sm lg:min-h-0">
          <WorldMap mode="readonly" title="Situation map" world={world} incidents={incidents} routes={payload?.routes ?? []}
            approvedRouteKeys={approvedKeys} zones={payload?.zones} selectedId={selected} onSelectIncident={setSelected} focus={focus} />
        </div>

        <div className="flex min-h-[20rem] min-w-0 flex-col gap-3 lg:min-h-0 lg:overflow-y-auto">
          <PlanReview plan={plan} routes={payload?.routes ?? []} onReview={review} />
          <HospitalCapacity world={world} />
          <AlertDrafts alerts={payload?.alerts ?? []} timeline={payload?.activity_log ?? []} />
        </div>
      </div>

      <ExecutionTracker items={world.execution} disruptions={world.disruptions} worldTs={world.ts} onReplan={replan} />
    </div>
  );
}

function Stat({ label, value, bad }: { label: string; value: number; bad?: boolean }) {
  return (
    <span className={`rounded-lg border px-2.5 py-1 ${bad ? 'border-[#f0c4bf] bg-[#fbe9e7] text-[#8c1d17]' : 'border-[#e8e4dc] bg-[#faf8f4] text-[#333]'}`}>
      <b className="mr-1 font-mono">{value}</b>{label}
    </span>
  );
}
