// Run: npm test   (Node's built-in test runner; Node strips the TypeScript types itself)
import assert from "node:assert/strict";
import { test } from "node:test";
import { visibleRange } from "./range.ts";

const bars = [
  { start: 100, high: "10.00", low: "9.50" },
  { start: 160, high: "10.40", low: "9.80" },   // highest high
  { start: 220, high: "10.10", low: "9.20" },   // lowest low
  { start: 280, high: "9.90", low: "9.60" },
];

test("high / low of the visible bars and % from the current price", () => {
  const r = visibleRange(bars, 0, 3, 10.0)!;
  assert.equal(r.high, 10.4);
  assert.equal(r.highStart, 160);
  assert.equal(r.low, 9.2);
  assert.equal(r.lowStart, 220);
  assert.equal(r.fromHighPct.toFixed(4), "-3.8462");   // (10 − 10.4) / 10.4
  assert.equal(r.fromLowPct.toFixed(4), "8.6957");     // (10 − 9.2) / 9.2
});

test("only the bars on screen count; partial and out-of-range indices are clamped", () => {
  const r = visibleRange(bars, 2.4, 50, 9.9)!;          // bars 3 only (index 2 is cut in half -> excluded)
  assert.equal(r.high, 9.9);
  assert.equal(r.low, 9.6);
  assert.equal(r.fromHighPct, 0);
  const all = visibleRange(bars, -5, 2, 9.9)!;          // bars 0..2
  assert.equal(all.high, 10.4);
  assert.equal(all.low, 9.2);
});

test("nothing to show without bars or a price", () => {
  assert.equal(visibleRange([], 0, 3, 10), null);
  assert.equal(visibleRange(bars, 0, 3, null), null);
  assert.equal(visibleRange(bars, 10, 20, 10), null);   // no bar on screen
});
