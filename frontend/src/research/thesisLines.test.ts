// Run: npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import { thesisLines } from "./thesisLines.ts";

const th = {
  id: 1, symbol: "AAPL", saved_at: "2026-11-21T05:00:00+00:00", saved_by: "user", current: true,
  verdict: {}, summary: "s", basis: [], risks: [], change_view: [], price_at_save: "200", currency: "USD",
  latest_filed: null, conversation_id: null,
  reference_levels: [
    { id: "fv_pe_own_median", label: "Price at own median P/E", price: "150.5", display: "USD 150.50",
      currency: "USD", source: "calculated", as_of: null, show_on_chart: true },
    { id: "target_mean", label: "Analyst target (mean)", price: "250", display: "USD 250.00",
      currency: "USD", source: "yfinance (unofficial)", as_of: null, show_on_chart: false },
  ],
} as const;

test("only ticked reference prices, as derived research lines", () => {
  const out = thesisLines(th as never, "AAPL");
  assert.equal(out.length, 1);
  assert.equal(out[0].id, "thesis-fv_pe_own_median");
  assert.equal(out[0].points[0].price, "150.5");
  assert.equal(out[0].group, "research");
  assert.equal(out[0].derived, true);
});

test("no lines for another ticker, a non-USD thesis or no thesis", () => {
  assert.deepEqual(thesisLines(th as never, "NVDA"), []);
  assert.deepEqual(thesisLines({ ...th, currency: "TWD" } as never, "AAPL"), []);
  assert.deepEqual(thesisLines(null, "AAPL"), []);
});
