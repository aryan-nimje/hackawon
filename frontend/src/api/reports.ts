import type { ReportReceipt, ReportStatusView, ReportSubmission } from '../types';
import { ApiError, request } from './client';
import { demoStatus, demoSubmit } from './reportsDemo';

export interface SubmitResult extends ReportReceipt {
  /** true when the backend has no /reports yet and the report was only stored in this browser */
  demo: boolean;
}

/** Backend missing the route / unreachable → fall back to demo. Real 4xx/5xx errors surface to the user. */
function backendMissing(e: unknown): boolean {
  if (e instanceof ApiError) return e.status === 404 || e.status === 405 || e.status === 501;
  return e instanceof TypeError; // fetch network failure
}

export const reportsApi = {
  async submit(body: ReportSubmission): Promise<SubmitResult> {
    try {
      const r = await request<ReportReceipt>('/reports', { method: 'POST', body: JSON.stringify(body) });
      return { ...r, demo: false };
    } catch (e) {
      if (!backendMissing(e)) throw e;
      return { ...demoSubmit(body), demo: true };
    }
  },

  async status(token: string): Promise<ReportStatusView> {
    if (token.startsWith('demo-')) {
      const v = demoStatus(token);
      if (!v) throw new ApiError(404, 'Report not found on this device');
      return v;
    }
    return request<ReportStatusView>(`/reports/${encodeURIComponent(token)}`);
  },
};

const TOKENS_KEY = 'relief.myReports.v1';

export function rememberToken(token: string) {
  try {
    const list = loadTokens();
    localStorage.setItem(TOKENS_KEY, JSON.stringify([token, ...list.filter((t) => t !== token)].slice(0, 10)));
  } catch {
    /* ignore */
  }
}

export function loadTokens(): string[] {
  try {
    return JSON.parse(localStorage.getItem(TOKENS_KEY) ?? '[]') as string[];
  } catch {
    return [];
  }
}
