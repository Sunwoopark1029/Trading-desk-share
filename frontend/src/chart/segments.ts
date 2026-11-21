// Toss-style drawn lines: each line covers only its part of the chart (from its start point to a little past the
// latest candle), with its label written on the chart at the line's left end. No price-axis tags.
// buildShapes() is pure (unit-tested in segments.test.ts); SegmentsPrimitive draws its result on the canvas.
// Positions use the chart's logical bar index (0 = first loaded bar), so lines can run past the last candle.

import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type {
  IChartApiBase, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, Logical,
  PrimitivePaneViewZOrder, SeriesAttachedParameter, SeriesType, Time,
} from "lightweight-charts";

export interface SegLine { x1: number; p1: number; x2: number; p2: number; color: string; width: number; dash: number[] }
export interface SegFill { x1: number; x2: number; top: number; bottom: number; color: string }
/** align "right": text ends just left of (x, p) (Fibonacci, like Toss); "above": starts at (x, p), sits on the line;
 *  "edge": flat lines: pinned to the chart's left edge at the line's height (keeps the right side uncluttered). */
export interface SegLabel { id: string; x: number; p: number; text: string; color: string; align: "right" | "above" | "edge" }
export interface Shapes { lines: SegLine[]; fills: SegFill[]; labels: SegLabel[] }

export interface ShapeInput {
  id: string;                                     // overlay id (labels are clickable: the legend opens this entry)
  type: "hline" | "trendline" | "fib" | "zone";
  points: { time: number; price: number }[];      // time = epoch s (bar start or any time inside a bar)
  levels?: { ratio: string; price: number }[];    // fib only (code-calculated)
  label: string;                                  // already includes "AI " for AI lines
  color: string;
  width: number;
  dash: number[];
}

export interface ShapeOpts {
  starts: number[];            // bar start times (epoch s), ascending
  extendBars: number;          // how far past the latest candle lines run
  backBars: number;            // a line anchored at the latest candle (no older start known) starts this many bars back
  fillAlpha: number;           // 0..1, Fibonacci / zone bands
  fmt: (p: number) => string;  // price text
  fade: (color: string, alpha: number) => string;
}

/** Toss-like Fibonacci colors per ratio; other ratios use the drawing's own color. */
export const FIB_RATIO_COLORS: Record<string, string> = {
  "0": "#9e9e9e", "0.236": "#ef5350", "0.382": "#ffa726", "0.5": "#66bb6a", "0.618": "#26a69a",
  "0.786": "#4dd0e1", "1": "#9e9e9e", "1.272": "#7e57c2", "1.618": "#5c6bc0", "2.618": "#ab47bc",
};

export const ratioText = (r: string) => {
  const n = Number(r);
  return Number.isFinite(n) ? String(Number(n.toFixed(3))) : r;
};

/** Index of the bar containing `time` (last bar starting at or before it), clamped to the loaded bars. */
export function barIndex(starts: number[], time: number): number {
  if (!starts.length) return 0;
  if (time <= starts[0]) return 0;
  let lo = 0, hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (starts[mid] <= time) lo = mid; else hi = mid - 1;
  }
  return lo;
}

export function buildShapes(items: ShapeInput[], o: ShapeOpts): Shapes {
  const out: Shapes = { lines: [], fills: [], labels: [] };
  const n = o.starts.length;
  if (!n) return out;
  const last = n - 1;
  const right = last + o.extendBars;
  const startAt = (time: number) => {
    const i = barIndex(o.starts, time);
    return i >= last - 1 ? Math.max(0, last - o.backBars) : i;   // anchored "now": show a stretch, not a stub
  };

  for (const it of items) {
    const pts = it.points.filter((p) => Number.isFinite(p.price) && Number.isFinite(p.time));
    if (it.type === "hline" && pts.length >= 1) {
      const x1 = startAt(pts[0].time), p = pts[0].price;
      out.lines.push({ x1, p1: p, x2: right, p2: p, color: it.color, width: it.width, dash: it.dash });
      out.labels.push({ id: it.id, x: x1, p, text: `${it.label} (${o.fmt(p)})`, color: it.color, align: "edge" });
    } else if (it.type === "zone" && pts.length >= 2) {
      const x1 = startAt(Math.min(pts[0].time, pts[1].time));
      const top = Math.max(pts[0].price, pts[1].price), bottom = Math.min(pts[0].price, pts[1].price);
      for (const p of [top, bottom]) out.lines.push({ x1, p1: p, x2: right, p2: p, color: it.color, width: 1, dash: it.dash });
      if (o.fillAlpha > 0) out.fills.push({ x1, x2: right, top, bottom, color: o.fade(it.color, o.fillAlpha) });
      out.labels.push({ id: it.id, x: x1, p: top, text: `${it.label} (${o.fmt(bottom)}–${o.fmt(top)})`, color: it.color, align: "edge" });
    } else if (it.type === "fib" && pts.length >= 1 && it.levels?.length) {
      const x1 = barIndex(o.starts, Math.min(...pts.map((p) => p.time)));
      const lv = it.levels.filter((l) => Number.isFinite(l.price)).sort((a, b) => b.price - a.price);
      lv.forEach((l, i) => {
        const color = FIB_RATIO_COLORS[ratioText(l.ratio)] ?? it.color;
        out.lines.push({ x1, p1: l.price, x2: right, p2: l.price, color, width: 1, dash: [] });
        const name = i === 0 && it.label ? `${it.label} · ` : "";
        out.labels.push({ id: it.id, x: x1, p: l.price, text: `${name}${ratioText(l.ratio)} (${o.fmt(l.price)})`, color, align: "right" });
        const below = lv[i + 1];
        if (below && o.fillAlpha > 0) {
          out.fills.push({ x1, x2: right, top: l.price, bottom: below.price, color: o.fade(color, o.fillAlpha) });
        }
      });
    } else if (it.type === "trendline" && pts.length >= 2) {
      const [a, b] = [...pts].sort((u, v) => u.time - v.time);
      const xa = barIndex(o.starts, a.time), xb = barIndex(o.starts, b.time);
      if (xa === xb) continue;
      const slope = (b.price - a.price) / (xb - xa);
      const pEnd = a.price + slope * (right - xa);           // continues to the right edge, like Toss
      out.lines.push({ x1: xa, p1: a.price, x2: right, p2: pEnd, color: it.color, width: it.width, dash: it.dash });
      out.labels.push({ id: it.id, x: xa, p: a.price, text: it.label, color: it.color, align: "above" });
    }
  }
  return out;
}

// ── canvas drawing ─────────────────────────────────────────────────────────

type Coords = { x: (l: number) => number | null; y: (p: number) => number | null };
export interface HitBox { id: string; x: number; y: number; w: number; h: number }

class SegRenderer implements IPrimitivePaneRenderer {
  private shapes: Shapes;
  private coords: Coords | null;
  private layer: "back" | "front";
  private hits: HitBox[];

  constructor(shapes: Shapes, coords: Coords | null, layer: "back" | "front", hits: HitBox[]) {
    this.shapes = shapes;
    this.coords = coords;
    this.layer = layer;
    this.hits = hits;
  }

  draw(target: CanvasRenderingTarget2D): void {
    if (this.layer === "front") this.paint(target);
  }

  drawBackground(target: CanvasRenderingTarget2D): void {
    if (this.layer === "back") this.paint(target);
  }

  private paint(target: CanvasRenderingTarget2D): void {
    const c = this.coords;
    if (!c) return;
    target.useMediaCoordinateSpace(({ context: ctx }) => {
      if (this.layer === "back") {
        for (const f of this.shapes.fills) {
          const x1 = c.x(f.x1), x2 = c.x(f.x2), y1 = c.y(f.top), y2 = c.y(f.bottom);
          if (x1 === null || x2 === null || y1 === null || y2 === null) continue;
          ctx.fillStyle = f.color;
          ctx.fillRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.max(1, Math.abs(y2 - y1)));
        }
        return;
      }
      for (const l of this.shapes.lines) {
        const x1 = c.x(l.x1), x2 = c.x(l.x2), y1 = c.y(l.p1), y2 = c.y(l.p2);
        if (x1 === null || x2 === null || y1 === null || y2 === null) continue;
        ctx.strokeStyle = l.color;
        ctx.lineWidth = l.width;
        ctx.setLineDash(l.dash);
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      this.hits.length = 0;
      ctx.font = "11px -apple-system, 'Segoe UI', 'Malgun Gothic', sans-serif";
      // left-edge labels: top to bottom, nudged apart so close prices don't print on top of each other
      const edgeY = new Map<SegLabel, number>();
      let prev = -Infinity;
      const edge = this.shapes.labels
        .map((lb) => ({ lb, y: c.y(lb.p) }))
        .filter((e): e is { lb: SegLabel; y: number } => e.lb.align === "edge" && e.y !== null)
        .sort((a, b) => a.y - b.y);
      for (const e of edge) {
        const ty = Math.max(e.y - 4, prev + 13);
        edgeY.set(e.lb, ty);
        prev = ty;
      }
      for (const lb of this.shapes.labels) {
        const x = c.x(lb.x), y = c.y(lb.p);
        if (x === null || y === null) continue;
        const w = ctx.measureText(lb.text).width;
        let tx: number, ty: number;
        if (lb.align === "edge") {
          tx = 4;
          ty = edgeY.get(lb) ?? y - 4;
        } else if (lb.align === "right") {
          tx = x - 6 - w;                       // left of the box, like Toss
          if (tx < 2) tx = x + 4;               // no room on the left: just inside the box
          ty = y + 4;
        } else {
          tx = Math.max(2, x + 2);
          ty = y - 4;                           // sits on top of the line at its left end
        }
        this.hits.push({ id: lb.id, x: tx - 2, y: ty - 12, w: w + 4, h: 16 });
        ctx.lineWidth = 3;
        ctx.strokeStyle = "rgba(22,22,27,0.85)"; // thin halo so text stays readable over candles
        ctx.strokeText(lb.text, tx, ty);
        ctx.fillStyle = lb.color;
        ctx.fillText(lb.text, tx, ty);
      }
    });
  }
}

export class SegmentsPrimitive implements ISeriesPrimitive<Time> {
  private shapes: Shapes = { lines: [], fills: [], labels: [] };
  private chart: IChartApiBase<Time> | null = null;
  private series: ISeriesApi<SeriesType> | null = null;
  private requestUpdate: (() => void) | null = null;
  private readonly views: IPrimitivePaneView[];
  private hits: HitBox[] = [];                   // label boxes from the last paint (media px)

  constructor() {
    const view = (layer: "back" | "front", z: PrimitivePaneViewZOrder): IPrimitivePaneView => ({
      zOrder: () => z,
      renderer: () => new SegRenderer(this.shapes, this.coords(), layer, this.hits),
    });
    this.views = [view("back", "bottom"), view("front", "top")];
  }

  private coords(): Coords | null {
    const ch = this.chart, se = this.series;
    if (!ch || !se) return null;
    const ts = ch.timeScale();
    return { x: (l) => ts.logicalToCoordinate(l as Logical), y: (p) => se.priceToCoordinate(p) };
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.chart = param.chart;
    this.series = param.series as ISeriesApi<SeriesType>;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.series = null;
    this.requestUpdate = null;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }

  /** The line whose label is under (x, y) in chart pixels, if any (topmost first). */
  labelAt(x: number, y: number): string | null {
    for (let i = this.hits.length - 1; i >= 0; i--) {
      const h = this.hits[i];
      if (x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + h.h) return h.id;
    }
    return null;
  }

  setShapes(shapes: Shapes): void {
    this.shapes = shapes;
    this.requestUpdate?.();
  }
}
