import { useState } from "react";
import { accountApi, api, type Tagged } from "../api";
import { fmtKstDateTime, fmtMoney, fmtNum, fmtPrice, signClass, tickerLabel } from "../format";
import { trBackend, useT, type Key } from "../i18n";
import { usePoll } from "../usePoll";
import { TickerLink } from "../TickerLink";
import StatusLine from "./Status";

interface Props {
  pollMs: number;
  now: number;
}

const TAG_KEY: Record<Tagged["status"], Key> = {
  "confirmed": "tag.confirmed", "typed": "tag.typed", "est.": "tag.est", "not set": "tag.notSet", "missing": "tag.missing",
};

export function Tag({ tag }: { tag: Tagged }) {
  const { t } = useT();
  if (tag.status === "confirmed") return null;
  return <span className={`tag tag-${tag.status.replace(/[^a-z]/g, "")}`}>{t(TAG_KEY[tag.status])}</span>;
}

function Money({ usd, krw, signed = true, title }: { usd: Tagged; krw: Tagged; signed?: boolean; title?: string }) {
  return (
    <>
      <td className={`num ${signed ? signClass(usd.value) : ""}`} title={title}>{fmtMoney(usd.value, "USD", signed)} <Tag tag={usd} /></td>
      <td className={`num ${signed ? signClass(krw.value) : ""}`} title={title}>{fmtMoney(krw.value, "KRW", signed)} <Tag tag={krw} /></td>
    </>
  );
}

/** Compact P&L. Net = realized + unrealized − fees − FX cost − AI credits. Details live under "Notes". */
export default function PnlPanel({ pollMs, now }: Props) {
  const { t } = useT();
  const [period, setPeriod] = useState<"today" | "all">("all");
  const [showSold, setShowSold] = useState(false);
  const pnl = usePoll(() => api.pnl(period), pollMs, [period]);
  const d = pnl.data;
  const acct = usePoll(() => accountApi.get(), pollMs);   // cached on the backend, no extra Toss call
  const a = acct.data;

  const feeTitle = d ? `${t("pnl.confirmed")} ${fmtMoney(d.total.fees_confirmed_usd.value, "USD")} / ${fmtMoney(d.total.fees_confirmed_krw.value, "KRW")}\n`
    + `${t("pnl.estimated")} ${fmtMoney(d.total.fees_est_usd.value, "USD")} / ${fmtMoney(d.total.fees_est_krw.value, "KRW")}` : undefined;
  const held = d ? d.symbols.filter((s) => Number(s.qty) > 0) : [];
  const sold = d ? d.symbols.length - held.length : 0;
  const rows = d ? (showSold ? d.symbols : held) : [];

  return (
    <section className="panel pnl-panel">
      <div className="panel-head">
        <h2>{t("pnl.title")}</h2>
        <div className="seg">
          {(["today", "all"] as const).map((p) => (
            <button key={p} className={period === p ? "on" : ""} onClick={() => setPeriod(p)}
              title={p === "all" ? t("pnl.allTitle") : undefined}>
              {p === "today" ? t("pnl.today") : t("pnl.all")}
            </button>
          ))}
        </div>
      </div>
      <div className="pnl-cash" title={t("pnl.cashTitle")}>
        <span className="muted">{t("pnl.cash")}</span>
        <b>{fmtMoney(a?.buying_power.usd ?? null, "USD")}</b>
        <span>{fmtMoney(a?.buying_power.krw ?? null, "KRW")}</span>
        <span className="small">
          <StatusLine status={a?.status} updated={a?.updated} error={a?.error} now={now} label="Toss"
            fetchError={acct.error && !a ? acct.error : null} />
        </span>
      </div>
      {pnl.error && !d && <div className="st-error">{t("pnl.unavailable", { msg: trBackend(pnl.error) })}</div>}
      {pnl.error && d && <div className="st-error small">{t("pnl.lastFail", { msg: trBackend(pnl.error) })}</div>}
      {!d && !pnl.error && <div className="muted">{t("app.loading")}</div>}
      {d && (
        <>
          <table className="pnl-table">
            <thead><tr><th></th><th className="num">USD</th><th className="num">KRW</th></tr></thead>
            <tbody>
              <tr><th>{t("pnl.realized")}</th><Money usd={d.total.realized_usd} krw={d.total.realized_krw} /></tr>
              <tr><th>{t("pnl.unrealized")}</th><Money usd={d.total.unrealized_usd} krw={d.total.unrealized_krw} /></tr>
              <tr><th title={feeTitle}>{t("pnl.fees")}</th><Money usd={d.total.fees_usd} krw={d.total.fees_krw} signed={false} title={feeTitle} /></tr>
              <tr><th>{t("pnl.fxCost")}</th><Money usd={d.total.fx_cost_usd} krw={d.total.fx_cost_krw} signed={false} /></tr>
              <tr><th>{t("pnl.aiCredits")}</th><Money usd={d.total.ai_credits_usd} krw={d.total.ai_credits_krw} signed={false} /></tr>
              <tr className="net"><th>{t("pnl.net")}</th><Money usd={d.total.net_usd} krw={d.total.net_krw} /></tr>
            </tbody>
          </table>

          {rows.length > 0 && (
            <table className="pnl-table syms">
              <thead>
                <tr><th></th><th className="num">{t("pnl.qty")}</th><th className="num">{t("pnl.avg")}</th><th className="num">{t("pnl.unrealized")}</th></tr>
              </thead>
              <tbody>
                {rows.map((s) => {
                  const krw = d.currencies?.[s.symbol] === "KRW";      // Korean stock: show in KRW
                  const label = tickerLabel(s.symbol, d.names?.[s.symbol]);
                  const un = krw ? s.unrealized_krw : s.unrealized_usd;
                  return (
                    <tr key={s.symbol} className={Number(s.qty) > 0 ? "" : "muted"}>
                      <th className="sym" title={`${d.names?.[s.symbol] ?? ""} (${s.symbol})`}><TickerLink sym={s.symbol}>{label}</TickerLink></th>
                      <td className="num">{fmtNum(s.qty, Number(s.qty) % 1 ? 4 : 0)}</td>
                      <td className="num">{krw ? fmtMoney(s.avg_krw.value, "KRW") : fmtPrice(s.avg_usd.value, "USD")}</td>
                      <td className={`num ${signClass(un.value)}`}>{fmtMoney(un.value, krw ? "KRW" : "USD", true)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {sold > 0 && (
            <button className="small" onClick={() => setShowSold(!showSold)}>
              {showSold ? t("pnl.hideSold") : t("pnl.showSold", { n: sold })}
            </button>
          )}

          <div className="muted small">
            {d.fx.rate ? `1 USD = ${fmtNum(d.fx.rate, 2)} KRW` : t("pnl.noRate")}
            {d.fx.time ? ` (${fmtKstDateTime(d.fx.time)})` : ""} · {d.source === "toss" ? "Toss" : "positions.json"} ·{" "}
            <StatusLine status={d.quotes.status} updated={d.quotes.updated} error={d.quotes.error} now={now} label={t("pnl.quotes")} />
          </div>
          <details className="pnl-notes">
            <summary className="small muted">{t("pnl.notes", { n: d.notes.length })}</summary>
            <div className="small">{d.source === "toss" ? t("pnl.sourceToss") : t("pnl.sourceManual")}</div>
            <div className="muted small">{t("pnl.legend")}</div>
            {d.notes.map((n) => <div key={n} className="muted small">• {trBackend(n)}</div>)}
          </details>
        </>
      )}
    </section>
  );
}
