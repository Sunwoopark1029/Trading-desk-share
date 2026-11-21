import { optionsApi, type OptionsBand } from "../api";
import { fmtKstTime, fmtNum, fmtPrice } from "../format";
import { trBackend, useT, type Key } from "../i18n";
import { usePoll } from "../usePoll";

interface Props {
  symbol: string;
  pollMs?: number;
}

const pct = (iv: number | null | undefined) => (iv === null || iv === undefined ? "—" : `${fmtNum(iv * 100, 0)}%`);
const shortDate = (d: string | null | undefined) => (d ? d.slice(5) : "");

/** Desk: options-implied context (expected moves, IV, skew, most traded strikes). Calculated in code; size, not direction. */
export default function OptionsPanel({ symbol, pollMs = 60_000 }: Props) {
  const { t } = useT();
  const q = usePoll(() => optionsApi.get(symbol), pollMs, [symbol]);
  const v = q.data;

  const band = (key: "day" | "week" | "month", b: OptionsBand | null) => {
    if (!b) return null;
    const label = key === "day" ? t("opt.day") : t("opt.toExpiry", { d: shortDate(b.expiry) });
    return (
      <tr key={key}>
        <th>{label}{b.days !== undefined && key !== "day" && <span className="muted small"> · {fmtNum(b.days, 1)}{t("opt.daysShort")}</span>}</th>
        <td className="num">±{fmtPrice(b.move, "USD")}<div className="muted small">{fmtNum(b.move_pct, 1)}%</div></td>
        <td className="num">{fmtNum(b.down, 2)} – {fmtNum(b.up, 2)}</td>
        <td className="num">{pct(b.iv)}</td>
        <td><span className={`opt-q ${b.quality}`}>{t(`opt.q.${b.quality}` as Key)}</span></td>
      </tr>
    );
  };

  return (
    <section className="panel options-panel">
      <div className="panel-head">
        <h2 title={t("opt.help")}>{t("opt.title")}</h2>
        <span className="small muted">
          {v?.source ?? "yfinance · ~15 min"}
          {v?.available && <> · <span className={v.session_live ? "" : "st-stale"}>{v.session_live ? t("opt.live") : t("opt.lastSession")}</span></>}
          {v?.stale && <> · <span className="st-stale">{t("opt.stale")}</span></>}
        </span>
      </div>
      {q.error && !v && <div className="st-error small">{trBackend(q.error)}</div>}
      {v && !v.available && <div className="muted small">{t("opt.none", { why: v.reason ?? "" })}</div>}
      {v?.available && (
        <>
          <div className="muted small">
            {t("opt.basis", { p: fmtPrice(v.price, "USD"), at: fmtKstTime(v.price_time_kst) })}
          </div>
          {v.day || v.week || v.month ? (
            <table className="otable opt-table">
              <thead><tr><th>{t("opt.horizon")}</th><th className="num">{t("opt.move")}</th><th className="num">{t("opt.range")}</th>
                <th className="num">IV</th><th>{t("opt.quality")}</th></tr></thead>
              <tbody>{band("day", v.day)}{band("week", v.week)}{band("month", v.month)}</tbody>
            </table>
          ) : <div className="st-stale small">{t("opt.thin")}</div>}
          <div className="opt-facts small">
            {v.iv_vs_realized !== null && (
              <span title={t("opt.ivrvHelp")}>{t("opt.ivrv")} <b>{fmtNum(v.iv_vs_realized, 2)}×</b>
                {v.realized_20d !== null && <span className="muted"> ({pct(v.realized_20d)})</span>}</span>
            )}
            {v.term && <span title={t("opt.termHelp")}>{t("opt.term")} <b>{t(`opt.term.${v.term}` as Key)}</b></span>}
            {v.skew && (
              <span title={t("opt.skewHelp")}>{t("opt.skew")} <b>{fmtNum(v.skew.put_call_price_ratio, 2)}</b>
                <span className="muted"> (P {v.skew.put_strike} / C {v.skew.call_strike})</span></span>
            )}
            {v.pc_volume !== null && <span title={t("opt.pcHelp")}>{t("opt.pc")} <b>{fmtNum(v.pc_volume, 2)}</b></span>}
          </div>
          {v.most_traded.length > 0 && (
            <div className="small">
              <span className="muted">{t("opt.mostTraded")}: </span>
              {v.most_traded.map((r, i) => (
                <span key={i} className="opt-strike">
                  <span className={r.side === "call" ? "up" : "down"}>{r.side === "call" ? "C" : "P"}</span> {r.strike}
                  <span className="muted"> {shortDate(r.expiry)} · {fmtNum(r.volume, 0)}</span>
                </span>
              ))}
            </div>
          )}
          <details className="small">
            <summary className="muted">{t("opt.expiries", { n: v.expiries.length })}</summary>
            <table className="otable">
              <thead><tr><th>{t("opt.expiry")}</th><th className="num">{t("opt.days")}</th><th className="num">IV</th>
                <th className="num" title={t("opt.yahooHelp")}>Yahoo IV</th><th className="num">{t("opt.straddle")}</th>
                <th className="num">{t("opt.spread")}</th><th>{t("opt.quality")}</th></tr></thead>
              <tbody>
                {v.expiries.map((e) => (
                  <tr key={e.expiry}>
                    <th>{e.expiry}</th>
                    <td className="num">{fmtNum(e.days, 1)}</td>
                    <td className="num">{pct(e.iv)}</td>
                    <td className="num muted">{pct(e.straddle?.yahoo_iv)}</td>
                    <td className="num">{e.straddle ? `${e.straddle.strike} · ${fmtNum(e.straddle.mid, 2)}` : "—"}</td>
                    <td className="num">{e.straddle ? `${fmtNum(e.straddle.spread_pct, 1)}%` : "—"}</td>
                    <td><span className={`opt-q ${e.quality}`}>{t(`opt.q.${e.quality}` as Key)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
          {v.warnings.filter((w) => w !== "oi_unverified" && w !== "last_session").map((w) => (
            <div key={w} className="st-stale small">⚠ {t(`opt.w.${w}` as Key)}</div>
          ))}
          <div className="muted small">{t("opt.sizeNotDirection")}</div>
        </>
      )}
    </section>
  );
}
