/** Highest high and lowest low of the bars visible on the chart, and how far the current price is from each.
 *  Pure function (unit-tested in range.test.ts). Bar indices = the chart's logical indices. */

export interface RangeBar {
  start: number;
  high: string | number;
  low: string | number;
}

export interface VisibleRange {
  high: number;
  highStart: number;
  low: number;
  lowStart: number;
  fromHighPct: number; // (current − high) / high × 100  (≤ 0 while below the high)
  fromLowPct: number;  // (current − low) / low × 100    (≥ 0 while above the low)
}

export function visibleRange(bars: RangeBar[], from: number, to: number, current: number | null): VisibleRange | null {
  if (!bars.length || current === null || !Number.isFinite(current) || !Number.isFinite(from) || !Number.isFinite(to)) {
    return null;
  }
  const i0 = Math.max(0, Math.ceil(Math.min(from, to)));
  const i1 = Math.min(bars.length - 1, Math.floor(Math.max(from, to)));
  let hi = -Infinity, lo = Infinity, hiAt = 0, loAt = 0;
  for (let i = i0; i <= i1; i++) {
    const h = Number(bars[i].high), l = Number(bars[i].low);
    if (Number.isFinite(h) && h > hi) { hi = h; hiAt = bars[i].start; }
    if (Number.isFinite(l) && l < lo) { lo = l; loAt = bars[i].start; }
  }
  if (!Number.isFinite(hi) || !Number.isFinite(lo) || hi <= 0 || lo <= 0) return null;
  return {
    high: hi, highStart: hiAt, low: lo, lowStart: loAt,
    fromHighPct: ((current - hi) / hi) * 100,
    fromLowPct: ((current - lo) / lo) * 100,
  };
}
