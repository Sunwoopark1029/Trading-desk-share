// Light colored bands between two prices (support / resistance zones), drawn behind the candles.
// A Lightweight Charts "series primitive": the library calls renderer().draw() on every repaint.

import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type {
  IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, PrimitivePaneViewZOrder,
  SeriesAttachedParameter, SeriesType, Time,
} from "lightweight-charts";

export interface Band {
  top: number;     // price
  bottom: number;  // price
  color: string;   // fill (already transparent, e.g. rgba(...,0.08))
}

class BandsRenderer implements IPrimitivePaneRenderer {
  constructor(private bands: Band[], private series: ISeriesApi<SeriesType> | null) {}

  draw(): void {}

  drawBackground(target: CanvasRenderingTarget2D): void {
    const series = this.series;
    if (!series || !this.bands.length) return;
    target.useBitmapCoordinateSpace(({ context, bitmapSize, verticalPixelRatio }) => {
      for (const b of this.bands) {
        const y1 = series.priceToCoordinate(b.top);
        const y2 = series.priceToCoordinate(b.bottom);
        if (y1 === null || y2 === null) continue;
        const top = Math.round(Math.min(y1, y2) * verticalPixelRatio);
        const h = Math.max(1, Math.round(Math.abs(y2 - y1) * verticalPixelRatio));
        context.fillStyle = b.color;
        context.fillRect(0, top, bitmapSize.width, h);
      }
    });
  }
}

export class BandsPrimitive implements ISeriesPrimitive<Time> {
  private bands: Band[] = [];
  private series: ISeriesApi<SeriesType> | null = null;
  private requestUpdate: (() => void) | null = null;
  private readonly view: IPrimitivePaneView;

  constructor() {
    this.view = {
      zOrder: (): PrimitivePaneViewZOrder => "bottom",
      renderer: () => new BandsRenderer(this.bands, this.series),
    };
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.series = param.series as ISeriesApi<SeriesType>;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.series = null;
    this.requestUpdate = null;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return [this.view];
  }

  setBands(bands: Band[]): void {
    this.bands = bands;
    this.requestUpdate?.();
  }
}

/** "#rrggbb" / "#rgb" -> rgba with alpha; other formats unchanged. */
export function fade(color: string, alpha: number): string {
  let hex = color.trim();
  if (/^#[0-9a-fA-F]{3}$/.test(hex)) hex = "#" + [...hex.slice(1)].map((c) => c + c).join("");
  if (!/^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(hex)) return color;
  const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${Math.max(0, Math.min(1, alpha))})`;
}
