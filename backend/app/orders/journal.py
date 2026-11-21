"""Trade journal (phase 4 part 7).

Scope (user choice 2026-11-17): only trades made WITH THIS PROGRAM. A "trade" is one round trip of a
stock: from 0 shares to owned and back to 0 (still open if not back to 0 yet). A round trip is in the
journal when at least one of its fills came from an order this program sent to Toss (real sends only;
DRY RUN sends are simulated and never fill). Inside such a round trip every fill counts, including buys
made before / outside the program for shares the program then sells. Round trips with no program order
are left out.

Numbers come from app.pnl.compute_pnl run on each round trip (same rules as the P&L panel: average
price, confirmed Toss fees, estimated FX cost). AI credits = Claude usage logged while the trade was
open (by time: the usage log has no ticker), and the totals row counts each call once.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.chat.usage import Totals
from app.pnl import MISSING, PnlError, PnlResult, Tagged, parse_trade

QTY_EPS = Decimal("0.000001")
FIELDS = ("realized", "unrealized", "fees", "fx_cost")


def program_order_ids(log: Iterable[dict[str, Any]], conditionals: Iterable[Any]) -> set[str]:
    """Toss order ids sent by this program: accepted new orders and modifies (logs/trades.jsonl), plus the
    orders Toss created when a program conditional order triggered."""
    orders: set[str] = set()
    conds: set[str] = set()
    for r in log:
        if r.get("event") != "result" or r.get("dry_run") or r.get("status") != "accepted":
            continue
        if r.get("action") == "cancel":
            continue
        if r.get("order_id"):
            oid = str(r["order_id"])
            (conds if "conditional-orders" in str(r.get("path", "")) else orders).add(oid)
        if r.get("conditional_order_id"):
            conds.add(str(r["conditional_order_id"]))
    for c in conditionals:
        if c.conditional_order_id in conds:
            for leg in (c.first, c.second):
                if leg is not None and leg.triggered_order_id:
                    orders.add(leg.triggered_order_id)
    return orders


def round_trips(trades: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split each stock's trades into round trips (0 -> owned -> 0). The last one may still be open."""
    by_sym: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
    for rec in trades:
        t = parse_trade(rec)
        by_sym.setdefault(t.symbol, []).append((t, rec))
    trips: list[list[dict[str, Any]]] = []
    for items in by_sym.values():
        items.sort(key=lambda x: x[0].time)
        cur: list[dict[str, Any]] = []
        qty = Decimal(0)
        for t, rec in items:
            cur.append(rec)
            qty += t.qty if t.side == "buy" else -t.qty
            if abs(qty) <= QTY_EPS:
                trips.append(cur)
                cur, qty = [], Decimal(0)
        if cur:
            trips.append(cur)
    return trips


def _usage_between(usage: Iterable[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    out = []
    for rec in usage:
        try:
            ts = datetime.fromisoformat(str(rec["ts"]))
        except (KeyError, ValueError):
            continue
        if ts.tzinfo is not None and start <= ts <= end:
            out.append(rec)
    return out


def _ai(recs: list[dict[str, Any]], mid: Decimal | None) -> tuple[Tagged, Tagged]:
    tot = Totals()
    for r in recs:
        tot.add(r)
    krw, est = tot.krw_total(mid)
    return Tagged(tot.usd, "confirmed"), (MISSING if krw is None else Tagged(krw, "est." if est else "confirmed"))


def _avg(recs: list[Any]) -> tuple[Decimal, Decimal | None]:
    qty = sum((t.qty for t in recs), Decimal(0))
    if qty <= 0:
        return Decimal(0), None
    return qty, sum((t.qty * t.price for t in recs), Decimal(0)) / qty


def build_journal(trades: list[dict[str, Any]], program_ids: set[str], usage: list[dict[str, Any]],
                  engine: Callable[[list[dict[str, Any]]], PnlResult], now: datetime,
                  mid: Decimal | None) -> dict[str, Any]:
    """engine: compute_pnl on one round trip (period "all", no AI credits)."""
    rows: list[dict[str, Any]] = []
    windows: list[tuple[datetime, datetime]] = []
    notes: list[str] = []
    for trip in round_trips(trades):
        prog = [r for r in trip if str(r.get("order_id") or "") in program_ids]
        if not prog:
            continue
        parsed = [parse_trade(r) for r in trip]
        sym = parsed[0].symbol
        try:
            res = engine(trip)
        except PnlError as exc:
            notes.append(f"{sym}: trade skipped ({exc})")
            continue
        real = [t for r, t in zip(trip, parsed) if not str(r.get("id", "")).startswith(("open-", "close-"))]
        before = str(trip[0].get("id", "")).startswith("open-")
        held = sum((t.qty if t.side == "buy" else -t.qty for t in parsed), Decimal(0))
        closed = abs(held) <= QTY_EPS
        entry = parsed[0].time if not before else None
        exit_ = parsed[-1].time if closed else None
        start = entry or min(t.time for t in real)
        end = exit_ or now
        windows.append((start, end))
        ai_usd, ai_krw = _ai(_usage_between(usage, start, end), mid)
        buy_qty, avg_in = _avg([t for t in parsed if t.side == "buy"])
        sell_qty, avg_out = _avg([t for t in parsed if t.side == "sell"])
        tot = res.total
        row: dict[str, Any] = {
            "symbol": sym, "currency": parsed[0].currency, "status": "closed" if closed else "open",
            "entry_time": entry, "entry_before_records": before, "exit_time": exit_,
            "exit_not_in_records": str(trip[-1].get("id", "")).startswith("close-"),
            "holding_seconds": int((end - start).total_seconds()),
            "buy_qty": buy_qty, "avg_entry": avg_in, "sell_qty": sell_qty, "avg_exit": avg_out,
            "held_qty": held if not closed else Decimal(0),
            "fills": len(real), "program_fills": len(prog),
            "ai_credits_usd": ai_usd, "ai_credits_krw": ai_krw,
            "net_usd": tot.net_usd - ai_usd, "net_krw": tot.net_krw - ai_krw,
        }
        for f in FIELDS:
            row[f"{f}_usd"], row[f"{f}_krw"] = getattr(tot, f"{f}_usd"), getattr(tot, f"{f}_krw")
        rows.append(row)
    rows.sort(key=lambda r: r["exit_time"] or now, reverse=True)

    # totals: money adds up per trade; AI credits once over the union of the trade windows
    total: dict[str, Any] = {}
    for f in (*FIELDS, "net"):
        for c in ("usd", "krw"):
            acc = Tagged(Decimal(0), "confirmed")
            for r in rows:
                acc = acc + r[f"{f}_{c}"]
            total[f"{f}_{c}"] = acc
    union = [rec for rec in usage if any(rec in _usage_between([rec], s, e) for s, e in windows)]
    ai_usd, ai_krw = _ai(union, mid)
    total["ai_credits_usd"], total["ai_credits_krw"] = ai_usd, ai_krw
    per_trip_ai_usd = sum((r["ai_credits_usd"].value or Decimal(0) for r in rows), Decimal(0))
    total["net_usd"] = total["net_usd"] + Tagged(per_trip_ai_usd, "confirmed") - ai_usd
    per_trip_ai_krw = Tagged(Decimal(0), "confirmed")
    for r in rows:
        per_trip_ai_krw = per_trip_ai_krw + r["ai_credits_krw"]
    total["net_krw"] = total["net_krw"] + per_trip_ai_krw - ai_krw
    total["trades"] = len(rows)
    total["closed"] = sum(1 for r in rows if r["status"] == "closed")
    return {"trips": rows, "total": total, "notes": notes}
