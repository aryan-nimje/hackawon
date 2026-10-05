import { useCallback, useState } from 'react';
import { WorldMap, type EntityClick } from '../shared/map/WorldMap';
import { Banner, Button } from '../shared/ui';
import { MapTools, type Tool, type ToolOptions } from './tools/MapTools';
import { EMPTY_DRAFT, EventLog, HospitalBeds, IncidentForm, ScenarioControls, ScoreCard, TeamList, ToolPalette, fmtClock, type IncidentDraft } from './Panels';
import { makePresets } from './presets';
import { useSimulation } from './useSimulation';

export default function SimApp() {
  const sim = useSimulation();
  const [tool, setTool] = useState<Tool>('select');
  const [opts, setOpts] = useState<ToolOptions>({ roadState: 'blocked', hazard: 'flood', severity: 'high', growing: true });
  const [pending, setPending] = useState<[number, number] | null>(null);
  const [draft, setDraft] = useState<IncidentDraft>(EMPTY_DRAFT);
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const started = sim.runId != null;

  const cancel = useCallback(() => { setTool('select'); setPending(null); }, []);

  const onEntity = (e: EntityClick) => {
    if (tool === 'bridge' && e.type === 'bridge') sim.act((w) => w.toggleBridge(e.id));
    else if (tool === 'site' && (e.type === 'incident' || e.type === 'facility')) sim.act((w) => w.toggleSite(e.id, e.lat, e.lng, e.label));
  };

  const submitIncident = async () => {
    if (!pending || adding) return;
    setAdding(true);
    setAddError(null);
    try {
      await sim.addIncident({
        text: draft.text.trim(), location: `${pending[0].toFixed(4)}, ${pending[1].toFixed(4)} (sim)`, lat: pending[0], lng: pending[1],
        need_type: draft.need_type, urgency: draft.urgency, people: draft.people,
      });
      setPending(null);
      setDraft(EMPTY_DRAFT);
    } catch (e) {
      setAddError(e instanceof Error ? e.message : 'Could not add the incident');  // keep the form open so nothing typed is lost
    } finally {
      setAdding(false);
    }
  };

  const cursor = tool === 'road' || tool === 'incident' || tool === 'region' ? 'crosshair' : tool === 'bridge' || tool === 'site' ? 'pointer' : undefined;
  const routes = sim.payload?.routes ?? [];

  return (
    <div className="min-h-screen p-3 lg:p-4">
      <header className="mb-3 flex flex-wrap items-center gap-3 rounded-2xl border border-[#e8e4dc] bg-white px-4 py-2.5 shadow-sm">
        <div>
          <h1 className="text-base font-bold">Disaster simulator <span className="ml-2 rounded-md bg-[#e3eeee] px-1.5 py-0.5 align-middle text-[10px] font-semibold uppercase text-[#1f4f52]">Simulation</span></h1>
          <p className="text-[11px] text-[#6b6b6b]">Plays the outside world against the agents. The authority sees every change live.</p>
        </div>
        <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${sim.connected ? 'bg-[#e3eeee] text-[#1f4f52]' : 'bg-[#fbe9e7] text-[#8c1d17]'}`}>{sim.connected ? '● Connected' : '○ Reconnecting…'}</span>
        <div className="ml-auto flex flex-wrap items-center gap-2 text-xs">
          <span className="rounded-lg border border-[#e8e4dc] bg-[#faf8f4] px-2.5 py-1" title="Reports still awaiting a team or being handled"><b className="mr-1 font-mono">{sim.score.incidents_open}</b>Open</span>
          <span className="rounded-lg border border-[#e8e4dc] bg-[#faf8f4] px-2.5 py-1" title="Teams finished on scene"><b className="mr-1 font-mono">{sim.score.incidents_resolved}</b>Resolved</span>
          <span className={`rounded-lg border px-2.5 py-1 ${sim.score.incidents_expired > 0 ? 'border-[#f0c4bf] bg-[#fbe9e7] text-[#8c1d17]' : 'border-[#e8e4dc] bg-[#faf8f4]'}`} title="No team assigned before the deadline"><b className="mr-1 font-mono">{sim.score.incidents_expired}</b>Expired</span>
          <span className="font-mono text-[#555]">⏱ {fmtClock(sim.snap.t)}{started && !sim.running && ' · paused'}</span>
        </div>
      </header>

      {!sim.connected && <div className="mb-3"><Banner tone="error">Cannot reach the live stream. Check VITE_API_BASE_URL. You can still place faults; they publish once the backend is back.</Banner></div>}
      {sim.startError && <div className="mb-3"><Banner tone="error">{sim.startError}</Banner></div>}
      {sim.pubError && <div className="mb-3"><Banner tone="warn">Could not publish world state: {sim.pubError}</Banner></div>}

      <div className="grid gap-3 lg:grid-cols-[19rem_1fr_20rem]">
        <div className="space-y-3">
          <ScenarioControls started={started} starting={sim.starting} running={sim.running} speed={sim.speed}
            autoReplan={sim.autoReplan} escalateAfterS={sim.escalateAfterS}
            onStart={sim.start} onTogglePlay={() => sim.setRunning(!sim.running)} onReset={() => { sim.reset(); cancel(); }}
            onSpeed={sim.setSpeed} onAutoReplan={sim.setAutoReplan} onEscalate={sim.setEscalateAfterS}
            onPreset={(id) => { const p = makePresets().find((x) => x.id === id); if (p) sim.act((w) => w.loadPreset(p)); }} />
          <ToolPalette tool={tool} onTool={(t) => { setTool(t); setPending(null); }} opts={opts} onOpts={setOpts} />
          {pending && <IncidentForm at={pending} draft={draft} onDraft={setDraft} onSubmit={submitIncident} onCancel={() => { setPending(null); setAddError(null); }}
            busy={adding} error={addError} canSubmit={started} />}
          <div className="flex gap-2">
            <Button small disabled={!sim.snap.canUndo} onClick={() => sim.act((w) => w.undo())}>↶ Undo</Button>
            <Button small variant="danger" onClick={() => sim.act((w) => w.clearFaults())}>Clear all faults</Button>
          </div>
        </div>

        <div className="h-[34rem] rounded-2xl border border-[#e8e4dc] bg-white p-3 shadow-sm lg:h-[44rem]">
          <WorldMap mode="edit" title="Simulated world" autoCity={!started} world={sim.snap} incidents={sim.incidents} routes={routes}
            approvedRouteKeys={sim.approvedKeys} zones={sim.payload?.zones} cursor={cursor} onEntityClick={onEntity}
            vehicleActions={(v) => (
              <div className="mt-2 flex flex-wrap gap-1">
                <Button small variant="danger" onClick={() => sim.act((w) => w.failVehicle(v.id, 'stop'))}>Stop</Button>
                <Button small variant="danger" onClick={() => sim.act((w) => w.failVehicle(v.id, 'vanish'))}>Vanish</Button>
                <Button small onClick={() => sim.act((w) => w.failVehicle(v.id, 'delay', 120))}>Delay 2m</Button>
                <Button small variant="primary" onClick={() => sim.act((w) => w.restoreVehicle(v.id))}>Restore</Button>
              </div>
            )}
            regionActions={(r) => r.id.startsWith('region-') ? (
              <div className="mt-2"><Button small variant="danger" onClick={() => sim.act((w) => w.removeRegion(r.id))}>Remove region</Button></div>
            ) : null}
            disruptionActions={(d) => (
              <div className="mt-2"><Button small variant="primary" onClick={() => sim.act((w) => w.clearDisruption(d.id))}>Clear</Button></div>
            )}>
            <MapTools tool={tool} opts={opts} routes={routes} pendingIncident={pending} onCancel={cancel}
              onRoad={(la, ln, s) => sim.act((w) => w.blockRoad(la, ln, s))}
              onPickIncident={(la, ln) => setPending([la, ln])}
              onRegion={(la, ln, r) => sim.act((w) => w.addRegion(opts.hazard, la, ln, r, opts.severity, opts.growing))} />
          </WorldMap>
        </div>

        <div className="space-y-3">
          <ScoreCard score={sim.score} />
          <HospitalBeds world={sim.snap} diverted={sim.snap.diverted}
            onAdjust={(id, d) => sim.act((w) => w.setBeds(id, (sim.snap.beds[id] ?? 0) + d))}
            onFull={(id, full) => sim.act((w) => w.markFull(id, full))}
            onOffline={(id, off) => sim.act((w) => w.setOffline(id, off))} />
          <TeamList teams={sim.snap.vehicles} />
          <EventLog events={sim.snap.events} />
        </div>
      </div>
    </div>
  );
}
