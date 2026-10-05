import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Circle, CircleMarker, MapContainer, Marker, Polygon, Polyline, Popup, TileLayer, Tooltip, useMap } from 'react-leaflet';
import { CITY, DEPOTS, HOSPITALS } from '../../lib/layers';
import { useCityVersion } from '../../lib/useCity';
import { hospitalView, occupancyColor } from '../hospitalLoad';
import { BRIDGES } from '../../data/bridges';
import { TILE_ATTRIBUTION, TILE_URL } from '../../components/map/leaflet';
import { AutoResize, FitToPoints } from '../../components/map/MapHelpers';
import { incidentOpacity, incidentStatusOf, isFeedIncident } from '../incidents';
import { STALE_AFTER_MS, type AffectedRegion, type Disruption, type Incident, type RouteInfo, type WorldState, type WorldVehicle, type Zone } from '../types';
import { URGENCY_COLOR } from '../ui';
import { useNow } from '../useNow';
import { CityAutoSwitch, type CityStatus } from './CityAutoSwitch';
import { PlaceSearch } from './PlaceSearch';
import { dotIcon, glyph } from './icons';

export type MapMode = 'readonly' | 'edit';
export type EntityClick =
  | { type: 'bridge'; id: string; lat: number; lng: number; label: string }
  | { type: 'incident'; id: string; lat: number; lng: number; label: string }
  | { type: 'facility'; id: string; lat: number; lng: number; label: string }
  | { type: 'disruption'; id: string; lat: number; lng: number; label: string };

export interface WorldMapProps {
  mode: MapMode;
  world: WorldState;
  incidents: Incident[];
  routes: RouteInfo[];
  /** keys `${assignment_type}-${assignment_id}` of approved plan items (drawn solid) */
  approvedRouteKeys: Set<string>;
  zones?: Zone[];
  selectedId?: string | null;
  onSelectIncident?: (id: string) => void;
  focus?: { lat: number; lng: number; key: number } | null;
  height?: string;
  title?: string;
  toolbar?: ReactNode;
  /** edit mode only: tools (Leaflet children) and entity click routing */
  children?: ReactNode;
  onEntityClick?: (e: EntityClick) => void;
  vehicleActions?: (v: WorldVehicle) => ReactNode;
  regionActions?: (r: AffectedRegion) => ReactNode;
  disruptionActions?: (d: Disruption) => ReactNode;
  cursor?: string;
  /** Load another city's data when the view settles on it. Pass `false` to lock the city (a run is in progress); omit to turn the feature off. */
  autoCity?: boolean;
}

type LayerKey = 'incidents' | 'regions' | 'disruptions' | 'vehicles' | 'routes' | 'hospitals' | 'depots' | 'bridges' | 'zones';
const LAYERS: { key: LayerKey; label: string; color: string }[] = [
  { key: 'incidents', label: 'Incidents', color: '#b3261e' },
  { key: 'regions', label: 'Affected regions', color: '#2563eb' },
  { key: 'disruptions', label: 'Disruptions', color: '#b3261e' },
  { key: 'vehicles', label: 'Vehicles', color: '#2b6f73' },
  { key: 'routes', label: 'Routes', color: '#2b6f73' },
  { key: 'bridges', label: 'Bridges', color: '#8a6d3b' },
  { key: 'hospitals', label: 'Hospitals', color: '#15803d' },
  { key: 'depots', label: 'Relief centres', color: '#7e22ce' },
  { key: 'zones', label: 'Priority zones', color: '#e07b1a' },
];

const HAZARD_COLOR: Record<string, string> = { flood: '#2563eb', fire: '#ea580c', collapse: '#78716c' };
const KIND_COLOR: Record<string, string> = { rescue: '#2b6f73', medical: '#15803d', logistics: '#7e22ce' };
const KIND_GLYPH: Record<string, string> = { rescue: '▲', medical: '+', logistics: '■' };
const SEV_FILL: Record<string, number> = { low: 0.18, medium: 0.28, high: 0.38, critical: 0.5 };

function FlyTo({ focus }: { focus?: { lat: number; lng: number; key: number } | null }) {
  const map = useMap();
  useEffect(() => {
    if (focus) map.flyTo([focus.lat, focus.lng], Math.max(map.getZoom(), 14), { duration: 0.6 });
  }, [focus, map]);
  return null;
}

export function WorldMap(p: WorldMapProps) {
  const edit = p.mode === 'edit';
  useCityVersion(); // HOSPITALS / DEPOTS / CITY are swapped in place when the city changes
  const [cityStatus, setCityStatus] = useState<CityStatus>(null);
  const [on, setOn] = useState<Record<LayerKey, boolean>>({
    incidents: true, regions: true, disruptions: true, vehicles: true, routes: true, bridges: true, hospitals: true, depots: true, zones: false,
  });
  const [refit, setRefit] = useState(0);
  const [place, setPlace] = useState<{ lat: number; lng: number; label: string; key: number } | null>(null);
  const now = useNow(5000);
  // resolved / expired incidents fade out over INCIDENT_FADE_S sim-seconds, then leave the map
  const incStatus = p.world.incident_status;
  const simT = p.world.sim_time_s;
  const pins = useMemo(
    () => p.incidents.filter((i) => !isFeedIncident(i) && Math.abs(i.lat) > 1 && incidentOpacity(incStatus?.[i.id], simT) > 0),
    [p.incidents, incStatus, simT],
  );
  const fitPoints = useMemo<[number, number][]>(
    () => (pins.length ? pins.map((x) => [x.lat, x.lng] as [number, number]) : [...HOSPITALS, ...DEPOTS].map((f) => [f.lat, f.lng] as [number, number])),
    [pins],
  );
  const click = (e: EntityClick) => { if (edit) p.onEntityClick?.(e); };

  return (
    <div className="flex h-full flex-col">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">{p.title ?? 'Live map'}
          <span className="ml-2 rounded-md bg-[#f1ece1] px-1.5 py-0.5 text-[10px] font-semibold uppercase text-[#6b6b6b]">{edit ? 'Editable' : 'Read-only'}</span>
        </h2>
        <div className="flex items-center gap-2">
          {p.toolbar}
          <button type="button" onClick={() => setRefit((k) => k + 1)} className="rounded-lg border border-[#e8e4dc] bg-white px-2 py-1 text-xs hover:bg-[#faf8f4]">Fit to incidents</button>
        </div>
      </div>

      <div className={`relative min-h-[18rem] flex-1 overflow-hidden rounded-xl border border-[#e8e4dc] ${p.height ?? ''}`} style={p.cursor ? { cursor: p.cursor } : undefined}>
        {cityStatus && (
          <div className={`absolute left-1/2 top-3 z-[1000] max-w-[70%] -translate-x-1/2 rounded-lg border px-3 py-1.5 text-xs shadow-md ${
            cityStatus.kind === 'error' ? 'border-[#f0c4bf] bg-[#fbe9e7] text-[#8c1d17]' : 'border-[#e8e4dc] bg-white text-[#333]'}`} role="status">
            {cityStatus.kind === 'loading' && <span className="mr-1 inline-block animate-pulse">●</span>}{cityStatus.text}
            {cityStatus.kind !== 'loading' && <button type="button" className="ml-2 font-semibold" onClick={() => setCityStatus(null)} aria-label="Dismiss">×</button>}
          </div>
        )}
        <PlaceSearch onPick={(r) => setPlace({ lat: r.lat, lng: r.lng, label: r.label, key: Date.now() })} />
        <MapContainer center={CITY.center} zoom={CITY.zoom} className="h-full w-full" scrollWheelZoom style={p.cursor ? { cursor: p.cursor } : undefined}>
          <TileLayer attribution={TILE_ATTRIBUTION} url={TILE_URL} />
          <AutoResize />
          {p.autoCity !== undefined && <CityAutoSwitch enabled={p.autoCity} onStatus={setCityStatus} />}
          <FitToPoints points={fitPoints} signature={`${pins.length > 0}`} refitKey={refit} />
          <FlyTo focus={p.focus} />
          <FlyTo focus={place} />
          {place && (
            <CircleMarker center={[place.lat, place.lng]} radius={9} pathOptions={{ color: '#2b6f73', weight: 3, fillColor: '#2b6f73', fillOpacity: 0.25 }}>
              <Popup>{place.label}</Popup>
            </CircleMarker>
          )}

          {on.regions && p.world.affected_regions.map((r) => (
            <Polygon key={r.id} positions={r.ring}
              pathOptions={{ color: HAZARD_COLOR[r.type], weight: 2, fillColor: HAZARD_COLOR[r.type], fillOpacity: SEV_FILL[r.severity] ?? 0.3, dashArray: r.growing ? '6 4' : undefined }}>
              <Popup>
                <strong>{r.name}</strong><br />Type {r.type} · severity {r.severity}{r.growing ? ' · growing' : ''}<br />Radius ≈ {r.radius_m} m
                {edit && p.regionActions?.(r)}
              </Popup>
            </Polygon>
          ))}

          {on.bridges && BRIDGES.map((b) => {
            const down = p.world.disruptions.some((d) => d.kind === 'bridge_collapsed' && d.target_id === b.id && d.status === 'active');
            return (
              <Polyline key={b.id} positions={b.geometry}
                pathOptions={{ color: down ? '#b3261e' : '#8a6d3b', weight: down ? 8 : 6, opacity: 0.9, dashArray: down ? '4 8' : undefined }}
                eventHandlers={{ click: () => click({ type: 'bridge', id: b.id, lat: b.geometry[0][0], lng: b.geometry[0][1], label: b.name }) }}>
                <Popup><strong>{b.name}</strong><br />{down ? 'COLLAPSED' : 'Open'}</Popup>
              </Polyline>
            );
          })}

          {on.routes && p.routes.filter((r) => r.geometry.length >= 2).map((r) => {
            const k = `${r.assignment_type}-${r.assignment_id}`;
            const ok = p.approvedRouteKeys.has(k);
            return (
              <Polyline key={k} positions={r.geometry.map(([a, b]) => [a, b] as [number, number])}
                pathOptions={{ color: ok ? KIND_COLOR[r.assignment_type] : '#a8a29e', weight: ok ? 4 : 2, opacity: ok ? 0.85 : 0.6, dashArray: ok ? undefined : '3 6' }}>
                <Popup><strong>{r.assignment_type}</strong> route · {r.assignment_id}<br />{r.distance_km.toFixed(1)} km, ~{Math.round(r.duration_min)} min<br />{ok ? 'Approved' : 'Awaiting approval'}</Popup>
              </Polyline>
            );
          })}

          {on.zones && p.zones?.map((z) => (
            <CircleMarker key={z.id} center={[z.center_lat, z.center_lng]} radius={10 + Math.min(18, Math.sqrt(z.incident_ids.length) * 4)}
              pathOptions={{ color: '#e07b1a', fillColor: '#e07b1a', fillOpacity: 0.15, weight: 2, dashArray: '4 4' }}>
              <Popup><strong>{z.name}</strong><br />{z.severity} · {z.incident_ids.length} incidents</Popup>
            </CircleMarker>
          ))}

          {on.hospitals && HOSPITALS.map((h) => {
            const v = hospitalView(h, p.world);
            const st = v.status;
            const colour = occupancyColor(v.occupancy, st);
            const l = v.load;
            return (
              <Marker key={h.id} position={[h.lat, h.lng]} icon={glyph(st === 'offline' ? '×' : 'H', colour)}
                eventHandlers={{ click: () => click({ type: 'facility', id: h.id, lat: h.lat, lng: h.lng, label: h.name }) }}>
                <Popup>
                  <strong>{h.name}</strong> (simulated)<br />
                  Beds occupied {v.occupied}/{v.capacity} ({Math.round(v.occupancy * 100)}%)
                  {st !== 'open' && <> · <b>{st === 'offline' ? 'OFFLINE' : 'FULL'}</b></>}
                  {v.atCapacity && <> · <b>AT CAPACITY, diverting</b></>}
                  {l && <><br />{l.walk_in_patients} walk-in, {l.incident_patients} incident patients · demand ×{l.demand_multiplier.toFixed(1)}
                    {l.next_discharge_s != null && <><br />Next discharge in {Math.floor(l.next_discharge_s / 60)}m {String(l.next_discharge_s % 60).padStart(2, '0')}s</>}
                    {(l.diverted_out > 0 || l.diverted_in > 0 || l.overflow > 0) && <><br />Diverted {l.diverted_out} out / {l.diverted_in} in{l.overflow > 0 && <> · <b>{l.overflow} overflow</b></>}</>}</>}
                  <br />{h.specialties.join(', ')}
                </Popup>
              </Marker>
            );
          })}
          {on.depots && DEPOTS.map((d) => (
            <Marker key={d.id} position={[d.lat, d.lng]} icon={glyph('R', '#7e22ce')}
              eventHandlers={{ click: () => click({ type: 'facility', id: d.id, lat: d.lat, lng: d.lng, label: d.name }) }}>
              <Popup><strong>{d.name}</strong> (simulated)</Popup>
            </Marker>
          ))}

          {on.incidents && pins.map((i) => {
            const esc = !!p.world.urgency_override[i.id];
            const status = incidentStatusOf(incStatus, i.id);
            return (
              <Marker key={i.id} position={[i.lat, i.lng]} opacity={incidentOpacity(incStatus?.[i.id], simT)}
                icon={dotIcon(i.urgency, { flagged: i.verification?.flagged, selected: p.selectedId === i.id, escalated: esc })}
                zIndexOffset={i.urgency === 'critical' ? 500 : 0}
                eventHandlers={{ click: () => { p.onSelectIncident?.(i.id); click({ type: 'incident', id: i.id, lat: i.lat, lng: i.lng, label: i.id }); } }}>
                <Popup>
                  <strong>{i.id} · {i.urgency} · {i.need_type}</strong>{esc && ' (escalated)'}<br />
                  <span style={{ color: '#6b6b6b' }}>Status: {status === 'assigned' ? 'team assigned' : status}</span><br />
                  {i.text.length > 140 ? `${i.text.slice(0, 140)}…` : i.text}<br />
                  <span style={{ color: '#6b6b6b' }}>{i.location} · {i.source}</span>
                  {i.verification && <><br />Credibility {(i.verification.credibility * 100).toFixed(0)}%{i.verification.flagged ? ' · FLAGGED' : ''}</>}
                </Popup>
              </Marker>
            );
          })}

          {on.disruptions && p.world.disruptions.filter((d) => d.status === 'active').map((d) => {
            const pos = d.latlng ?? d.geometry?.[0];
            if (!pos) return null;
            const label = d.note ?? d.kind;
            const popup = <Popup><strong>{label}</strong><br />Severity {d.severity}{edit && p.disruptionActions?.(d)}</Popup>;
            const handlers = { click: () => click({ type: 'disruption', id: d.id, lat: pos[0], lng: pos[1], label }) };
            if (d.kind === 'road_blocked') {
              return (
                <Circle key={d.id} center={pos} radius={150} eventHandlers={handlers}
                  pathOptions={{ color: '#b3261e', weight: 3, dashArray: '6 5', fillColor: '#b3261e', fillOpacity: 0.2 }}>{popup}</Circle>
              );
            }
            if (d.kind === 'bridge_collapsed') return null; // drawn as the red bridge line
            const g = d.kind === 'site_inaccessible' ? '⛔' : '!';
            return <Marker key={d.id} position={pos} icon={glyph(g, '#b3261e', 22)} eventHandlers={handlers} zIndexOffset={800}>{popup}</Marker>;
          })}

          {on.vehicles && p.world.vehicles.map((v) => {
            const bad = !!v.failed || !!v.needs_replan;
            const src = v.position_source ?? 'reported';
            const ageMs = now - (v.last_update_ts ?? p.world.ts);
            const stale = p.world.ts > 0 && ageMs > STALE_AFTER_MS;
            return (
              <Marker key={v.id} position={[v.lat, v.lng]} zIndexOffset={900}
                icon={glyph(bad ? '!' : KIND_GLYPH[v.kind], v.needs_replan ? '#d97706' : v.failed ? '#b3261e' : KIND_COLOR[v.kind], 24, { estimated: src === 'estimated', stale })}>
                <Tooltip permanent direction="right" offset={[12, 0]} className="!px-1.5 !py-0.5 !text-[10px]">
                  {v.id} · {src === 'estimated' ? 'est.' : 'live'}{stale ? ' · stale' : ''}
                </Tooltip>
                <Popup>
                  <strong>{v.id}</strong> · {v.kind} team<br />Target {v.target_id} · {v.status === 'en_route' ? `ETA ${v.eta_s}s` : 'on scene'}
                  <br />Position: <b>{src}</b>{src === 'estimated' ? ' (from elapsed time)' : ''} · updated {Math.max(0, Math.round(ageMs / 1000))}s ago
                  {stale && <><br /><b style={{ color: '#b45309' }}>No recent update, position may be wrong</b></>}
                  {v.failed && <><br /><b>{v.failed === 'delayed' ? 'Delayed' : 'Broken down'}</b></>}
                  {v.needs_replan && <><br /><b>Blocked, awaiting re-plan</b></>}
                  {edit && p.vehicleActions?.(v)}
                </Popup>
              </Marker>
            );
          })}

          {edit && p.children}
        </MapContainer>
      </div>

      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
        {LAYERS.map((l) => (
          <label key={l.key} className="flex cursor-pointer items-center gap-1.5 text-xs text-[#555]">
            <input type="checkbox" className="accent-[#2b6f73]" checked={on[l.key]} onChange={() => setOn((s) => ({ ...s, [l.key]: !s[l.key] }))} />
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: l.color }} />{l.label}
          </label>
        ))}
      </div>
      <p className="mt-1 text-[11px] text-[#6b6b6b]">
        Pin colour = urgency <span style={{ color: URGENCY_COLOR.critical }}>critical</span> · <span style={{ color: URGENCY_COLOR.high }}>high</span> · <span style={{ color: URGENCY_COLOR.medium }}>medium</span> · low. Red dashed = blocked road / failed asset. Dashed region = growing. Dashed vehicle = estimated position; faded with amber ring = stale.
      </p>
    </div>
  );
}
