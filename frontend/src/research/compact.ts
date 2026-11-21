// Compact money / share-count text for the statement tables: "USD 35.1B", "15.40B" shares, "USD 6.08" per share.
// Display only: the exact value comes from the backend (calculated there). No imports, so Node's test runner can load it.

function toNum(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function group(n: number, dp: number): string {
  return n.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

export function compact(v: string | number | null | undefined, measure: string, currency: string | null): string {
  const n = toNum(v);
  if (n === null) return "—";
  const sign = n < 0 ? "−" : "";
  const a = Math.abs(n);
  const cur = currency ? `${currency} ` : "";
  if (measure === "per_share") return `${sign}${cur}${group(a, 2)}`;
  const dp = measure === "shares" ? 2 : 1;
  const [d, s] = a >= 1e12 ? [1e12, "T"] : a >= 1e9 ? [1e9, "B"] : a >= 1e6 ? [1e6, "M"] : [1, ""];
  let body = s ? `${group(a / d, dp)}${s}` : group(a, 0);
  // rounding can reach the next unit (999.96M -> "1,000.0M"): step up instead
  if (s && Number((a / d).toFixed(dp)) >= 1000 && s !== "T") body = `${group(a / (d * 1000), dp)}${s === "M" ? "B" : "T"}`;
  return measure === "shares" ? `${sign}${body}` : `${sign}${cur}${body}`;
}
