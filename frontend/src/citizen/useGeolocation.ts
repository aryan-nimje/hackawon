import { useCallback, useEffect, useRef, useState } from 'react';

export type GeoState = 'idle' | 'requesting' | 'granted' | 'denied' | 'unavailable' | 'insecure' | 'timeout';

export interface GeoFix {
  lat: number;
  lng: number;
  accuracy: number;
}

/** Browser location with explicit failure states so the UI can offer the manual fallbacks. */
export function useGeolocation(autoStart = true) {
  const [state, setState] = useState<GeoState>('idle');
  const [fix, setFix] = useState<GeoFix | null>(null);
  const watchId = useRef<number | null>(null);

  const stop = useCallback(() => {
    if (watchId.current != null) {
      navigator.geolocation.clearWatch(watchId.current);
      watchId.current = null;
    }
  }, []);

  const request = useCallback(() => {
    if (!('geolocation' in navigator)) {
      setState('unavailable');
      return;
    }
    if (!window.isSecureContext) {
      setState('insecure');
      return;
    }
    stop();
    setState('requesting');
    // watch for a few seconds so a coarse first fix can sharpen
    watchId.current = navigator.geolocation.watchPosition(
      (pos) => {
        setFix((prev) =>
          !prev || pos.coords.accuracy <= prev.accuracy
            ? { lat: pos.coords.latitude, lng: pos.coords.longitude, accuracy: pos.coords.accuracy }
            : prev,
        );
        setState('granted');
      },
      (err) => {
        stop();
        setState(err.code === err.PERMISSION_DENIED ? 'denied' : err.code === err.TIMEOUT ? 'timeout' : 'unavailable');
      },
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 },
    );
    window.setTimeout(stop, 12000);
  }, [stop]);

  useEffect(() => {
    if (autoStart) request();
    return stop;
  }, [autoStart, request, stop]);

  return { state, fix, request };
}
