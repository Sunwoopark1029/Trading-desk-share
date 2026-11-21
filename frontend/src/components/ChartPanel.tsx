import { useEffect, useMemo, useState } from "react";
import { api, optionsApi, type ChartDefaults, type FillMark, type Overlay, type Timeframe } from "../api";
import PriceChart, { EMA_COLORS, RSI_COLOR, VWAP_COLOR, type ChartToggles, type PickedPoint } from "../chart/PriceChart";
import { epochToKstIso, fmtKstDateTime, fmtPrice } from "../format";
import { trBackend, useT, type Key } from "../i18n";
import { usePoll } from "../usePoll";
import StatusLine from "./Status";
import TickerSearch from "./TickerSearch";

interface Props {
  symbol: string;
  onSymbol: (s: string) => void;
  quick: string[];
  candlesMs: number;
  derivedLines: Overlay[]; // avg price + plan lines from positions.json (already for this symbol)
  now: number;
  overlayTick: number; // changes when the AI drew or removed a line
  onScreen: (s: Record<string, unknown>) => void; // what the chat's get_screen_state reports
  defaults?: ChartDefaults; // from the Settings page
  fills?: FillMark[]; // real Toss fills for this symbol (markers)
}

const TOGGLES: (keyof ChartToggles)[] = ["ema", "vwap", "rsi", "fib", "swings", "lines", "aiLines", "orders", "options", "research", "sessions", "volume"];
const BAND_COLOR = "#a78bfa";

export default function ChartPanel({ symbol, onSymbol, quick, candlesMs, derivedLines, now, overlayTick, onScreen,
  defaults, fills }: Props) {
  const { t } = useT();
  const [tf, setTf] = useState<Timeframe>(defaults?.timeframe ?? "5m");
  const [toggles, setToggles] = useState<ChartToggles>({
    volume: true, sessions: false, ema: true, vwap: true, rsi: false, fib: false, swings: false, lines: true, aiLines: true, orders: true, options: true,
    research: true,
    ...(defaults?.toggles as Partial<ChartToggles> | undefined),
  });
  const [picking, setPicking] = useState(false);
  const [firstPick, setFirstPick] = useState<PickedPoint | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const candles = usePoll(() => api.candles(symbol, tf), candlesMs, [symbol, tf]);
  const saved = usePoll(() => api.overlays(symbol, tf), 30_000, [symbol, tf]);   // only this timeframe's lines

  // options expected-move bands (calculated in the backend; yfinance ~15 min); only fetched while the toggle is on
  const opt = usePoll(() => (toggles.options ? optionsApi.get(symbol) : Promise.resolve(null)), 60_000, [symbol, toggles.options]);
  const bands = useMemo<Overlay[]>(() => {
    const v = opt.data;
    if (!toggles.options || !v?.available) return [];
    const at = new Date().toISOString();
    const out: Overlay[] = [];
    for (const [key, b] of [["day", v.day], ["week", v.week]] as const) {
      if (!b) continue;
      const span = key === "day" ? t("opt.day") : t("opt.toExpiry", { d: (b.expiry ?? "").slice(5) });
      for (const [side, price] of [["up", b.up], ["down", b.down]] as const) {
        out.push({ id: `em-${key}-${side}`, type: "hline", points: [{ time: at, price: price.toFixed(2) }],
          label: `${t("opt.band")} ${span} ${side === "up" ? "+" : "−"}1σ`, color: BAND_COLOR, source: "indicator",
          visible: true, note: null, symbol, derived: true, dashed: true });
      }
    }
    return out;
  }, [opt.data, toggles.options, symbol, t]);

  const overlays = useMemo(
    () => [...(saved.data?.overlays ?? []), ...derivedLines, ...bands].filter((o) =>
      (o.group !== "orders" || toggles.orders !== false) && (o.group !== "research" || toggles.research !== false)),
    [saved.data, derivedLines, bands, toggles.orders, toggles.research],
  );

  useEffect(() => {
    if (overlayTick > 0) saved.refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overlayTick]);

  useEffect(() => {
    onScreen({
      symbol, timeframe: tf,
      indicators: { ema: toggles.ema, vwap: toggles.vwap, fib: toggles.fib, swings: toggles.swings,
        sessions: toggles.sessions, volume: toggles.volume },
      visible_lines: overlays
        .filter((o) => o.visible && (o.source === "ai" ? toggles.aiLines : toggles.lines))
        .map((o) => ({ id: o.id, type: o.type, source: o.source, label: o.label,
          prices: o.points.map((p) => p.price) })),
      hidden_line_groups: [!toggles.lines && "user lines", !toggles.aiLines && "AI lines"].filter(Boolean),
    });
  }, [symbol, tf, toggles, overlays, onScreen]);

  const startPick = () => {
    setPicking(!picking);
    setFirstPick(null);
    setActionMsg(picking ? null : t("chart.pick1"));
  };

  const onPick = async (p: PickedPoint) => {
    if (!firstPick) {
      setFirstPick(p);
      setActionMsg(t("chart.pick2"));
      return;
    }
    if (firstPick.time === p.time) {
      setActionMsg(t("chart.pickSame"));
      return;
    }
    setPicking(false);
    setFirstPick(null);
    try {
      await api.saveOverlay({
        type: "fib",
        points: [firstPick, p].map((x) => ({ time: epochToKstIso(x.time), price: x.price })),
        label: t("chart.myFib"),
        color: "#ce93d8",
        source: "user",
        visible: true,
        note: null,
        symbol,
        timeframe: tf,
      });
      setActionMsg(t("chart.fibSaved"));
      saved.refresh();
    } catch (e) {
      setActionMsg(t("chart.saveFail", { msg: e instanceof Error ? e.message : String(e) }));
    }
  };

  const remove = async (id: string) => {
    try {
      await api.deleteOverlay(id);
      saved.refresh();
    } catch (e) {
      setActionMsg(t("chart.deleteFail", { msg: e instanceof Error ? e.message : String(e) }));
    }
  };

  const d = candles.data;
  const meta = d?.meta;
  const cur = meta?.currency ?? "USD";
  const savedList = saved.data?.overlays ?? [];

  return (
    <section className="panel chart-panel">
      <div className="toolbar">
        <TickerSearch symbol={symbol} quick={quick} onChange={onSymbol} />
        <div className="seg">
          {(["1m", "5m", "1d"] as Timeframe[]).map((x) => (
            <button key={x} className={tf === x ? "on" : ""} onClick={() => setTf(x)}>
              {x === "1d" ? t("chart.daily") : x}
            </button>
          ))}
        </div>
        <button className={picking ? "on" : ""} onClick={startPick} disabled={tf === "1d" && !picking}
          title={t("chart.pickTitle")}>
          {picking ? t("chart.cancelPick") : t("chart.pickFib")}
        </button>
      </div>

      <div className="chart-info">
        <b>{symbol}</b> <span className="muted">{tf === "1d" ? t("chart.daily") : tf} · {t("chart.pricesIn", { cur })}</span>{" "}
        <StatusLine status={meta?.status} updated={meta?.updated} error={meta?.error} now={now}
          label={meta?.source ? `Toss · ${trBackend(meta.delay ?? "")}` : undefined} fetchError={candles.error} />
        {toggles.ema && (
          <span className="key">
            {Object.entries(EMA_COLORS).map(([k, c]) => (
              <span key={k} style={{ color: c }}>EMA{k} </span>
            ))}
          </span>
        )}
        {toggles.vwap && d?.vwap && (
          <span className="key" style={{ color: VWAP_COLOR }}>VWAP{d.vwap_label ? ` (${trBackend(d.vwap_label)})` : ""}</span>
        )}
        {toggles.rsi && (
          <span className="key" style={{ color: RSI_COLOR }}>
            RSI14 {(() => {
              const v = [...(d?.rsi ?? [])].reverse().find((x) => x !== null && x !== undefined);
              return v === undefined || v === null ? "–" : Number(v).toFixed(1);
            })()}
          </span>
        )}
        {toggles.fib && d?.fib && (
          <span className="key muted">
            {t("chart.toggle.fib")} {d.fib_date} {d.fib.direction === "up" ? t("chart.lowHigh") : t("chart.highLow")}{" "}
            {fmtPrice(d.fib.low, cur)}–{fmtPrice(d.fib.high, cur)}
          </span>
        )}
        {toggles.sessions && tf !== "1d" && (
          <span className="key">
            <span className="sess-day">{t("chart.sess.day")}</span> <span className="sess-pre">{t("chart.sess.pre")}</span>{" "}
            <span className="muted">{t("chart.sess.regular")}</span> <span className="sess-after">{t("chart.sess.after")}</span>
          </span>
        )}
      </div>
      {actionMsg && <div className="action-msg">{actionMsg}</div>}

      <PriceChart data={d} overlays={overlays} toggles={toggles} picking={picking} onPick={onPick}
        defaultHeightPct={defaults?.height_pct} dottedStyle={defaults?.dotted_style}
        colorScheme={defaults?.color_scheme} grid={defaults?.grid}
        dottedOpacity={defaults?.dotted_opacity} zoneFillPct={defaults?.zone_fill_pct} solidWidth={defaults?.solid_width} vwapWidth={defaults?.vwap_width}
        fills={toggles.orders !== false ? fills : undefined} />

      <div className="toggles chart-toggles">
        {TOGGLES.map((k) => (
          <label key={k}>
            <input type="checkbox" checked={toggles[k]} onChange={() => setToggles({ ...toggles, [k]: !toggles[k] })} />
            {t(`chart.toggle.${k}` as Key)}
          </label>
        ))}
      </div>

      <div className="chart-foot muted">
        {tf === "1d" ? t("chart.footDaily") : t("chart.footIntraday")}
        {meta?.extended_hours_note && tf !== "1d" ? ` ${t("chart.extended")}` : ""}
        {!d && candles.loading && ` ${t("chart.loading")}`}
        {d && d.bars.length === 0 && ` ${t("chart.noBars")}`}
      </div>

      {savedList.length > 0 && (
        <details className="saved-lines">
          <summary>{t("chart.saved", { sym: symbol, n: savedList.length })}</summary>
          <ul>
            {savedList.map((o) => (
              <li key={o.id}>
                <span style={{ color: o.color }}>■</span> {o.label || o.type} <span className="muted">
                  {o.type} · {o.source} · {o.type === "hline" || o.type === "zone"
                    ? t("chart.drawnAt", { t: fmtKstDateTime(o.points[0]?.time) })
                    : fmtKstDateTime(o.points[0]?.time)}
                </span>{" "}
                <button className="small" onClick={() => remove(o.id)}>{t("common.delete")}</button>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
