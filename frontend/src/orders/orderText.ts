import type { CondLegOut, CondOut, OrderOut } from "../api";
import { fmtPrice } from "../format";
import { useT, type Key } from "../i18n";

/** Reserved-order currency from Toss `market` (KR | US, pinned spec); USD when missing. */
export function condCurrency(c: CondOut): string {
  return c.market === "KR" ? "KRW" : "USD";
}

/** Shared wording for orders and reserved-order legs (Orders tab + desk panel). */
export function useOrderText() {
  const { t } = useT();
  const side = (s: string) => (s === "BUY" || s === "SELL" ? t(`ord.side.${s}` as Key) : s);
  const tif = (s: string | null) => (s === "DAY" || s === "CLS" ? t(`ord.tif.${s}` as Key) : s ?? "");
  const price = (o: OrderOut) => (o.price === null ? t("ord.market") : fmtPrice(o.price, o.currency));
  const leg = (l: CondLegOut, cur = "USD") =>
    `${side(l.order_side)} ${l.trigger_price !== null ? fmtPrice(l.trigger_price, cur) : `${l.target_profit_rate}%`} → `
    + `${l.order_price !== null ? fmtPrice(l.order_price, cur) : t("ord.market")} (${l.status})`;
  return { side, tif, price, leg };
}
