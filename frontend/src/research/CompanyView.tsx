// Company view (phase 6 part B): Overview · Financials · Earnings · Analysts · News · Peers for the active ticker.
// Every number comes from the backend (calculated in code there); this screen only formats and labels it, always
// with its currency, source and time. A tab loads only while it is open.
import { Fragment, useEffect, useState } from "react";
import { researchApi, type PackValue, type RsCell, type RsMeta, type RsTable } from "../api";
import { compact } from "./compact";
import { fmtAge, fmtKstDateTime, fmtPct, fmtPrice, num, signClass } from "../format";
import { trBackend, useT, type Key } from "../i18n";
import { usePoll } from "../usePoll";

type Tab = "overview" | "financials" | "earnings" | "analysts" | "news" | "peers";
const TABS: Tab[] = ["overview", "financials", "earnings", "analysts", "news", "peers"];

function Src({ m, now }: { m?: RsMeta | null; now: number }) {
  const { t } = useT();
  if (!m) return null;
  const bad = m.status === "error";
  return (
    <span className={`small ${bad ? "st-error" : m.status === "stale" ? "st-stale" : "muted"}`}>
      {m.source ?? "—"}{m.updated ? ` · ${fmtAge(m.updated, now)}` : ""}
      {bad && ` · ${trBackend(m.error ?? "error")}`}{m.status === "stale" && ` · ${t("st.stale")}`}
    </span>
  );
}

export default function CompanyView({ symbol, now }: { symbol: string; now: number }) {
  const { t } = useT();
  const [tab, setTab] = useState<Tab>(() => {
    try {
      const v = localStorage.getItem("desk.companyTab") as Tab | null;
      return v && TABS.includes(v) ? v : "overview";
    } catch {
      return "overview";
    }
  });
  const pick = (v: Tab) => {
    setTab(v);
    try { localStorage.setItem("desk.companyTab", v); } catch { /* storage off */ }
  };
  const ov = usePoll(() => researchApi.overview(symbol), 60_000, [symbol]);
  const o = ov.data;
  const name = o?.basics?.english_name || o?.profile?.name || o?.basics?.name;
  return (
    <div className="company">
      <div className="company-head">
        <div>
          <span className="company-name">{name ?? ""}</span>
          {o?.kind === "etf" && <span className="tag">ETF</span>}
          {o?.kind === "depositary_receipt" && <span className="tag">ADR</span>}
          {o?.sector && <span className="small muted"> · {o.sector.name}</span>}
        </div>
        {o?.price && (
          <div className="small">
            <b>{fmtPrice(o.price.last, o.price.currency)}</b> <Src m={o.meta.price} now={now} />
          </div>
        )}
      </div>
      {ov.error && <div className="st-error small">{trBackend(ov.error)}</div>}
      <span className="seg wrap company-tabs">
        {TABS.map((v) => (
          <button key={v} className={`small ${tab === v ? "on" : ""}`} onClick={() => pick(v)}>{t(`co.tab.${v}` as Key)}</button>
        ))}
      </span>
      {tab === "overview" && <Overview symbol={symbol} now={now} ov={o} />}
      {tab === "financials" && <Financials symbol={symbol} now={now} />}
      {tab === "earnings" && <Earnings symbol={symbol} now={now} />}
      {tab === "analysts" && <Analysts symbol={symbol} now={now} />}
      {tab === "news" && <News symbol={symbol} now={now} />}
      {tab === "peers" && <Peers symbol={symbol} now={now} />}
    </div>
  );
}

// ── Overview ────────────────────────────────────────────────────────────

const GRID: [string, Key][][] = [
  [["mcap", "co.m.mcap"], ["ev", "co.m.ev"], ["pe_ttm", "co.m.pe"], ["ps_ttm", "co.m.ps"], ["pb", "co.m.pb"],
   ["ev_ebit_ttm", "co.m.evebit"], ["fcf_yield_ttm", "co.m.fcfy"]],
  [["rev_yoy", "co.m.revyoy"], ["rev_cagr_3y", "co.m.rev3"], ["rev_cagr_5y", "co.m.rev5"], ["eps_yoy", "co.m.epsyoy"],
   ["ni_cagr_3y", "co.m.ni3"]],
  [["gross_margin_ttm", "co.m.gm"], ["op_margin_ttm", "co.m.om"], ["net_margin_ttm", "co.m.nm"],
   ["fcf_margin_ttm", "co.m.fm"], ["roe", "co.m.roe"], ["net_debt", "co.m.netdebt"], ["fcf_ttm", "co.m.fcf"],
   ["shares_change_3y", "co.m.shares3"]],
  [["pe_own_median", "co.m.peown"], ["fv_pe_own_median", "co.m.fvown"], ["pe_peer_median", "co.m.pepeer"],
   ["fv_pe_peer_median", "co.m.fvpeer"], ["target_mean", "co.m.target"]],
];

function Overview({ symbol, now, ov }: { symbol: string; now: number; ov: import("../api").RsOverview | null }) {
  const { t } = useT();
  const m = usePoll(() => researchApi.metrics(symbol), 60_000, [symbol]);
  const pack = m.data?.pack;
  const by = new Map((pack?.values ?? []).map((v) => [v.id, v]));
  const sm = m.data?.meta?.statements as RsMeta | undefined;
  if (ov?.kind === "etf") {
    return (
      <div className="co-body">
        <p>{t("co.etfNote")}</p>
        {ov.basics && (
          <div className="co-grid">
            <Tile label={t("co.m.type")} value={ov.basics.security_type} />
            <Tile label={t("co.m.leverage")} value={ov.basics.leverage_factor ? `${num(ov.basics.leverage_factor)}x` : "—"} />
            <Tile label={t("co.m.listed")} value={ov.basics.list_date ?? "—"} />
          </div>
        )}
      </div>
    );
  }
  return (
    <div className="co-body">
      {ov?.next_earnings && (
        <div className="small">
          {t("co.nextEarnings")}: <b>{ov.next_earnings.date}</b> {t("chart.usDate")}{ov.next_earnings.hour ? ` (${ov.next_earnings.hour})` : ""}
          {ov.next_earnings.eps_estimate !== null && ` · ${t("co.epsEst")} ${fmtPrice(ov.next_earnings.eps_estimate, ov.next_earnings.currency)}`}
          {" "}<Src m={ov.meta.calendar} now={now} />
          <span className="muted"> · {ov.next_earnings.note}</span>
        </div>
      )}
      {ov?.meta.calendar?.status === "error" && !ov.next_earnings && (
        <div className="small">{t("co.nextEarnings")}: <Src m={ov.meta.calendar} now={now} /></div>
      )}
      {ov?.meta.profile?.status === "error" && <div className="small">{t("co.profile")}: <Src m={ov.meta.profile} now={now} /></div>}
      {ov?.profile && (
        <div className="small muted">
          {[ov.profile.industry, ov.profile.country, ov.profile.ipo ? `IPO ${ov.profile.ipo}` : null].filter(Boolean).join(" · ")}
          {ov.profile.weburl && <> · <a href={ov.profile.weburl} target="_blank" rel="noreferrer">{ov.profile.weburl}</a></>}
        </div>
      )}
      {m.error && <div className="st-error small">{trBackend(m.error)}</div>}
      {sm && !pack && <div className="muted small">{trBackend(sm.note ?? sm.error ?? t("co.noStatements"))}</div>}
      {pack && GRID.map((row, i) => (
        <div key={i} className="co-grid">
          {row.map(([id, label]) => <PackTile key={id} v={by.get(id)} label={t(label)} primary={sm?.source ?? null} now={now} />)}
        </div>
      ))}
      {pack && (
        <div className="small muted">
          <div>{t("co.statementsIn", { cur: pack.currency })} · <Src m={sm} now={now} /></div>
          <div>{t("co.livePrice")}: <Src m={m.data?.meta.price as RsMeta | undefined} now={now} /> · {t("co.yearEndPrices")}: <Src m={m.data?.meta.history as RsMeta | undefined} now={now} /></div>
          {((m.data?.meta.notes as string[] | undefined) ?? []).map((n) => <div key={n} className="st-stale">⚠ {n}</div>)}
          {pack.warnings.map((w) => <div key={w} className="st-stale">⚠ {w}</div>)}
        </div>
      )}
      {ov?.basics?.shares_note && <div className="small muted">{ov.basics.shares_note}</div>}
    </div>
  );
}

function Tile({ label, value, note }: { label: string; value: string; note?: string | null }) {
  return (
    <div className="co-tile" title={note ?? undefined}>
      <div className="muted small">{label}</div>
      <div className="co-val">{value}</div>
      {note && <div className="muted tiny">{note}</div>}
    </div>
  );
}

function PackTile({ v, label, primary, now }: { v?: PackValue; label: string; primary: string | null; now: number }) {
  if (!v) return <Tile label={label} value="—" />;
  const missing = v.value === null;
  const other = !missing && v.source !== primary;          // a source other than the statements: shown on the tile
  const unofficial = /unofficial|yfinance/i.test(v.source);
  return (
    <div className={`co-tile${missing ? " missing" : ""}`} title={`${v.label} · ${v.source}${v.as_of ? ` · ${fmtKstDateTime(v.as_of)}` : ""}`}>
      <div className="muted small">{label}</div>
      <div className={`co-val ${v.kind === "pct" ? signClass(v.value) : ""}`}>{missing ? "—" : v.display}</div>
      <div className="muted tiny">{missing ? v.display : [v.period, v.note].filter(Boolean).join(" · ")}</div>
      {other && <div className={`tiny ${unofficial ? "st-stale" : "muted"}`}>{v.source}{v.as_of ? ` · ${fmtAge(v.as_of, now)}` : ""}</div>}
    </div>
  );
}

// ── Financials ──────────────────────────────────────────────────────────

function Financials({ symbol, now }: { symbol: string; now: number }) {
  const { t } = useT();
  const [freq, setFreq] = useState<"annual" | "quarterly">("annual");
  const f = usePoll(() => researchApi.financials(symbol, freq), 600_000, [symbol, freq]);
  const tb = f.data?.table;
  return (
    <div className="co-body">
      <div className="ex-controls">
        <span className="seg">
          <button className={`small ${freq === "annual" ? "on" : ""}`} onClick={() => setFreq("annual")}>{t("co.annual")}</button>
          <button className={`small ${freq === "quarterly" ? "on" : ""}`} onClick={() => setFreq("quarterly")}>{t("co.quarterly")}</button>
        </span>
        {f.data && <Src m={f.data.meta} now={now} />}
      </div>
      {f.error && <div className="st-error small">{trBackend(f.error)}</div>}
      {f.data && !tb && <div className="muted small">{trBackend(f.data.meta.note ?? f.data.meta.error ?? t("co.noStatements"))}</div>}
      {f.data?.meta.fallback && <div className="st-stale small">⚠ {f.data.meta.note}</div>}
      {tb && <StatementTable tb={tb} withTtm={freq === "annual"} />}
    </div>
  );
}

function StatementTable({ tb, withTtm }: { tb: RsTable; withTtm: boolean }) {
  const { t } = useT();
  const groups: [string, Key][] = [["income", "co.income"], ["balance", "co.balance"], ["cashflow", "co.cashflow"]];
  // The backend fills "TTM" with the latest fiscal year when there are no quarterly reports (label says so):
  // that is not a trailing-twelve-month figure, so the column is left out and the reason shown.
  const realTtm = withTtm && !!tb.ttm && tb.ttm.label.startsWith("TTM");
  const cols = tb.periods.length + 1 + (realTtm ? 1 : 0);
  const cell = (c: RsCell | undefined, measure: string) => {
    if (!c || c.value === null) return <td className="num muted" title={c?.note ?? ""}>—</td>;
    return (
      <td className="num" title={[c.concept, c.note].filter(Boolean).join(" · ")}>
        {compact(c.value, measure, tb.currency)}{c.derived ? "*" : ""}
      </td>
    );
  };
  return (
    <div className="co-table-wrap">
      <table className="co-table">
        <thead>
          <tr>
            <th>{tb.currency}</th>
            {realTtm && <th title={tb.ttm!.label}>TTM</th>}
            {tb.periods.map((p) => <th key={p.key} title={p.label}>{p.key.replace(/^FY(\d{4})Q(\d)$/, "Q$2 FY$1")}</th>)}
          </tr>
        </thead>
        <tbody>
          {groups.map(([g, label]) => {
            const rows = tb.rows.filter((r) => r.statement === g);
            if (!rows.length) return null;
            return [
              <tr key={g} className="co-group"><td colSpan={cols}>{t(label)}</td></tr>,
              ...rows.map((r) => (
                <tr key={r.id}>
                  <td>{r.label}</td>
                  {realTtm && cell(tb.ttm!.cells[r.id], r.measure)}
                  {tb.periods.map((p) => <Fragment key={p.key}>{cell(r.cells[p.key], r.measure)}</Fragment>)}
                </tr>
              )),
            ];
          })}
        </tbody>
      </table>
      <div className="small muted">{t("co.derivedNote")}</div>
      {withTtm && tb.ttm && !realTtm && <div className="small muted">{t("co.noTtm", { label: tb.ttm.label })}</div>}
      {tb.warnings.map((w) => <div key={w} className="small st-stale">⚠ {w}</div>)}
    </div>
  );
}

// ── Earnings ────────────────────────────────────────────────────────────

function Earnings({ symbol, now }: { symbol: string; now: number }) {
  const { t } = useT();
  const e = usePoll(() => researchApi.earnings(symbol), 600_000, [symbol]);
  const d = e.data;
  return (
    <div className="co-body">
      {e.error && <div className="st-error small">{trBackend(e.error)}</div>}
      <h4>{t("co.surprises")} <Src m={d?.meta.surprises} now={now} /></h4>
      <div className="co-table-wrap">
      <table className="co-table">
        <thead><tr><th>{t("co.period")} {t("chart.usDate")}</th><th>{t("co.actual")}</th><th>{t("co.estimate")}</th><th>{t("co.surprisePct")}</th></tr></thead>
        <tbody>
          {(d?.surprises ?? []).map((s) => (
            <tr key={`${s.period}`}>
              <td>{s.period ?? "—"}{s.quarter ? ` (Q${s.quarter} ${s.year})` : ""}</td>
              <td className="num">{fmtPrice(s.actual, s.currency)}</td>
              <td className="num">{fmtPrice(s.estimate, s.currency)}</td>
              <td className={`num ${signClass(s.surprise_pct)}`}>{s.surprise_pct === null ? "—" : fmtPct(s.surprise_pct)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      <h4>{t("co.calendar")} <span className="small muted">{t("chart.usDate")}</span> <Src m={d?.meta.calendar} now={now} /></h4>
      <ul className="co-list small">
        {(d?.calendar ?? []).map((c) => (
          <li key={`${c.date}`}>{c.date}{c.hour ? ` (${c.hour})` : ""} · {t("co.epsEst")} {fmtPrice(c.eps_estimate, c.currency)}
            {c.eps_actual !== null && ` · ${t("co.actual")} ${fmtPrice(c.eps_actual, c.currency)}`}</li>
        ))}
      </ul>
      {(d?.sec_eps_history.length ?? 0) > 0 && (
        <>
          <h4>{t("co.secEps")} <Src m={d?.meta.statements} now={now} /></h4>
          <div className="co-chips small">
            {d!.sec_eps_history.map((h) => (
              <span key={h.period} title={h.note ?? ""}>{h.period.split(",")[0]}: <b>{h.eps_diluted === null ? "—" : fmtPrice(h.eps_diluted, h.currency)}</b></span>
            ))}
          </div>
        </>
      )}
      {d && <div className="small muted">{[...d.notes, d.currency_note].join(" · ")}</div>}
    </div>
  );
}

// ── Analysts ────────────────────────────────────────────────────────────

function Analysts({ symbol, now }: { symbol: string; now: number }) {
  const { t } = useT();
  const a = usePoll(() => researchApi.analysts(symbol), 600_000, [symbol]);
  const d = a.data;
  const tg = d?.targets;
  const lo = num(tg?.low), hi = num(tg?.high), cur = num(d?.price?.last);   // Toss price only (never yfinance's)
  const pos = (v: number | null) => (v === null || lo === null || hi === null || hi === lo ? null
    : Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100)));
  return (
    <div className="co-body">
      {a.error && <div className="st-error small">{trBackend(a.error)}</div>}
      <h4>{t("co.ratings")} <Src m={d?.meta.ratings} now={now} /></h4>
      {(d?.ratings ?? []).slice(0, 4).map((r) => {
        const parts: [number | null, string][] = [[r.strong_buy, "sb"], [r.buy, "b"], [r.hold, "h"], [r.sell, "s"], [r.strong_sell, "ss"]];
        const total = parts.reduce((s, [v]) => s + (v ?? 0), 0) || 1;
        return (
          <div key={`${r.period}`} className="co-rating small">
            <span className="co-rating-period">{r.period}</span>
            <span className="co-bar">{parts.map(([v, k]) => <span key={k} className={`co-${k}`} style={{ width: `${((v ?? 0) / total) * 100}%` }} title={`${k}: ${v ?? 0}`} />)}</span>
            <span className="muted">{t("co.ratingCounts", { sb: r.strong_buy ?? 0, b: r.buy ?? 0, h: r.hold ?? 0, s: r.sell ?? 0, ss: r.strong_sell ?? 0 })}</span>
          </div>
        );
      })}
      <h4>{t("co.targets")} <Src m={d?.meta.targets} now={now} /></h4>
      {!tg && <div className="muted small">{t("co.noTargets")}</div>}
      {tg && (
        <>
          <div className="co-range">
            <span className="co-range-bar" />
            {[["low", tg.low], ["median", tg.median], ["mean", tg.mean], ["high", tg.high]].map(([k, v]) => {
              const p = pos(num(v as string | null));
              return p === null ? null : <span key={k as string} className="co-mark" style={{ left: `${p}%` }} title={`${k}: ${fmtPrice(v as string, tg.currency)}`} />;
            })}
            {pos(cur) !== null && <span className="co-mark now" style={{ left: `${pos(cur)}%` }} title={t("co.current")} />}
          </div>
          <div className="small">
            {t("co.low")} {fmtPrice(tg.low, tg.currency)} · {t("co.median")} {fmtPrice(tg.median, tg.currency)} ·
            {" "}{t("co.mean")} {fmtPrice(tg.mean, tg.currency)} · {t("co.high")} {fmtPrice(tg.high, tg.currency)}
            {d?.price && <> · {t("co.current")} <b>{fmtPrice(d.price.last, d.price.currency)}</b> <Src m={d.meta.price} now={now} /></>}
          </div>
          <div className="small st-stale">{tg.source} · {tg.currency_note}</div>
        </>
      )}
    </div>
  );
}

// ── News ────────────────────────────────────────────────────────────────

function News({ symbol, now }: { symbol: string; now: number }) {
  const { t, lang } = useT();
  const n = usePoll(() => researchApi.news(symbol, 30), 900_000, [symbol]);
  return (
    <div className="co-body">
      <div className="small muted">{t("co.news30")} <Src m={n.data?.meta} now={now} /></div>
      {n.error && <div className="st-error small">{trBackend(n.error)}</div>}
      {n.data && n.data.items.length === 0 && <div className="muted small">{t("co.noNews")}</div>}
      <ul className="co-list">
        {(n.data?.items ?? []).map((i) => (
          <li key={i.id}>
            <span className="small muted">{i.published_kst ? fmtKstDateTime(i.published_kst) : t("co.dateUnverified")} · {i.source}</span>
            {i.tag?.priority && <span className={`prio prio-${i.tag.priority}`}> {i.tag.priority}</span>}
            <div><a href={i.url} target="_blank" rel="noreferrer">{i.headline}</a></div>
            {i.tag && (lang === "ko" ? i.tag.reason_ko : i.tag.reason_en) && <div className="small muted">{lang === "ko" ? i.tag.reason_ko : i.tag.reason_en}</div>}
          </li>
        ))}
      </ul>
    </div>
  );
}

// ── Peers ───────────────────────────────────────────────────────────────

const PEER_COLS: [string, Key][] = [["pe_ttm", "co.m.pe"], ["ps_ttm", "co.m.ps"], ["ev_ebit_ttm", "co.m.evebit"],
  ["fcf_yield_ttm", "co.m.fcfy"], ["rev_yoy", "co.m.revyoy"], ["gross_margin_ttm", "co.m.gm"],
  ["op_margin_ttm", "co.m.om"], ["net_margin_ttm", "co.m.nm"], ["roe", "co.m.roe"]];

function Peers({ symbol, now }: { symbol: string; now: number }) {
  const { t } = useT();
  const p = usePoll(() => researchApi.peers(symbol), 600_000, [symbol]);
  const d = p.data;
  const building = d?.sector_index.status === "building";
  const { refresh } = p;
  useEffect(() => {                                   // re-check every 20 s only while the sector index builds
    if (!building) return;
    const id = window.setInterval(refresh, 20_000);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [building, symbol]);
  return (
    <div className="co-body">
      {p.error && <div className="st-error small">{trBackend(p.error)}</div>}
      {d?.sector && <div className="small">{t("co.peersIn", { s: d.sector.name, n: d.peers.length })} {d.meta && <Src m={d.meta} now={now} />}</div>}
      {building && <div className="small muted">{t("co.indexBuilding", { a: d!.sector_index.progress[0] ?? 0, b: d!.sector_index.progress[1] ?? 0 })}</div>}
      {d && !d.sector && !building && d.sector_index.status === "error" && (
        <div className="small st-error">{t("co.indexError")}: {trBackend(d.sector_index.error ?? "error")}</div>
      )}
      {d && !d.sector && !building && d.sector_index.status !== "error" && <div className="small muted">{trBackend(d.note ?? "")}</div>}
      {d?.note && d.sector && <div className="small muted">{d.note}</div>}
      {d && d.peers.length > 0 && (
        <div className="co-table-wrap">
          <table className="co-table">
            <thead><tr><th>{t("co.peer")}</th>{PEER_COLS.map(([id, k]) => <th key={id}>{t(k)}</th>)}</tr></thead>
            <tbody>
              {d.peers.map((r) => (
                <tr key={r.symbol}>
                  <td title={[r.english_name ?? r.name, r.source, r.note].filter(Boolean).join(" · ")}>
                    <b>{r.symbol}</b> <span className="muted small co-peer-name">{r.english_name ?? r.name ?? ""}</span>
                    {r.source && !r.source.startsWith("SEC") && <span className="tiny st-stale"> {r.source}</span>}
                  </td>
                  {PEER_COLS.map(([id]) => {
                    const has = r.metrics?.[id] !== null && r.metrics?.[id] !== undefined;
                    return <td key={id} className={`num${has ? "" : " muted"}`} title={has ? undefined : r.display?.[id] ?? r.note ?? ""}>
                      {has ? r.display?.[id] ?? "—" : "—"}</td>;
                  })}
                </tr>
              ))}
              <tr className="co-group">
                <td>{t("co.median")}</td>
                {PEER_COLS.map(([id]) => {
                  const m = d.medians.find((x) => x.metric_id === id);
                  const v = num(m?.value);
                  const pct = !["pe_ttm", "ps_ttm", "ev_ebit_ttm"].includes(id);
                  return <td key={id} className="num" title={m?.note ?? ""}>{v === null ? "—" : pct ? `${(v * 100).toFixed(1)}%` : `${v.toFixed(1)}x`}
                    {m && m.count < m.of ? ` (${m.count}/${m.of})` : ""}</td>;
                })}
              </tr>
            </tbody>
          </table>
          {d.peers.some((r) => r.source && !r.source.startsWith("SEC")) && <div className="small st-stale">⚠ {t("co.peersUnofficial")}</div>}
        </div>
      )}
    </div>
  );
}
