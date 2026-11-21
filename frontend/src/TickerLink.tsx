import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useT } from "./i18n";

/** Opens the Research page for a ticker (App sets the current ticker and switches the view). */
const OpenResearch = createContext<((symbol: string) => void) | null>(null);
export const OpenResearchProvider = OpenResearch.Provider;

/** A clickable ticker: switches the app to this ticker and opens Research. Outside the provider it is plain text. */
export function TickerLink({ sym, children, className }: { sym: string; children?: ReactNode; className?: string }) {
  const open = useContext(OpenResearch);
  const { t } = useT();
  if (!open || !sym) return <>{children ?? sym}</>;
  return (
    <button type="button" className={`tick-link${className ? ` ${className}` : ""}`} title={t("tick.open", { sym })}
      onClick={(e) => { e.stopPropagation(); open(sym); }}>
      {children ?? sym}
    </button>
  );
}

// ── the real US tickers (Toss list), loaded once per page: chat answers link only these ──────────────

let known: Set<string> | null = null;
let loading: Promise<Set<string>> | null = null;

function loadKnown(): Promise<Set<string>> {
  if (known) return Promise.resolve(known);
  loading ??= fetch("/api/tickers/symbols")
    .then((r) => (r.ok ? r.json() : { symbols: [] }))
    .then((b: { symbols?: string[] }) => {
      const s = new Set(b.symbols ?? []);
      if (s.size > 0) known = s;                    // empty (list not loaded yet): try again next time
      loading = null;
      return s;
    })
    .catch(() => { loading = null; return new Set<string>(); });
  return loading;
}

export function useKnownTickers(): Set<string> | null {
  const [set, setSet] = useState<Set<string> | null>(known);
  useEffect(() => {
    if (!known) void loadKnown().then((s) => { if (s.size) setSet(s); });
  }, []);
  return set;
}

export { NOT_TICKERS, remarkTickers } from "./tickerText";
