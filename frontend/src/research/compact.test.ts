// Run: npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import { compact } from "./compact.ts";

test("money picks a scale and keeps the currency", () => {
  assert.equal(compact("391035000000", "money", "USD"), "USD 391.0B");
  assert.equal(compact("3500000000000", "money", "USD"), "USD 3.5T");
  assert.equal(compact("12500000", "money", "TWD"), "TWD 12.5M");
  assert.equal(compact("950000", "money", "USD"), "USD 950,000");
});

test("negatives use the minus sign", () => {
  assert.equal(compact("-2100000000", "money", "USD"), "−USD 2.1B");
});

test("shares and per-share values", () => {
  assert.equal(compact("15408095000", "shares", null), "15.41B");
  assert.equal(compact("6.08", "per_share", "USD"), "USD 6.08");
  assert.equal(compact("-0.5", "per_share", "USD"), "−USD 0.50");
});

test("rounding up to the next unit steps the scale", () => {
  assert.equal(compact("999960000", "money", "USD"), "USD 1.0B");
});

test("missing values show a dash", () => {
  assert.equal(compact(null, "money", "USD"), "—");
  assert.equal(compact("", "money", "USD"), "—");
  assert.equal(compact("abc", "money", "USD"), "—");
});
