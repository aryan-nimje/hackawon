import { escapeHtml } from '../api/client';
import type { ResponsePlan } from '../types';

interface DraftPlanProps {
  plan: ResponsePlan | null | undefined;
  loading: boolean;
  error: string | null;
}

export function DraftPlan({ plan, loading, error }: DraftPlanProps) {
  return (
    <div className="rounded-xl border border-disaster-border bg-disaster-panel p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">
          Draft Response Plan
        </h2>
        {plan?.is_final && (
          <span className="rounded-full bg-disaster-success/20 px-2 py-0.5 text-xs text-green-300">
            Approved &amp; Final
          </span>
        )}
      </div>

      {error && <p className="text-sm text-red-300">{error}</p>}
      {loading && !plan && <p className="text-sm text-slate-500">Generating plan…</p>}
      {!loading && !plan && (
        <p className="text-sm text-slate-500">Plan will appear after agents complete.</p>
      )}

      {plan && (
        <div className="max-h-96 space-y-3 overflow-y-auto">
          {plan.items.length === 0 && (
            <p className="text-sm text-slate-500">No plan items yet.</p>
          )}
          {plan.items.slice(0, 20).map((item) => (
            <div
              key={item.id}
              className={`rounded-lg border px-3 py-2 text-sm ${
                item.incomplete
                  ? 'border-amber-600/50 bg-amber-950/20'
                  : 'border-disaster-border bg-slate-800/40'
              }`}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-slate-200">{escapeHtml(item.title)}</span>
                <span className="rounded bg-slate-700 px-1.5 text-xs uppercase">{item.category}</span>
                <span className="rounded bg-slate-700 px-1.5 text-xs">{item.status}</span>
                {item.incomplete && (
                  <span className="text-xs text-amber-400">INCOMPLETE</span>
                )}
              </div>
              <p className="text-slate-400">{escapeHtml(item.description)}</p>
              <p className="mt-1 text-xs italic text-slate-500">
                Why: {escapeHtml(item.reasoning)}
              </p>
            </div>
          ))}
          {plan.items.length > 20 && (
            <p className="text-xs text-slate-500">+ {plan.items.length - 20} more items</p>
          )}
        </div>
      )}
    </div>
  );
}
