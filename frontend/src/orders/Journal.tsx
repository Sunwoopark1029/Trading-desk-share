import { useState } from "react";
import { orderApi, type JournalTrip, type Tagged } from "../api";
import { Tag } from "../components/PnlPanel";
import { fmtKstDateTime, fmtMoney, fmtNum, fmtPrice, signClass, tickerLabel } from "../format";
import { trBackend, useT, type Key } from "../i18n";
import { usePoll } from "../usePoll";
import { TickerLink } from "../TickerLink";

type Filter = "all" | "closed" | "open";

function duration(sec: number, t: (k: Key, v?: Record<string, string | number>) => string): string {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d > 0) return t("jr.dh", { d, h });
  if (h > 0) return t("jr.hm", { h, m });
  return t("jr.m", { m: Math.max(m, sec > 0 ? 1 : 0) });
}

function MoneyRow({ label, usd, krw, signed = true, strong = false }: { label: string; usd: Tagged; krw: Tagged; signed?: boolean; strong?: boolean }) {
  return (
    <tr className={strong ? "net" : ""}>
      <th>{label}</th>
      <td className={`num ${signed ? signClass(usd.value) : ""}`}>{fmtMoney(usd.value, "USD", signed)} <Tag tag={usd} /></td>
      <td className={`num ${signed ? signClass(krw.value) : ""}`}>{fmtMoney(krw.value, "KRW", signed)} <Tag tag={krw} /></td>
    </tr>
  );
}

/** Trade journal: only trades made with this program (calculated in the backend). Read only. */
export default function Journal() {
  const { t } = useT();
  const [filter, setFilter] = useState<Filter>("all");
  const j = usePoll(() => orderApi.journal(), 30_000);
  const d = j.data;
  const trips = (d?.trips ?? []).filter((r) => filter === "all" || r.status === filter);

  const card = (r: JournalTrip, i: number) => {
    const cur = r.currency;
    return (
      <div key={i} className={`jr-card ${r.status}`}>
        <div className="jr-top">
          <b className="jr-sym"><TickerLink sym={r.symbol}>{tickerLabel(r.symbol, d?.names[r.symbol])}</TickerLink></b>
          <span className={`tag ${r.status === "open" ? "tag-typed" : ""}`}>{t(r.status === "open" ? "jr.open" : "jr.closed")}</span>
          <span className="muted small">{t("jr.held", { d: duration(r.holding_seconds, t) })}{r.status === "open" ? t("jr.soFar") : ""}</span>
          <span className={`jr-net ${signClass(r.net_usd.value)}`}>{fmtMoney(r.net_usd.value, "USD", true)} <Tag tag={r.net_usd} /></span>
        </div>
        <div className="jr-legs">
          <div className="jr-leg">
            <div className="muted small">{t("jr.entry")}</div>
            <div>{r.entry_before_records ? t("jr.beforeRecords") : fmtKstDateTime(r.entry_time)}</div>
            <div className="jr-px">{t("jr.bought", { q: fmtNum(Number(r.buy_qty), Number(r.buy_qty) % 1 ? 4 : 0) })} @ {fmtPrice(r.avg_entry, cur)}</div>
          </div>
          <div className="jr-arrow">→</div>
          <div className="jr-leg">
            <div className="muted small">{t("jr.exit")}</div>
            <div>{r.status === "open" ? t("jr.stillHeld", { q: fmtNum(Number(r.held_qty), Number(r.held_qty) % 1 ? 4 : 0) })
              : r.exit_not_in_records ? t("jr.exitNotInRecords") : fmtKstDateTime(r.exit_time)}</div>
            <div className="jr-px">{Number(r.sell_qty) > 0
              ? <>{t("jr.sold", { q: fmtNum(Number(r.sell_qty), Number(r.sell_qty) % 1 ? 4 : 0) })} @ {fmtPrice(r.avg_exit, cur)}</>
              : "—"}</div>
          </div>
        </div>
        <table className="pnl-table jr-money">
          <thead><tr><th></th><th className="num">USD</th><th className="num">KRW</th></tr></thead>
          <tbody>
            <MoneyRow label={t("pnl.realized")} usd={r.realized_usd} krw={r.realized_krw} />
            {r.status === "open" && <MoneyRow label={t("pnl.unrealized")} usd={r.unrealized_usd} krw={r.unrealized_krw} />}
            <MoneyRow label={t("pnl.fees")} usd={r.fees_usd} krw={r.fees_krw} signed={false} />
            <MoneyRow label={t("pnl.fxCost")} usd={r.fx_cost_usd} krw={r.fx_cost_krw} signed={false} />
            <MoneyRow label={t("jr.ai")} usd={r.ai_credits_usd} krw={r.ai_credits_krw} signed={false} />
            <MoneyRow label={t("pnl.net")} usd={r.net_usd} krw={r.net_krw} strong />
          </tbody>
        </table>
        <div className="muted small">{t("jr.fills", { n: r.fills, p: r.program_fills })}</div>
      </div>
    );
  };

  return (
    <div className="orders-page journal">
      <section className="panel">
        <div className="panel-head">
          <h2>{t("jr.title")}</h2>
          <div className="seg">
            {(["all", "closed", "open"] as const).map((f) => (
              <button key={f} className={`small ${filter === f ? "on" : ""}`} onClick={() => setFilter(f)}>{t(`jr.f.${f}` as Key)}</button>
            ))}
          </div>
        </div>
        <div className="muted small">{t("jr.scope")}</div>
        {j.error && <div className="st-error small">{trBackend(j.error)}</div>}
        {!d && !j.error && <div className="muted">{t("app.loading")}</div>}
        {d && !d.ready && <div className="muted">{t("jr.notReady")}</div>}
        {d?.ready && d.total && d.total.trades > 0 && (
          <table className="pnl-table jr-total">
            <thead><tr><th>{t("jr.totals", { n: d.total.trades, c: d.total.closed })}</th><th className="num">USD</th><th className="num">KRW</th></tr></thead>
            <tbody>
              <MoneyRow label={t("pnl.realized")} usd={d.total.realized_usd} krw={d.total.realized_krw} />
              <MoneyRow label={t("pnl.unrealized")} usd={d.total.unrealized_usd} krw={d.total.unrealized_krw} />
              <MoneyRow label={t("pnl.fees")} usd={d.total.fees_usd} krw={d.total.fees_krw} signed={false} />
              <MoneyRow label={t("pnl.fxCost")} usd={d.total.fx_cost_usd} krw={d.total.fx_cost_krw} signed={false} />
              <MoneyRow label={t("jr.aiOnce")} usd={d.total.ai_credits_usd} krw={d.total.ai_credits_krw} signed={false} />
              <MoneyRow label={t("pnl.net")} usd={d.total.net_usd} krw={d.total.net_krw} strong />
            </tbody>
          </table>
        )}
        {d?.ready && trips.length === 0 && (
          <div className="muted">{d.trips.length === 0 ? t(d.dry_run ? "jr.emptyDry" : "jr.empty") : t("jr.noneFilter")}</div>
        )}
        {d?.notes.map((n) => <div key={n} className="st-stale small">{trBackend(n)}</div>)}
      </section>
      <div className="jr-cards">{trips.map(card)}</div>
    </div>
  );
}
