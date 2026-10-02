import { useState } from 'react';
import { escapeHtml } from '../api/client';
import type { PlanItem, ResponsePlan } from '../types';

interface ReviewPanelProps {
  plan: ResponsePlan | null | undefined;
  onReview: (actions: { item_id: string; action: string; edited_content?: string }[]) => Promise<void>;
  loading: boolean;
}

export function ReviewPanel({ plan, onReview, loading }: ReviewPanelProps) {
  const [busy, setBusy] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');

  if (!plan) {
    return (
      <div className="rounded-xl border border-disaster-border bg-disaster-panel p-4">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-400">
          Human Review
        </h2>
        <p className="text-sm text-slate-500">Review panel activates when a draft plan exists.</p>
      </div>
    );
  }

  const pending = plan.items.filter((i) => i.status === 'pending');

  const act = async (item: PlanItem, action: string) => {
    setBusy(true);
    try {
      await onReview([
        {
          item_id: item.id,
          action,
          edited_content: action === 'edit' ? editText : undefined,
        },
      ]);
      setEditId(null);
      setEditText('');
    } finally {
      setBusy(false);
    }
  };

  const approveAll = async () => {
    setBusy(true);
    try {
      await onReview(pending.map((i) => ({ item_id: i.id, action: 'approve' })));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-xl border border-disaster-border bg-disaster-panel p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">
          Human Review
        </h2>
        {pending.length > 0 && !plan.is_final && (
          <button
            type="button"
            disabled={busy || loading}
            onClick={approveAll}
            className="rounded bg-disaster-success px-3 py-1 text-xs font-medium text-slate-900 hover:bg-green-400 disabled:opacity-50"
          >
            Approve All Pending
          </button>
        )}
      </div>

      {plan.is_final ? (
        <p className="text-sm text-green-300">
          Plan finalized at {plan.approved_at ? new Date(plan.approved_at).toLocaleString() : '—'}.
          No further edits without starting a new scenario.
        </p>
      ) : (
        <p className="mb-3 text-xs text-slate-500">
          Approve, edit, or reject each item. Plan locks only when all items are reviewed (no
          pending or rejected).
        </p>
      )}

      <ul className="max-h-64 space-y-2 overflow-y-auto">
        {plan.items.slice(0, 15).map((item) => (
          <li
            key={item.id}
            className="rounded-lg border border-disaster-border bg-slate-800/40 px-3 py-2 text-sm"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">{escapeHtml(item.title)}</span>
              <span className="text-xs uppercase text-slate-500">{item.status}</span>
            </div>
            {item.status === 'pending' && !plan.is_final && (
              <div className="mt-2 flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => act(item, 'approve')}
                  className="rounded bg-green-800 px-2 py-0.5 text-xs hover:bg-green-700 disabled:opacity-50"
                >
                  Approve
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => act(item, 'reject')}
                  className="rounded bg-red-900 px-2 py-0.5 text-xs hover:bg-red-800 disabled:opacity-50"
                >
                  Reject
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setEditId(item.id);
                    setEditText(item.edited_content ?? item.description);
                  }}
                  className="rounded bg-slate-700 px-2 py-0.5 text-xs hover:bg-slate-600 disabled:opacity-50"
                >
                  Edit
                </button>
              </div>
            )}
            {editId === item.id && (
              <div className="mt-2 space-y-2">
                <textarea
                  className="w-full rounded border border-disaster-border bg-slate-900 p-2 text-xs"
                  rows={2}
                  value={editText}
                  onChange={(e) => setEditText(e.target.value)}
                />
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => act(item, 'edit')}
                  className="rounded bg-disaster-accent px-2 py-0.5 text-xs text-slate-900"
                >
                  Save Edit
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
