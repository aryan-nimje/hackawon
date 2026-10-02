import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { HealthResponse, RunState } from '../types';

export function useRun(runId: string | null) {
  const [run, setRun] = useState<RunState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!runId) return;
    try {
      setLoading(true);
      const data = await api.getRun(runId);
      setRun(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load run');
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    if (!runId) {
      setRun(null);
      return;
    }
    refresh();
    const interval = setInterval(refresh, 3000);
    return () => clearInterval(interval);
  }, [runId, refresh]);

  return { run, loading, error, refresh };
}

export function useHealth() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch((e) => setError(e instanceof Error ? e.message : 'Backend unavailable'));
  }, []);

  return { health, error };
}
