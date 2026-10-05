import { useEffect, useRef } from 'react';
import { useMap, useMapEvents } from 'react-leaflet';
import { CITY, loadCity, slugify } from '../../lib/layers';
import { cityNameAt } from '../../lib/geocode';

export type CityStatus = { kind: 'loading' | 'ok' | 'error' | 'info'; text: string } | null;

const SETTLE_MS = 1200; // wait for the map to stop moving before asking anything
const MIN_ZOOM = 9;     // zoomed out further than this there is no single city to load

const outside = (lat: number, lng: number) => {
  const b = CITY.bbox;
  if (!b) return false;
  return lat < b[0] || lat > b[2] || lng < b[1] || lng > b[3];
};

/**
 * Lives inside the map. When the view settles on a spot outside the loaded city, finds which city it is
 * (Nominatim reverse) and loads its layers from the backend (which fetches + caches it on first use).
 * `enabled=false` (a simulation run is in progress) only tells the user why nothing switches.
 */
export function CityAutoSwitch({ enabled, onStatus }: { enabled: boolean; onStatus: (s: CityStatus) => void }) {
  const map = useMap();
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const busy = useRef(false);
  const failed = useRef(new Set<string>()); // 0.1-degree cells that already failed: do not retry on every pan
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const statusRef = useRef(onStatus);
  statusRef.current = onStatus;

  const settle = async () => {
    if (busy.current || map.getZoom() < MIN_ZOOM) return;
    const c = map.getCenter();
    if (!outside(c.lat, c.lng)) return;
    if (!enabledRef.current) {
      statusRef.current({ kind: 'info', text: 'Reset the simulation to switch city.' });
      return;
    }
    const cell = `${c.lat.toFixed(1)},${c.lng.toFixed(1)}`;
    if (failed.current.has(cell)) return;
    busy.current = true;
    try {
      statusRef.current({ kind: 'loading', text: 'Finding the city here…' });
      const name = await cityNameAt(c.lat, c.lng);
      if (!name) {
        failed.current.add(cell);
        statusRef.current({ kind: 'error', text: 'Could not tell which city this is.' });
        return;
      }
      if (slugify(name) === CITY.slug) { statusRef.current(null); return; } // same city, just outside its box
      statusRef.current({ kind: 'loading', text: `Loading ${name}… (the first time can take a minute)` });
      const r = await loadCity(name);
      if (r.ok) statusRef.current({ kind: 'ok', text: `Loaded ${CITY.name}` });
      else { failed.current.add(cell); statusRef.current({ kind: 'error', text: r.error ?? `Could not load ${name}.` }); }
    } finally {
      busy.current = false;
    }
  };

  useMapEvents({
    movestart: () => { if (timer.current) clearTimeout(timer.current); },
    moveend: () => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => void settle(), SETTLE_MS);
    },
  });
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  return null;
}
