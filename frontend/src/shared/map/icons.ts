import L from 'leaflet';
import { URGENCY_COLOR } from '../ui';

const cache = new Map<string, L.DivIcon>();

export function dotIcon(urgency: string, o: { flagged?: boolean; selected?: boolean; escalated?: boolean } = {}): L.DivIcon {
  const key = `d:${urgency}:${!!o.flagged}:${!!o.selected}:${!!o.escalated}`;
  let ic = cache.get(key);
  if (!ic) {
    const size = urgency === 'critical' ? 20 : 16;
    const color = URGENCY_COLOR[urgency] ?? '#7a8b86';
    const border = o.flagged ? '2px dashed #b3261e' : '2px solid #fff';
    const ring = o.selected ? 'box-shadow:0 0 0 3px #2b6f73;' : o.escalated ? 'box-shadow:0 0 0 3px rgba(179,38,30,.35);' : 'box-shadow:0 1px 3px rgba(0,0,0,.35);';
    ic = L.divIcon({ className: '', iconSize: [size, size], iconAnchor: [size / 2, size / 2],
      html: `<div style="width:${size}px;height:${size}px;border-radius:50%;background:${color};border:${border};${ring}"></div>` });
    cache.set(key, ic);
  }
  return ic;
}

export function glyph(g: string, bg: string, size = 24, o: { estimated?: boolean; stale?: boolean } = {}): L.DivIcon {
  const key = `g:${g}:${bg}:${size}:${!!o.estimated}:${!!o.stale}`;
  const border = o.estimated ? '2px dashed #fff' : '2px solid #fff';
  const ring = o.stale ? 'box-shadow:0 0 0 3px #d9a406;opacity:.55;' : 'box-shadow:0 1px 3px rgba(0,0,0,.4);';
  let ic = cache.get(key);
  if (!ic) {
    ic = L.divIcon({ className: '', iconSize: [size, size], iconAnchor: [size / 2, size / 2],
      html: `<div style="width:${size}px;height:${size}px;border-radius:7px;background:${bg};border:${border};${ring}color:#fff;display:flex;align-items:center;justify-content:center;font:700 ${Math.round(size * 0.55)}px/1 sans-serif">${g}</div>` });
    cache.set(key, ic);
  }
  return ic;
}
