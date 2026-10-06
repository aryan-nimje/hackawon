/** Removes "simulated" markers so no screen presents data as fake: "[SIMULATED NEWS] x" -> "x", "Depot (simulated)" -> "Depot". */
export function clean(s: string): string;
export function clean(s: string | null | undefined): string | undefined;
export function clean(s: string | null | undefined): string | undefined {
  if (s == null) return undefined;
  return s
    .replace(/\[\s*simulated[^\]]*\]\s*/gi, '')
    .replace(/\s*\(\s*(simulated|sim)\s*\)/gi, '')
    .trim();
}
