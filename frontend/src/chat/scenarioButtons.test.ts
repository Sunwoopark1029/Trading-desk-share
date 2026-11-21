// Run: npm test   (Node's built-in test runner; Node strips the TypeScript types itself)
import assert from "node:assert/strict";
import { test } from "node:test";
import type { ScenarioRowOut } from "../api.ts";
import { buttons, tick } from "./scenarioButtons.ts";

const row = (over: Partial<ScenarioRowOut> = {}): ScenarioRowOut => ({
  direction: "long", entry: "18.4", invalidation: "17.9", target: "19.5",
  entry_basis: "a", invalidation_basis: "b", target_basis: "c", reasoning: "d",
  risk_per_share: "0.5", reward_per_share: "1.1", risk_reward: "2.20", gross_move_pct: "5.98",
  costs_pct_est: "0.2", fx_cost_known: false, spread_pct_est: null, net_move_pct_est: "5.78",
  net_per_share_usd_est: "1.06", problems: [], valid: true, ...over,
} as ScenarioRowOut);

test("price step rounding", () => {
  assert.equal(tick("18.4"), "18.40");
  assert.equal(tick("0.12345"), "0.1235");
  assert.equal(tick(null), "");
});

test("no quantity unless the AI row has one (the user asked)", () => {
  const { single } = buttons("SOXS", row());
  assert.equal(single[0].p.price, "18.40");
  assert.equal(single[0].p.quantity, undefined);
  assert.equal(single[1].p.quantity, undefined);
});

test("AI-suggested quantity goes into every button, price unchanged", () => {
  const { single, combined } = buttons("SOXS", row({ quantity: 27, plan: "entry_exits" }));
  assert.deepEqual(single.map((b) => b.p.quantity), ["27", "27"]);
  assert.deepEqual(combined.map((b) => b.p.quantity), ["27", "27"]);
  assert.equal(single[0].p.kind, "BUY");
  assert.equal(single[0].p.price, "18.40");
});

test("exits rows carry the quantity to the sells", () => {
  const { single, combined } = buttons("SOXS", row({ quantity: 5, plan: "exits" }));
  assert.deepEqual(single.map((b) => [b.p.kind, b.p.quantity]), [["SELL", "5"], ["COND_SELL", "5"]]);
  assert.equal(combined[0].p.quantity, "5");
});

test("bad quantity values are ignored", () => {
  for (const q of [0, -1, 2.5, null]) {
    assert.equal(buttons("SOXS", row({ quantity: q as number | null })).single[0].p.quantity, undefined);
  }
});
