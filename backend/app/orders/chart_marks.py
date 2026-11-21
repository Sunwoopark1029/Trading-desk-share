"""Orders and fills on the chart (phase 4): read-only data, shared overlay format for lines.

- Open Toss orders (limit price), open conditional orders (each trigger price) and, in DRY RUN, the
  simulated open orders become horizontal lines (group "orders", dashed).
- Real fills from Toss (closed orders with an execution) become buy / sell markers.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.timeutil import KST

BUY_COLOR = "#26a69a"
SELL_COLOR = "#ef5350"
MAX_FILLS = 500


def _q(d: Any) -> str:
    return "?" if d is None else format(Decimal(str(d)).normalize(), "f")


def _line(oid: str, symbol: str | None, price: Any, side: str, label: str, note: str, now_iso: str) -> dict[str, Any]:
    return {"id": oid, "type": "hline", "points": [{"time": now_iso, "price": format(Decimal(str(price)), "f")}],
            "label": label, "color": BUY_COLOR if side == "BUY" else SELL_COLOR, "source": "user",
            "visible": True, "note": note, "symbol": symbol, "derived": True, "dashed": True, "group": "orders"}


def order_lines(svc: Any, now_iso: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    acct = getattr(svc, "account", None)
    if acct is not None:
        for o in acct.state.open_orders:
            if o.price is not None:
                side = o.side.lower()
                lines.append(_line(f"ord-{o.order_id[:10]}", o.symbol, o.price, o.side,
                                   f"Open {side} {_q(o.quantity)}", f"Toss open order {o.order_id}", now_iso))
        for c in acct.state.open_conditional:
            for i, leg in enumerate((c.first, c.second)):
                if leg is not None and leg.trigger_price is not None:
                    lines.append(_line(f"cond-{c.conditional_order_id[:10]}-{i + 1}", c.symbol, leg.trigger_price,
                                       leg.order_side, f"Reserved {leg.order_side.lower()} {_q(c.quantity)} trigger",
                                       f"Toss conditional order {c.conditional_order_id} ({c.type})", now_iso))
    sender = getattr(svc, "sender", None)
    if sender is not None and sender.dry_run:
        for s in sender.simulated:
            if s["status"] != "OPEN":
                continue
            v = s["values"]
            side = str(v.get("side") or "")
            prices: list[tuple[str, Any]] = []
            if v.get("price") is not None and not v.get("conditional"):
                prices.append(("", v["price"]))
            for i, leg in enumerate(v.get("legs") or []):
                if leg.get("trigger_price") is not None:
                    prices.append((f" trigger {i + 1}", leg["trigger_price"]))
            for tag, p in prices:
                lines.append(_line(f"sim-{s['id'][-8:]}{tag.replace(' ', '-')}", v.get("symbol"), p, side,
                                   f"DRY RUN {side.lower()} {_q(v.get('quantity'))}{tag}", s.get("summary_en", ""),
                                   now_iso))
    return lines


def fill_marks(svc: Any) -> list[dict[str, Any]]:
    """Real Toss fills (newest MAX_FILLS), KST times. DRY RUN never fills, so nothing simulated here."""
    acct = getattr(svc, "account", None)
    if acct is None:
        return []
    out = []
    for o in acct.fills.orders.values():
        ex = o.execution
        if ex is None or not ex.filled_quantity or not ex.average_filled_price:
            continue
        when = ex.filled_at or o.ordered_at
        if when is None:
            continue
        out.append({"symbol": o.symbol, "time": when.astimezone(KST).isoformat(), "side": o.side,
                    "qty": ex.filled_quantity, "price": ex.average_filled_price, "currency": o.currency})
    out.sort(key=lambda f: f["time"])
    return out[-MAX_FILLS:]
