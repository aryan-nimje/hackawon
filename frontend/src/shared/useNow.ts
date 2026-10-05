import { useEffect, useState } from 'react';

/** Re-renders every `everyMs` and returns the current time, so "updated Ns ago" / stale flags stay fresh. */
export function useNow(everyMs = 5000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(id);
  }, [everyMs]);
  return now;
}
