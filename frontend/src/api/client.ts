import type {
  AlertDraft,
  HealthResponse,
  Incident,
  ResponsePlan,
  RunState,
} from '../types';

const BASE = import.meta.env.VITE_API_BASE_URL || '/api';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new ApiError(res.status, text || `HTTP ${res.status}`);
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

/**
 * React already escapes text rendered via JSX, so escaping here as well shows
 * literal "&#039;" to users. This only strips control characters from
 * untrusted text; never pass the result to dangerouslySetInnerHTML.
 */
export function escapeHtml(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, '');
}
