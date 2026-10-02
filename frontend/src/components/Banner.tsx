interface BannerProps {
  mockMode: boolean;
}

export function Banner({ mockMode }: BannerProps) {
  return (
    <div className="space-y-2">
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-2 text-sm text-amber-200">
        Simulated data. Decision-support prototype. Not for real emergency use.
      </div>
      {mockMode && (
        <div className="inline-flex items-center rounded-full border border-purple-400/50 bg-purple-500/20 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-purple-200">
          Mock Mode
        </div>
      )}
    </div>
  );
}
