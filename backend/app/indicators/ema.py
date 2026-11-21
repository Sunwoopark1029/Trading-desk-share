"""Exponential moving average."""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence


def ema(closes: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """EMA with alpha = 2/(N+1), seeded with the SMA of the first N closes.

    Values before index N-1 are None. If there are fewer than N closes,
    every value is None. Continuous across gaps (just follows the list).
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[Decimal | None] = [None] * len(closes)
    if len(closes) < period:
        return out

    n = Decimal(period)
    prev = sum(closes[:period], Decimal(0)) / n
    out[period - 1] = prev
    for i in range(period, len(closes)):
        # prev + 2/(N+1) * (c - prev), written with a single division.
        prev = (2 * closes[i] + (n - 1) * prev) / (n + 1)
        out[i] = prev
    return out
