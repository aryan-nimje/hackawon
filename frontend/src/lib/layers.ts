/**
 * Map layers shared by the dashboard, simulation and citizen pages.
 *
 * The arrays/objects below start with built-in Pune defaults (so the demo never depends on the
 * network) and are replaced IN PLACE by `loadLayers()` from `GET /layers?city=<name>` before the
 * apps render. Always import the exported constants; never copy them, so a reload is seen everywhere.
 * Depot stock and flood zones are simulated; hospitals / bridges come from the backend (OSM).
 */
import { API_BASE } from '../shared/bus';

export const CITY: { name: string; slug: string; center: [number, number]; zoom: number; bbox: [number, number, number, number] | null } = {
  name: 'Pune, Maharashtra (Simulated)',
  slug: 'pune',
  center: [18.5204, 73.8567],
  zoom: 12,
  bbox: [18.4, 73.7, 18.72, 74.0], // [south, west, north, east]
};

export interface Hospital {
  id: string;
  name: string;
  lat: number;
  lng: number;
  beds: number;
  specialties: string[];
  limited?: boolean;
}

export interface Depot {
  id: string;
  name: string;
  lat: number;
  lng: number;
  stock: Record<string, number>;
}

export const HOSPITALS: Hospital[] = [
  { id: 'h001', name: 'Sassoon General Hospital', lat: 18.5285, lng: 73.8745, beds: 45, specialties: ['trauma', 'surgery', 'burn'] },
  { id: 'h002', name: 'Ruby Hall Clinic', lat: 18.5322, lng: 73.8778, beds: 32, specialties: ['trauma', 'cardiology', 'neurology'] },
  { id: 'h003', name: 'KEM Hospital', lat: 18.5192, lng: 73.8705, beds: 28, specialties: ['general', 'emergency', 'pediatrics'] },
  { id: 'h004', name: 'Deenanath Mangeshkar Hospital', lat: 18.5018, lng: 73.8326, beds: 30, specialties: ['cardiology', 'pediatrics', 'neonatal'] },
  { id: 'h005', name: 'Sahyadri Hospital, Deccan', lat: 18.5143, lng: 73.8398, beds: 22, specialties: ['neurology', 'orthopedics', 'emergency'], limited: true },
  { id: 'h006', name: 'Aditya Birla Memorial Hospital', lat: 18.6253, lng: 73.7825, beds: 20, specialties: ['trauma', 'burn', 'emergency'] },
  { id: 'h007', name: 'Noble Hospital, Hadapsar', lat: 18.5040, lng: 73.9265, beds: 18, specialties: ['general', 'emergency'] },
];

export const DEPOTS: Depot[] = [
  { id: 'w001', name: 'PMC Relief Warehouse, Swargate (simulated)', lat: 18.5018, lng: 73.8636, stock: { food_meals: 5000, water_bottles: 8000, blankets: 1200, vehicles: 12 } },
  { id: 'w002', name: 'Red Cross Supply Depot, Shivajinagar (simulated)', lat: 18.5308, lng: 73.8475, stock: { food_meals: 2000, water_bottles: 4000, medicine_kits: 500, vehicles: 8 } },
  { id: 'w003', name: 'NDRF Staging Area, Pimpri-Chinchwad (simulated)', lat: 18.6279, lng: 73.8009, stock: { food_meals: 10000, water_bottles: 15000, medicine_kits: 800, vehicles: 25, boats: 6 } },
  { id: 'w004', name: 'Community Relief Centre, Kothrud (simulated)', lat: 18.5074, lng: 73.8077, stock: { food_meals: 800, water_bottles: 1500, blankets: 300, vehicles: 3 } },
];

export interface FloodZone {
  id: string;
  name: string;
  reason: string;
  /** Leaflet order: [lat, lng] */
  ring: [number, number][];
}

function rect(lat1: number, lng1: number, lat2: number, lng2: number): [number, number][] {
  return [[lat1, lng1], [lat1, lng2], [lat2, lng2], [lat2, lng1], [lat1, lng1]];
}

export const BASE_FLOOD_ZONES: FloodZone[] = [
  { id: 'flood-1', name: 'Mutha Riverbank, Deccan (SIMULATED)', reason: 'River overtopping', ring: rect(18.5100, 73.8430, 18.5160, 73.8500) },
  { id: 'flood-2', name: 'Mula-Mutha Confluence, Sangamwadi (SIMULATED)', reason: 'Deep water on low-lying roads', ring: rect(18.5280, 73.8650, 18.5340, 73.8730) },
  { id: 'flood-3', name: 'Sinhagad Road Low-Lying Block (SIMULATED)', reason: 'Street flooding 3ft+', ring: rect(18.4800, 73.8180, 18.4860, 73.8260) },
];

/** Bridge crossings, [lat,lng] segments. Defaults are approximate; /layers supplies OSM bridges. */
export interface Bridge {
  id: string;
  name: string;
  geometry: [number, number][];
}

export const BRIDGES: Bridge[] = [
  { id: 'br-sambhaji', name: 'Sambhaji (Lakdi Pul) Bridge (SIMULATED)', geometry: [[18.5122, 73.8456], [18.5152, 73.8456]] },
  { id: 'br-shivaji', name: 'Shivaji Bridge (SIMULATED)', geometry: [[18.5172, 73.8556], [18.5202, 73.8556]] },
  { id: 'br-sangam', name: 'Sangam Bridge (SIMULATED)', geometry: [[18.5275, 73.8700], [18.5305, 73.8700]] },
  { id: 'br-holkar', name: 'Holkar Bridge (SIMULATED)', geometry: [[18.5345, 73.8760], [18.5375, 73.8760]] },
  { id: 'br-vitthalwadi', name: 'Vitthalwadi Bridge (SIMULATED)', geometry: [[18.4935, 73.8300], [18.4965, 73.8300]] },
];

export function centroid(ring: [number, number][]): [number, number] {
  const pts = ring.slice(0, -1);
  const n = Math.max(pts.length, 1);
  return [pts.reduce((s, p) => s + p[0], 0) / n, pts.reduce((s, p) => s + p[1], 0) / n];
}

/** Scale a ring around its centroid (used to grow floods in the simulator). */
export function scaleRing(ring: [number, number][], factor: number): [number, number][] {
  const [cy, cx] = centroid(ring);
  return ring.map(([lat, lng]) => [cy + (lat - cy) * factor, cx + (lng - cx) * factor]);
}

/** Roughly circular polygon (radius in metres) for injected floods. */
export function circleRing(lat: number, lng: number, radiusM: number, steps = 14): [number, number][] {
  const dLat = radiusM / 111_320;
  const dLng = radiusM / (111_320 * Math.cos((lat * Math.PI) / 180));
  const ring: [number, number][] = [];
  for (let i = 0; i < steps; i++) {
    const a = (i / steps) * Math.PI * 2;
    ring.push([lat + dLat * Math.sin(a), lng + dLng * Math.cos(a)]);
  }
  ring.push(ring[0]);
  return ring;
}

export function pointInRing(lat: number, lng: number, ring: [number, number][]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [yi, xi] = ring[i];
    const [yj, xj] = ring[j];
    if (yi > lat !== yj > lat && lng < ((xj - xi) * (lat - yi)) / (yj - yi + 1e-12) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

export function haversineKm(aLat: number, aLng: number, bLat: number, bLng: number): number {
  const r = 6371;
  const p1 = (aLat * Math.PI) / 180;
  const p2 = (bLat * Math.PI) / 180;
  const dp = p2 - p1;
  const dl = ((bLng - aLng) * Math.PI) / 180;
  const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * r * Math.asin(Math.sqrt(h));
}


/* ───────── dynamic loading ───────── */
interface LayersPayload {
  city?: { name?: string; slug?: string; center?: [number, number]; zoom?: number; bbox?: [number, number, number, number] };
  hospitals?: Partial<Hospital>[];
  depots?: Partial<Depot>[];
  flood_zones?: { id?: string; name?: string; reason?: string; ring?: [number, number][] }[];
  bridges?: Partial<Bridge>[];
}

const isNum = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n);
const validPt = (la: unknown, ln: unknown) => isNum(la) && isNum(ln) && Math.abs(la) <= 90 && Math.abs(ln) <= 180 && !(la === 0 && ln === 0);

/** Same rule as the backend: 'Pune, Maharashtra (Simulated)' -> 'pune'. */
export function slugify(name: string): string {
  const first = name.trim().split(/[,(]/, 1)[0];
  return first.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'city';
}

/* Components read HOSPITALS / CITY / ... directly, so a city switch has to tell them to re-render. */
let layersVersion = 0;
const listeners = new Set<() => void>();
export const subscribeLayers = (fn: () => void) => { listeners.add(fn); return () => { listeners.delete(fn); }; };
/** Bumps every time a different city's layers are swapped in. Use with `useCityVersion()`. */
export const getLayersVersion = () => layersVersion;

/** City name from `?city=` or VITE_CITY (default: Pune). */
export function requestedCity(): string {
  try {
    const q = new URLSearchParams(window.location.search).get('city');
    if (q && q.trim()) return q.trim();
  } catch { /* no window */ }
  return (import.meta.env.VITE_CITY as string | undefined)?.trim() || 'Pune';
}

/** Replace array contents in place only when the new list is non-empty, so a bad payload never blanks the map. */
function swap<T>(target: T[], next: T[], force = false) {
  if (force || next.length > 0) target.splice(0, target.length, ...next);
}

/**
 * Pune is cached on the backend (4 s). Any other city may need a first-time OpenStreetMap fetch (Nominatim +
 * Overpass, can take a minute or more); the backend then caches it, so later loads are instant.
 */
function defaultTimeoutMs(city: string): number {
  return slugify(city) === 'pune' ? 4000 : 150000;
}

export interface LoadResult { ok: boolean; error?: string }

let loadSeq = 0;

/**
 * Fetch GET /layers?city=… and swap it into the shared constants. Never throws and never waits more
 * than `timeoutMs` (see `defaultTimeoutMs`): on any failure the current layers stay in place and `error`
 * says why (the backend's own message when it sent one).
 * When the payload is a different city than the one on screen, every layer is replaced (a city with no
 * bridges must not keep the old city's bridges), and subscribers are notified.
 */
export async function loadCity(city = requestedCity(), timeoutMs = defaultTimeoutMs(city)): Promise<LoadResult> {
  const seq = ++loadSeq;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(`${API_BASE}/layers?city=${encodeURIComponent(city)}`, { signal: ctl.signal });
    if (!res.ok) {
      let detail = '';
      try { detail = ((await res.json()) as { detail?: string }).detail ?? ''; } catch { /* not JSON */ }
      return { ok: false, error: detail || `Could not load ${city} (HTTP ${res.status}).` };
    }
    const d = (await res.json()) as LayersPayload;
    if (seq !== loadSeq) return { ok: false, error: 'Superseded by a newer city request.' };

    const hospitals = (d.hospitals ?? []).filter((h): h is Hospital => !!h.id && !!h.name && validPt(h.lat, h.lng) && isNum(h.beds))
      .map((h) => ({ ...h, specialties: Array.isArray(h.specialties) ? h.specialties : [] }));
    const depots = (d.depots ?? []).filter((x): x is Depot => !!x.id && !!x.name && validPt(x.lat, x.lng))
      .map((x) => ({ ...x, stock: x.stock ?? {} }));
    const zones = (d.flood_zones ?? []).filter((z) => Array.isArray(z.ring) && z.ring.length >= 4 && z.ring.every((p) => validPt(p[0], p[1])))
      .map((z, i) => ({ id: z.id ?? `flood-${i + 1}`, name: z.name ?? `Flood zone ${i + 1} (SIMULATED)`, reason: z.reason ?? 'Flooding', ring: z.ring! }));
    const bridges = (d.bridges ?? []).filter((b): b is Bridge => !!b.id && !!b.name && Array.isArray(b.geometry) && b.geometry.length >= 2 && b.geometry.every((p) => validPt(p[0], p[1])));

    // Hospitals are the anchor: without them keep the current layers entirely so map and agents stay consistent.
    if (hospitals.length === 0) return { ok: false, error: `No usable hospital data for ${city}.` };
    const slug = d.city?.slug || slugify(d.city?.name ?? city);
    const switching = slug !== CITY.slug;
    swap(HOSPITALS, hospitals);
    swap(DEPOTS, depots, switching);
    swap(BASE_FLOOD_ZONES, zones, switching);
    swap(BRIDGES, bridges, switching);
    CITY.slug = slug;
    if (d.city && d.city.center && validPt(d.city.center[0], d.city.center[1])) {
      CITY.center = d.city.center;
      CITY.zoom = isNum(d.city.zoom) ? d.city.zoom : CITY.zoom;
    }
    const b = d.city?.bbox;
    CITY.bbox = Array.isArray(b) && b.length === 4 && b.every(isNum) ? b : null;
    if (d.city?.name) CITY.name = d.city.name;
    if (switching) { layersVersion++; listeners.forEach((fn) => fn()); }
    return { ok: true };
  } catch (e) {
    const aborted = e instanceof DOMException && e.name === 'AbortError';
    return { ok: false, error: aborted ? `Loading ${city} took too long. Try again; the backend keeps what it already fetched.` : `Could not reach the backend to load ${city}.` };
  } finally {
    clearTimeout(timer);
  }
}

/** Startup loader used by the three apps. Never throws; resolves true when the layers were loaded. */
export async function loadLayers(city = requestedCity(), timeoutMs = defaultTimeoutMs(city)): Promise<boolean> {
  return (await loadCity(city, timeoutMs)).ok;
}

/** Ids of the n bridges nearest to a point (used by scenario presets). */
export function nearestBridgeIds(lat: number, lng: number, n = 1): string[] {
  return [...BRIDGES]
    .map((b) => ({ id: b.id, d: haversineKm(lat, lng, b.geometry[0][0], b.geometry[0][1]) }))
    .sort((a, b) => a.d - b.d)
    .slice(0, n)
    .map((x) => x.id);
}
