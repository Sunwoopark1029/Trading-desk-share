import { useEffect, useState } from "react";
import { marketApi, type OrderbookView } from "../api";
import { fmtKstTime, fmtNum, num, priceDp } from "../format";
import { trBackend, useT, type Key } from "../i18n";

interface Props {
  symbol: string;
}

/** Ask/bid ladder with size bars, spread, session, recent trades. Read-only (no order buttons). */
export default function OrderbookPanel({ symbol }: Props) {
  const { t } = useT();
  const [view, setView] = useState<OrderbookView | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let closed = false;
    setView(null);
    marketApi.orderbook(symbol).then((v) => {
      if (!closed) setView(v);
    }).catch((e) => !closed && setErr(e instanceof Error ? e.message : String(e)));
    const es = new EventSource(marketApi.streamUrl(symbol));
    es.onmessage = (ev) => {
      try {
        setView(JSON.parse(ev.data) as OrderbookView);
        setErr(null);
      } catch {
        /* ignore a broken frame */
      }
    };
    es.onerror = () => setErr("backend not reachable (is start.cmd running?)"); // EventSource retries by itself
    return () => {
      closed = true;
      es.close();
    };
  }, [symbol]);

  const v = view;
  const cur = v?.currency ?? "USD";
  const ref = num(v?.spread?.mid ?? v?.trades[0]?.price ?? v?.bids[0]?.price) ?? 1;
  const dp = priceDp(ref);
  const maxVol = Math.max(1, ...[...(v?.asks ?? []), ...(v?.bids ?? [])].map((l) => num(l.volume) ?? 0));
  const asksTopDown = [...(v?.asks ?? [])].reverse(); // highest ask on top, best ask next to the spread

  let streamText = t("ob.connecting");
  let streamCls = "st-pending";
  if (v) {
    const s = v.stream;
    if (s.status === "connected") { streamText = t("ob.live"); streamCls = "st-ok"; }
    else if (s.status === "off") { streamText = `${t("ob.off")} · ${t("ob.polling")}`; streamCls = "st-stale"; }
    else {
      streamText = `${t("ob.down", { s: Math.round(s.retry_in ?? 0) })} · ${t("ob.polling")}`;
      streamCls = "st-stale";
    }
  }
  if (err) { streamText = trBackend(err); streamCls = "st-error"; }

  // colour each trade by tick direction vs the trade before it
  const trades = v?.trades ?? [];
  const tickCls = (i: number) => {
    const p = num(trades[i].price), q = i + 1 < trades.length ? num(trades[i + 1].price) : null;
    return p === null || q === null || p === q ? "" : p > q ? "up" : "down";
  };

  return (
    <section className="panel orderbook">
      <div className="panel-head">
        <h2>{t("ob.title")} · {symbol}</h2>
        <span className={`small ${streamCls}`} title={trBackend(v?.stream.message) || undefined}>{streamText}</span>
      </div>
      <div className="muted small">
        {v?.session ? t("ob.session", { s: t(`ob.sessionName.${v.session}` as Key) }) : t("ob.noSession")}
        {v && ` · ${t("ob.levels", { a: v.levels_per_side.asks, b: v.levels_per_side.bids })}`}
        {v?.book_time && ` · ${t("ob.updated", { t: fmtKstTime(v.book_time) })}`}
      </div>
      {v?.error && <div className="st-error small">{trBackend(v.error)}</div>}

      {v && (v.asks.length || v.bids.length) ? (
        <table className="ladder">
          <thead><tr><th className="num">{t("ob.price")} ({cur})</th><th className="num">{t("ob.size")}</th></tr></thead>
          <tbody>
            {asksTopDown.map((l, i) => (
              <tr key={`a${i}`} className="ask">
                <td className="num down">{fmtNum(l.price, dp)}</td>
                <td className="num bar-cell">
                  <span className="bar ask-bar" style={{ width: `${((num(l.volume) ?? 0) / maxVol) * 100}%` }} />
                  <span className="bar-num">{fmtNum(l.volume, 0)}</span>
                </td>
              </tr>
            ))}
            <tr className={`spread-row${v.spread?.wide ? " wide" : ""}`}>
              <td colSpan={2}>
                {v.spread ? (
                  <>
                    {t("ob.spread")} {cur} {fmtNum(v.spread.abs, dp)}
                    {v.spread.pct !== null && ` · ${fmtNum(v.spread.pct, 3)}%`}
                    {v.spread.wide && ` · ${t("ob.wide")} (> ${fmtNum(v.spread_warn_pct, 2)}%)`}
                  </>
                ) : "—"}
              </td>
            </tr>
            {v.bids.map((l, i) => (
              <tr key={`b${i}`} className="bid">
                <td className="num up">{fmtNum(l.price, dp)}</td>
                <td className="num bar-cell">
                  <span className="bar bid-bar" style={{ width: `${((num(l.volume) ?? 0) / maxVol) * 100}%` }} />
                  <span className="bar-num">{fmtNum(l.volume, 0)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        v && <div className="muted small">{t("ob.noBook")}</div>
      )}

      {trades.length > 0 && (
        <>
          <div className="muted small tape-title">{t("ob.trades")}</div>
          <div className="tape">
            <table>
              <tbody>
                {trades.slice(0, 30).map((tr, i) => (
                  <tr key={`${tr.time}-${i}`}>
                    <td className="muted">{fmtKstTime(tr.time).replace(" KST", "")}</td>
                    <td className={`num ${tickCls(i)}`}>{fmtNum(tr.price, dp)}</td>
                    <td className="num muted">{fmtNum(tr.volume, Number(tr.volume) % 1 ? 4 : 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
