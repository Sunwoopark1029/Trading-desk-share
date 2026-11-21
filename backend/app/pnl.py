"""P&L from hand-entered trade records (average-cost method).

Pure module: no I/O, no settings. Callers pass trades, live prices, FX and config.
All math uses Decimal; nothing is rounded here (see ``format_money`` for display).

Status tags on every value:
- ``confirmed``: from entered fill data (price, qty, entered fee, entered fx_rate)
- ``typed``: depends on a typed-in average price (avg_override)
- ``est.``: estimate (fee rate, FX spread, reference/current FX conversion)
- ``not set``: an estimate input is missing in config (value shown as 0)
- ``missing``: cannot be calculated (value is None), e.g. no FX rate for that time
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

KST = timezone(timedelta(hours=9))  # Korea has no DST

Status = Literal["confirmed", "typed", "est.", "not set", "missing"]
Period = Literal["today", "all"]

# Worse status wins when values are combined.
_RANK: dict[str, int] = {"confirmed": 0, "typed": 1, "est.": 2, "not set": 3, "missing": 4}
_ZERO = Decimal(0)
_BP = Decimal(10000)


class PnlError(ValueError):
    """Invalid trade data (oversell, bad field). Caller shows the message."""


@dataclass(frozen=True)
class Tagged:
    """A value with its status. ``value`` is None only when status is ``missing``."""

    value: Decimal | None
    status: Status

    def __add__(self, other: Tagged) -> Tagged:
        return _combine(self, other, lambda a, b: a + b)

    def __sub__(self, other: Tagged) -> Tagged:
        return _combine(self, other, lambda a, b: a - b)

    def __mul__(self, other: Tagged) -> Tagged:
        return _combine(self, other, lambda a, b: a * b)

    def __truediv__(self, other: Tagged) -> Tagged:
        return _combine(self, other, lambda a, b: a / b)


def _worst(*statuses: Status) -> Status:
    return max(statuses, key=lambda s: _RANK[s])


def _combine(a: Tagged, b: Tagged, op: Callable[[Decimal, Decimal], Decimal]) -> Tagged:
    status = _worst(a.status, b.status)
    if a.value is None or b.value is None:
        return Tagged(None, "missing")
    return Tagged(op(a.value, b.value), status)


def _c(value: Decimal | int) -> Tagged:
    return Tagged(Decimal(value), "confirmed")


MISSING = Tagged(None, "missing")


@dataclass(frozen=True)
class Trade:
    """One trade record (fields as in positions.example.json)."""

    id: str
    symbol: str
    side: Literal["buy", "sell"]
    time: datetime
    price: Decimal
    currency: Literal["USD", "KRW"]
    qty: Decimal
    fee: Decimal | None = None
    fee_currency: Literal["USD", "KRW"] | None = None
    fx_rate: Decimal | None = None  # KRW per USD applied to this trade, if known


@dataclass(frozen=True)
class PnlConfig:
    fee_rate: Decimal  # e.g. Decimal("0.001") = 0.1 %
    fee_free_below_usd: Decimal  # order total below this (USD) has no fee
    fx_spread_bp: Decimal | None = None  # set by user in config.yaml
    basis_point_default: Decimal | None = None  # Toss basisPoint (meaning UNVERIFIED)


@dataclass(frozen=True)
class FxQuote:
    rate: Decimal | None  # KRW per USD (Toss mid)
    time: datetime | None


@dataclass(frozen=True)
class AvgOverride:
    price: Decimal
    currency: Literal["USD", "KRW"] = "USD"
    source: Literal["typed", "toss"] = "typed"   # toss = the broker's own average (confirmed)


@dataclass
class SymbolPnl:
    symbol: str
    qty: Decimal
    avg_source: Literal["computed", "typed", "toss"]
    avg_usd: Tagged  # per share; avg used for unrealized
    avg_krw: Tagged  # per share
    avg_usd_computed: Tagged
    realized_usd: Tagged
    realized_krw: Tagged
    unrealized_usd: Tagged
    unrealized_krw: Tagged
    fees_confirmed_usd: Tagged
    fees_confirmed_krw: Tagged
    fees_est_usd: Tagged
    fees_est_krw: Tagged
    fees_usd: Tagged
    fees_krw: Tagged
    fx_cost_usd: Tagged
    fx_cost_krw: Tagged
    ai_credits_usd: Tagged
    ai_credits_krw: Tagged
    net_usd: Tagged
    net_krw: Tagged


@dataclass
class PnlTotals:
    realized_usd: Tagged
    realized_krw: Tagged
    unrealized_usd: Tagged
    unrealized_krw: Tagged
    fees_confirmed_usd: Tagged
    fees_confirmed_krw: Tagged
    fees_est_usd: Tagged
    fees_est_krw: Tagged
    fees_usd: Tagged
    fees_krw: Tagged
    fx_cost_usd: Tagged
    fx_cost_krw: Tagged
    ai_credits_usd: Tagged
    ai_credits_krw: Tagged
    net_usd: Tagged
    net_krw: Tagged


@dataclass
class PnlResult:
    period: Period
    fx: FxQuote
    symbols: list[SymbolPnl]
    total: PnlTotals
    notes: list[str] = field(default_factory=list)


# ---------- parsing ----------

def _dec(value: Any, name: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise PnlError(f"{name}: number required")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - any parse failure is bad input
        raise PnlError(f"{name}: not a number") from None


def _get(rec: Any, key: str) -> Any:
    if isinstance(rec, Mapping):
        return rec.get(key)
    return getattr(rec, key, None)


def parse_trade(rec: Any) -> Trade:
    """Build a Trade from a dict or object; raise PnlError on bad fields."""
    if isinstance(rec, Trade):
        return rec
    tid = str(_get(rec, "id") or "?")
    where = f"trade {tid}"
    symbol = str(_get(rec, "symbol") or "").strip().upper()
    if not symbol:
        raise PnlError(f"{where}: symbol required")
    side = _get(rec, "side")
    if side not in ("buy", "sell"):
        raise PnlError(f"{where}: side must be buy or sell")
    currency = _get(rec, "currency")
    if currency not in ("USD", "KRW"):
        raise PnlError(f"{where}: currency must be USD or KRW")
    raw_time = _get(rec, "time")
    if isinstance(raw_time, datetime):
        t = raw_time
    else:
        try:
            t = datetime.fromisoformat(str(raw_time))
        except ValueError:
            raise PnlError(f"{where}: time must be ISO 8601") from None
    if t.tzinfo is None:
        raise PnlError(f"{where}: time needs a UTC offset (e.g. +09:00)")
    price = _dec(_get(rec, "price"), f"{where} price")
    qty = _dec(_get(rec, "qty"), f"{where} qty")
    if price <= 0 or qty <= 0:
        raise PnlError(f"{where}: price and qty must be positive")
    raw_fee = _get(rec, "fee")
    fee = None if raw_fee is None else _dec(raw_fee, f"{where} fee")
    if fee is not None and fee < 0:
        raise PnlError(f"{where}: fee cannot be negative")
    fee_currency = _get(rec, "fee_currency") or (currency if fee is not None else None)
    if fee_currency not in (None, "USD", "KRW"):
        raise PnlError(f"{where}: fee_currency must be USD or KRW")
    raw_fx = _get(rec, "fx_rate")
    fx_rate = None if raw_fx is None else _dec(raw_fx, f"{where} fx_rate")
    if fx_rate is not None and fx_rate <= 0:
        raise PnlError(f"{where}: fx_rate must be positive")
    return Trade(tid, symbol, side, t, price, currency, qty, fee, fee_currency, fx_rate)


def _parse_override(value: AvgOverride | Decimal | Mapping[str, Any] | None) -> AvgOverride | None:
    if value is None or isinstance(value, AvgOverride):
        return value
    if isinstance(value, Mapping):
        cur = value.get("currency", "USD")
        if cur not in ("USD", "KRW"):
            raise PnlError("avg_override: currency must be USD or KRW")
        price = _dec(value.get("price"), "avg_override price")
    else:
        cur, price = "USD", _dec(value, "avg_override")
    if price <= 0:
        raise PnlError("avg_override: price must be positive")
    return AvgOverride(price, cur)


# ---------- core ----------

@dataclass
class _State:
    qty: Decimal = _ZERO
    avg_usd: Tagged = field(default_factory=lambda: _c(0))
    cost_krw: Tagged = field(default_factory=lambda: _c(0))  # total KRW basis of held qty
    realized_usd: Tagged = field(default_factory=lambda: _c(0))
    realized_krw: Tagged = field(default_factory=lambda: _c(0))
    fees_conf_usd: Tagged = field(default_factory=lambda: _c(0))
    fees_conf_krw: Tagged = field(default_factory=lambda: _c(0))
    fees_est_usd: Tagged = field(default_factory=lambda: _c(0))
    fees_est_krw: Tagged = field(default_factory=lambda: _c(0))
    fx_usd: Tagged = field(default_factory=lambda: _c(0))
    fx_krw: Tagged = field(default_factory=lambda: _c(0))


def _trade_fx(t: Trade, ref_fx: Callable[[datetime], Decimal | None]) -> Tagged:
    """KRW per USD for this trade: entered fx_rate (confirmed) or reference (est.)."""
    if t.fx_rate is not None:
        return _c(t.fx_rate)
    ref = ref_fx(t.time)
    return MISSING if ref is None else Tagged(ref, "est.")


def _prices(t: Trade, fx: Tagged) -> tuple[Tagged, Tagged]:
    """Per-share price in (USD, KRW)."""
    if t.currency == "USD":
        usd = _c(t.price)
        return usd, usd * fx
    krw = _c(t.price)
    return krw / fx, krw


def estimate_fee_usd(total_usd: Decimal, cfg: PnlConfig) -> Decimal:
    """Fee rate × order total; no fee when the total is below the free limit (USD)."""
    if total_usd < cfg.fee_free_below_usd:
        return _ZERO
    return cfg.fee_rate * total_usd


def _apply_costs(st: _State, t: Trade, fx: Tagged, px_usd: Tagged, px_krw: Tagged,
                 cfg: PnlConfig) -> None:
    qty = _c(t.qty)
    total_usd = px_usd * qty
    total_krw = px_krw * qty
    # Fees: entered = confirmed; else estimate (applied to buys and sells separately).
    if t.fee is not None:
        fee = _c(t.fee)
        if t.fee_currency == "USD":
            fee_usd, fee_krw = fee, fee * fx
        else:
            fee_usd, fee_krw = fee / fx, fee
        st.fees_conf_usd += fee_usd
        st.fees_conf_krw += fee_krw
    elif total_usd.value is None:
        st.fees_est_usd += MISSING
        st.fees_est_krw += MISSING
    else:
        fee_usd = Tagged(estimate_fee_usd(total_usd.value, cfg), "est.")
        st.fees_est_usd += fee_usd
        st.fees_est_krw += fee_usd * fx
    # FX spread cost: only trades paid in KRW (a KRW->USD conversion happens).
    if t.currency == "KRW":
        # Toss basisPoint = (rate - midRate) / midRate x 10000 (spec); it can be negative,
        # so only its size is used as the spread estimate. A cost is never negative.
        bp = cfg.fx_spread_bp if cfg.fx_spread_bp is not None else cfg.basis_point_default
        if bp is not None:
            bp = abs(bp)
        if bp is None:
            st.fx_usd += Tagged(_ZERO, "not set")
            st.fx_krw += Tagged(_ZERO, "not set")
        else:
            rate = Tagged(bp / _BP, "est.")
            st.fx_usd += total_usd * rate
            st.fx_krw += total_krw * rate


def _apply_trade(st: _State, t: Trade, fx: Tagged) -> tuple[Tagged, Tagged]:
    """Update qty/avg/basis; return realized (USD, KRW) of this trade."""
    px_usd, px_krw = _prices(t, fx)
    q = _c(t.qty)
    if t.side == "buy":
        old_qty = _c(st.qty)
        new_qty = st.qty + t.qty
        st.avg_usd = (old_qty * st.avg_usd + q * px_usd) / _c(new_qty)
        st.cost_krw = st.cost_krw + q * px_krw
        st.qty = new_qty
        return _c(0), _c(0)
    if t.qty > st.qty:
        raise PnlError(
            f"trade {t.id}: sell {t.qty} {t.symbol} but only {st.qty} held (oversell)"
        )
    avg_krw = st.cost_krw / _c(st.qty)
    r_usd = q * (px_usd - st.avg_usd)
    r_krw = q * (px_krw - avg_krw)
    st.qty -= t.qty
    if st.qty == 0:  # full exit: reset average and basis
        st.avg_usd, st.cost_krw = _c(0), _c(0)
    else:
        st.cost_krw = st.cost_krw - q * avg_krw
    return r_usd, r_krw


def _in_period(t: Trade, period: Period, today_kst: date) -> bool:
    return period == "all" or t.time.astimezone(KST).date() == today_kst


QTY_EPS = Decimal("0.000001")


def current_holding_trades(trades: Iterable[Any]) -> list[Any]:
    """Only the trades of the CURRENT portfolio (user choice 2026-11-17): for each stock still held,
    the trades since it was last bought from zero shares. Stocks sold out completely, and earlier
    round trips of stocks held now, are left out. Records are returned unchanged."""
    by_sym: dict[str, list[tuple[Trade, Any]]] = {}
    for rec in trades:
        t = parse_trade(rec)
        by_sym.setdefault(t.symbol, []).append((t, rec))
    out: list[Any] = []
    for items in by_sym.values():
        items.sort(key=lambda x: x[0].time)
        qty = Decimal(0)
        start = 0
        for i, (t, _rec) in enumerate(items):
            qty += t.qty if t.side == "buy" else -t.qty
            if abs(qty) <= QTY_EPS:
                start = i + 1                      # back to zero: the next buy starts a new holding
        if qty > QTY_EPS:
            out.extend(rec for _t, rec in items[start:])
    return out


def compute_pnl(
    trades: Iterable[Any],
    live_usd: Mapping[str, Decimal | None],
    fx_now: FxQuote,
    ref_fx: Callable[[datetime], Decimal | None],
    config: PnlConfig,
    now: datetime,
    avg_override: Mapping[str, AvgOverride | Decimal | Mapping[str, Any] | None] | None = None,
    period: Period = "all",
    ai_credits: tuple[Tagged, Tagged] | None = None,
) -> PnlResult:
    """Compute per-symbol and total P&L. Raises PnlError on bad data or oversell.

    ``period`` filters realized, fees and FX cost by KST date of ``now``;
    holdings, averages and unrealized always use the full history.
    """
    if period not in ("today", "all"):
        raise PnlError("period must be 'today' or 'all'")
    if now.tzinfo is None:
        raise PnlError("now must be timezone-aware")
    today_kst = now.astimezone(KST).date()
    parsed = sorted((parse_trade(r) for r in trades), key=lambda t: t.time)
    overrides = {k.strip().upper(): _parse_override(v) for k, v in (avg_override or {}).items()}

    states: dict[str, _State] = {}
    for t in parsed:
        st = states.setdefault(t.symbol, _State())
        fx = _trade_fx(t, ref_fx)
        r_usd, r_krw = _apply_trade(st, t, fx)
        if _in_period(t, period, today_kst):
            st.realized_usd += r_usd
            st.realized_krw += r_krw
            px_usd, px_krw = _prices(t, fx)
            _apply_costs(st, t, fx, px_usd, px_krw, config)

    mid = MISSING if fx_now.rate is None else Tagged(fx_now.rate, "est.")
    rows = [
        _symbol_row(sym, st, live_usd.get(sym), mid, overrides.get(sym))
        for sym, st in sorted(states.items())
    ]
    notes = []
    if config.fx_spread_bp is None and config.basis_point_default is not None:
        notes.append("FX cost uses |Toss basisPoint| (gap between Toss rate and mid); "
                     "the actual spread charged is UNVERIFIED (est.)")
    if config.fx_spread_bp is None and config.basis_point_default is None:
        notes.append("FX spread not set: FX cost shown as 0 (not set)")
    totals = _totals(rows)
    if ai_credits is not None:  # AI spend is a desk cost, not per ticker: totals only
        ai_usd, ai_krw = ai_credits
        totals.ai_credits_usd, totals.ai_credits_krw = ai_usd, ai_krw
        totals.net_usd = totals.net_usd - ai_usd
        totals.net_krw = totals.net_krw - ai_krw
    return PnlResult(period, fx_now, rows, totals, notes)


def _symbol_row(sym: str, st: _State, live: Decimal | None, mid: Tagged,
                ov: AvgOverride | None) -> SymbolPnl:
    qty = _c(st.qty)
    if st.qty == 0:
        avg_usd, avg_krw, basis_krw = _c(0), _c(0), _c(0)
        source: Literal["computed", "typed", "toss"] = "computed"
    elif ov is not None:
        source = ov.source
        typed = Tagged(ov.price, "typed" if ov.source == "typed" else "confirmed")
        if ov.currency == "USD":
            avg_usd, avg_krw = typed, typed * mid
        else:
            avg_usd, avg_krw = typed / mid, typed
        basis_krw = qty * avg_krw
    else:
        source = "computed"
        avg_usd, basis_krw = st.avg_usd, st.cost_krw
        avg_krw = basis_krw / qty

    if st.qty == 0:
        un_usd, un_krw = _c(0), _c(0)
    elif live is None:
        un_usd, un_krw = MISSING, MISSING
    else:
        lv = _c(live)
        un_usd = qty * (lv - avg_usd)
        un_krw = qty * lv * mid - basis_krw

    fees_usd = st.fees_conf_usd + st.fees_est_usd
    fees_krw = st.fees_conf_krw + st.fees_est_krw
    ai_usd, ai_krw = _c(0), _c(0)  # Phase 2 adds AI credits
    net_usd = st.realized_usd + un_usd - fees_usd - st.fx_usd - ai_usd
    net_krw = st.realized_krw + un_krw - fees_krw - st.fx_krw - ai_krw
    return SymbolPnl(
        symbol=sym, qty=st.qty, avg_source=source,
        avg_usd=avg_usd, avg_krw=avg_krw, avg_usd_computed=st.avg_usd,
        realized_usd=st.realized_usd, realized_krw=st.realized_krw,
        unrealized_usd=un_usd, unrealized_krw=un_krw,
        fees_confirmed_usd=st.fees_conf_usd, fees_confirmed_krw=st.fees_conf_krw,
        fees_est_usd=st.fees_est_usd, fees_est_krw=st.fees_est_krw,
        fees_usd=fees_usd, fees_krw=fees_krw,
        fx_cost_usd=st.fx_usd, fx_cost_krw=st.fx_krw,
        ai_credits_usd=ai_usd, ai_credits_krw=ai_krw,
        net_usd=net_usd, net_krw=net_krw,
    )


_TOTAL_FIELDS = (
    "realized_usd", "realized_krw", "unrealized_usd", "unrealized_krw",
    "fees_confirmed_usd", "fees_confirmed_krw", "fees_est_usd", "fees_est_krw",
    "fees_usd", "fees_krw", "fx_cost_usd", "fx_cost_krw",
    "ai_credits_usd", "ai_credits_krw", "net_usd", "net_krw",
)


def _totals(rows: list[SymbolPnl]) -> PnlTotals:
    sums: dict[str, Tagged] = {}
    for name in _TOTAL_FIELDS:
        acc = _c(0)
        for r in rows:
            acc = acc + getattr(r, name)
        sums[name] = acc
    return PnlTotals(**sums)


# ---------- display ----------

def format_money(value: Decimal | None, currency: Literal["USD", "KRW"]) -> str:
    """Display only: 'USD 1,234.57' / 'KRW 1,234,568'; None -> 'USD —'."""
    if value is None:
        return f"{currency} —"
    places = Decimal("0.01") if currency == "USD" else Decimal("1")
    q = value.quantize(places, rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)  # avoid "-0.00"
    text = f"{q:,.2f}" if currency == "USD" else f"{q:,.0f}"
    return f"{currency} {text}"
