import type { ButtonHTMLAttributes, ReactNode } from 'react';

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-2xl border border-[#e8e4dc] bg-white p-4 shadow-sm ${className}`}>{children}</section>;
}

export function StepHeader({ n, title, right }: { n: number | string; title: string; right?: ReactNode }) {
  return (
    <div className="mb-3 flex items-center justify-between gap-2">
      <h2 className="flex items-center gap-2 text-sm font-semibold">
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-[#2b6f73] text-[11px] font-bold text-white">{n}</span>
        {title}
      </h2>
      {right}
    </div>
  );
}

export function Chip({ active, children, onClick, tone }: { active?: boolean; children: ReactNode; onClick?: () => void; tone?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`min-h-8 rounded-lg border px-3 text-xs ${active ? 'border-[#2b6f73] bg-[#e3eeee] text-[#1f4f52]' : 'border-[#e8e4dc] bg-white text-[#444] hover:bg-[#faf8f4]'}`}
    >
      {tone && <span className="mr-1.5 inline-block h-2 w-2 rounded-full" style={{ background: tone }} />}
      {children}
    </button>
  );
}

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'danger'; small?: boolean };
export function Button({ variant = 'secondary', small, className = '', ...rest }: BtnProps) {
  const v =
    variant === 'primary' ? 'bg-[#2b6f73] text-white hover:bg-[#245d60]'
    : variant === 'danger' ? 'bg-[#b3261e] text-white hover:bg-[#9a1f19]'
    : 'border border-[#e8e4dc] bg-white text-[#333] hover:bg-[#faf8f4]';
  return <button type="button" {...rest} className={`rounded-lg font-medium disabled:cursor-not-allowed disabled:opacity-50 ${small ? 'min-h-8 px-3 text-xs' : 'min-h-10 px-4 text-sm'} ${v} ${className}`} />;
}

export function Banner({ tone = 'info', children }: { tone?: 'info' | 'error' | 'warn'; children: ReactNode }) {
  const c = tone === 'error' ? 'border-[#f0c4bf] bg-[#fbe9e7] text-[#8c1d17]' : tone === 'warn' ? 'border-[#eadfbf] bg-[#f6efd9] text-[#6b5514]' : 'border-[#e8e1d3] bg-[#f1ece1] text-[#3d3a33]';
  return <div role="note" className={`rounded-lg border px-3 py-2 text-xs font-medium ${c}`}>{children}</div>;
}

export function Badge({ children, color = '#6b6b6b' }: { children: ReactNode; color?: string }) {
  return <span className="rounded-md px-1.5 py-0.5 text-[11px] font-semibold uppercase" style={{ background: `${color}22`, color }}>{children}</span>;
}

export interface ToastMsg { id: number; text: string; tone: 'info' | 'error' | 'warn' }
export function Toasts({ items, onDismiss }: { items: ToastMsg[]; onDismiss: (id: number) => void }) {
  return (
    <div className="pointer-events-none fixed right-3 top-3 z-[2000] flex w-80 max-w-[calc(100vw-1.5rem)] flex-col gap-2" aria-live="polite">
      {items.map((t) => (
        <button key={t.id} type="button" onClick={() => onDismiss(t.id)}
          className={`pointer-events-auto rounded-xl border px-3 py-2 text-left text-xs shadow-md ${t.tone === 'error' ? 'border-[#f0c4bf] bg-[#fbe9e7] text-[#8c1d17]' : t.tone === 'warn' ? 'border-[#eadfbf] bg-[#f6efd9] text-[#6b5514]' : 'border-[#e8e4dc] bg-white text-[#333]'}`}>
          {t.text}
        </button>
      ))}
    </div>
  );
}

export const URGENCY_COLOR: Record<string, string> = { critical: '#b3261e', high: '#e07b1a', medium: '#d9a406', low: '#7a8b86' };
