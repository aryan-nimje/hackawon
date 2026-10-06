import { useEffect, useRef, useState } from 'react';
import { Circle, Marker, useMap, useMapEvents } from 'react-leaflet';
import { haversineKm } from '../../lib/layers';
import { glyph } from '../../shared/map/icons';
import type { HazardType, RouteInfo } from '../../shared/types';

export type Tool = 'select' | 'road' | 'bridge' | 'site' | 'incident' | 'region';

export interface ToolOptions {
  hazard: HazardType;
  severity: 'low' | 'medium' | 'high' | 'critical';
  growing: boolean;
}

interface Props {
  tool: Tool;
  opts: ToolOptions;
  routes: RouteInfo[];
  pendingIncident: [number, number] | null;
  onRoad: (lat: number, lng: number) => void;
  onPickIncident: (lat: number, lng: number) => void;
  onRegion: (lat: number, lng: number, radiusM: number) => void;
  onCancel: () => void;
}

/** Snap a click to the nearest planned-route vertex within 150 m, else keep the click. */
function snap(lat: number, lng: number, routes: RouteInfo[]): [number, number] {
  let best: [number, number] = [lat, lng];
  let bd = 0.15;
  for (const r of routes) {
    for (const [a, b] of r.geometry) {
      const d = haversineKm(lat, lng, a, b);
      if (d < bd) { bd = d; best = [a, b]; }
    }
  }
  return best;
}

/** Leaflet child (mounted by WorldMap only in edit mode): click / drag handlers for the active tool. */
export function MapTools({ tool, opts, routes, pendingIncident, onRoad, onPickIncident, onRegion, onCancel }: Props) {
  const map = useMap();
  const [drag, setDrag] = useState<{ c: [number, number]; r: number } | null>(null);
  const dragRef = useRef<{ c: [number, number]; r: number } | null>(null);
  const moved = useRef(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { dragRef.current = null; setDrag(null); onCancel(); } };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  // region tool needs the map not to pan while dragging
  useEffect(() => {
    if (tool === 'region') map.dragging.disable();
    else map.dragging.enable();
    return () => { map.dragging.enable(); };
  }, [tool, map]);

  useMapEvents({
    click: (e) => {
      if (tool === 'road') {
        const [la, ln] = snap(e.latlng.lat, e.latlng.lng, routes);
        onRoad(la, ln);
      } else if (tool === 'incident') {
        onPickIncident(e.latlng.lat, e.latlng.lng);
      }
    },
    mousedown: (e) => {
      if (tool !== 'region') return;
      moved.current = false;
      dragRef.current = { c: [e.latlng.lat, e.latlng.lng], r: 0 };
      setDrag(dragRef.current);
    },
    mousemove: (e) => {
      const d = dragRef.current;
      if (!d) return;
      moved.current = true;
      const r = haversineKm(d.c[0], d.c[1], e.latlng.lat, e.latlng.lng) * 1000;
      dragRef.current = { c: d.c, r };
      setDrag(dragRef.current);
    },
    mouseup: () => {
      const d = dragRef.current;
      dragRef.current = null;
      setDrag(null);
      if (!d || tool !== 'region') return;
      onRegion(d.c[0], d.c[1], moved.current && d.r > 60 ? d.r : 300);
    },
  });

  const color = opts.hazard === 'fire' ? '#ea580c' : opts.hazard === 'collapse' ? '#78716c' : '#2563eb';
  return (
    <>
      {drag && <Circle center={drag.c} radius={Math.max(60, drag.r)} pathOptions={{ color, weight: 2, dashArray: '4 4', fillOpacity: 0.15 }} interactive={false} />}
      {pendingIncident && <Marker position={pendingIncident} icon={glyph('?', '#2b6f73', 26)} interactive={false} />}
    </>
  );
}
