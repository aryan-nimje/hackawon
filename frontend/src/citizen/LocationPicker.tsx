import type { Marker as LMarker } from 'leaflet';
import { useEffect } from 'react';
import { Circle, MapContainer, Marker, Polygon, TileLayer, useMap, useMapEvents } from 'react-leaflet';
import { BASE_FLOOD_ZONES, CITY } from '../lib/layers';
import { TILE_ATTRIBUTION, TILE_URL, pinIcon } from '../components/map/leaflet';
import { AutoResize } from '../components/map/MapHelpers';

interface Props {
  position: [number, number] | null;
  accuracy: number | null;
  /** bump to re-centre the map on `position` (GPS fix, search result) */
  focusKey: number;
  onMove: (lat: number, lng: number) => void;
}

function Focus({ position, focusKey }: { position: [number, number] | null; focusKey: number }) {
  const map = useMap();
  useEffect(() => {
    if (position) map.setView(position, Math.max(map.getZoom(), 16), { animate: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusKey, map]);
  return null;
}

function ClickToMove({ onMove }: { onMove: (lat: number, lng: number) => void }) {
  useMapEvents({ click: (e) => onMove(e.latlng.lat, e.latlng.lng) });
  return null;
}

export function LocationPicker({ position, accuracy, focusKey, onMove }: Props) {
  return (
    <div className="h-72 overflow-hidden rounded-xl border border-disaster-border sm:h-80">
      <MapContainer
        center={position ?? CITY.center}
        zoom={position ? 16 : CITY.zoom}
        className="h-full w-full"
        scrollWheelZoom={false}
      >
        <TileLayer attribution={TILE_ATTRIBUTION} url={TILE_URL} />
        <AutoResize />
        <Focus position={position} focusKey={focusKey} />
        <ClickToMove onMove={onMove} />
        {BASE_FLOOD_ZONES.map((z) => (
          <Polygon
            key={z.id}
            positions={z.ring}
            pathOptions={{ color: '#60a5fa', weight: 1, fillColor: '#1d4ed8', fillOpacity: 0.25 }}
          />
        ))}
        {position && accuracy != null && accuracy > 0 && (
          <Circle
            center={position}
            radius={accuracy}
            pathOptions={{ color: '#38bdf8', weight: 1, fillColor: '#38bdf8', fillOpacity: 0.15 }}
          />
        )}
        {position && (
          <Marker
            position={position}
            draggable
            icon={pinIcon('#ef4444')}
            eventHandlers={{
              dragend: (e) => {
                const ll = (e.target as LMarker).getLatLng();
                onMove(ll.lat, ll.lng);
              },
            }}
          />
        )}
      </MapContainer>
    </div>
  );
}
