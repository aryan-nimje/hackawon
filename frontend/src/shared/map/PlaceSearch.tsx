import L from 'leaflet';
import { useEffect, useRef, useState } from 'react';
import { CITY } from '../../lib/layers';
import { searchAddress, type GeoResult } from '../../lib/geocode';

/**
 * Place search box overlaid on the map (both Authority and Simulation get it through WorldMap).
 * Nominatim usage policy: query only on an explicit submit, never while typing.
 */
export function PlaceSearch({ onPick }: { onPick: (r: GeoResult) => void }) {
  const [q, setQ] = useState('');
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<GeoResult[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!box.current) return;
    L.DomEvent.disableClickPropagation(box.current);
    L.DomEvent.disableScrollPropagation(box.current);
  }, []);

  const run = async () => {
    if (q.trim().length < 3 || busy) return;
    setBusy(true);
    setErr(null);
    try {
      setResults(await searchAddress(q, CITY.center));
    } catch (e) {
      setResults(null);
      setErr(e instanceof Error ? e.message : 'Search failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div ref={box} className="absolute left-3 top-3 z-[1000] w-64 max-w-[calc(100%-1.5rem)] text-xs">
      <div className="flex gap-1 rounded-lg border border-[#e8e4dc] bg-white p-1 shadow-md">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void run(); if (e.key === 'Escape') { setResults(null); setErr(null); } }}
          placeholder="Search a place…"
          aria-label="Search a place"
          className="min-w-0 flex-1 rounded-md px-2 py-1.5 outline-none"
        />
        <button type="button" onClick={() => void run()} disabled={busy || q.trim().length < 3}
          className="rounded-md bg-[#2b6f73] px-2.5 py-1.5 font-medium text-white disabled:opacity-50">{busy ? '…' : 'Go'}</button>
      </div>
      {err && <p className="mt-1 rounded-lg border border-[#f0c4bf] bg-[#fbe9e7] px-2 py-1 text-[#8c1d17]">{err}</p>}
      {results && (
        <ul className="mt-1 max-h-52 overflow-y-auto rounded-lg border border-[#e8e4dc] bg-white shadow-md">
          {results.length === 0 && <li className="px-2 py-1.5 text-[#6b6b6b]">No places found.</li>}
          {results.map((r, i) => (
            <li key={`${r.lat},${r.lng},${i}`}>
              <button type="button" onClick={() => { onPick(r); setResults(null); }}
                className="block w-full truncate px-2 py-1.5 text-left hover:bg-[#faf8f4]" title={r.label}>{r.label}</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
