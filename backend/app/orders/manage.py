"""Modify / cancel drafts for open orders (Toss docs, read 2026-11-17).

Normal orders (US): modify = PRICE ONLY (orderType LIMIT with price, or MARKET without); quantity can't
change ("us-modify-quantity-not-supported"); Toss returns a NEW orderId. Cancel = POST .../cancel.
Conditional orders: modify = full reset (type, quantity, orderType, expireDate, first, second), only
while WATCHING or PAUSED; Toss cancels and creates a new one (new id). Cancel = DELETE.
Neither modify nor cancel has an idempotency key, so they are never retried automatically.
Targets: Toss open orders / open conditional orders, or (DRY RUN) simulated orders.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.orders.validate import Context, Issue, Result, _band, _price, parse_number, validate

MODIFIABLE = ("PENDING", "PARTIAL_FILLED")
COND_EDITABLE = ("WATCHING", "PAUSED")
TARGET_TYPES = ("order", "conditional", "simulated")


def find_target(svc: Any, ttype: str, tid: str) -> dict[str, Any] | None:
    """Snapshot of the open order the user picked, from the latest account data (or simulated list)."""
    if ttype == "simulated":
        sender = getattr(svc, "sender", None)
        for s in (sender.simulated if sender else []):
            if s["id"] == tid:
                v = s["values"]
                return {"type": "simulated", "id": tid, "symbol": v.get("symbol"), "side": v.get("side"),
                        "status": s["status"], "order_type": v.get("order_type"), "price": v.get("price"),
                        "quantity": v.get("quantity"), "conditional": bool(v.get("conditional")),
                        "kind": v.get("kind"), "legs": v.get("legs") or [], "expire_date": v.get("expire_date")}
        return None
    acct = getattr(svc, "account", None)
    if acct is None:
        return None
    if ttype == "order":
        for o in acct.state.open_orders:
            if o.order_id == tid:
                filled = o.execution.filled_quantity if o.execution and o.execution.filled_quantity else Decimal(0)
                return {"type": "order", "id": tid, "symbol": o.symbol, "side": o.side, "status": o.status,
                        "order_type": o.order_type, "price": o.price, "quantity": o.quantity, "filled": filled,
                        "conditional": False}
    if ttype == "conditional":
        for c in acct.state.open_conditional:
            if c.conditional_order_id == tid:
                legs = [{"side": l.order_side, "trigger_price": l.trigger_price, "order_price": l.order_price}
                        for l in (c.first, c.second) if l is not None]
                kind = c.type if c.type in ("OCO", "OTO") else ("COND_BUY" if c.first.order_side == "BUY" else "COND_SELL")
                return {"type": "conditional", "id": tid, "symbol": c.symbol, "side": c.first.order_side,
                        "status": c.status, "order_type": c.order_type, "quantity": c.quantity, "conditional": True,
                        "kind": kind, "legs": legs, "expire_date": c.expire_date}
    return None


def validate_action(inp: dict[str, Any], target: dict[str, Any] | None, ctx: Context) -> Result:
    """inp: {action: modify|cancel, target_type, target_id, + new values for modify}."""
    errors: list[Issue] = []
    warnings: list[Issue] = []
    action = inp.get("action")
    norm: dict[str, Any] = {"action": action, "target_type": inp.get("target_type"), "target_id": inp.get("target_id")}
    if action not in ("modify", "cancel"):
        return Result([Issue("action_invalid")], [], norm, {})
    if target is None:
        return Result([Issue("target_not_found")], [], norm, {})
    # the id and type come from the order that was actually found, never from free input
    norm.update(symbol=target["symbol"], side=target["side"], target=_plain(target),
                target_type=target["type"], target_id=target["id"])
    open_ok = target["status"] in (COND_EDITABLE if target["conditional"] else MODIFIABLE) \
        or (target["type"] == "simulated" and target["status"] == "OPEN")
    if not open_ok:
        errors.append(Issue("target_not_open", {"status": target["status"]}))
    if action == "cancel":
        return Result(errors, warnings, norm, {})

    if not target["conditional"]:
        if parse_number(inp.get("quantity")) is not None:
            errors.append(Issue("modify_price_only"))             # US: quantity can't be changed
        ot = str(inp.get("order_type") or "").upper()
        if ot not in ("LIMIT", "MARKET"):
            errors.append(Issue("order_type_required"))
        norm["order_type"] = ot
        price = None
        if ot == "LIMIT":
            price = _price(inp.get("price"), "price", errors)
            if price is not None and price > 0:
                _band(price, "price", ctx, errors)
                if target.get("price") is not None and price == target["price"] and target.get("order_type") == "LIMIT":
                    errors.append(Issue("nothing_changed"))
        elif parse_number(inp.get("price")) is not None:
            errors.append(Issue("market_no_price"))
        if ot == "MARKET" and ctx.session != "regular":
            (warnings if ctx.allow_market_outside_regular else errors).append(
                Issue("market_thin_book" if ctx.allow_market_outside_regular else "market_regular_only",
                      {"session": ctx.session or "closed"}))
        norm["price"] = price
        return Result(errors, warnings, norm, {})

    # conditional: re-validate the whole new definition with the normal rules (same kind, same quantity)
    full = {"kind": target["kind"], "symbol": target["symbol"], "currency": "USD",
            "order_type": inp.get("order_type") or target["order_type"], "size_mode": "shares",
            "quantity": str(target["quantity"]), "legs": inp.get("legs"), "expire_date": inp.get("expire_date"),
            "confirm_high_value": inp.get("confirm_high_value")}
    # the shares being reset are the ones this order already holds back: don't count them twice
    held_ctx = Context(**{**ctx.__dict__, "held_qty": ctx.held_qty + (target["quantity"] if target["side"] == "SELL" else 0)})
    res = validate(full, held_ctx)
    errors += res.errors
    warnings += res.warnings
    n = res.normalized
    norm.update(kind=target["kind"], order_type=n.get("order_type"), quantity=n.get("quantity"), legs=n.get("legs"),
                expire_date=n.get("expire_date"), conditional=True,
                confirm_high_value=bool(inp.get("confirm_high_value") is True))
    old = [(l["trigger_price"], l["order_price"]) for l in target["legs"]]
    new = [(l["trigger_price"], l["order_price"]) for l in n.get("legs") or []]
    if not errors and old == new and str(n.get("expire_date")) == str(target.get("expire_date")) \
            and n.get("order_type") == target["order_type"]:
        errors.append(Issue("nothing_changed"))
    return Result(errors, warnings, norm, res.estimate)


def _plain(t: dict[str, Any]) -> dict[str, Any]:
    return {k: (format(v, "f") if isinstance(v, Decimal) else v) for k, v in t.items() if k != "legs"} | {
        "legs": [{k: (format(v, "f") if isinstance(v, Decimal) else v) for k, v in l.items()} for l in t.get("legs", [])]}


def summarize_action(n: dict[str, Any]) -> tuple[str, str]:
    t = n.get("target") or {}
    side_en = "buy" if t.get("side") == "BUY" else "sell"
    side_ko = "매수" if t.get("side") == "BUY" else "매도"
    what_en = f"{t.get('symbol')} {side_en} order ({t.get('quantity')} shares"
    what_ko = f"{t.get('symbol')} {side_ko} 주문 ({t.get('quantity')}주"
    if t.get("conditional"):
        what_en, what_ko = f"reserved {what_en}", f"예약 {what_ko}"
    else:
        what_en += f", {t.get('price')} USD" if t.get("price") else ", market"
        what_ko += f", {t.get('price')} USD" if t.get("price") else ", 시장가"
    what_en += ")"
    what_ko += ")"
    if n.get("action") == "cancel":
        return f"Cancel the {what_en}", f"{what_ko} 취소"
    if not t.get("conditional"):
        new_en = f"{n.get('price')} USD limit" if n.get("order_type") == "LIMIT" else "market"
        new_ko = f"{n.get('price')} USD 지정가" if n.get("order_type") == "LIMIT" else "시장가"
        return f"Change the {what_en} to {new_en}", f"{what_ko}을 {new_ko}로 정정"
    legs = n.get("legs") or []
    parts = "; ".join(f"trigger {l['trigger_price']} → {l['order_price'] or 'market'}" for l in legs)
    parts_ko = "; ".join(f"감시가 {l['trigger_price']} → {l['order_price'] or '시장가'}" for l in legs)
    return (f"Reset the {what_en}: {parts}; expires {n.get('expire_date')}",
            f"{what_ko} 재설정: {parts_ko}, 만료 {n.get('expire_date')}")


def _s(d: Decimal) -> str:
    return format(d, "f")


def action_request(n: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
    """(method, path, body) for Toss. Ids are opaque; the client URL-checks them."""
    from urllib.parse import quote
    tid = quote(str(n["target_id"]), safe="-_.~")
    if n["target_type"] == "order" or (n["target_type"] == "simulated" and not n.get("conditional")
                                       and not (n.get("target") or {}).get("conditional")):
        if n["action"] == "cancel":
            return "POST", f"/api/v1/orders/{tid}/cancel", None
        body: dict[str, Any] = {"orderType": n["order_type"], "confirmHighValueOrder": False}
        if n["order_type"] == "LIMIT":
            body["price"] = _s(n["price"])
        return "POST", f"/api/v1/orders/{tid}/modify", body
    if n["action"] == "cancel":
        return "DELETE", f"/api/v1/conditional-orders/{tid}", None
    legs = n["legs"]

    def leg(l: dict[str, Any]) -> dict[str, Any]:
        c: dict[str, Any] = {"orderSide": l["side"], "triggerPrice": _s(l["trigger_price"])}
        if n["order_type"] == "LIMIT":
            c["orderPrice"] = _s(l["order_price"])
        return c

    ctype = {"COND_BUY": "SINGLE", "COND_SELL": "SINGLE"}.get(n["kind"], n["kind"])
    return "POST", f"/api/v1/conditional-orders/{tid}/modify", {
        "type": ctype, "quantity": _s(n["quantity"]), "orderType": n["order_type"],
        "expireDate": n["expire_date"].isoformat(), "first": leg(legs[0]),
        "second": leg(legs[1]) if len(legs) > 1 else None,
        "confirmHighValueOrder": bool(n.get("confirm_high_value"))}
