"""Relative Strength Index (Wilder)."""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

RSI_PERIOD = 14


def rsi(closes: Sequence[Decimal], period: int = RSI_PERIOD) -> list[Decimal | None]:
    """Wilder's RSI, 0..100, one value per close.

    Changes are close[i] - close[i-1]. The first average gain / loss is the simple
    average of the first N changes (value at index N); after that
    avg = (prev_avg * (N-1) + current) / N. RSI = 100 - 100 / (1 + avg_gain / avg_loss).
    No losses -> 100; no gains -> 0; no movement at all -> 50 (neutral).
    Values before index N are None; with N or fewer closes every value is None.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[Decimal | None] = [None] * len(closes)
    if len(closes) <= period:
        return out

    n = Decimal(period)
    gains = losses = Decimal(0)
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        if ch > 0:
            gains += ch
        else:
            losses -= ch
    avg_gain, avg_loss = gains / n, losses / n
    out[period] = _value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + (ch if ch > 0 else Decimal(0))) / n
        avg_loss = (avg_loss * (n - 1) + (-ch if ch < 0 else Decimal(0))) / n
        out[i] = _value(avg_gain, avg_loss)
    return out


def _value(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0:
        return Decimal(50) if avg_gain == 0 else Decimal(100)
    return Decimal(100) - Decimal(100) / (1 + avg_gain / avg_loss)
