// Saved research thesis -> chart lines (phase 6 part C). Only the reference prices the user ticked "show on the
// chart", only when the thesis is in USD like the chart. Derived lines: full width, labeled on the right axis.
// No imports at runtime, so Node's test runner can load it.
import type { Overlay, RsThesis } from "../api";

export const THESIS_COLOR = "#ab47bc";

export function thesisLines(th: RsThesis | null | undefined, symbol: string): Overlay[] {
  if (!th || th.symbol !== symbol || th.currency !== "USD") return [];
  return th.reference_levels
    .filter((r) => r.show_on_chart && r.price !== null && r.price !== undefined && r.price !== "")
    .map((r) => ({
      id: `thesis-${r.id}`, type: "hline" as const, points: [{ time: th.saved_at, price: String(r.price) }],
      label: `Thesis: ${r.label}`, color: THESIS_COLOR, source: "indicator" as const, visible: true,
      note: `${r.display} · ${r.source}${r.as_of ? ` · ${r.as_of}` : ""} (research thesis, ${r.id})`,
      symbol, derived: true, dashed: true, group: "research" as const,
    }));
}
