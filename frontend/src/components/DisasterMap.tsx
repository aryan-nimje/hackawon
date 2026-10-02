import { useMemo } from 'react';
import { MapContainer, TileLayer, Marker, Popup, Polygon, Polyline, CircleMarker } from 'react-leaflet';
import L from 'leaflet';
import type { Incident, RouteInfo, RunState, Severity } from '../types';
import { blockedZones } from '../data/blockedZones';

import 'leaflet/dist/leaflet.css';

// Fix default marker icons in bundlers
import iconRetina from 'leaflet/dist/images/marker-icon-2x.png';
import icon from 'leaflet/dist/images/marker-icon.png';
import iconShadow from 'leaflet/dist/images/marker-shadow.png';

delete (L.Icon.Default.prototype as unknown as { _getIconUrl?: unknown })._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: iconRetina,
  iconUrl: icon,
  shadowUrl: iconShadow,
});

const severityColor: Record<Severity, string> = {
  Low: '#22c55e',
  Medium: '#eab308',
  High: '#f97316',
  Critical: '#ef4444',
};

const urgencyColor: Record<string, string> = {
  critical: '#ef4444',
  high: '#f97316',
  medium: '#eab308',
  low: '#64748b',
};

interface DisasterMapProps {
  run: RunState | null;
  incidents: Incident[];
  loading: boolean;
  error: string | null;
}

const HOSPITALS = [
  { id: 'h001', name: 'Memorial Hermann-TMC', lat: 29.7074, lng: -95.3978 },
  { id: 'h002', name: 'Houston Methodist', lat: 29.7097, lng: -95.3984 },
  { id: 'h003', name: 'Ben Taub Hospital', lat: 29.7372, lng: -95.3618 },
];

const WAREHOUSES = [
  { id: 'w001', name: 'Houston Food Bank', lat: 29.8123, lng: -95.3234 },
  { id: 'w002', name: 'Red Cross NRG Park', lat: 29.6847, lng: -95.4107 },
];

export function DisasterMap({ run, incidents, loading, error }: DisasterMapProps) {
  const routes: RouteInfo[] = run?.routes ?? [];

  const floodPolygons = useMemo(
    () =>
      blockedZones.features.map((f) => ({
        name: f.properties.name ?? 'Flooded zone',
        positions: f.geometry.coordinates[0].map(([lng, lat]) => [lat, lng] as [number, number]),
      })),
    [],
  );

  if (error) {
    return (
      <div className="flex h-80 items-center justify-center rounded-xl border border-disaster-border bg-disaster-panel text-sm text-red-300">
        Map unavailable: {error}
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-disaster-border bg-disaster-panel p-4">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">
        Live Map — Houston (Simulated)
      </h2>
      {loading && !run && (
        <p className="mb-2 text-xs text-slate-500">Loading map data…</p>
      )}
      <div className="h-80 overflow-hidden rounded-lg">
        <MapContainer center={[29.7604, -95.3698]} zoom={11} className="h-full w-full">
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          />

          {floodPolygons.map((poly) => (
            <Polygon
              key={poly.name}
              positions={poly.positions}
              pathOptions={{ color: '#3b82f6', fillColor: '#1d4ed8', fillOpacity: 0.35 }}
            >
              <Popup>{poly.name}</Popup>
            </Polygon>
          ))}

          {run?.zones.map((zone) => (
            <CircleMarker
              key={zone.id}
              center={[zone.center_lat, zone.center_lng]}
              radius={12}
              pathOptions={{
                color: severityColor[zone.severity],
                fillColor: severityColor[zone.severity],
                fillOpacity: 0.5,
              }}
            >
              <Popup>
                <strong>{zone.name}</strong>
                <br />
                Severity: {zone.severity}
              </Popup>
            </CircleMarker>
          ))}

          {incidents.map((inc) => (
            <Marker key={inc.id} position={[inc.lat, inc.lng]}>
              <Popup>
                <strong>{inc.id}</strong> — {inc.urgency}
                <br />
                {inc.text.slice(0, 80)}…
              </Popup>
            </Marker>
          ))}

          {HOSPITALS.map((h) => (
            <CircleMarker
              key={h.id}
              center={[h.lat, h.lng]}
              radius={8}
              pathOptions={{ color: '#22c55e', fillColor: '#166534', fillOpacity: 0.8 }}
            >
              <Popup>{h.name} (Hospital)</Popup>
            </CircleMarker>
          ))}

          {WAREHOUSES.map((w) => (
            <CircleMarker
              key={w.id}
              center={[w.lat, w.lng]}
              radius={7}
              pathOptions={{ color: '#a855f7', fillColor: '#6b21a8', fillOpacity: 0.8 }}
            >
              <Popup>{w.name} (Relief Center)</Popup>
            </CircleMarker>
          ))}

          {routes.slice(0, 8).map((rt) => (
            <Polyline
              key={`${rt.assignment_id}-${rt.assignment_type}`}
              positions={rt.geometry.map(([lat, lng]) => [lat, lng] as [number, number])}
              pathOptions={{
                color: rt.blocked_warning ? '#ef4444' : urgencyColor[rt.assignment_type] ?? '#38bdf8',
                weight: rt.blocked_warning ? 4 : 2,
                dashArray: rt.blocked_warning ? '8 8' : undefined,
              }}
            />
          ))}
        </MapContainer>
      </div>
      <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-500">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-4 rounded bg-blue-600/60" /> Flooded zones
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-green-700" /> Hospitals
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-purple-700" /> Relief centers
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-0.5 w-4 bg-sky-400" /> Routes
        </span>
      </div>
    </div>
  );
}
