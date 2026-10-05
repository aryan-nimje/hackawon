import { HOSPITALS } from '../lib/layers';
import { Badge, Button, Card, Chip, StepHeader } from '../shared/ui';
import { hospitalView, occupancyColor } from '../shared/hospitalLoad';
import type { HazardType, SimEvent, SimScore, WorldState, WorldVehicle } from '../shared/types';
import type { Tool, ToolOptions } from './tools/MapTools';
import { makePresets } from './presets';

export function fmtClock(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
}

/* ───────── scenario controls ───────── */
const SPEEDS = [1, 2, 5, 10];

export function ScenarioControls(p: {
  started: boolean; starting: boolean; running: boolean; speed: number; autoReplan: boolean; escalateAfterS: number;
  onStart: () => void; onTogglePlay: () => void; onReset: () => void; onSpeed: (s: number) => void;
  onAutoReplan: (v: boolean) => void; onEscalate: (s: number) => void; onPreset: (id: string) => void;
}) {
  return (
    <Card>
      <StepHeader n={1} title="Scenario" />
      <div className="flex flex-wrap gap-2">
        {!p.started ? (
          <Button variant="primary" disabled={p.starting} onClick={p.onStart}>{p.starting ? 'Starting…' : '▶ Start simulation'}</Button>
        ) : (
          <>
            <Button variant="primary" onClick={p.onTogglePlay}>{p.running ? '⏸ Pause' : '▶ Resume'}</Button>
            <Button onClick={p.onReset}>↺ Reset</Button>
          </>
        )}
      </div>
      <div className="mt-3">
        <span className="mb-1 block text-xs text-[#6b6b6b]">World speed</span>
        <div className="flex gap-1.5">{SPEEDS.map((s) => <Chip key={s} active={p.speed === s} onClick={() => p.onSpeed(s)}>{s}×</Chip>)}</div>
      </div>
      <label className="mt-3 block text-xs text-[#6b6b6b]">Load scenario preset
        <select className="mt-1 block w-full rounded-lg border border-[#e8e4dc] bg-white px-2 py-2 text-sm" defaultValue="" onChange={(e) => { if (e.target.value) { p.onPreset(e.target.value); e.target.value = ''; } }}>
          <option value="">Choose…</option>
          {makePresets().map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
        </select>
      </label>
      <label className="mt-3 flex items-center gap-2 text-xs text-[#444]">
        Escalate unattended incidents after
        <input type="number" min={20} step={10} value={p.escalateAfterS} onChange={(e) => p.onEscalate(Math.max(20, Number(e.target.value) || 120))}
          className="w-16 rounded-lg border border-[#e8e4dc] px-2 py-1 text-xs" /> sim-s
      </label>
      <label className="mt-2 flex cursor-pointer items-center gap-2 text-xs text-[#444]" title="Off: blocked vehicles wait for the authority to approve a re-route.">
        <input type="checkbox" className="accent-[#2b6f73]" checked={p.autoReplan} onChange={(e) => p.onAutoReplan(e.target.checked)} />
        Auto re-route around blockages (skip authority)
      </label>
    </Card>
  );
}

/* ───────── tool palette ───────── */
const TOOLS: { id: Tool; label: string; hint: string }[] = [
  { id: 'select', label: 'Select', hint: 'Click a vehicle, region or fault marker for actions.' },
  { id: 'road', label: 'Block road', hint: 'Click anywhere on a road. It snaps to planned routes.' },
  { id: 'bridge', label: 'Break bridge', hint: 'Click a bridge (brown line) to collapse or restore it.' },
  { id: 'site', label: 'Block site', hint: 'Click an incident or facility to toggle “entry not possible”.' },
  { id: 'incident', label: 'Add incident', hint: 'Click the map, then fill in the form.' },
  { id: 'region', label: 'Affected region', hint: 'Click and drag to size the region (click = 300 m).' },
];

export function ToolPalette({ tool, onTool, opts, onOpts }: { tool: Tool; onTool: (t: Tool) => void; opts: ToolOptions; onOpts: (o: ToolOptions) => void }) {
  const hint = TOOLS.find((t) => t.id === tool)?.hint;
  return (
    <Card>
      <StepHeader n={2} title="Map tools" />
      <div className="flex flex-wrap gap-1.5">{TOOLS.map((t) => <Chip key={t.id} active={tool === t.id} onClick={() => onTool(t.id)}>{t.label}</Chip>)}</div>
      <p className="mt-2 text-[11px] text-[#6b6b6b]">{hint} Press Esc to cancel.</p>
      {tool === 'road' && (
        <div className="mt-2 flex gap-1.5">
          {(['blocked', 'flooded'] as const).map((s) => <Chip key={s} active={opts.roadState === s} onClick={() => onOpts({ ...opts, roadState: s })}>{s}</Chip>)}
        </div>
      )}
      {tool === 'region' && (
        <div className="mt-2 space-y-2">
          <div className="flex gap-1.5">{(['flood', 'fire', 'collapse'] as HazardType[]).map((h) => <Chip key={h} active={opts.hazard === h} onClick={() => onOpts({ ...opts, hazard: h })}>{h}</Chip>)}</div>
          <div className="flex flex-wrap gap-1.5">{(['low', 'medium', 'high', 'critical'] as const).map((s) => <Chip key={s} active={opts.severity === s} onClick={() => onOpts({ ...opts, severity: s })}>{s}</Chip>)}</div>
          <label className="flex items-center gap-2 text-xs"><input type="checkbox" className="accent-[#2b6f73]" checked={opts.growing} onChange={(e) => onOpts({ ...opts, growing: e.target.checked })} />Growing (expands and floods roads)</label>
        </div>
      )}
    </Card>
  );
}

/* ───────── add-incident form ───────── */
export interface IncidentDraft { text: string; need_type: 'rescue' | 'medical' | 'shelter' | 'supplies' | 'evacuation'; urgency: 'low' | 'medium' | 'high' | 'critical'; people: number }
export const EMPTY_DRAFT: IncidentDraft = { text: '', need_type: 'rescue', urgency: 'high', people: 1 };

export function IncidentForm({ at, draft, onDraft, onSubmit, onCancel, busy = false, error = null, canSubmit = true }: {
  at: [number, number]; draft: IncidentDraft; onDraft: (d: IncidentDraft) => void; onSubmit: () => void; onCancel: () => void;
  busy?: boolean; error?: string | null; canSubmit?: boolean;
}) {
  return (
    <Card>
      <StepHeader n="+" title="New incident" />
      <p className="mb-2 font-mono text-[11px] text-[#6b6b6b]">{at[0].toFixed(4)}, {at[1].toFixed(4)}</p>
      <div className="space-y-2 text-xs">
        <textarea rows={2} placeholder="What is happening?" value={draft.text} onChange={(e) => onDraft({ ...draft, text: e.target.value })}
          className="w-full rounded-lg border border-[#e8e4dc] p-2 text-sm focus:border-[#2b6f73] focus:outline-none" />
        <div className="flex flex-wrap gap-1.5">{(['rescue', 'medical', 'shelter', 'supplies', 'evacuation'] as const).map((n) => <Chip key={n} active={draft.need_type === n} onClick={() => onDraft({ ...draft, need_type: n })}>{n}</Chip>)}</div>
        <div className="flex flex-wrap gap-1.5">{(['low', 'medium', 'high', 'critical'] as const).map((u) => <Chip key={u} active={draft.urgency === u} onClick={() => onDraft({ ...draft, urgency: u })}>{u}</Chip>)}</div>
        <label className="flex items-center gap-2">People affected
          <input type="number" min={1} value={draft.people} onChange={(e) => onDraft({ ...draft, people: Math.max(1, Number(e.target.value) || 1) })} className="w-16 rounded-lg border border-[#e8e4dc] px-2 py-1" /></label>
        <div className="flex gap-1.5"><Button small variant="primary" disabled={!draft.text.trim() || busy || !canSubmit} onClick={onSubmit}>{busy ? 'Adding…' : 'Add incident'}</Button><Button small onClick={onCancel}>Cancel</Button></div>
        {!canSubmit && <p className="text-[11px] text-[#8c1d17]">Start the simulation first: the backend plans every incident inside a run.</p>}
        {error && <p className="text-[11px] text-[#8c1d17]">{error}</p>}
      </div>
    </Card>
  );
}

/* ───────── side panels ───────── */
function Meter({ label, value, total, good }: { label: string; value: number; total: number; good: 'high' | 'low' }) {
  const pct = total ? Math.round((value / total) * 100) : 0;
  const ok = good === 'high' ? pct >= 70 : pct <= 10;
  const mid = good === 'high' ? pct >= 40 : pct <= 25;
  const color = total === 0 ? '#c9c4b8' : ok ? '#15803d' : mid ? '#d9a406' : '#b3261e';
  return (
    <div>
      <div className="flex justify-between text-xs"><span>{label}</span><span className="font-mono">{value}/{total}</span></div>
      <div className="mt-1 h-1.5 overflow-hidden rounded bg-[#eee9df]"><div className="h-full transition-all" style={{ width: `${total ? pct : 0}%`, background: color }} /></div>
    </div>
  );
}

export function ScoreCard({ score }: { score: SimScore }) {
  return (
    <Card>
      <StepHeader n={3} title="Score card" />
      <div className="space-y-2.5">
        <Meter label="Junk reports caught" value={score.junk_caught} total={score.junk_total} good="high" />
        <Meter label="Genuine wrongly flagged" value={score.false_flags} total={score.genuine_total} good="low" />
        <Meter label="Critical cases served" value={score.critical_served} total={score.critical_total} good="high" />
      </div>
      <dl className="mt-3 grid grid-cols-3 gap-2 text-center">
        {[['Open', String(score.incidents_open)], ['Resolved', String(score.incidents_resolved)], ['Expired', String(score.incidents_expired)],
          ['Avg response', score.avg_response_s == null ? '—' : fmtClock(score.avg_response_s)], ['Unsafe routes', String(score.unsafe_routes)], ['Hosp. overflow', String(score.hospital_overflow)]].map(([l, v]) => (
          <div key={l} className="rounded-lg bg-[#faf8f4] px-1 py-2"><dd className="font-mono text-base">{v}</dd><dt className="text-[10px] text-[#6b6b6b]">{l}</dt></div>
        ))}
      </dl>
    </Card>
  );
}

export function HospitalBeds({ world, diverted, onAdjust, onFull, onOffline }: {
  world: Pick<WorldState, 'beds' | 'hospital_status' | 'hospital_load'>;
  diverted: number;
  onAdjust: (id: string, delta: number) => void;
  onFull: (id: string, full: boolean) => void;
  onOffline: (id: string, offline: boolean) => void;
}) {
  const views = HOSPITALS.map((h) => ({ h, v: hospitalView(h, world) }));
  const occupied = views.reduce((n, { v }) => n + v.occupied, 0);
  const capacity = views.reduce((n, { v }) => n + v.capacity, 0);
  return (
    <Card>
      <StepHeader n={4} title="Hospital beds" right={diverted > 0 ? <Badge color="#7e22ce">{diverted} diverted</Badge> : undefined} />
      <p className="mb-1 text-[11px] text-[#6b6b6b]">
        Occupied beds over capacity ({occupied}/{capacity} city-wide). Every patient keeps a bed until their length of stay ends, then it is freed.
        Walk-in demand rises with disaster severity and near hazards. A full or offline hospital sends new patients to the nearest one with room; if none has room they overflow.
      </p>
      <ul className="max-h-80 space-y-2.5 overflow-y-auto pr-1">
        {views.map(({ h, v }) => {
          const st = v.status;
          const l = v.load;
          return (
            <li key={h.id} className={st === 'offline' ? 'opacity-70' : ''}>
              <div className="flex items-center justify-between gap-2 text-xs">
                <span className="truncate">{h.name}</span>
                <span className="flex shrink-0 items-center gap-1.5">
                  {st !== 'open' && <Badge color={st === 'offline' ? '#57534e' : '#b3261e'}>{st}</Badge>}
                  {v.atCapacity && <Badge color="#b3261e">at capacity</Badge>}
                  <span className="font-mono text-[#6b6b6b]">{v.occupied}/{v.capacity}</span>
                </span>
              </div>
              <div className="mt-1 h-1.5 overflow-hidden rounded bg-[#eee9df]"><div className="h-full transition-all" style={{ width: `${Math.round(v.occupancy * 100)}%`, background: occupancyColor(v.occupancy, st) }} /></div>
              {l && (
                <p className="mt-0.5 text-[10px] text-[#6b6b6b]">
                  {l.walk_in_patients} walk-in · {l.incident_patients} incident{l.surge_patients > 0 && ` · ${l.surge_patients} held`} · demand ×{l.demand_multiplier.toFixed(1)}
                  {l.next_discharge_s != null && ` · next discharge ${fmtClock(l.next_discharge_s)}`}
                  {(l.diverted_out > 0 || l.diverted_in > 0) && ` · diverted ${l.diverted_out} out / ${l.diverted_in} in`}
                  {l.overflow > 0 && <b className="text-[#b3261e]"> · {l.overflow} overflow</b>}
                </p>
              )}
              <div className="mt-1 flex flex-wrap gap-1">
                <Button small disabled={st !== 'open' || v.free <= 0} onClick={() => onAdjust(h.id, -5)} aria-label={`Hold 5 free beds at ${h.name}`}>−5 free</Button>
                <Button small disabled={st !== 'open' || v.free >= v.capacity} onClick={() => onAdjust(h.id, 5)} aria-label={`Free up 5 beds at ${h.name}`}>+5 free</Button>
                <Button small variant={st === 'full' ? 'primary' : 'secondary'} disabled={st === 'offline'} onClick={() => onFull(h.id, st !== 'full')}>{st === 'full' ? 'Reopen' : 'Mark full'}</Button>
                <Button small variant={st === 'offline' ? 'primary' : 'danger'} onClick={() => onOffline(h.id, st !== 'offline')}>{st === 'offline' ? 'Bring online' : 'Take offline'}</Button>
              </div>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

export function TeamList({ teams }: { teams: WorldVehicle[] }) {
  return (
    <Card>
      <StepHeader n={5} title={`Field teams (${teams.length})`} />
      {teams.length === 0 ? <p className="text-xs text-[#6b6b6b]">No teams on the road yet. They appear once the authority approves a rescue, medical or supply item.</p> : (
        <ul className="max-h-40 space-y-1 overflow-y-auto text-xs">
          {teams.map((t) => (
            <li key={t.id} className="flex items-center justify-between rounded-lg bg-[#faf8f4] px-2 py-1">
              <span><span className="font-mono text-[#6b6b6b]">{t.id}</span> {t.kind} → {t.target_id}</span>
              {t.failed ? <Badge color="#b3261e">{t.failed}</Badge> : t.needs_replan ? <Badge color="#d97706">blocked</Badge> : <span className="text-[#6b6b6b]">{t.status === 'en_route' ? `ETA ${t.eta_s}s` : 'on scene'}</span>}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

const KIND_STYLE: Record<SimEvent['kind'], string> = {
  info: '#555', flood: '#2563eb', dispatch: '#2b6f73', arrive: '#15803d', bed: '#7e22ce', citizen: '#b45309', warn: '#b3261e',
};

export function EventLog({ events }: { events: SimEvent[] }) {
  const shown = [...events].reverse().slice(0, 80);
  return (
    <Card>
      <StepHeader n={6} title="World event log" />
      <ul className="max-h-60 space-y-1 overflow-y-auto font-mono text-[11px]">
        {shown.map((e, i) => <li key={`${e.at_s}-${i}`} style={{ color: KIND_STYLE[e.kind] }}><span className="mr-2 text-[#9a9a9a]">{fmtClock(e.at_s)}</span>{e.text}</li>)}
      </ul>
    </Card>
  );
}
