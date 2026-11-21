// Explore rail (phase 6 part A): Watch folders · Toss rankings · Toss sectors. Every row sets the active ticker.
// All numbers come from the backend (Toss, exact decimals; change % calculated there). Polls only while visible.
import { useMemo, useState } from "react";
import {
  exploreApi, watchApi, type RankingDuration, type RankingType, type SectorItem, type WatchFolder, type WatchlistDoc,
} from "../api";
import TickerSearch from "../components/TickerSearch";
import { fmtAge, fmtKstDateTime, fmtNum, fmtPct, fmtPrice, num, signClass } from "../format";
import { trBackend, useT, type Key } from "../i18n";
import { usePoll } from "../usePoll";

type Tab = "watch" | "rank" | "sectors";

export interface WatchState {
  doc: WatchlistDoc | null;
  error: string | null;
  save: (folders: Partial<WatchFolder>[]) => Promise<void>;
}

interface Props {
  symbol: string;
  onSymbol: (s: string) => void;
  watch: WatchState;
  now: number;
}

export default function ExploreRail({ symbol, onSymbol, watch, now }: Props) {
  const { t } = useT();
  const [tab, setTab] = useState<Tab>(() => {
    try {
      const v = localStorage.getItem("desk.exploreTab");
      return v === "rank" || v === "sectors" ? v : "watch";
    } catch {
      return "watch";
    }
  });
  const pick = (v: Tab) => {
    setTab(v);
    try { localStorage.setItem("desk.exploreTab", v); } catch { /* storage off: not remembered */ }
  };
  return (
    <section className="panel explore">
      <div className="panel-head">
        <h2>{t("ex.title")}</h2>
        <span className="seg">
          {(["watch", "rank", "sectors"] as Tab[]).map((v) => (
            <button key={v} className={`small ${tab === v ? "on" : ""}`} onClick={() => pick(v)}>{t(`ex.${v}` as Key)}</button>
          ))}
        </span>
      </div>
      {tab === "watch" && <WatchTab symbol={symbol} onSymbol={onSymbol} watch={watch} now={now} />}
      {tab === "rank" && <RankTab symbol={symbol} onSymbol={onSymbol} watch={watch} now={now} />}
      {tab === "sectors" && <SectorsTab symbol={symbol} onSymbol={onSymbol} watch={watch} now={now} />}
    </section>
  );
}

// ── star: add / remove a ticker in any folder ───────────────────────────

export function Star({ sym, watch }: { sym: string; watch: WatchState }) {
  const { t } = useT();
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const folders = watch.doc?.folders ?? [];
  const inAny = folders.some((f) => f.symbols.includes(sym));
  const toggle = async (f: WatchFolder) => {
    setErr(null);
    const next = folders.map((x) => x.id !== f.id ? x
      : { ...x, symbols: x.symbols.includes(sym) ? x.symbols.filter((s) => s !== sym) : [...x.symbols, sym] });
    try {
      await watch.save(next);
    } catch (e) {
      setErr(t("ex.saveFail", { msg: trBackend(e instanceof Error ? e.message : String(e)) }));
    }
  };
  return (
    <span className="star-wrap" onClick={(e) => e.stopPropagation()}>
      <button className={`star ${inAny ? "on" : ""}`} title={t("ex.star")} onClick={() => setOpen((v) => !v)}>
        {inAny ? "★" : "☆"}
      </button>
      {open && (
        <div className="star-menu" onMouseLeave={() => setOpen(false)}>
          {folders.map((f) => (
            <label key={f.id}>
              <input type="checkbox" checked={f.symbols.includes(sym)} onChange={() => void toggle(f)} />
              {f.name}{f.pinned ? " 📌" : ""}
            </label>
          ))}
          {err && <div className="st-error small">{err}</div>}
        </div>
      )}
    </span>
  );
}

function Meta({ updated, source, status, error, now }: {
  updated: string | null | undefined; source?: string | null; status?: string; error?: string | null; now: number;
}) {
  const { t } = useT();
  return (
    <div className={`small ${status === "error" ? "st-error" : "muted"}`}>
      {source ? `${source} · ` : ""}{fmtAge(updated ?? null, now)}
      {status === "error" && ` · ${trBackend(error ?? "error")}`}
      {status === "stale" && ` · ${t("st.stale")}`}
    </div>
  );
}

// ── Rank ────────────────────────────────────────────────────────────────

const RANK_TYPES: [RankingType, Key][] = [["MARKET_TRADING_VOLUME", "ex.r.volume"], ["MARKET_TRADING_AMOUNT", "ex.r.amount"],
  ["TOP_GAINERS", "ex.r.gainers"], ["TOP_LOSERS", "ex.r.losers"], ["TOSS_SECURITIES_TRADING_AMOUNT", "ex.r.toss"]];
const DURATIONS: RankingDuration[] = ["realtime", "1d", "1w", "1mo", "3mo", "6mo", "1y"];

function RankTab({ symbol, onSymbol, watch, now }: Props) {
  const { t } = useT();
  const [type, setType] = useState<RankingType>("MARKET_TRADING_VOLUME");
  const [duration, setDuration] = useState<RankingDuration>("realtime");
  const noRealtime = type === "TOP_GAINERS" || type === "TOP_LOSERS";
  const dur = noRealtime && duration === "realtime" ? "1d" : duration;
  const r = usePoll(() => exploreApi.rankings(type, dur, 50), 15_000, [type, dur]);
  const d = r.data;
  return (
    <div className="ex-body">
      <div className="ex-controls">
        <span className="seg wrap">
          {RANK_TYPES.map(([k, label]) => (
            <button key={k} className={`small ${type === k ? "on" : ""}`} onClick={() => setType(k)}>{t(label)}</button>
          ))}
        </span>
        <span className="seg wrap">
          {DURATIONS.map((x) => (
            <button key={x} className={`small ${dur === x ? "on" : ""}`} disabled={noRealtime && x === "realtime"}
              onClick={() => setDuration(x)}>{t(`ex.d.${x}` as Key)}</button>
          ))}
        </span>
      </div>
      <div className="small muted">
        {d?.ranked_at ? t("ex.rankedAt", { t: fmtKstDateTime(d.ranked_at) }) : ""}
        {d ? ` · ${t(d.base_price_means === "previous close" ? "ex.base.prev" : "ex.base.start")}` : ""}
        {d?.volume_counts === "Toss executions only" ? ` · ${t("ex.tossOnly")}` : ""}
      </div>
      {r.error && <div className="st-error small">{trBackend(r.error)}</div>}
      {d && d.rows.length === 0 && d.meta.status !== "error" && <div className="muted small">{t("ex.empty")}</div>}
      <ul className="ex-list">
        {(d?.rows ?? []).map((row) => (
          <li key={row.symbol} className={row.symbol === symbol ? "on" : ""} onClick={() => onSymbol(row.symbol)}>
            <span className="ex-rank">{row.rank}</span>
            <span className="ex-name"><b>{row.symbol}</b> <span className="muted">{row.name ?? row.english_name ?? ""}</span></span>
            <span className="ex-num">{fmtPrice(row.last_price, row.currency)}</span>
            <span className={`ex-num ${signClass(row.change_pct)}`}>{fmtPct(row.change_pct)}</span>
            <Star sym={row.symbol} watch={watch} />
          </li>
        ))}
      </ul>
      {d && <Meta updated={d.meta.updated} source={d.meta.source} status={d.meta.status} error={d.meta.error} now={now} />}
    </div>
  );
}

// ── Sectors ─────────────────────────────────────────────────────────────

const SECTOR_DURATIONS = ["1d", "1w", "1mo", "3mo", "6mo", "1y"];

/** KRW amounts from Toss (US converted too): 1.23조 / 4,567억 in Korean, KRW 1.2T / 456.7B in English. */
function krw(v: string | null, lang: string): string {
  const n = num(v);
  if (n === null) return "—";
  if (lang === "ko") return n >= 1e12 ? `${fmtNum(n / 1e12, 2)}조원` : `${fmtNum(n / 1e8, 0)}억원`;
  return n >= 1e12 ? `KRW ${fmtNum(n / 1e12, 2)}T` : `KRW ${fmtNum(n / 1e9, 1)}B`;
}

function SectorsTab({ symbol, onSymbol, watch, now }: Props) {
  const { t, lang } = useT();
  const [mode, setMode] = useState<"rank" | "all">("rank");
  const [type, setType] = useState<"TOP_GAINERS" | "MARKET_TRADING_AMOUNT">("TOP_GAINERS");
  const [duration, setDuration] = useState("1d");
  const [open, setOpen] = useState<string | null>(null);
  const [parent, setParent] = useState<string | null>(null);
  const showRanks = open === null && mode === "rank";      // only fetch while the ranking list is on screen
  const ranks = usePoll(() => (showRanks ? exploreApi.sectorRankings(type, duration) : Promise.resolve(null)),
    30_000, [type, duration, showRanks]);
  const all = usePoll(exploreApi.sectors, 3_600_000);
  const byParent = useMemo(() => {
    const m = new Map<string | null, SectorItem[]>();
    for (const s of all.data?.sectors ?? []) m.set(s.parent_sector_id, [...(m.get(s.parent_sector_id) ?? []), s]);
    return m;
  }, [all.data]);

  if (open) return <SectorDetail id={open} symbol={symbol} onSymbol={onSymbol} watch={watch} now={now} onBack={() => setOpen(null)} />;
  return (
    <div className="ex-body">
      <div className="ex-controls">
        <span className="seg">
          <button className={`small ${mode === "rank" ? "on" : ""}`} onClick={() => setMode("rank")}>{t("ex.sectorsRank")}</button>
          <button className={`small ${mode === "all" ? "on" : ""}`} onClick={() => setMode("all")}>{t("ex.sectorsAll")}</button>
        </span>
        {mode === "rank" && (
          <>
            <span className="seg">
              <button className={`small ${type === "TOP_GAINERS" ? "on" : ""}`} onClick={() => setType("TOP_GAINERS")}>{t("ex.r.gainers")}</button>
              <button className={`small ${type === "MARKET_TRADING_AMOUNT" ? "on" : ""}`} onClick={() => setType("MARKET_TRADING_AMOUNT")}>{t("ex.r.amount")}</button>
            </span>
            <span className="seg wrap">
              {SECTOR_DURATIONS.map((x) => (
                <button key={x} className={`small ${duration === x ? "on" : ""}`} onClick={() => setDuration(x)}>{t(`ex.d.${x}` as Key)}</button>
              ))}
            </span>
          </>
        )}
      </div>
      {mode === "rank" ? (
        <>
          {ranks.error && <div className="st-error small">{trBackend(ranks.error)}</div>}
          <ul className="ex-list">
            {(ranks.data?.rows ?? []).map((r) => (
              <li key={r.sector_id} onClick={() => setOpen(r.sector_id)}>
                <span className="ex-rank">{r.rank}</span>
                <span className="ex-name">{r.name}</span>
                <span className="ex-num muted" title={t(type === "MARKET_TRADING_AMOUNT" ? "ex.r.amount" : "ex.mcap")}>
                  {krw(type === "MARKET_TRADING_AMOUNT" ? r.trading_amount : r.market_cap, lang)}</span>
                <span className={`ex-num ${signClass(r.change_pct)}`}>{fmtPct(r.change_pct)}</span>
              </li>
            ))}
          </ul>
          <div className="small muted">
            {type === "MARKET_TRADING_AMOUNT" ? t("ex.r.amount") : `${t("ex.mcap")} (${t("ex.mcapDaily")})`} · {t("ex.krwNote")}
          </div>
          {ranks.data && <Meta updated={ranks.data.meta.updated} source={ranks.data.meta.source} status={ranks.data.meta.status} error={ranks.data.meta.error} now={now} />}
        </>
      ) : (
        <>
          {parent && <button className="small" onClick={() => setParent(byParent.get(null)?.some((s) => s.sector_id === parent) ? null
            : (all.data?.sectors.find((s) => s.sector_id === parent)?.parent_sector_id ?? null))}>{t("ex.back")}</button>}
          {all.error && <div className="st-error small">{trBackend(all.error)}</div>}
          <ul className="ex-list">
            {(byParent.get(parent) ?? []).map((s) => (
              <li key={s.sector_id}>
                <span className="ex-name" onClick={() => setOpen(s.sector_id)}>{s.name}</span>
                {s.children > 0 && <button className="small" onClick={() => setParent(s.sector_id)}>{s.children} ›</button>}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function SectorDetail({ id, symbol, onSymbol, watch, now, onBack }: Props & { id: string; onBack: () => void }) {
  const { t, lang } = useT();
  const p = usePoll(() => exploreApi.sector(id), 60_000, [id]);
  const d = p.data;
  return (
    <div className="ex-body">
      <button className="small" onClick={onBack}>{t("ex.back")}</button>
      {p.error && <div className="st-error small">{trBackend(p.error)}</div>}
      {d?.detail && (
        <>
          <h3 className="ex-sector-name">{d.detail.name}</h3>
          {d.detail.summary && <div className="small">{d.detail.summary}</div>}
          <div className="ex-rates small">
            {d.detail.change_rates.map((c) => (
              <span key={c.duration}>{t(`ex.d.${c.duration}` as Key)} <b className={signClass(c.change_pct)}>{c.change_pct === null ? "—" : fmtPct(c.change_pct)}</b></span>
            ))}
          </div>
        </>
      )}
      {d && d.meta.detail.status === "error" && <div className="st-error small">{trBackend(d.meta.detail.error ?? "error")}</div>}
      <h4>{t("ex.stocks")}</h4>
      <ul className="ex-list">
        {(d?.stocks ?? []).map((s) => (
          <li key={s.symbol} className={s.symbol === symbol ? "on" : ""} onClick={() => onSymbol(s.symbol)}>
            <span className="ex-name"><b>{s.symbol}</b> <span className="muted">{s.name}</span></span>
            <span className="ex-num muted" title={t("ex.mcap")}>{krw(s.market_cap, lang)}</span>
            <span className="ex-num">{s.last_price ? fmtPrice(s.last_price, s.price_currency) : ""}</span>
            <Star sym={s.symbol} watch={watch} />
          </li>
        ))}
      </ul>
      {(d?.etfs.length ?? 0) > 0 && <h4>{t("ex.etfs")}</h4>}
      <ul className="ex-list">
        {(d?.etfs ?? []).map((e) => (
          <li key={e.symbol} className={e.symbol === symbol ? "on" : ""} onClick={() => onSymbol(e.symbol)}>
            <span className="ex-name"><b>{e.symbol}</b> <span className="muted">{e.name}</span></span>
            <span className="ex-num">{e.last_price ? fmtPrice(e.last_price, e.price_currency) : ""}</span>
            <Star sym={e.symbol} watch={watch} />
          </li>
        ))}
      </ul>
      {d && (
        <div className="small muted">
          {t("ex.noChange")} · {t("ex.krwNote")} · {t("ex.mcapDaily")}
          {d.stocks.length > d.prices_for_top || d.etfs.length > d.prices_for_top ? ` · ${t("ex.pricesTop", { n: d.prices_for_top })}` : ""}
        </div>
      )}
      {d && <Meta updated={d.meta.stocks.updated} source={d.meta.stocks.source} status={d.meta.stocks.status} error={d.meta.stocks.error} now={now} />}
      {d?.meta.prices && (
        <div className="small muted">{t("ex.pricesLabel")}{" "}
          <Meta updated={d.meta.prices.updated} source={d.meta.prices.source} status={d.meta.prices.status} error={d.meta.prices.error} now={now} />
        </div>
      )}
      {d && d.meta.etfs.status === "error" && <div className="st-error small">{t("ex.etfs")}: {trBackend(d.meta.etfs.error ?? "error")}</div>}
    </div>
  );
}

// ── Watch ───────────────────────────────────────────────────────────────

function WatchTab({ symbol, onSymbol, watch, now }: Props) {
  const { t } = useT();
  const folders = watch.doc?.folders ?? [];
  const [sel, setSel] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const folder = folders.find((f) => f.id === sel) ?? folders.find((f) => f.pinned) ?? folders[0];
  const q = usePoll(() => (folder ? watchApi.quotes(folder.id) : Promise.resolve(null)), 5_000,
    [folder?.id, folder?.symbols.join(",")]);

  const save = async (next: Partial<WatchFolder>[]) => {
    setErr(null);
    try {
      await watch.save(next);
    } catch (e) {
      setErr(t("ex.saveFail", { msg: trBackend(e instanceof Error ? e.message : String(e)) }));
    }
  };
  const update = (patch: Partial<WatchFolder>) => folders.map((f) => (f.id === folder?.id ? { ...f, ...patch } : f));

  if (watch.error && !watch.doc) return <div className="st-error small">{trBackend(watch.error)}</div>;
  if (!folder) return <div className="muted small">{t("app.loading")}</div>;
  return (
    <div className="ex-body">
      <div className="ex-controls">
        <span className="seg wrap">
          {folders.map((f) => (
            <button key={f.id} className={`small ${f.id === folder.id ? "on" : ""}`} onClick={() => setSel(f.id)}>
              {f.pinned ? "📌 " : ""}{f.name} <span className="muted">{f.symbols.length}</span>
            </button>
          ))}
        </span>
        <button className="small" onClick={() => {
          const name = window.prompt(t("ex.folder.name"));
          if (name && name.trim()) void save([...folders, { name: name.trim(), symbols: [] }]);
        }}>{t("ex.folder.new")}</button>
      </div>
      <div className="ex-controls">
        {!folder.pinned && (
          <button className="small" title={t("ex.folder.pinHint")}
            onClick={() => void save(folders.map((f) => ({ ...f, pinned: f.id === folder.id })))}>{t("ex.folder.pin")}</button>
        )}
        {folder.pinned && <span className="small muted" title={t("ex.folder.pinHint")}>{t("ex.folder.pinned")}</span>}
        <button className="small" onClick={() => {
          const name = window.prompt(t("ex.folder.name"), folder.name);
          if (name && name.trim()) void save(update({ name: name.trim() }));
        }}>{t("ex.folder.rename")}</button>
        <button className="small" disabled={folders.length <= 1} title={folders.length <= 1 ? t("ex.folder.lastOne") : undefined}
          onClick={() => {
            if (window.confirm(t("ex.folder.deleteConfirm", { n: folder.name }))) {
              void save(folders.filter((f) => f.id !== folder.id));
              setSel(null);
            }
          }}>{t("ex.folder.delete")}</button>
      </div>
      <div className="ex-add">
        <TickerSearch symbol="" quick={[]} onChange={(s) => {
          if (!folder.symbols.includes(s)) void save(update({ symbols: [...folder.symbols, s] }));
        }} />
      </div>
      {err && <div className="st-error small">{err}</div>}
      {folder.symbols.length === 0 && <div className="muted small">{t("ex.emptyFolder")}</div>}
      <ul className="ex-list">
        {(q.data?.items ?? folder.symbols.map((s) => ({ symbol: s, name: null, last_price: null, currency: null,
          change_pct: null }))).map((it) => (
          <li key={it.symbol} className={it.symbol === symbol ? "on" : ""} onClick={() => onSymbol(it.symbol)}>
            <span className="ex-name"><b>{it.symbol}</b> <span className="muted">{it.name ?? ""}</span></span>
            <span className="ex-num">{fmtPrice(it.last_price, it.currency)}</span>
            <span className={`ex-num ${signClass(it.change_pct)}`}>{fmtPct(it.change_pct)}</span>
            <button className="small ex-x" title={t("ex.remove")} onClick={(e) => {
              e.stopPropagation();
              void save(update({ symbols: folder.symbols.filter((s) => s !== it.symbol) }));
            }}>✕</button>
          </li>
        ))}
      </ul>
      {q.data && q.data.prev_close_pending > 0 && <div className="small muted">{t("ex.pending", { n: q.data.prev_close_pending })}</div>}
      {q.error && <div className="st-error small">{trBackend(q.error)}</div>}
      {q.data?.meta && <Meta updated={q.data.meta.updated} source={q.data.meta.source} status={q.data.meta.status} error={q.data.meta.error} now={now} />}
    </div>
  );
}
