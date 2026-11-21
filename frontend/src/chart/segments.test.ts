// Run: npm test   (Node's built-in test runner; Node strips the TypeScript types itself)
import assert from "node:assert/strict";
import { test } from "node:test";
import { barIndex, buildShapes, FIB_RATIO_COLORS, type ShapeInput, type ShapeOpts } from "./segments.ts";

const starts = Array.from({ length: 100 }, (_, i) => 1000 + i * 60);   // 100 one-minute bars
const opts: ShapeOpts = {
  starts, extendBars: 8, backBars: 60, fillAlpha: 0.1,
  fmt: (p) => p.toFixed(2), fade: (c, a) => `${c}@${a}`,
};
const base = { id: "x", label: "AI 저항", color: "#29b6f6", width: 1, dash: [] as number[] };

test("bar index: floor to the bar containing the time, clamped", () => {
  assert.equal(barIndex(starts, 0), 0);
  assert.equal(barIndex(starts, 1000), 0);
  assert.equal(barIndex(starts, 1000 + 5 * 60 + 30), 5);   // inside bar 5
  assert.equal(barIndex(starts, 999999), 99);
  assert.equal(barIndex([], 5), 0);
});

test("horizontal line floats from its start point to just past the last candle", () => {
  const s = buildShapes([{ ...base, type: "hline", points: [{ time: 1000 + 20 * 60, price: 42.61 }] }], opts);
  assert.deepEqual([s.lines[0].x1, s.lines[0].x2, s.lines[0].p1, s.lines[0].p2], [20, 107, 42.61, 42.61]);
  assert.deepEqual(s.labels[0], { id: "x", x: 20, p: 42.61, text: "AI 저항 (42.61)", color: "#29b6f6", align: "edge" });
});

test("a line anchored at the latest candle starts backBars earlier (not a stub)", () => {
  const s = buildShapes([{ ...base, type: "hline", points: [{ time: 1000 + 99 * 60, price: 42 }] }], opts);
  assert.equal(s.lines[0].x1, 39);
});

test("zone: box with two edges, fill, one label with the range", () => {
  const s = buildShapes([{ ...base, type: "zone", points: [{ time: 1000 + 10 * 60, price: 41.9 }, { time: 1000 + 30 * 60, price: 42.1 }] }], opts);
  assert.equal(s.lines.length, 2);
  assert.deepEqual(s.fills[0], { x1: 10, x2: 107, top: 42.1, bottom: 41.9, color: "#29b6f6@0.1" });
  assert.equal(s.labels[0].text, "AI 저항 (41.90–42.10)");
  assert.equal(s.labels[0].align, "edge");                  // flat line: label on the chart's left side
});

test("fib: box from the first swing point, Toss colors per ratio, labels left of the box, bands between", () => {
  const fib: ShapeInput = {
    ...base, label: "AI Fib", type: "fib",
    points: [{ time: 1000 + 70 * 60, price: 41.68 }, { time: 1000 + 40 * 60, price: 42.88 }],
    levels: [{ ratio: "1", price: 41.68 }, { ratio: "0.618", price: 42.1384 }, { ratio: "0.000", price: 42.88 }],
  };
  const s = buildShapes([fib], opts);
  assert.ok(s.lines.every((l) => l.x1 === 40 && l.x2 === 107));
  assert.deepEqual(s.labels.map((l) => l.text), ["AI Fib · 0 (42.88)", "0.618 (42.14)", "1 (41.68)"]);
  assert.ok(s.labels.every((l) => l.align === "right"));
  assert.equal(s.lines[1].color, FIB_RATIO_COLORS["0.618"]);
  assert.equal(s.fills.length, 2);
  assert.deepEqual([s.fills[0].top, s.fills[0].bottom], [42.88, 42.1384]);
});

test("trendline: between its two points, then continues to the right edge on the same slope", () => {
  const s = buildShapes([{ ...base, type: "trendline", points: [{ time: 1000 + 60 * 60, price: 42 }, { time: 1000 + 50 * 60, price: 41 }] }], opts);
  const l = s.lines[0];
  assert.deepEqual([l.x1, l.p1, l.x2], [50, 41, 107]);
  assert.ok(Math.abs(l.p2 - (41 + 0.1 * 57)) < 1e-9);       // slope 0.1 per bar
  assert.equal(buildShapes([{ ...base, type: "trendline", points: [{ time: 1000, price: 1 }, { time: 1010, price: 2 }] }], opts).lines.length, 0);
});

test("no bars: nothing drawn", () => {
  assert.deepEqual(buildShapes([{ ...base, type: "hline", points: [{ time: 1, price: 1 }] }], { ...opts, starts: [] }),
    { lines: [], fills: [], labels: [] });
});
