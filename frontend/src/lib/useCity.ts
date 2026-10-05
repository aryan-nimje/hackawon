import { useSyncExternalStore } from 'react';
import { getLayersVersion, subscribeLayers } from './layers';

/** Re-renders the component when a different city's layers are swapped in (HOSPITALS, CITY, ... change in place). */
export function useCityVersion(): number {
  return useSyncExternalStore(subscribeLayers, getLayersVersion, getLayersVersion);
}
