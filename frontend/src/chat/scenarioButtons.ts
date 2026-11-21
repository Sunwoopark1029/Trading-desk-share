/** Order buttons for one chat scenario row. Pure function (unit-tested in scenarioButtons.test.ts).
 *  Buttons only PRE-FILL the order ticket; the user checks every value, creates the draft and approves it. */
import type { ScenarioRowOut } from "../api";
import type { Key } from "../i18n";
import type { Prefill } from "../orders/OrderTicket";

/** US price step (Toss docs): 2 decimals at $1 and above, 4 below. */
export function tick(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "";
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(Math.abs(n) >= 1 ? 2 : 4) : "";
}

export type OrderBtn = { key: Key; vars: Record<string, string>; p: Omit<Prefill, "nonce" | "from"> };

/** Simple single orders always; combined orders (OTO / OCO) only when the AI marked the row as entry + exits,
 *  or exits only. An AI-suggested quantity (only when the user asked for one) is carried into every button. */
export function buttons(sym: string, r: ScenarioRowOut): { single: OrderBtn[]; combined: OrderBtn[] } {
  const e = tick(r.entry), s = tick(r.invalidation), g = tick(r.target);
  const long = r.direction === "long";
  const plan = r.plan ?? "entry";
  const q = r.quantity != null && Number.isInteger(r.quantity) && r.quantity > 0 ? { quantity: String(r.quantity) } : {};
  const leg = (p: string) => ({ trigger_price: p, order_price: p });
  const mk = (key: Key, vars: Record<string, string>, p: Omit<Prefill, "nonce" | "from" | "quantity">): OrderBtn =>
    ({ key, vars, p: { ...p, ...q } });
  if (long && plan === "exits") {          // protect shares already held: take-profit / stop
    return {
      single: [
        mk("scen.tpSell", { p: g }, { symbol: sym, kind: "SELL", order_type: "LIMIT", price: g }),
        mk("scen.stopSell", { p: s }, { symbol: sym, kind: "COND_SELL", order_type: "LIMIT", legs: [leg(s)] }),
      ],
      combined: [mk("scen.oco", { g, s }, { symbol: sym, kind: "OCO", order_type: "LIMIT", legs: [leg(g), leg(s)] })],
    };
  }
  const single: OrderBtn[] = long
    ? [mk("scen.buyAt", { p: e }, { symbol: sym, kind: "BUY", order_type: "LIMIT", price: e }),
       mk("scen.condBuyAt", { p: e }, { symbol: sym, kind: "COND_BUY", order_type: "LIMIT", legs: [leg(e)] })]
    : [mk("scen.sellAt", { p: e }, { symbol: sym, kind: "SELL", order_type: "LIMIT", price: e }),
       mk("scen.condSellAt", { p: e }, { symbol: sym, kind: "COND_SELL", order_type: "LIMIT", legs: [leg(e)] })];
  const combined: OrderBtn[] = long && plan === "entry_exits"
    ? [mk("scen.oto", { e, g }, { symbol: sym, kind: "OTO", order_type: "LIMIT", legs: [leg(e), leg(g)] }),
       mk("scen.oco", { g, s }, { symbol: sym, kind: "OCO", order_type: "LIMIT", legs: [leg(g), leg(s)] })]
    : [];
  return { single, combined };
}
