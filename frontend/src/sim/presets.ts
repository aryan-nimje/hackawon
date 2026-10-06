import { CITY, nearestBridgeIds } from '../lib/layers';
import type { HazardType, Incident } from '../shared/types';

export interface Preset {
  id: string;
  name: string;
  regions: { type: HazardType; center: [number, number]; radius_m: number; severity: 'low' | 'medium' | 'high' | 'critical'; growing: boolean }[];
  incidents: Pick<Incident, 'text' | 'location' | 'lat' | 'lng' | 'need_type' | 'urgency'>[];
  roads: { latlng: [number, number] }[];
  bridges: string[];
}

/** Offsets in degrees from the city centre (0.01 deg is about 1.1 km), so presets work for any city. */
type Off = [number, number];
const at = (c: [number, number], o: Off): [number, number] => [c[0] + o[0], c[1] + o[1]];

/** Built when called (not at import) so they follow the city loaded from /layers. */
export function makePresets(): Preset[] {
  const c = CITY.center;
  const floodC = at(c, [-0.0100, 0.0180]);
  const quakeC = at(c, [0.0036, -0.0030]);
  return [
    {
      id: 'flood',
      name: 'Flood',
      regions: [{ type: 'flood', center: floodC, radius_m: 600, severity: 'high', growing: true }],
      incidents: [
        { text: 'Family trapped on roof, water rising fast', location: 'Near river (sim)', lat: floodC[0] + 0.002, lng: floodC[1] - 0.002, need_type: 'rescue', urgency: 'critical' },
        { text: 'Elderly resident needs insulin, cannot leave home', location: 'Central (sim)', lat: c[0] - 0.0194, lng: c[1] - 0.0120, need_type: 'medical', urgency: 'high' },
        { text: 'Shelter running out of drinking water', location: 'East side (sim)', lat: c[0] - 0.0124, lng: c[1] + 0.0420, need_type: 'supplies', urgency: 'medium' },
      ],
      roads: [{ latlng: at(c, [-0.0144, 0.0150]) }],
      bridges: nearestBridgeIds(floodC[0], floodC[1], 1),
    },
    {
      id: 'earthquake',
      name: 'Earthquake',
      regions: [{ type: 'collapse', center: quakeC, radius_m: 350, severity: 'critical', growing: false }],
      incidents: [
        { text: 'Building partially collapsed, people trapped', location: 'Central (sim)', lat: quakeC[0] + 0.0005, lng: quakeC[1] - 0.0005, need_type: 'rescue', urgency: 'critical' },
        { text: 'Multiple injuries at street corner', location: 'Main road (sim)', lat: c[0] - 0.0004, lng: c[1] + 0.0078, need_type: 'medical', urgency: 'high' },
        { text: 'Gas smell reported near apartments', location: 'North (sim)', lat: c[0] + 0.0346, lng: c[1] - 0.0200, need_type: 'evacuation', urgency: 'high' },
      ],
      roads: [{ latlng: at(c, [0.0021, -0.0010]) }],
      bridges: nearestBridgeIds(quakeC[0], quakeC[1], 1),
    },
  ];
}
