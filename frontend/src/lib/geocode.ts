export interface GeoResult {
  label: string;
  lat: number;
  lng: number;
}

/** Address search via OpenStreetMap Nominatim. Called only on explicit user action (usage policy). */
export async function searchAddress(query: string, near: [number, number]): Promise<GeoResult[]> {
  const q = query.trim();
  if (q.length < 3) return [];
  const [lat, lng] = near;
  const d = 0.6; // bias results to ~60 km around the affected city
  const url =
    'https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&addressdetails=0' +
    `&viewbox=${lng - d},${lat + d},${lng + d},${lat - d}&q=${encodeURIComponent(q)}`;
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error('Address search is unavailable right now');
  const data = (await res.json()) as { display_name: string; lat: string; lon: string }[];
  return data.map((r) => ({ label: r.display_name, lat: Number(r.lat), lng: Number(r.lon) }));
}

export async function reverseGeocode(lat: number, lng: number): Promise<string | null> {
  try {
    const res = await fetch(
      `https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=18&lat=${lat}&lon=${lng}`,
      { headers: { Accept: 'application/json' } },
    );
    if (!res.ok) return null;
    const d = (await res.json()) as { display_name?: string };
    return d.display_name ?? null;
  } catch {
    return null;
  }
}

/**
 * Name of the city around a point (English), or null. Used when the map settles outside the loaded city.
 * Nominatim usage policy: one request per settled map move, never while dragging.
 */
export async function cityNameAt(lat: number, lng: number): Promise<string | null> {
  try {
    const res = await fetch(
      `https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=10&addressdetails=1&accept-language=en&lat=${lat}&lon=${lng}`,
      { headers: { Accept: 'application/json' } },
    );
    if (!res.ok) return null;
    const a = ((await res.json()) as { address?: Record<string, string> }).address;
    if (!a) return null;
    return a.city || a.town || a.municipality || a.city_district || a.state_district || a.county || null;
  } catch {
    return null;
  }
}
