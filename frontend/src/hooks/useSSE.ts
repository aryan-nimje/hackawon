import { useEffect, useState } from 'react';
import type { ActivityEvent } from '../types';
import { api } from '../api/client';

export function useSSE(runId: string | null) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!runId) {
      setEvents([]);
      setConnected(false);
      return;
    }

    const url = api.streamUrl(runId);
    const source = new EventSource(url);

    source.onopen = () => {
      setConnected(true);
      setError(null);
    };

    source.onmessage = (msg) => {
      try {
        const event = JSON.parse(msg.data) as ActivityEvent;
        setEvents((prev) => {
          const key = `${event.agent}-${event.status}-${event.timestamp}-${event.summary}`;
          const exists = prev.some(
            (e) => `${e.agent}-${e.status}-${e.timestamp}-${e.summary}` === key,
          );
          if (exists) return prev;
          return [...prev, event];
        });
      } catch {
        /* ignore malformed */
      }
    };

    source.onerror = () => {
      setConnected(false);
      setError('Lost connection to activity stream');
    };

    return () => source.close();
  }, [runId]);

  return { events, connected, error };
}
