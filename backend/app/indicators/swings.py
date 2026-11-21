"""Swing high / low markers."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Sequence, TypedDict

from app.indicators.candles import Bar


class SwingMarker(TypedDict):
    end: int
    price: Decimal
    kind: Literal["high", "low"]


def swings(bars: Sequence[Bar], n: int = 3) -> list[SwingMarker]:
    """Mark swing highs and lows.

    High: high strictly above the N bars before and >= the N bars after
    (so a flat top marks its first bar). Low is the mirror. The first and
    last N bars are never marked. Output is in bar order, high before low
    when one bar is both.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    out: list[SwingMarker] = []
    for i in range(n, len(bars) - n):
        before = bars[i - n : i]
        after = bars[i + 1 : i + 1 + n]
        b = bars[i]
        if all(b.high > x.high for x in before) and all(b.high >= x.high for x in after):
            out.append({"end": b.end, "price": b.high, "kind": "high"})
        if all(b.low < x.low for x in before) and all(b.low <= x.low for x in after):
            out.append({"end": b.end, "price": b.low, "kind": "low"})
    return out
