// Main chart: candles, volume, session shading, EMA 9/20/60, VWAP, Fibonacci, swing
// markers and overlay lines (shared overlay format). All numbers come from the backend;
// this file only draws them. Bar times are bar START times (epoch s), shown in KST.

import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  TickMarkType,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { BarOut, CandlesOut, FillMark, LineLook, Overlay } from "../api";
import { chartCrosshair, chartTick, fmtNum, num, priceDp } from "../format";
import { fade } from "./bands";
import { buildShapes, FIB_RATIO_COLORS, ratioText, SegmentsPrimitive, type ShapeInput } from "./segments";
import { tr, type Key } from "../i18n";
import { visibleRange, type VisibleRange } from "./range";

export interface ChartToggles {
  volume: boolean;
  sessions: boolean;
  ema: boolean;
  vwap: boolean;
  rsi?: boolean; // RSI(14) in its own panel under the candles
  fib: boolean; // default (code-calculated) Fibonacci
  swings: boolean;
  lines: boolean; // user lines: avg price, plans, own Fibonacci
  aiLines: boolean; // lines drawn by the AI (source "ai")
  orders?: boolean; // fills (markers) and open-order lines
  options?: boolean; // options expected-move bands (day / week), from GET /api/options
  research?: boolean; // saved research thesis reference prices (phase 6 part C)
}

export interface PickedPoint {
  time: number; // bar start, epoch s
  price: string; // bar high or low (whichever is nearer the click)
}

interface Props {
  data: CandlesOut | null;
  overlays: Overlay[];
  toggles: ChartToggles;
  picking: boolean;
  onPick: (p: PickedPoint) => void;
  defaultHeightPct?: number; // Settings page: chart share of the window (wide screens)
  dottedStyle?: LineLook;    // Settings page: all dotted/dashed lines
  colorScheme?: "kr" | "us";
  grid?: boolean;
  vwapWidth?: number;
  dottedOpacity?: number;  // % (Settings)
  zoneFillPct?: number;    // % (Settings)
  solidWidth?: number;     // px (Settings)
  fills?: FillMark[];      // real Toss fills for this symbol
}

const LOOK: Record<LineLook, LineStyle> = {
  solid: LineStyle.Solid, dotted: LineStyle.Dotted, sparse_dotted: LineStyle.SparseDotted,
  dashed: LineStyle.Dashed, large_dashed: LineStyle.LargeDashed,
};

export const EMA_COLORS: Record<string, string> = { "9": "#3dd68c", "20": "#f5a524", "60": "#a970ff" };
export const VWAP_COLOR = "#d8d8de";
export const RSI_COLOR = "#26c6da";
const RSI_LEVELS = [70, 30]; // usual overbought / oversold guides
export const PALETTES = {
  kr: { up: "#f04452", down: "#3182f6" },   // Korean convention: rising red, falling blue
  us: { up: "#26a69a", down: "#ef5350" },
} as const;
const BG = "#16161b";
const TEXT = "#8b8b96";
const GRID = "#24242c";
const hexA = (hex: string, a: number) =>
  `rgba(${parseInt(hex.slice(1, 3), 16)},${parseInt(hex.slice(3, 5), 16)},${parseInt(hex.slice(5, 7), 16)},${a})`;
const FIB_COLOR = "#9e9d24";
const SESSION_COLORS: Record<string, string> = {
  day: "rgba(126, 87, 194, 0.10)",
  pre: "rgba(66, 165, 245, 0.10)",
  regular: "rgba(0, 0, 0, 0)",
  after: "rgba(255, 167, 38, 0.10)",
};

const t = (s: number) => s as UTCTimestamp;
// canvas dash pattern for each line look (drawn lines are painted by SegmentsPrimitive, not the library)
const DASH: Record<number, number[]> = {
  [LineStyle.Solid]: [], [LineStyle.Dotted]: [1, 2], [LineStyle.Dashed]: [6, 4],
  [LineStyle.LargeDashed]: [10, 6], [LineStyle.SparseDotted]: [1, 4],
};

const FIB_MEANING: Record<string, string> = {
  "0": "key.fib.0", "0.236": "key.fib.0.236", "0.382": "key.fib.0.382", "0.5": "key.fib.0.5", "0.618": "key.fib.0.618",
  "0.786": "key.fib.0.786", "1": "key.fib.1", "1.272": "key.fib.ext", "1.618": "key.fib.ext", "2.618": "key.fib.ext",
};

/** One entry of the line key (bottom-left): press it to see what the line means. */
interface KeyItem {
  id: string;
  type: "hline" | "trendline" | "fib" | "zone";
  title: string;
  color: string;
  price: string;
  source: "user" | "indicator" | "ai";
  note: string | null;
  fib?: { ratio: string; price: string; color: string }[];
}

type Line = ISeriesApi<"Line">;

interface Legend {
  o: string; h: string; l: string; c: string; v: string; time: number;
}

export default function PriceChart({ data, overlays, toggles, picking, onPick, defaultHeightPct, dottedStyle, vwapWidth,
  colorScheme, grid, dottedOpacity, zoneFillPct, solidWidth, fills }: Props) {
  const solidW = Math.min(3, Math.max(1, solidWidth ?? 1));
  const dotA = Math.min(100, Math.max(10, dottedOpacity ?? 45)) / 100;
  const fillA = Math.min(30, Math.max(0, zoneFillPct ?? 8)) / 100;
  const segs = useRef<SegmentsPrimitive | null>(null);
  const [keyItems, setKeyItems] = useState<KeyItem[]>([]);
  const [keyOpen, setKeyOpen] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const openKey = useRef((id: string) => { setKeyOpen(true); setExpanded(id); });
  const pal = PALETTES[colorScheme ?? "kr"];
  const UP = pal.up;
  const DOWN = pal.down;
  const palRef = useRef(pal);
  palRef.current = pal;
  const dot = LOOK[dottedStyle ?? "sparse_dotted"];
  const dotRef = useRef(dot);
  dotRef.current = dot;
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const s = useRef<{
    shade: ISeriesApi<"Histogram">;
    candle: ISeriesApi<"Candlestick">;
    volume: ISeriesApi<"Histogram">;
    ema: Record<string, Line>;
    vwap: Line;
    markers: ISeriesMarkersPluginApi<Time>;
  } | null>(null);
  const priceLines = useRef<IPriceLine[]>([]);
  const extraSeries = useRef<Line[]>([]);
  const rsiSeries = useRef<Line | null>(null); // created only while the RSI toggle is on (pane 1)
  const fitKey = useRef<string>("");
  const dailyRef = useRef(false);
  const barsRef = useRef<Map<number, BarOut>>(new Map());
  const barsArr = useRef<BarOut[]>([]);
  const [vis, setVis] = useState<VisibleRange | null>(null);
  // high / low of the bars on screen and % from the current price; follows scrolling, zooming and new prices
  const updateVis = useRef(() => {});
  updateVis.current = () => {
    const c = chart.current;
    const arr = barsArr.current;
    const r = c?.timeScale().getVisibleLogicalRange();
    const last = arr.length ? num(arr[arr.length - 1].close) : null;
    setVis(r ? visibleRange(arr, r.from, r.to, last) : null);
  };
  const pickRef = useRef({ picking, onPick });
  pickRef.current = { picking, onPick };
  const [legend, setLegend] = useState<Legend | null>(null);
  const [height, setHeight] = useState<number | null>(loadHeight); // null = default (CSS)
  const wrap = useRef<HTMLDivElement>(null);

  // Drag the handle under the chart to change its height (mouse or touch); double-click resets.
  const startResize = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = wrap.current?.getBoundingClientRect().height ?? 400;
    const handle = e.currentTarget;
    handle.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => setHeight(clampHeight(startH + ev.clientY - startY));
    const up = (ev: PointerEvent) => {
      handle.releasePointerCapture(ev.pointerId);
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      setHeight((h) => {
        saveHeight(h);
        return h;
      });
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  };
  const resetHeight = () => {
    setHeight(null);
    saveHeight(null);
  };
  const lastLine = useRef<IPriceLine | null>(null);
  const lastBar = useRef<{ bar: BarOut; interval: number } | null>(null);

  // Last price line on the right axis + time into the current candle ("37/60s", "2:37/5:00").
  useEffect(() => {
    const tick = () => {
      const line = lastLine.current;
      const lb = lastBar.current;
      if (!line) return;
      if (!lb) {
        line.applyOptions({ axisLabelVisible: false, title: "", lineVisible: false });
        return;
      }
      const { bar, interval } = lb;
      const up = Number(bar.close) >= Number(bar.open);
      let title = "";
      if (interval < 86400) {
        const elapsed = Math.floor(Date.now() / 1000) - bar.start;
        if (elapsed >= 0 && elapsed < interval) {
          title = interval === 60 ? `${elapsed}/60s`
            : `${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")}/${interval / 60}:00`;
        } else {
          title = tr("chart.closed"); // latest candle is over: market closed or data not updating
        }
      }
      line.applyOptions({ price: Number(bar.close), color: up ? palRef.current.up : palRef.current.down, axisLabelVisible: true,
        lineVisible: true, lineStyle: dotRef.current, title });
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, []);

  // create once
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const c = createChart(el, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: BG }, textColor: TEXT, fontSize: 11 },
      grid: { vertLines: { color: GRID, visible: false }, horzLines: { color: GRID, visible: false } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#2a2e39" },
      timeScale: {
        borderColor: "#2a2e39",
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: (time: Time, type: TickMarkType) =>
          chartTick(time as number, dailyRef.current, type <= TickMarkType.DayOfMonth),
      },
      localization: { timeFormatter: (time: Time) => chartCrosshair(time as number, dailyRef.current) },
    });
    const shade = c.addSeries(HistogramSeries, {
      priceScaleId: "shade", priceLineVisible: false, lastValueVisible: false, base: 0,
    });
    c.priceScale("shade").applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
    const volume = c.addSeries(HistogramSeries, {
      priceScaleId: "vol", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false,
    });
    c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 }, visible: false });
    const candle = c.addSeries(CandlestickSeries, {
      upColor: UP, downColor: DOWN, borderVisible: false, wickUpColor: UP, wickDownColor: DOWN,
      lastValueVisible: false, priceLineVisible: false, // replaced by the timer line below
    });
    const ema: Record<string, Line> = {};
    for (const [k, color] of Object.entries(EMA_COLORS)) {
      ema[k] = c.addSeries(LineSeries, {
        color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        autoscaleInfoProvider: () => null,   // the price axis fits the candles only (Toss-like); far lines run off-screen
      });
    }
    const vwap = c.addSeries(LineSeries, {
      color: VWAP_COLOR, lineWidth: 2, lineStyle: LineStyle.Dotted, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false, autoscaleInfoProvider: () => null,
    });
    // candles use more of the height (library default leaves 20% empty on top, 10% below)
    c.priceScale("right").applyOptions({ scaleMargins: { top: 0.06, bottom: 0.14 } });
    const markers = createSeriesMarkers(candle, []);
    segs.current = new SegmentsPrimitive();
    candle.attachPrimitive(segs.current);
    s.current = { shade, candle, volume, ema, vwap, markers };
    lastLine.current = candle.createPriceLine({
      price: 0, color: UP, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "",
    });
    chart.current = c;

    const onClick = (p: MouseEventParams<Time>) => {
      if (!pickRef.current.picking && p.point) {          // pressing a line's label opens its explanation
        const id = segs.current?.labelAt(p.point.x, p.point.y);
        if (id) openKey.current(id);
        return;
      }
      if (!pickRef.current.picking || p.time === undefined || !p.point) return;
      const bar = barsRef.current.get(p.time as number);
      const clicked = candle.coordinateToPrice(p.point.y);
      if (!bar || clicked === null) return;
      const hi = num(bar.high) ?? 0;
      const lo = num(bar.low) ?? 0;
      const price = Math.abs(clicked - hi) <= Math.abs(clicked - lo) ? bar.high : bar.low;
      pickRef.current.onPick({ time: bar.start, price });
    };
    const onMove = (p: MouseEventParams<Time>) => {
      const bar = p.time !== undefined ? barsRef.current.get(p.time as number) : undefined;
      setLegend(bar ? { o: bar.open, h: bar.high, l: bar.low, c: bar.close, v: bar.volume, time: bar.start } : null);
    };
    const onRange = () => updateVis.current();
    c.subscribeClick(onClick);
    c.subscribeCrosshairMove(onMove);
    c.timeScale().subscribeVisibleLogicalRangeChange(onRange);
    return () => {
      c.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);
      c.unsubscribeClick(onClick);
      c.unsubscribeCrosshairMove(onMove);
      c.remove();
      chart.current = null;
      s.current = null;
      priceLines.current = [];
      lastLine.current = null;
      segs.current = null;
      extraSeries.current = [];
      rsiSeries.current = null;
    };
  }, []);

  // colors + grid from the Settings page
  useEffect(() => {
    const c = chart.current;
    const ss = s.current;
    if (!c || !ss) return;
    ss.candle.applyOptions({ upColor: UP, downColor: DOWN, wickUpColor: UP, wickDownColor: DOWN });
    c.applyOptions({ grid: { vertLines: { visible: !!grid }, horzLines: { visible: !!grid } } });
  }, [UP, DOWN, grid]);

  // series data
  useEffect(() => {
    const c = chart.current;
    const ss = s.current;
    if (!c || !ss) return;
    const done = data?.bars ?? [];
    const lb = data?.live_bar;
    const bars = !lb ? done
      : done.length && done[done.length - 1].start === lb.start ? [...done.slice(0, -1), lb]
      : [...done, lb];
    const daily = data?.tf === "1d";
    dailyRef.current = daily;
    barsRef.current = new Map(bars.map((b) => [b.start, b]));
    barsArr.current = bars;
    lastBar.current = bars.length ? { bar: bars[bars.length - 1], interval: data?.interval_seconds ?? 60 } : null;
    c.applyOptions({ timeScale: { timeVisible: !daily } });

    const last = bars.length ? num(bars[bars.length - 1].close) ?? 1 : 1;
    const dp = priceDp(last);
    ss.candle.applyOptions({ priceFormat: { type: "price", precision: dp, minMove: 1 / 10 ** dp } });
    ss.candle.setData(bars.map((b) => ({
      time: t(b.start), open: Number(b.open), high: Number(b.high), low: Number(b.low), close: Number(b.close),
    })));

    ss.volume.applyOptions({ visible: toggles.volume });
    ss.volume.setData(bars.map((b) => ({
      time: t(b.start), value: Number(b.volume),
      color: Number(b.close) >= Number(b.open) ? hexA(UP, 0.35) : hexA(DOWN, 0.35),
    })));

    ss.shade.applyOptions({ visible: toggles.sessions && !daily });
    ss.shade.setData(bars.map((b) => ({
      time: t(b.start), value: 1, color: SESSION_COLORS[b.session ?? ""] ?? "rgba(0,0,0,0)",
    })));

    const lineData = (vals: (string | null)[] | null | undefined) =>
      done.map((b, i) => {
        const v = vals ? num(vals[i]) : null;
        return v === null ? { time: t(b.start) } : { time: t(b.start), value: v };
      });
    for (const [k, series] of Object.entries(ss.ema)) {
      series.applyOptions({ visible: toggles.ema });
      series.setData(lineData(data?.ema?.[k]));
    }
    ss.vwap.applyOptions({ visible: toggles.vwap && !!data?.vwap, lineStyle: dot, color: fade(VWAP_COLOR, dotA),
      lineWidth: Math.min(4, Math.max(1, vwapWidth ?? 2)) as 1 | 2 | 3 | 4 });
    ss.vwap.setData(data?.vwap ? lineData(data.vwap) : []);

    // RSI (code-calculated in the backend) in its own panel; removed again when switched off
    if (toggles.rsi) {
      if (!rsiSeries.current) {
        const r = c.addSeries(LineSeries, {
          color: RSI_COLOR, lineWidth: 1, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
          priceFormat: { type: "price", precision: 1, minMove: 0.1 },
          autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
        }, 1);
        for (const lv of RSI_LEVELS) {
          r.createPriceLine({ price: lv, color: "#5d6270", lineWidth: 1, lineStyle: LineStyle.Dashed,
            axisLabelVisible: false, title: "" });
        }
        const pane = c.panes()[1];
        if (pane) pane.setHeight(Math.max(70, Math.round((box.current?.clientHeight ?? 400) * 0.22)));
        rsiSeries.current = r;
      }
      rsiSeries.current.setData(lineData(data?.rsi));
    } else if (rsiSeries.current) {
      c.removeSeries(rsiSeries.current);
      rsiSeries.current = null;
      if (c.panes().length > 1) c.removePane(1);
    }

    const marks: SeriesMarker<Time>[] = toggles.swings
      ? (data?.swings ?? []).map((m) => ({
          time: t(m.start),
          position: m.kind === "high" ? "aboveBar" : "belowBar",
          shape: m.kind === "high" ? "arrowDown" : "arrowUp",
          color: m.kind === "high" ? DOWN : UP,
          size: 0.6,
        }))
      : [];
    if (toggles.orders !== false && fills?.length && bars.length) {
      // each fill goes on the bar it happened in (bar start <= fill time)
      for (const f of fills) {
        const ft = Date.parse(f.time) / 1000;
        if (!Number.isFinite(ft) || ft < bars[0].start) continue;
        let lo = 0, hi = bars.length - 1;
        while (lo < hi) {
          const mid = (lo + hi + 1) >> 1;
          if (bars[mid].start <= ft) lo = mid; else hi = mid - 1;
        }
        const buy = f.side === "BUY";
        marks.push({
          time: t(bars[lo].start),
          position: buy ? "belowBar" : "aboveBar",
          shape: buy ? "arrowUp" : "arrowDown",
          color: buy ? UP : DOWN,
          text: `${buy ? "B" : "S"} ${fmtNum(Number(f.qty), Number(f.qty) % 1 ? 4 : 0)} @ ${fmtNum(Number(f.price), priceDp(Number(f.price)))}`,
          size: 1,
        });
      }
    }
    marks.sort((a, b) => (a.time as number) - (b.time as number));
    ss.markers.setMarkers(marks);

    const key = data ? `${data.symbol}|${data.tf}` : "";
    if (key && key !== fitKey.current && bars.length) {
      fitKey.current = key;
      const n = bars.length;
      const show = daily ? 120 : data?.tf === "1m" ? 240 : 200;
      c.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - show), to: n + 5 });
    }
    updateVis.current();
  }, [data, toggles, dot, vwapWidth, UP, DOWN, dotA, fills]);

  // price lines + trendlines (default fib + overlays)
  useEffect(() => {
    const c = chart.current;
    const ss = s.current;
    if (!c || !ss) return;
    for (const pl of priceLines.current) ss.candle.removePriceLine(pl);
    priceLines.current = [];
    for (const ser of extraSeries.current) c.removeSeries(ser);
    extraSeries.current = [];

    const addLine = (price: string | number, color: string, title: string, style = LineStyle.Solid, width = 1) => {
      const p = num(price);
      if (p === null) return;
      const faint = style !== LineStyle.Solid;   // dotted/dashed lines are drawn fainter (Settings)
      priceLines.current.push(ss.candle.createPriceLine({
        price: p, color: faint ? fade(color, dotA) : color, lineWidth: width as 1, lineStyle: style,
        axisLabelVisible: true, axisLabelColor: color, title,
      }));
    };
    const bars = data?.bars ?? [];
    const dp = priceDp(num(bars[bars.length - 1]?.close ?? null) ?? 1);
    const fmtP = (p: number) => fmtNum(p, dp);
    const toEpoch = (iso: string) => Math.floor(new Date(iso).getTime() / 1000);
    const shapes: ShapeInput[] = [];
    const keys: KeyItem[] = [];
    const fibLevels = (levels: { ratio: string; price: string | number }[]) =>
      levels.map((l) => ({ ratio: l.ratio, price: num(l.price) ?? NaN })).filter((l) => Number.isFinite(l.price));
    const fibKey = (levels: { ratio: string; price: number }[], fallback: string) =>
      [...levels].sort((a, b) => b.price - a.price).map((l) => ({
        ratio: ratioText(l.ratio), price: fmtP(l.price), color: FIB_RATIO_COLORS[ratioText(l.ratio)] ?? fallback }));

    if (toggles.fib && data?.fib) {              // default (code-calculated) Fibonacci, same Toss-style box
      const levels = fibLevels(data.fib.levels);
      const pts = data.fib.points.map((p) => ({ time: p.start, price: num(p.price) ?? NaN }));
      shapes.push({ id: "fib-default", type: "fib", points: pts, levels, label: "Fib", color: FIB_COLOR, width: 1, dash: [] });
      keys.push({ id: "fib-default", type: "fib", title: "Fib", color: FIB_COLOR, source: "indicator", note: null,
        price: `${fmtP(num(data.fib.high) ?? 0)} / ${fmtP(num(data.fib.low) ?? 0)}`, fib: fibKey(levels, FIB_COLOR) });
    }

    const mapColor = (c: string) => (c === "#26a69a" ? UP : c === "#ef5350" ? DOWN : c);
    for (const o0 of overlays) {
      const o = o0.derived ? { ...o0, color: mapColor(o0.color) } : o0;
      const shown = o.group === "research" ? toggles.research !== false   // own toggle, not "My lines"
        : (o.source === "ai" ? toggles.aiLines : toggles.lines);
      if (!o.visible || !shown) continue;
      const tag = o.source === "ai" ? "AI " : "";
      // "fact" lines (orders, reserved triggers, average price, plans) stay full width, labeled on the right axis
      if (o.group === "orders" || o.derived) {
        if (o.type === "hline") addLine(o.points[0].price, o.color, `${tag}${o.label}`, o.dashed ? dot : LineStyle.Solid, solidW);
        continue;
      }
      const pts = o.points.map((p) => ({ time: toEpoch(p.time), price: num(p.price) ?? NaN }));
      const title = `${tag}${o.label || (o.type === "fib" ? "Fib" : "")}`;
      const look = o.type === "zone" || o.dashed ? dot : LineStyle.Solid;
      const levels = o.type === "fib" ? fibLevels(o.levels ?? []) : undefined;
      shapes.push({ id: o.id, type: o.type, points: pts, levels, label: title, color: o.color,
        width: o.type === "zone" ? 1 : solidW, dash: DASH[look] ?? [] });
      const ps = pts.map((p) => p.price).filter(Number.isFinite);
      const price = !ps.length ? "" : o.type === "hline" ? fmtP(ps[0])
        : o.type === "zone" || o.type === "fib" ? `${fmtP(Math.max(...ps))} / ${fmtP(Math.min(...ps))}` : "";
      keys.push({ id: o.id, type: o.type, title, color: o.color, price, source: o.source, note: o.note,
        fib: levels ? fibKey(levels, o.color) : undefined });
    }
    const built = buildShapes(shapes, {
      starts: bars.map((b) => b.start), extendBars: 8, backBars: 60, fillAlpha: fillA, fmt: fmtP, fade,
    });
    segs.current?.setShapes(built);
    setKeyItems(keys);
  }, [data, overlays, toggles, dot, UP, DOWN, dotA, fillA, solidW]);

  const cur = data?.meta.currency ?? "";
  const lg = legend ?? (data?.bars.length ? (() => {
    const b = data.bars[data.bars.length - 1];
    return { o: b.open, h: b.high, l: b.low, c: b.close, v: b.volume, time: b.start };
  })() : null);
  const dp = lg ? priceDp(num(lg.c) ?? 1) : 2;

  return (
    <>
    <div ref={wrap} className={`chart-wrap${picking ? " picking" : ""}`} style={height ? { height } : ({ "--chart-h": `${defaultHeightPct ?? 40}vh` } as React.CSSProperties)}>
      <div ref={box} className="chart-box" />
      {vis && (
        <div className="chart-range" title={tr("chart.rangeTitle")}>
          <span>{tr("chart.rangeHigh")} {fmtNum(vis.high, priceDp(vis.high))} <b className={vis.fromHighPct < 0 ? "down" : "up"}>
            ({vis.fromHighPct > 0 ? "+" : ""}{vis.fromHighPct.toFixed(2)}%)</b></span>
          <span>{tr("chart.rangeLow")} {fmtNum(vis.low, priceDp(vis.low))} <b className={vis.fromLowPct > 0 ? "up" : "down"}>
            ({vis.fromLowPct > 0 ? "+" : ""}{vis.fromLowPct.toFixed(2)}%)</b></span>
          <span className="muted">{cur}</span>
        </div>
      )}
      {lg && (
        <div className="chart-legend">
          <span className="muted">{chartCrosshair(lg.time, data?.tf === "1d")}</span>{" "}
          <span>O {fmtNum(lg.o, dp)}</span> <span>H {fmtNum(lg.h, dp)}</span>{" "}
          <span>L {fmtNum(lg.l, dp)}</span> <span>C {fmtNum(lg.c, dp)}</span>{" "}
          <span className="muted">{cur}</span> <span className="muted">{tr("chart.vol")} {fmtNum(lg.v, 0)}</span>
        </div>
      )}
      {keyItems.length > 0 && (
        <div className="line-key">
          <button className="line-key-head" onClick={() => setKeyOpen((v) => !v)} title={tr("key.headTitle")}>
            {tr("key.title", { n: keyItems.length })} {keyOpen ? "▾" : "▸"}
          </button>
          {keyOpen && keyItems.map((k) => (
            <div key={k.id} className={`line-key-item${expanded === k.id ? " open" : ""}`}>
              <button className="line-key-row" onClick={() => setExpanded((e) => (e === k.id ? null : k.id))}>
                <span className="line-key-swatch" style={{ background: k.color }} />
                <span className="line-key-name">{k.title}</span>
                {k.price && <span className="muted">{k.price} {cur}</span>}
              </button>
              {expanded === k.id && (
                <div className="line-key-body">
                  <div>{tr(`key.${k.type}` as Key)}</div>
                  <div className="muted">{tr(`key.src.${k.source}` as Key)}</div>
                  {k.note && <div className="line-key-note">{k.source === "ai" ? "AI: " : ""}{k.note}</div>}
                  {k.fib && (
                    <div className="line-key-fib">
                      {k.fib.map((f) => (
                        <div key={f.ratio} className="line-key-fibrow">
                          <span className="line-key-swatch" style={{ background: f.color }} />
                          <b style={{ color: f.color }}>{f.ratio}</b>
                          <span className="muted">{f.price}</span>
                          <span>{tr((FIB_MEANING[f.ratio] ?? "key.fib.other") as Key)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
    <div className="chart-resize" onPointerDown={startResize} onDoubleClick={resetHeight}
      title={tr("chart.resize")} role="separator" aria-orientation="horizontal" />
    </>
  );
}

const LS_HEIGHT = "desk.chart.height";
// at least 200 px of chart; on wide screens leave ~300 px for the chat below it
const clampHeight = (h: number) => Math.round(Math.min(Math.max(h, 200),
  window.innerWidth > 900 ? Math.max(260, window.innerHeight - 150) : window.innerHeight * 1.5));

function loadHeight(): number | null {
  try {
    const v = Number(localStorage.getItem(LS_HEIGHT));
    return Number.isFinite(v) && v >= 200 ? clampHeight(v) : null;
  } catch {
    return null;
  }
}

function saveHeight(h: number | null): void {
  try {
    if (h) localStorage.setItem(LS_HEIGHT, String(h));
    else localStorage.removeItem(LS_HEIGHT);
  } catch {
    /* storage unavailable: height lasts for this visit */
  }
}
