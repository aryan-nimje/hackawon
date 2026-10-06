import { useEffect, useRef, useState } from 'react';

export const API_BASE: string = import.meta.env.VITE_API_BASE_URL || '/api';

type Handler = (data: unknown) => void;

/** SSE client with exponential-backoff reconnect. One stream per app. */
export class Bus {
  private es: EventSource | null = null;
  private handlers = new Map<string, Set<Handler>>();
  private statusCbs = new Set<(up: boolean) => void>();
  private retry = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private known = new Set<string>();

  private path: string;
  constructor(path = '/bus/stream') {
    this.path = path;
  }

  on(type: string, h: Handler): () => void {
    let set = this.handlers.get(type);
    if (!set) {
      set = new Set();
      this.handlers.set(type, set);
    }
    set.add(h);
    if (this.es && !this.known.has(type)) this.attach(type);
    return () => set.delete(h);
  }

  onStatus(cb: (up: boolean) => void): () => void {
    this.statusCbs.add(cb);
    return () => this.statusCbs.delete(cb);
  }

  private attach(type: string) {
    if (!this.es) return;
    this.known.add(type);
    this.es.addEventListener(type, (m) => {
      let data: unknown = null;
      try { data = JSON.parse((m as MessageEvent).data); } catch { return; }
      this.handlers.get(type)?.forEach((h) => h(data));
    });
  }

  connect() {
    this.closed = false;
    this.open();
  }

  private open() {
    if (this.closed) return;
    this.known.clear();
    const es = new EventSource(`${API_BASE}${this.path}`);
    this.es = es;
    for (const t of this.handlers.keys()) this.attach(t);
    es.onopen = () => {
      this.retry = 0;
      this.statusCbs.forEach((c) => c(true));
    };
    es.onerror = () => {
      es.close();
      this.es = null;
      this.statusCbs.forEach((c) => c(false));
      const wait = Math.min(15000, 500 * 2 ** this.retry++);
      this.timer = setTimeout(() => this.open(), wait);
    };
  }

  close() {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.es?.close();
    this.es = null;
  }
}

export async function post<T = unknown>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
  return (await res.json().catch(() => ({}))) as T;
}

export async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
  return (await res.json()) as T;
}

/** Subscribe a component to bus events; returns connection state. */
export function useBus(subs: Record<string, Handler>, onOpen?: () => void): { connected: boolean } {
  const [connected, setConnected] = useState(false);
  const subsRef = useRef(subs);
  subsRef.current = subs;
  const openRef = useRef(onOpen);
  openRef.current = onOpen;
  const keys = Object.keys(subs).sort().join('|');

  useEffect(() => {
    const bus = new Bus();
    for (const k of keys.split('|')) bus.on(k, (d) => subsRef.current[k]?.(d));
    bus.onStatus((up) => {
      if (up) openRef.current?.();  // fires before the backend replays its state
      setConnected(up);
    });
    bus.connect();
    return () => bus.close();
  }, [keys]);

  return { connected };
}
