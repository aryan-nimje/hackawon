import { useEffect, useRef } from 'react';
import { useMap, useMapEvents } from 'react-leaflet';
import L from 'leaflet';

/** Keeps Leaflet sized correctly when its container resizes (grid reflow, sidebar toggles). */
export function AutoResize() {
  const map = useMap();
  useEffect(() => {
    const el = map.getContainer();
    const ro = new ResizeObserver(() => map.invalidateSize());
    ro.observe(el);
    return () => ro.disconnect();
  }, [map]);
  return null;
}

/**
 * Fits the view to the given points, but only when the *set of points* changes
 * meaningfully (first load, new run), never on every poll. Skips once the user
 * has panned/zoomed so the map doesn't fight them.
 */
export function FitToPoints({
  points,
  signature,
  refitKey = 0,
}: {
  points: [number, number][];
  signature: string;
  refitKey?: number;
}) {
  const map = useMap();
  const lastSig = useRef<string>('');
  const userMoved = useRef(false);
  const lastRefit = useRef(refitKey);

  useMapEvents({
    dragstart: () => {
      userMoved.current = true;
    },
  });

  useEffect(() => {
    const forced = refitKey !== lastRefit.current;
    if (forced) {
      lastRefit.current = refitKey;
      userMoved.current = false;
    }
    if (!points.length) return;
    if (!forced && signature === lastSig.current) return;
    if (!forced && userMoved.current && lastSig.current) return;
    lastSig.current = signature;
    const bounds = L.latLngBounds(points);
    if (bounds.isValid()) map.fitBounds(bounds.pad(0.2), { maxZoom: 14, animate: false });
  }, [map, points, signature, refitKey]);

  return null;
}

export function RecenterButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded border border-disaster-border bg-slate-800 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
    >
      Re-centre
    </button>
  );
}
