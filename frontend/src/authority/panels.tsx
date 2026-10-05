import { useState } from 'react';
import { HOSPITALS } from '../lib/layers';
import { hospitalView, occupancyColor } from '../shared/hospitalLoad';
import { Badge, Button, Card, Chip, StepHeader, URGENCY_COLOR } from '../shared/ui';
import { incidentOpacity, incidentStatusOf } from '../shared/incidents';
import { useNow } from '../shared/useNow';
import { STALE_AFTER_MS } from '../shared/types';
import type { ActivityEvent, AlertDraft, Disruption, ExecutionItem, Incident, IncidentStatusEntry, PlanItem, ReplanAction, ResponsePlan, RouteInfo, WorldState } from '../shared/types';

/* ───────── Weather ───────── */
/** Latest weather alert from the incident stream: rainfall reading and whether it is live or simulated. */
export function WeatherCard({ incidents }: { incidents: Incident[] }) {
  const w = incidents.find((i) => i.source === 'weather');
  if (!w) return null;
  const meta = w.raw_metadata ?? {};
  const precip = typeof meta.precipitation_mm === 'number' ? meta.precipitation_mm : null;
  const simulated = meta.mock === true;
  return (
    <Card className="!p-3">
      <div className="mb-1 flex items-center gap-2">
        <span className="text-xs font-bold">Weather</span>
        <Badge color={URGENCY_COLOR[w.urgency]}>{w.urgency}</Badge>
        <Badge>{simulated ? 'simulated' : 'live reading'}</Badge>
        {precip !== null && <span className="ml-auto font-mono text-xs">{precip} mm</span>}
      </div>
      <p className="text-[11px] leading-snug text-[#555]">{w.text.replace('[SIMULATED WEATHER ALERT] ', '')}</p>
      <p className="mt-1 text-[10px] text-[#6b6b6b]">{w.location}</p>
    </Card>
  );
}

/* ───────── 1. Live feed ───────── */
const URG = ['all', 'critical', 'high', 'medium', 'low'] as const;

export function LiveFeed({ incidents, escalated, selectedId, onSelect, incidentStatus, simTimeS = 0 }: {
  incidents: Incident[]; escalated: Record<string, string>; selectedId: string | null; onSelect: (i: Incident) => void;
  /** lifecycle per incident id (from the sim world); resolved / expired items fade out of the feed */
  incidentStatus?: Record<string, IncidentStatusEntry>; simTimeS?: number;
}) {
  const [tab, setTab] = useState<'citizen' | 'stream'>('citizen');
  const [urg, setUrg] = useState<(typeof URG)[number]>('all');
  const visible = incidents.filter((i) => incidentOpacity(incidentStatus?.[i.id], simTimeS) > 0);
  const citizen = visible.filter((i) => i.source === 'citizen');
  const stream = visible.filter((i) => i.source !== 'citizen');
  const list = (tab === 'citizen' ? citizen : stream).filter((i) => urg === 'all' || i.urgency === urg);
  const order = { critical: 0, high: 1, medium: 2, low: 3 } as const;
  list.sort((a, b) => order[a.urgency] - order[b.urgency] || b.timestamp.localeCompare(a.timestamp));

  return (
    <Card className="flex min-h-0 flex-1 flex-col">
      <StepHeader n={1} title="Live feed" />
      <div className="mb-2 flex gap-1.5">
        <Chip active={tab === 'citizen'} onClick={() => setTab('citizen')}>Citizen reports ({citizen.length})</Chip>
        <Chip active={tab === 'stream'} onClick={() => setTab('stream')}>Incident stream ({stream.length})</Chip>
      </div>
      <div className="mb-2 flex flex-wrap gap-1.5">
        {URG.map((u) => <Chip key={u} active={urg === u} onClick={() => setUrg(u)} tone={u === 'all' ? undefined : URGENCY_COLOR[u]}>{u}</Chip>)}
      </div>
      <ul className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
        {list.length === 0 && <li className="text-xs text-[#6b6b6b]">Nothing here yet.</li>}
        {list.map((i) => {
          const status = incidentStatusOf(incidentStatus, i.id);
          return (
          <li key={i.id} style={{ opacity: incidentOpacity(incidentStatus?.[i.id], simTimeS) }}>
            <button type="button" onClick={() => onSelect(i)}
              className={`w-full rounded-xl border p-2.5 text-left text-xs ${selectedId === i.id ? 'border-[#2b6f73] bg-[#e3eeee]' : 'border-[#e8e4dc] bg-white hover:bg-[#faf8f4]'}`}>
              <div className="mb-1 flex flex-wrap items-center gap-1.5">
                <Badge color={URGENCY_COLOR[i.urgency]}>{i.urgency}</Badge>
                <Badge>{i.need_type}</Badge>
                {escalated[i.id] && <Badge color="#b3261e">escalated</Badge>}
                {status === 'expired' && <Badge color="#6b6b6b">expired</Badge>}
                {status === 'resolved' && <Badge color="#15803d">resolved</Badge>}
                {status === 'assigned' && <Badge color="#2b6f73">team assigned</Badge>}
                {i.verification?.flagged && <Badge color="#b3261e">flagged</Badge>}
                {i.verification && !i.verification.flagged && <span className="text-[11px] text-[#6b6b6b]">cred {(i.verification.credibility * 100).toFixed(0)}%</span>}
                <span className="ml-auto font-mono text-[10px] text-[#9a9a9a]">{i.id}</span>
              </div>
              <p className="text-[13px] text-[#222]">{i.text}</p>
              <p className="mt-0.5 text-[11px] text-[#6b6b6b]">{i.location} · {i.source}</p>
            </button>
          </li>
          );
        })}
      </ul>
    </Card>
  );
}

/* ───────── 2. Plan review (the only place for decisions) ───────── */
export function PlanReview({ plan, routes, onReview }: {
  plan: ResponsePlan | null | undefined; routes: RouteInfo[];
  onReview: (a: { item_id: string; action: string; edited_content?: string }[]) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [err, setErr] = useState<string | null>(null);

  const run = async (a: { item_id: string; action: string; edited_content?: string }[]) => {
    setBusy(true); setErr(null);
    try { await onReview(a); setEditId(null); } catch (e) { setErr(e instanceof Error ? e.message : 'Decision failed'); } finally { setBusy(false); }
  };

  if (!plan) {
    return (
      <Card><StepHeader n={2} title="Plan review" /><p className="text-xs text-[#6b6b6b]">Waiting for a draft plan from the agents.</p></Card>
    );
  }
  const risky = (it: PlanItem) => it.incomplete || routes.some((r) => `${r.assignment_type}-${r.assignment_id}` === it.id && r.blocked_warning);
  const lowRisk = plan.items.filter((i) => i.status === 'pending' && !risky(i));
  const pending = plan.items.filter((i) => i.status === 'pending').length;

  return (
    <Card className="flex min-h-0 flex-1 flex-col">
      <StepHeader n={2} title="Plan review" right={
        <Button small variant="primary" disabled={busy || lowRisk.length === 0 || plan.is_final}
          onClick={() => run(lowRisk.map((i) => ({ item_id: i.id, action: 'approve' })))}>
          Approve all low-risk ({lowRisk.length})
        </Button>} />
      <p className="mb-2 text-[11px] text-[#6b6b6b]">
        {plan.is_final ? 'Plan finalized.' : `${pending} pending. Items on routes crossing flood zones, or incomplete, need individual review.`}
      </p>
      {err && <p className="mb-2 rounded-lg bg-[#fbe9e7] px-2 py-1 text-xs text-[#8c1d17]">{err}</p>}
      <ul className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
        {plan.items.map((it) => (
          <li key={it.id} className="rounded-xl border border-[#e8e4dc] p-2.5 text-xs">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[13px] font-semibold">{it.title}</span>
              <Badge>{it.category}</Badge>
              <Badge color={it.status === 'approved' || it.status === 'edited' ? '#15803d' : it.status === 'rejected' ? '#b3261e' : '#6b6b6b'}>{it.status}</Badge>
              {risky(it) && <Badge color="#b3261e">risk</Badge>}
            </div>
            <p className="mt-1 text-[#444]">{it.edited_content ?? it.description}</p>
            <p className="mt-0.5 italic text-[#8a8a8a]">Why: {it.reasoning}</p>
            {it.status === 'pending' && !plan.is_final && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                <Button small variant="primary" disabled={busy} onClick={() => run([{ item_id: it.id, action: 'approve' }])}>Approve</Button>
                <Button small variant="danger" disabled={busy} onClick={() => run([{ item_id: it.id, action: 'reject' }])}>Reject</Button>
                <Button small disabled={busy} onClick={() => { setEditId(it.id); setText(it.edited_content ?? it.description); }}>Edit</Button>
              </div>
            )}
            {editId === it.id && (
              <div className="mt-2 space-y-1.5">
                <textarea className="w-full rounded-lg border border-[#e8e4dc] bg-white p-2 text-xs focus:border-[#2b6f73] focus:outline-none" rows={3} value={text} onChange={(e) => setText(e.target.value)} />
                <div className="flex gap-1.5">
                  <Button small variant="primary" disabled={busy} onClick={() => run([{ item_id: it.id, action: 'edit', edited_content: text }])}>Save edit</Button>
                  <Button small onClick={() => setEditId(null)}>Cancel</Button>
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function AlertDrafts({ alerts, timeline }: { alerts: AlertDraft[]; timeline: ActivityEvent[] }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const copy = async (a: AlertDraft) => {
    await navigator.clipboard.writeText(`${a.title}\n\n${a.body}`);
    setCopied(a.audience);
    setTimeout(() => setCopied(null), 1500);
  };
  return (
    <Card>
      <button type="button" onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between text-left">
        <span className="text-sm font-semibold">Alert drafts &amp; agent timeline</span>
        <span className="text-xs text-[#6b6b6b]">{open ? 'Hide' : `Show (${alerts.length} drafts)`}</span>
      </button>
      {open && (
        <div className="mt-3 space-y-2">
          <p className="text-[11px] text-[#6b6b6b]">Copy only. Nothing is sent from this prototype.</p>
          {alerts.map((a) => (
            <div key={a.audience} className="rounded-xl border border-[#e8e4dc] p-2.5 text-xs">
              <div className="flex items-center justify-between"><Badge color="#2b6f73">{a.audience.replace('_', ' ')}</Badge>
                <Button small onClick={() => copy(a)}>{copied === a.audience ? 'Copied' : 'Copy'}</Button></div>
              <p className="mt-1 font-semibold">{a.title}</p><p className="whitespace-pre-wrap text-[#555]">{a.body}</p>
            </div>
          ))}
          <ul className="max-h-40 space-y-1 overflow-y-auto text-[11px] text-[#555]">
            {timeline.map((e, i) => <li key={i}><b className="capitalize">{e.agent}</b> · {e.status} · {e.summary}</li>)}
          </ul>
        </div>
      )}
    </Card>
  );
}

/* ───────── 3. Execution tracker + field problems ───────── */
const STATUS_COLOR: Record<ExecutionItem['status'], string> = {
  queued: '#6b6b6b', en_route: '#2b6f73', on_site: '#15803d', done: '#15803d', blocked: '#b3261e', failed: '#b3261e',
};

export function ExecutionTracker({ items, disruptions, worldTs, onReplan }: {
  items: ExecutionItem[]; disruptions: Disruption[]; worldTs: number; onReplan: (item_id: string, action: ReplanAction) => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const now = useNow(5000);
  const problems = items.filter((i) => i.problem);
  const active = disruptions.filter((d) => d.status === 'active');
  const act = async (id: string, a: ReplanAction) => { setBusy(id); try { await onReplan(id, a); } finally { setBusy(null); } };

  return (
    <Card>
      <StepHeader n={3} title="Execution tracker" right={<span className="text-xs text-[#6b6b6b]">{items.length} approved · {problems.length} with problems</span>} />
      <div className="grid gap-3 lg:grid-cols-2">
        <ul className="max-h-48 space-y-2 overflow-y-auto pr-1">
          {items.length === 0 && <li className="text-xs text-[#6b6b6b]">Approved items appear here with live progress from the field.</li>}
          {items.map((it) => {
            const live = it.status === 'en_route' || it.status === 'blocked' || it.status === 'on_site';
            const ageS = Math.max(0, Math.round((now - (it.last_update_ts ?? worldTs)) / 1000));
            const stale = live && worldTs > 0 && ageS * 1000 > STALE_AFTER_MS;
            return (
            <li key={it.item_id} className="rounded-xl border border-[#e8e4dc] p-2 text-xs">
              <div className="flex items-center gap-1.5">
                <span className="font-semibold">{it.title}</span>
                <Badge color={STATUS_COLOR[it.status]}>{it.status.replace('_', ' ')}</Badge>
                {live && it.position_source === 'estimated' && <Badge color="#6b6b6b">estimated</Badge>}
                {stale && <Badge color="#b45309">no update {ageS}s</Badge>}
                {it.vehicle_id && <span className="font-mono text-[10px] text-[#8a8a8a]">{it.vehicle_id}</span>}
                <span className="ml-auto text-[#6b6b6b]">{it.status === 'en_route' || it.status === 'blocked' ? `ETA ${it.eta_s}s` : ''}</span>
              </div>
              <div className="mt-1.5 h-1.5 overflow-hidden rounded bg-[#eee9df]">
                <div className="h-full transition-all" style={{ width: `${Math.round(it.progress * 100)}%`, background: STATUS_COLOR[it.status] }} />
              </div>
            </li>
            );
          })}
        </ul>
        <div className="max-h-48 space-y-2 overflow-y-auto pr-1">
          <h3 className="text-xs font-semibold text-[#8c1d17]">Field problems reported by simulation</h3>
          {problems.length === 0 && active.length === 0 && <p className="text-xs text-[#6b6b6b]">No problems reported.</p>}
          {problems.map((it) => (
            <div key={it.item_id} className="rounded-xl border border-[#f0c4bf] bg-[#fbe9e7] p-2.5 text-xs text-[#8c1d17]">
              <p className="font-semibold">{it.title}: {it.problem}</p>
              {it.status === 'blocked' && it.problem !== 'Vehicle broke down' && it.problem !== 'Vehicle delayed' && (
                <div className="mt-1.5 flex gap-1.5">
                  <Button small variant="danger" disabled={busy === it.item_id} onClick={() => act(it.item_id, 'reroute')}>Approve re-route</Button>
                  <Button small disabled={busy === it.item_id} onClick={() => act(it.item_id, 'hold')}>Hold</Button>
                </div>
              )}
            </div>
          ))}
          {active.map((d) => (
            <div key={d.id} className="rounded-lg border border-[#e8e4dc] bg-white px-2 py-1 text-[11px] text-[#555]">
              <Badge color="#b3261e">{d.kind.replace('_', ' ')}</Badge> {d.note}
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}


/* ───────── Hospital capacity ───────── */
/** Live occupancy of every hospital, from the same census the simulator and the Medical Agent use. */
export function HospitalCapacity({ world }: { world: Pick<WorldState, 'beds' | 'hospital_status' | 'hospital_load'> }) {
  const views = HOSPITALS.map((h) => ({ h, v: hospitalView(h, world) })).sort((a, b) => b.v.occupancy - a.v.occupancy);
  const occupied = views.reduce((n, { v }) => n + v.occupied, 0);
  const capacity = views.reduce((n, { v }) => n + v.capacity, 0);
  const overflow = views.reduce((n, { v }) => n + (v.load?.overflow ?? 0), 0);
  return (
    <Card>
      <StepHeader n={4} title="Hospital capacity" right={<span className="text-xs text-[#6b6b6b]">{occupied}/{capacity} beds occupied{overflow > 0 && <b className="ml-1 text-[#b3261e]">· {overflow} overflow</b>}</span>} />
      <ul className="max-h-48 space-y-2 overflow-y-auto pr-1">
        {views.map(({ h, v }) => (
          <li key={h.id} className="text-xs">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate">{h.name}</span>
              <span className="flex shrink-0 items-center gap-1.5">
                {v.status !== 'open' && <Badge color={v.status === 'offline' ? '#57534e' : '#b3261e'}>{v.status}</Badge>}
                {v.atCapacity && <Badge color="#b3261e">diverting</Badge>}
                <span className="font-mono text-[#6b6b6b]">{v.occupied}/{v.capacity}</span>
              </span>
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded bg-[#eee9df]">
              <div className="h-full transition-all" style={{ width: `${Math.round(v.occupancy * 100)}%`, background: occupancyColor(v.occupancy, v.status) }} />
            </div>
          </li>
        ))}
      </ul>
    </Card>
  );
}
