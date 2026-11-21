"""Order draft validation. Pure functions (no network): every rule is unit tested.

Rules come from CLAUDE.md "Order safety" and the Toss order docs (read 2026-11-17):
- US prices are USD; Toss silently TRUNCATES extra decimals (2 dp at >= $1, 4 dp below $1), so we
  REJECT such prices instead: the user's number is never changed.
- LIMIT needs a price, MARKET must not have one. CLS (at the close) only for LIMIT.
- Whole shares, except MARKET SELL (up to 6 dp). Amount (USD) orders: MARKET only. Both only from the
  regular open until 1 hour before the regular close.
- Conditional (reserved): SINGLE (LIMIT or MARKET), OCO (two SELLs, LIMIT, first trigger > current >
  second trigger), OTO (BUY then SELL, LIMIT); expireDate up to 1 year; whole shares (fractional
  conditional orders are UNVERIFIED, so off).
- Orders over KRW 100,000,000 need an explicit "high value" confirmation (Toss confirmHighValueOrder).
Codes are returned (not sentences); the screen shows them in English or Korean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Literal

HIGH_VALUE_KRW = Decimal("100000000")
MAX_FRACTION_DP = 6
D0 = Decimal(0)

Kind = Literal["BUY", "SELL", "COND_BUY", "COND_SELL", "OCO", "OTO"]
KINDS: tuple[str, ...] = ("BUY", "SELL", "COND_BUY", "COND_SELL", "OCO", "OTO")


@dataclass
class Issue:
    code: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Context:
    """Everything the rules need, gathered by the caller (all values may be missing)."""

    current_price: Decimal | None
    price_source: str | None
    session: str | None                  # day | pre | regular | after | None (closed)
    in_fraction_window: bool             # regular open .. 1 h before regular close
    held_qty: Decimal                    # shares held minus open sell orders (sellable)
    buying_power_usd: Decimal | None
    fx_mid: Decimal | None               # KRW per USD (Toss reference), for the KRW 100M check
    today_kst: date
    price_band_pct: Decimal
    fee_rate: Decimal
    fee_free_below_usd: Decimal
    allow_market_outside_regular: bool
    max_order_usd: Decimal | None = None
    max_day_usd: Decimal | None = None
    sent_today_usd: Decimal = D0
    max_orders_per_day: int | None = None
    sent_today_count: int = 0


@dataclass
class Result:
    errors: list[Issue]
    warnings: list[Issue]
    normalized: dict[str, Any]           # exact user values (Decimal) + computed shares
    estimate: dict[str, Any]

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_number(raw: Any) -> Decimal | None:
    """The user's typed number as an exact Decimal (strings only; never floats). None if not a number."""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s:
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def decimals(d: Decimal) -> int:
    exp = d.normalize().as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def price_dp_ok(p: Decimal) -> bool:
    """Toss US tick: 2 dp at $1 and above, 4 dp below $1."""
    return decimals(p) <= (2 if p >= 1 else 4)


def _price(raw: Any, name: str, errors: list[Issue]) -> Decimal | None:
    p = parse_number(raw)
    if p is None:
        errors.append(Issue("price_required", {"field": name}))
        return None
    if p <= 0:
        errors.append(Issue("price_positive", {"field": name}))
        return None
    if not price_dp_ok(p):
        errors.append(Issue("price_decimals", {"field": name, "value": str(raw).strip(),
                                               "max_dp": 2 if p >= 1 else 4}))
    return p


def _band(p: Decimal, name: str, ctx: Context, errors: list[Issue]) -> None:
    if ctx.current_price is None or ctx.current_price <= 0:
        errors.append(Issue("no_current_price", {"field": name}))
        return
    diff_pct = abs(p - ctx.current_price) / ctx.current_price * 100
    if diff_pct > ctx.price_band_pct:
        errors.append(Issue("price_out_of_band", {"field": name, "value": str(p), "current": str(ctx.current_price),
                                                  "diff_pct": str(diff_pct.quantize(Decimal("0.01"))),
                                                  "band_pct": str(ctx.price_band_pct)}))


def validate(inp: dict[str, Any], ctx: Context) -> Result:
    """Check one draft input. `inp` holds the user's raw values (strings)."""
    errors: list[Issue] = []
    warnings: list[Issue] = []
    norm: dict[str, Any] = {}

    kind = inp.get("kind")
    if kind not in KINDS:
        return Result([Issue("kind_invalid")], [], {}, {})
    norm["kind"] = kind
    symbol = str(inp.get("symbol") or "").strip().upper()
    if not symbol:
        errors.append(Issue("symbol_required"))
    norm["symbol"] = symbol
    currency = str(inp.get("currency") or "").upper()
    if currency != "USD":
        errors.append(Issue("currency_usd_required", {"given": currency or "none"}))
    order_type = str(inp.get("order_type") or "").upper()
    if order_type not in ("LIMIT", "MARKET"):
        errors.append(Issue("order_type_required"))
    norm["order_type"] = order_type
    conditional = kind in ("COND_BUY", "COND_SELL", "OCO", "OTO")
    norm["conditional"] = conditional
    side = "BUY" if kind in ("BUY", "COND_BUY", "OTO") else "SELL"
    norm["side"] = side

    # ── size: shares | percent | amount ─────────────────────────────────
    size_mode = inp.get("size_mode") or "shares"
    norm["size_mode"] = size_mode
    qty: Decimal | None = None
    amount: Decimal | None = None
    ref_price = None
    if order_type == "LIMIT" and not conditional:
        ref_price = parse_number(inp.get("price"))
    if ref_price is None or ref_price <= 0:
        ref_price = ctx.current_price
    if size_mode == "shares":
        qty = parse_number(inp.get("quantity"))
        if qty is None:
            errors.append(Issue("quantity_required"))
        elif qty <= 0:
            errors.append(Issue("quantity_positive"))
            qty = None
    elif size_mode == "percent":
        pct = parse_number(inp.get("percent"))
        if pct is None or pct <= 0 or pct > 100:
            errors.append(Issue("percent_range"))
        elif side == "BUY":
            if ctx.buying_power_usd is None:
                errors.append(Issue("no_buying_power"))
            elif ref_price is None or ref_price <= 0:
                errors.append(Issue("no_current_price", {"field": "percent"}))
            else:
                qty = (ctx.buying_power_usd * pct / 100 / ref_price).to_integral_value(rounding=ROUND_FLOOR)
                norm["percent"] = pct
                norm["computed_from"] = {"buying_power_usd": str(ctx.buying_power_usd), "price": str(ref_price)}
        else:
            qty = (ctx.held_qty * pct / 100).to_integral_value(rounding=ROUND_FLOOR)
            norm["percent"] = pct
            norm["computed_from"] = {"held_qty": str(ctx.held_qty)}
        if qty is not None and qty <= 0 and not errors:
            errors.append(Issue("percent_zero_shares"))
    elif size_mode == "amount":
        amount = parse_number(inp.get("amount"))
        if conditional or order_type != "MARKET":
            errors.append(Issue("amount_market_only"))
        if amount is None or amount <= 0:
            errors.append(Issue("amount_required"))
        elif decimals(amount) > 2:
            errors.append(Issue("amount_decimals"))
        if not ctx.in_fraction_window:
            errors.append(Issue("amount_outside_window"))
    else:
        errors.append(Issue("size_mode_invalid"))
    norm["quantity"] = qty
    norm["amount"] = amount

    if qty is not None and qty > 0:
        frac = qty != qty.to_integral_value()
        if frac:
            if conditional:
                errors.append(Issue("fraction_conditional_off"))
            elif not (order_type == "MARKET" and side == "SELL"):
                errors.append(Issue("fraction_market_sell_only"))
            elif decimals(qty) > MAX_FRACTION_DP:
                errors.append(Issue("fraction_too_many_dp"))
            elif not ctx.in_fraction_window:
                errors.append(Issue("fraction_outside_window"))

    # ── normal order fields ─────────────────────────────────────────────
    price: Decimal | None = None
    if not conditional:
        tif = str(inp.get("time_in_force") or "").upper()
        if tif not in ("DAY", "CLS"):
            errors.append(Issue("tif_required"))
        elif tif == "CLS" and order_type != "LIMIT":
            errors.append(Issue("cls_limit_only"))
        norm["time_in_force"] = tif
        if order_type == "LIMIT":
            price = _price(inp.get("price"), "price", errors)
            if price is not None and price > 0:
                _band(price, "price", ctx, errors)
        elif order_type == "MARKET" and parse_number(inp.get("price")) is not None:
            errors.append(Issue("market_no_price"))
        norm["price"] = price
        if order_type == "MARKET":
            if ctx.session != "regular":
                if ctx.allow_market_outside_regular:
                    warnings.append(Issue("market_thin_book", {"session": ctx.session or "closed"}))
                else:
                    errors.append(Issue("market_regular_only"))
        if ctx.session is None:
            warnings.append(Issue("market_closed_now"))

    # ── conditional (reserved) fields ───────────────────────────────────
    else:
        legs: list[dict[str, Any]] = []
        n_legs = 2 if kind in ("OCO", "OTO") else 1
        if kind in ("OCO", "OTO") and order_type != "LIMIT":
            errors.append(Issue("oco_oto_limit_only"))
        for i in range(n_legs):
            raw = (inp.get("legs") or [{}, {}])[i] if isinstance(inp.get("legs"), list) and len(inp["legs"]) > i else {}
            leg_side = side if kind in ("COND_BUY", "COND_SELL") else ("SELL" if kind == "OCO" else ("BUY" if i == 0 else "SELL"))
            trig = _price(raw.get("trigger_price"), f"leg{i + 1}_trigger", errors)
            op = None
            if order_type == "LIMIT":
                # no +-band check here: reserved orders are meant to trigger away from today's price
                op = _price(raw.get("order_price"), f"leg{i + 1}_order_price", errors)
            elif parse_number(raw.get("order_price")) is not None:
                errors.append(Issue("market_no_price"))
            legs.append({"side": leg_side, "trigger_price": trig, "order_price": op})
        if kind == "OCO" and all(l["trigger_price"] for l in legs):
            cur = ctx.current_price
            if cur is None:
                errors.append(Issue("no_current_price", {"field": "oco"}))
            elif not (legs[0]["trigger_price"] > cur > legs[1]["trigger_price"]):
                errors.append(Issue("oco_order", {"current": str(cur)}))
        if kind == "OTO" and all(l["order_price"] for l in legs) and legs[1]["order_price"] <= legs[0]["order_price"]:
            warnings.append(Issue("oto_sell_below_buy"))
        norm["legs"] = legs
        exp_raw = str(inp.get("expire_date") or "").strip()
        try:
            exp = date.fromisoformat(exp_raw)
        except ValueError:
            exp = None
            errors.append(Issue("expire_date_required"))
        if exp is not None:
            if exp < ctx.today_kst:
                errors.append(Issue("expire_date_past"))
            elif exp > ctx.today_kst + timedelta(days=365):
                errors.append(Issue("expire_date_too_far"))
        norm["expire_date"] = exp
        price = next((l["order_price"] for l in legs if l["order_price"]), None)

    # ── holdings, buying power, value, limits ───────────────────────────
    est_price = price if price is not None else ctx.current_price
    notional: Decimal | None = None
    if amount is not None:
        notional = amount
    elif qty is not None and est_price is not None:
        notional = qty * est_price
    if side == "SELL" and qty is not None and qty > ctx.held_qty:
        errors.append(Issue("not_enough_shares", {"held": str(ctx.held_qty), "qty": str(qty)}))
    if side == "BUY" and notional is not None and ctx.buying_power_usd is not None and notional > ctx.buying_power_usd:
        (errors if order_type == "LIMIT" else warnings).append(
            Issue("not_enough_buying_power", {"needed": str(notional.quantize(Decimal("0.01"))),
                                              "available": str(ctx.buying_power_usd)}))
    if notional is not None and ctx.fx_mid is not None and notional * ctx.fx_mid >= HIGH_VALUE_KRW:
        if inp.get("confirm_high_value") is not True:
            errors.append(Issue("high_value_confirm"))
        norm["confirm_high_value"] = inp.get("confirm_high_value") is True
    if ctx.max_order_usd is not None and notional is not None and notional > ctx.max_order_usd:
        errors.append(Issue("over_max_order", {"max": str(ctx.max_order_usd)}))
    if ctx.max_day_usd is not None and notional is not None and ctx.sent_today_usd + notional > ctx.max_day_usd:
        errors.append(Issue("over_max_day", {"max": str(ctx.max_day_usd)}))
    if ctx.max_orders_per_day is not None and ctx.sent_today_count >= ctx.max_orders_per_day:
        errors.append(Issue("over_max_orders", {"max": ctx.max_orders_per_day}))

    fee = None
    if notional is not None:
        fee = D0 if notional < ctx.fee_free_below_usd else (notional * ctx.fee_rate).quantize(Decimal("0.01"))
    estimate = {
        "price_used": None if est_price is None else str(est_price),
        "price_is_estimate": price is None,
        "notional_usd": None if notional is None else str(notional.quantize(Decimal("0.01"))),
        "fee_usd_est": None if fee is None else str(fee),
        "total_usd_est": None if notional is None or fee is None else str(
            (notional + fee if side == "BUY" else notional - fee).quantize(Decimal("0.01"))),
        "krw_ref": None if notional is None or ctx.fx_mid is None else str((notional * ctx.fx_mid).quantize(Decimal("1"))),
        "fx_mid": None if ctx.fx_mid is None else str(ctx.fx_mid),
    }
    return Result(errors, warnings, norm, estimate)
