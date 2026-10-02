import type {
  AlertDraft,
  HealthResponse,
  Incident,
  ResponsePlan,
  RunState,
} from '../types';

const BASE = import.meta.env.VITE_API_BASE_URL || '/api';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<HealthResponse>('/health'),

  startScenario: (replaySpeed = 5, simulateFailures: string[] = []) =>
    request<{ run_id: string; mock_mode: boolean; message: string }>(
      '/scenario/start',
      {
        method: 'POST',
        body: JSON.stringify({ replay_speed: replaySpeed, simulate_failures: simulateFailures }),
      },
    ),

  getRun: (runId: string) => request<RunState>(`/runs/${runId}`),

  getIncidents: (runId?: string) =>
    request<Incident[]>(runId ? `/incidents?run_id=${runId}` : '/incidents'),

  getPlan: (runId: string) => request<ResponsePlan>(`/plan/${runId}`),

  reviewPlan: (
    runId: string,
    actions: { item_id: string; action: string; edited_content?: string }[],
  ) =>
    request<ResponsePlan>(`/plan/${runId}/review`, {
      method: 'POST',
      body: JSON.stringify({ actions }),
    }),

  getAlerts: (runId: string) => request<AlertDraft[]>(`/alerts/${runId}`),

  streamUrl: (runId: string) => `${BASE}/runs/${runId}/stream`,
};

export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
