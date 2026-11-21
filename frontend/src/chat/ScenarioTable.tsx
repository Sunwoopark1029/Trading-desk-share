import type { ScenarioTable as Table } from "../api";
import { fmtPrice, signClass } from "../format";
import { useT } from "../i18n";
import { TickerLink } from "../TickerLink";
import type { Prefill } from "../orders/OrderTicket";
import { buttons, type OrderBtn } from "./scenarioButtons";

export { buttons };

/** Scenarios from the backend's numbers (risk/reward and after-cost move calculated in code), one card each:
 *  entry / stop / target side by side with the level each comes from, then the reasoning.
 *  With onOrder, each card gets buttons that pre-fill the order ticket (never a send). */
export default function ScenarioTable({ table, onOrder }: { table: Table; onOrder?: (p: Omit<Prefill, "nonce">) => void }) {
  const { t } = useT();
  const cur = table.currency || "USD";
  const canOrder = !!onOrder && cur === "USD";
  return (
    <div className="scenarios">
      <div className="muted small"><TickerLink sym={table.symbol} /> · {t(canOrder ? "scen.titleOrder" : "scen.title", { sym: table.symbol })}</div>
      <div className="scen-cards">
        {table.rows.map((r, i) => {
          const long = r.direction === "long";
          const net = Number(r.net_move_pct_est);
          return (
            <div key={i} className={`scen-card ${long ? "long" : "short"}${r.valid ? "" : " invalid"}`}>
              <div className="scen-top">
                <span className={`scen-dir ${long ? "up" : "down"}`}>{t(long ? "scen.longFull" : "scen.shortFull")}</span>
                <span className="scen-stat">{t("scen.rr")} <b>{r.risk_reward ?? "—"}</b></span>
                <span className="scen-stat" title={t("scen.afterTitle")}>
                  {t("scen.after")} <b className={signClass(r.net_move_pct_est)}>{net > 0 ? "+" : ""}{r.net_move_pct_est}%</b>
                  <span className="muted"> ({t("scen.perShareFull")} {Number(r.net_per_share_usd_est) > 0 ? "+" : ""}{r.net_per_share_usd_est} {cur}{!r.fx_cost_known && t("scen.fxNa")})</span>
                </span>
              </div>
              <div className="scen-levels">
                <div className="scen-lvl">
                  <div className="muted small">{t("scen.entry")}</div>
                  <div className="scen-price">{fmtPrice(r.entry, cur)}</div>
                  <div className="muted small">{r.entry_basis}</div>
                </div>
                <div className="scen-lvl stop">
                  <div className="muted small">{t("scen.stop")}</div>
                  <div className="scen-price">{fmtPrice(r.invalidation, cur)}</div>
                  <div className="muted small">{r.invalidation_basis}</div>
                </div>
                <div className="scen-lvl target">
                  <div className="muted small">{t("scen.target")}</div>
                  <div className="scen-price">{fmtPrice(r.target, cur)}</div>
                  <div className="muted small">{r.target_basis}</div>
                </div>
              </div>
              {r.quantity != null && (
                <div className="scen-qty small">
                  <b>{t("scen.qty", { q: String(r.quantity) })}</b>{" "}
                  <span className="muted">{t("scen.qtyAi")}</span>
                  {" · "}{t("scen.qtyCost", { c: fmtPrice(r.position_usd ?? null, cur) })}
                  {" · "}<span className="down">{t("scen.qtyLoss", { l: fmtPrice(r.loss_at_stop_usd_est ?? null, cur) })}</span>
                  {" · "}<span className={signClass(r.net_at_target_usd_est ?? null)}>{t("scen.qtyNet", { n: fmtPrice(r.net_at_target_usd_est ?? null, cur) })}</span>
                  {(r.quantity_warnings ?? []).map((w) => <div key={w} className="st-stale small">⚠ {w}</div>)}
                </div>
              )}
              <div className="scen-why">{r.reasoning}</div>
              {r.problems.map((p) => <div key={p} className="st-error small">⚠ {p}</div>)}
              {(r.price_warnings ?? []).map((p) => <div key={p} className="st-stale small">⚠ {p}</div>)}
              {canOrder && (() => {
                const { single, combined } = buttons(table.symbol, r);
                const btn = (b: OrderBtn) => (
                  <button key={b.key} className={`small ${b.p.kind.includes("BUY") || b.p.kind === "OTO" ? "buyish" : "sellish"}`}
                    disabled={!r.valid}
                    onClick={() => onOrder!({ ...b.p, from: `${table.symbol} · ${t(b.key, b.vars)}` })}>
                    {t(b.key, b.vars)}
                  </button>
                );
                return (
                  <>
                    <div className="scen-order">{single.map(btn)}</div>
                    {combined.length > 0 && (
                      <div className="scen-order combined"><span className="muted small">{t("scen.together")}</span>{combined.map(btn)}</div>
                    )}
                  </>
                );
              })()}
            </div>
          );
        })}
      </div>
    </div>
  );
}
