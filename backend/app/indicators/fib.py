"""Fibonacci retracement levels."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Sequence

from app.indicators.candles import EXTENDED_SESSIONS, Bar

FIB_RATIOS: tuple[Decimal, ...] = tuple(
    Decimal(r) for r in ("0", "0.236", "0.382", "0.5", "0.618", "0.786", "1")
)


@dataclass(frozen=True)
class FibPoint:
    """An anchor point: time (epoch s) and price."""

    time: int
    price: Decimal


@dataclass(frozen=True)
class FibLevel:
    ratio: Decimal
    price: Decimal


@dataclass(frozen=True)
class FibResult:
    """Two anchors (in time order), direction, and computed levels."""

    p1: FibPoint
    p2: FibPoint
    direction: Literal["up", "down"]
    low: Decimal
    high: Decimal
    levels: list[FibLevel]


def fib_levels(p1: FibPoint, p2: FibPoint) -> FibResult:
    """Levels for two points (given in any order; sorted by time).

    Up move (earlier point is the low): level = high - r*(high-low).
    Down move (earlier point is the high): level = low + r*(high-low).
    Equal times keep the given order.
    """
    first, second = (p2, p1) if p2.time < p1.time else (p1, p2)
    low = min(first.price, second.price)
    high = max(first.price, second.price)
    rng = high - low
    up = first.price <= second.price
    levels = [
        FibLevel(r, high - r * rng if up else low + r * rng) for r in FIB_RATIOS
    ]
    return FibResult(first, second, "up" if up else "down", low, high, levels)


def default_fib(bars: Sequence[Bar]) -> FibResult | None:
    """Default Fib from the low and high of the given trading-date bars.

    The caller passes the bars of the current trading date (or the previous
    one before pre-market opens). Only pre/regular/after bars count.
    Ties take the earliest bar. If one bar holds both the low and the high,
    the move is 'up' when close >= open. None if no bars count.
    """
    use = [b for b in bars if b.session in EXTENDED_SESSIONS]
    if not use:
        return None
    lo = min(use, key=lambda b: b.low)  # min/max return the first on ties
    hi = max(use, key=lambda b: b.high)
    if lo.end < hi.end or (lo.end == hi.end and lo.close >= lo.open):
        return fib_levels(FibPoint(lo.end, lo.low), FibPoint(hi.end, hi.high))
    return fib_levels(FibPoint(hi.end, hi.high), FibPoint(lo.end, lo.low))
