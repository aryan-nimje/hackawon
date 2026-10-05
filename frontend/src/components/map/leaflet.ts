import L from 'leaflet';
import iconRetina from 'leaflet/dist/images/marker-icon-2x.png';
import icon from 'leaflet/dist/images/marker-icon.png';
import iconShadow from 'leaflet/dist/images/marker-shadow.png';
import 'leaflet/dist/leaflet.css';

delete (L.Icon.Default.prototype as unknown as { _getIconUrl?: unknown })._getIconUrl;
L.Icon.Default.mergeOptions({ iconRetinaUrl: iconRetina, iconUrl: icon, shadowUrl: iconShadow });

export const TILE_URL = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';
export const TILE_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';

export const URGENCY_COLOR: Record<string, string> = {
  critical: '#ef4444',
  high: '#f97316',
  medium: '#eab308',
  low: '#94a3b8',
};

export const SEVERITY_COLOR: Record<string, string> = {
  Low: '#22c55e',
  Medium: '#eab308',
  High: '#f97316',
  Critical: '#ef4444',
};

export const ROUTE_COLOR: Record<string, string> = {
  rescue: '#38bdf8',
  medical: '#4ade80',
  logistics: '#c084fc',
};

const cache = new Map<string, L.DivIcon>();

/** Urgency-coloured teardrop-free dot pin. `flagged` = dashed ring (low credibility). */
export function incidentIcon(urgency: string, opts: { flagged?: boolean; junk?: boolean; selected?: boolean } = {}): L.DivIcon {
  const key = `i:${urgency}:${!!opts.flagged}:${!!opts.junk}:${!!opts.selected}`;
  let ic = cache.get(key);
  if (!ic) {
    const color = URGENCY_COLOR[urgency] ?? '#94a3b8';
    const size = urgency === 'critical' ? 20 : 16;
    const border = opts.flagged ? '2px dashed #fca5a5' : '2px solid #0f172a';
    const ring = opts.selected ? 'box-shadow:0 0 0 3px #fff;' : '';
    const glyph = opts.junk ? '<span style="color:#fff;font:700 11px/1 sans-serif">✕</span>' : '';
    ic = L.divIcon({
      className: '',
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
      html: `<div style="width:${size}px;height:${size}px;border-radius:50%;background:${opts.junk ? '#7f1d1d' : color};border:${border};${ring}display:flex;align-items:center;justify-content:center">${glyph}</div>`,
    });
    cache.set(key, ic);
  }
  return ic;
}

export function glyphIcon(glyph: string, bg: string, size = 24): L.DivIcon {
  const key = `g:${glyph}:${bg}:${size}`;
  let ic = cache.get(key);
  if (!ic) {
    ic = L.divIcon({
      className: '',
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
      html: `<div style="width:${size}px;height:${size}px;border-radius:6px;background:${bg};border:2px solid #0f172a;color:#fff;display:flex;align-items:center;justify-content:center;font:700 ${size * 0.55}px/1 sans-serif">${glyph}</div>`,
    });
    cache.set(key, ic);
  }
  return ic;
}

export function pinIcon(color = '#38bdf8', size = 34): L.DivIcon {
  const key = `p:${color}:${size}`;
  let ic = cache.get(key);
  if (!ic) {
    ic = L.divIcon({
      className: '',
      iconSize: [size, size],
      iconAnchor: [size / 2, size],
      html: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" style="filter:drop-shadow(0 2px 3px rgba(0,0,0,.5))"><path d="M12 2C7.6 2 4 5.5 4 9.8 4 15.5 12 22 12 22s8-6.5 8-12.2C20 5.5 16.4 2 12 2z" fill="${color}" stroke="#0f172a" stroke-width="1.5"/><circle cx="12" cy="9.8" r="3.2" fill="#fff"/></svg>`,
    });
    cache.set(key, ic);
  }
  return ic;
}
