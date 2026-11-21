// Display helpers. All times are shown in Korea time (KST); every money value carries
// its currency. Values stay exact strings until the moment they are displayed.

import { tr } from "./i18n";

export const KST_TZ = "Asia/Seoul";

const kstTime = new Intl.DateTimeFormat("en-GB", {
  timeZone: KST_TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
const kstHm = new Intl.DateTimeFormat("en-GB", {
  timeZone: KST_TZ, hour: "2-digit", minute: "2-digit", hour12: false,
});
const kstDateTime = new Intl.DateTimeFormat("en-GB", {
  timeZone: KST_TZ, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});
const kstDate = new Intl.DateTimeFormat("en-CA", {
  timeZone: KST_TZ, year: "numeric", month: "2-digit", day: "2-digit",
});
const utcDate = new Intl.DateTimeFormat("en-CA", {
  timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit",
});

function toDate(t: string | number | Date): Date {
  if (t instanceof Date) return t;
  return typeof t === "number" ? new Date(t * 1000) : new Date(t);
}

/** Korean stocks are number codes (005930): show the name instead; US tickers stay tickers. */
export function tickerLabel(symbol: string, name?: string | null): string {
  return /^[0-9][0-9A-Z]{5}$/.test(symbol) && name ? name : symbol;
}

/** "14:05:09 KST" */
export function fmtKstTime(t: string | number | null | undefined): string {
  if (t === null || t === undefined) return "—";
  return `${kstTime.format(toDate(t))} KST`;
}

/** "11/16 14:05 KST" */
export function fmtKstDateTime(t: string | number | null | undefined): string {
  if (t === null || t === undefined) return "—";
  return `${kstDateTime.format(toDate(t))} KST`;
}

/** Chart axis label for an epoch-second bar time (intraday = KST, daily = US trading date). */
export function chartTick(epoch: number, daily: boolean, withDate: boolean): string {
  const d = new Date(epoch * 1000);
  if (daily) return utcDate.format(d);
  return withDate ? kstDateTime.format(d) : kstHm.format(d);
}

export function chartCrosshair(epoch: number, daily: boolean): string {
  const d = new Date(epoch * 1000);
  return daily ? `${utcDate.format(d)} ${tr("chart.usDate")}` : `${kstDate.format(d)} ${kstHm.format(d)} KST`;
}

export function num(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Group thousands; fixed decimals. */
export function fmtNum(v: string | number | null | undefined, dp: number): string {
  const n = num(v);
  if (n === null) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

/** Share price: up to 4 dp for prices under $10, else 2 dp. */
export function priceDp(v: number): number {
  return Math.abs(v) < 10 ? 4 : 2;
}

/** "USD 4.1200" / "KRW 6,500" */
export function fmtPrice(v: string | number | null | undefined, currency: string | null | undefined): string {
  const n = num(v);
  if (n === null) return "—";
  const cur = currency || "";
  if (cur === "KRW") return `KRW ${fmtNum(n, 0)}`;
  return `${cur ? cur + " " : ""}${fmtNum(n, priceDp(n))}`;
}

/** Money amount: USD 2 dp, KRW 0 dp. Signed when `signed`. */
export function fmtMoney(v: string | number | null | undefined, currency: "USD" | "KRW", signed = false): string {
  const n = num(v);
  if (n === null) return "—";
  const body = fmtNum(Math.abs(n), currency === "KRW" ? 0 : 2);
  const sign = n < 0 ? "−" : signed && n > 0 ? "+" : "";
  return `${sign}${currency} ${body}`;
}

export function fmtPct(v: string | number | null | undefined): string {
  const n = num(v);
  if (n === null) return "";
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(2)}%`;
}

export function signClass(v: string | number | null | undefined): string {
  const n = num(v);
  if (n === null || n === 0) return "";
  return n > 0 ? "up" : "down";
}

/** "12 s ago", "4 min ago", "3 h ago" */
export function fmtAge(t: string | null | undefined, now: number): string {
  if (!t) return tr("age.never");
  const s = Math.max(0, Math.round((now - new Date(t).getTime()) / 1000));
  if (s < 90) return tr("age.s", { n: s });
  if (s < 5400) return tr("age.min", { n: Math.round(s / 60) });
  if (s < 172800) return tr("age.h", { n: Math.round(s / 3600) });
  return tr("age.d", { n: Math.round(s / 86400) });
}

/** ISO 8601 in KST (+09:00) for an epoch-second time, e.g. for saved overlay points. */
export function epochToKstIso(epoch: number): string {
  const d = new Date((epoch + 9 * 3600) * 1000);
  return d.toISOString().replace(/\.\d{3}Z$/, "+09:00");
}
