"""Live context for order validation: current price, session, sellable shares, buying power,
today's sent orders (optional limits). Shared by draft creation and the final check before sending."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.market import session_at
from app.orders.validate import Context
from app.timeutil import KST, to_epoch

PRICE_MAX_AGE_S = 300


async def current_price(svc: Any, symbol: str) -> tuple[Decimal | None, str | None]:
    """Latest streamed trade (< 5 min old), else the Toss quote, else a fresh Toss quote."""
    now = svc.clock()
    market = getattr(svc, "market", None)
    if market is not None:
        market.interest(symbol)
        lt = market.last_trade(symbol)
        if lt is not None and now - to_epoch(lt.timestamp) < PRICE_MAX_AGE_S:
            return lt.price, "last trade (Toss stream)"
    q = svc._quote(symbol)
    if q is not None and q.timestamp is not None and now - to_epoch(q.timestamp) < PRICE_MAX_AGE_S:
        return q.last_price, "Toss quote"
    if svc.toss is not None:
        try:
            prices = await svc.toss.prices([symbol])
            if prices:
                return prices[0].last_price, "Toss quote (just fetched)"
        except Exception:  # noqa: BLE001 - validation then reports "no current price"
            pass
    return None, None


async def build_context(svc: Any, symbol: str, band: Decimal) -> Context:
    now = svc.clock()
    price, src = await current_price(svc, symbol)
    sessions = svc.sessions()
    sess = session_at(now, sessions)
    reg = next((s for s in sessions if s.name == "regular" and s.start <= now < s.end), None)
    in_window = bool(reg and now <= reg.end - 3600)
    held = Decimal(0)
    bp = None
    acct = getattr(svc, "account", None)
    if acct is not None:
        if acct.state.updated is None or now - acct.state.updated > 30:
            await acct.refresh_now()
        held = acct.held_quantity(symbol)
        for o in acct.state.open_orders:  # shares already promised to open sell orders
            if o.symbol == symbol and o.side == "SELL" and o.quantity is not None:
                filled = o.execution.filled_quantity if o.execution and o.execution.filled_quantity else Decimal(0)
                held -= max(Decimal(0), o.quantity - filled)
        bp = acct.state.buying_power_usd
    mid, _t, _bp = svc.fx_rate()
    oc, pc = svc.config.orders, svc.config.pnl
    sender = getattr(svc, "sender", None)
    sent_count, sent_usd = sender.log.sent_today(now) if sender is not None else (0, Decimal(0))
    return Context(current_price=price, price_source=src, session=sess, in_fraction_window=in_window,
                   held_qty=max(Decimal(0), held), buying_power_usd=bp, fx_mid=mid,
                   today_kst=datetime.fromtimestamp(now, UTC).astimezone(KST).date(), price_band_pct=band,
                   fee_rate=pc.fee_rate, fee_free_below_usd=pc.fee_free_below_usd,
                   allow_market_outside_regular=oc.allow_market_outside_regular,
                   max_order_usd=oc.max_order_usd, max_day_usd=oc.max_day_usd,
                   max_orders_per_day=oc.max_orders_per_day, sent_today_count=sent_count, sent_today_usd=sent_usd)
