/**
 * Browser-only stand-in for POST /reports and GET /reports/{token}, used when the backend
 * does not implement them yet. Reports live in localStorage on this device only and their
 * status advances on a timer. The simulation page can also read them (see sim/localSim).
 */
import type { ReportReceipt, ReportStatus, ReportStatusView, ReportSubmission } from '../types';

const KEY = 'relief.demoReports.v1';

export interface DemoReport {
  token: string;
  created_at: string;
  submission: ReportSubmission;
}

const STEPS: { status: ReportStatus; afterS: number; note: string }[] = [
  { status: 'received', afterS: 0, note: 'We have your report.' },
  { status: 'verifying', afterS: 6, note: 'A coordinator system is checking your report.' },
  { status: 'prioritized', afterS: 16, note: 'Your report has been ranked in the response queue.' },
  { status: 'assigned', afterS: 30, note: 'A response team has been assigned (simulated).' },
  { status: 'resolved', afterS: 55, note: 'Marked as resolved (simulated).' },
];

function read(): DemoReport[] {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as DemoReport[]) : [];
  } catch {
    return [];
  }
}

function write(list: DemoReport[]) {
  try {
    localStorage.setItem(KEY, JSON.stringify(list.slice(-50)));
  } catch {
    /* storage unavailable (private mode): demo reports just won't persist */
  }
}

/** Forget all browser-stored demo reports (they are not tied to any run, so a new sim must not inherit them). */
export function clearDemoReports() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable */
  }
}

export function listDemoReports(): DemoReport[] {
  return read();
}

export function demoSubmit(submission: ReportSubmission): ReportReceipt {
  const token = `demo-${crypto.randomUUID().replace(/-/g, '').slice(0, 14)}`;
  const created_at = new Date().toISOString();
  write([...read(), { token, created_at, submission }]);
  return { token, status: 'received', created_at };
}

export function demoStatus(token: string): ReportStatusView | null {
  const rep = read().find((r) => r.token === token);
  if (!rep) return null;
  const elapsed = (Date.now() - new Date(rep.created_at).getTime()) / 1000;
  const reached = STEPS.filter((s) => elapsed >= s.afterS);
  const current = reached[reached.length - 1];
  return {
    token,
    status: current.status,
    updated_at: new Date(new Date(rep.created_at).getTime() + current.afterS * 1000).toISOString(),
    history: reached.map((s) => ({
      status: s.status,
      at: new Date(new Date(rep.created_at).getTime() + s.afterS * 1000).toISOString(),
    })),
    summary: rep.submission.text.slice(0, 120),
    note: current.note,
  };
}
