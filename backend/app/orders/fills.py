"""P&L from the Toss account (read-only): closed-order fills + holdings + transactions.

- Fills: GET /api/v1/orders?status=CLOSED (paged). Toss merges all fills of one order into one
  `execution` (filledQuantity, averageFilledPrice, commission, tax, filledAt).
- Applied FX: GET /api/v1/transactions (BUY/SELL) carries `exchangeRate`; matched to an order by
  symbol + side + settlement date + quantity. What exactly that rate means for USD-settled trades
  is UNVERIFIED (shown as Toss's applied rate; unmatched trades fall back to the current mid, est.).
- Shares that predate the order history (e.g. transferred in) become an "opening" position at
  Toss's average purchase price (est.), so P&L never crashes on an oversell.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.data.toss import HoldingItem, Order, TossClient
from app.timeutil import KST

log = logging.getLogger("desk.fills")
MAX_PAGES = 100           # 100 x 100 orders
D0 = Decimal(0)


@dataclass
class FillHistory:
    orders: dict[str, Order] = field(default_factory=dict)   # filled (incl. partially) closed orders
    transactions: list[dict[str, Any]] = field(default_factory=list)
    loaded: bool = False
    last_full: float | None = None
    error: str | None = None

    async def refresh(self, toss: TossClient, seq: int, now: float, full: bool) -> None:
        """full: whole history (first time); otherwise the last 3 KST days (merged by order id)."""
        today = datetime.fromtimestamp(now, UTC).astimezone(KST).date()
        start = None if full else today - timedelta(days=3)
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            page, cursor = await toss.closed_orders(seq, date_from=start, date_to=today if start else None,
                                                    cursor=cursor)
            for o in page:
                ex = o.execution
                if ex is not None and ex.filled_quantity and ex.filled_quantity > 0:
                    self.orders[o.order_id] = o
            if not cursor:
                break
        earliest = min((o.ordered_at.astimezone(KST).date() for o in self.orders.values() if o.ordered_at),
                       default=today)
        tx_from = earliest - timedelta(days=1) if full or not self.transactions else today - timedelta(days=7)
        txs: list[dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_PAGES):
            page, cursor = await toss.transactions(seq, date_from=tx_from, date_to=today, cursor=cursor)
            txs.extend(page)
            if not cursor:
                break
        if full or not self.transactions:
            self.transactions = txs
        else:
            seen = {_tx_key(t) for t in self.transactions}
            self.transactions += [t for t in txs if _tx_key(t) not in seen]
        self.loaded = True
        if full:
            self.last_full = now


def _tx_key(t: dict[str, Any]) -> tuple[Any, ...]:
    return (t.get("transactionType"), t.get("symbol"), t.get("quantity"), t.get("filledPrice"),
            t.get("settlementDate"), t.get("transactionTime"))


def _d(v: Any) -> Decimal | None:
    try:
        return None if v in (None, "") else Decimal(str(v))
    except ArithmeticError:
        return None


def match_fx(o: Order, txs: Iterable[dict[str, Any]]) -> Decimal | None:
    """Toss's applied exchange rate for this order's settlement, if a transaction matches."""
    ex = o.execution
    if ex is None:
        return None
    for t in txs:
        if (t.get("transactionType") == o.side and str(t.get("symbol") or "").upper() == o.symbol
                and str(t.get("settlementDate") or "") == str(ex.settlement_date or "")
                and _d(t.get("quantity")) == ex.filled_quantity and not t.get("isCancellation")):
            rate = _d(t.get("exchangeRate"))
            if rate and rate > 0:
                return rate
    return None


def to_trades(orders: Iterable[Order], txs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Toss fills -> trade records for app.pnl.compute_pnl (fees confirmed)."""
    out = []
    for o in orders:
        ex = o.execution
        if ex is None or not ex.filled_quantity or not ex.average_filled_price or o.currency not in ("USD", "KRW"):
            continue
        when = ex.filled_at or o.ordered_at
        if when is None:
            continue
        fee = (ex.commission or D0) + (ex.tax or D0)
        out.append({"id": o.order_id[:12], "order_id": o.order_id, "time": when, "symbol": o.symbol, "side": o.side.lower(),
                    "qty": ex.filled_quantity, "price": ex.average_filled_price, "currency": o.currency,
                    "fee": fee, "fee_currency": o.currency, "fx_rate": match_fx(o, txs)})
    return sorted(out, key=lambda r: r["time"])


def opening_positions(trades: list[dict[str, Any]], holdings: Iterable[HoldingItem]) -> tuple[list[dict[str, Any]], list[str]]:
    """Synthetic opening buys for shares the order history can't explain (est. at Toss's avg)."""
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t)
    held = {h.symbol: h for h in holdings}
    opens, notes = [], []
    for sym in sorted(set(by_sym) | set(held)):
        run, low = D0, D0
        short_trade: dict[str, Any] | None = None   # first trade that sells shares the history doesn't have
        for t in by_sym.get(sym, []):
            run += t["qty"] if t["side"] == "buy" else -t["qty"]
            if run < low:
                low = run
                short_trade = short_trade or t
        need = max(-low, (held[sym].quantity - run) if sym in held else D0)
        if need <= 0:
            continue
        h = held.get(sym)
        first = min((t["time"] for t in by_sym.get(sym, [])), default=datetime(2000, 1, 1, tzinfo=UTC))
        if h is not None and h.average_purchase_price:
            price, cur = h.average_purchase_price, h.currency or "USD"
            notes.append(f"{sym}: {need} shares predate the order history; counted at Toss's average {price} (est.)")
        elif short_trade is not None:
            # e.g. a fractional piece from outside normal orders, no longer held: cost unknown, so it is
            # counted at the price it was sold for (its P&L ~ 0) instead of stopping the whole P&L
            price, cur = short_trade["price"], short_trade["currency"]
            notes.append(f"{sym}: {need} shares are not in the order history (e.g. fractional or transferred); "
                         f"counted at the sell price {price} (est.)")
        else:
            notes.append(f"{sym}: {need} shares are not in the order history and have no price; P&L may be off")
            continue
        opens.append({"id": f"open-{sym}", "time": first - timedelta(seconds=1), "symbol": sym, "side": "buy",
                      "qty": need, "price": price, "currency": cur, "fee": D0, "fx_rate": None})  # no made-up fees
    return opens, notes


def closing_positions(rows: Iterable[Any], holdings: Iterable[HoldingItem], currencies: dict[str, str],
                      when: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """Toss holdings are the truth for what is owned. Shares the history shows as still held but Toss
    doesn't (their sale isn't in the history) are closed at their average cost: 0 P&L, no fee."""
    held = {h.symbol: h.quantity for h in holdings}
    closes, notes = [], []
    for row in rows:
        excess = row.qty - held.get(row.symbol, D0)
        if excess <= 0:
            continue
        cur = currencies.get(row.symbol, "USD")
        price = row.avg_krw.value if cur == "KRW" else row.avg_usd_computed.value
        if not price or price <= 0:
            continue
        closes.append({"id": f"close-{row.symbol}", "time": when, "symbol": row.symbol, "side": "sell",
                       "qty": excess, "price": price, "currency": cur, "fee": D0, "fx_rate": None})
        notes.append(f"{row.symbol}: {excess} shares appear bought in the history but aren't held now "
                     f"(their sale isn't in the history); closed at cost, 0 P&L (est.)")
    return closes, notes


def mismatches(result_symbols: Iterable[Any], holdings: Iterable[HoldingItem], tol: Decimal = Decimal("0.005")) -> list[str]:
    """Where our numbers and Toss's disagree (shown as warnings, never hidden)."""
    held = {h.symbol: h for h in holdings}
    out = []
    for row in result_symbols:
        h = held.get(row.symbol)
        toss_qty = h.quantity if h else D0
        if row.qty != toss_qty:
            out.append(f"{row.symbol}: quantity from fills {row.qty} vs Toss {toss_qty}")
        ours = row.avg_usd_computed.value
        if h and h.average_purchase_price and ours and h.currency == "USD":
            if abs(ours - h.average_purchase_price) > h.average_purchase_price * tol:
                out.append(f"{row.symbol}: average from fills {ours.quantize(Decimal('0.0001'))} vs Toss "
                           f"{h.average_purchase_price}; unrealized P&L uses Toss's")
    return out


def today_kst(now: float) -> date:
    return datetime.fromtimestamp(now, UTC).astimezone(KST).date()
