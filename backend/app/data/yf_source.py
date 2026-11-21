"""yfinance strip quotes (futures, indices, VIX, ^TNX, KRW=X). Delayed data.

yfinance is synchronous, so the async wrapper runs it in a thread.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import yfinance

DEFAULT_STRIP_SYMBOLS: tuple[str, ...] = ("NQ=F", "ES=F", "^ICESEMI", "^VIX", "^TNX", "KRW=X")


class YFinanceError(Exception):
    """Download failed. Carries only a short code (never the library message)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"yfinance error code={code}")

    def __repr__(self) -> str:
        return f"YFinanceError(code={self.code!r})"


@dataclass(frozen=True)
class StripQuote:
    symbol: str
    status: str  # "ok" | "missing" (fewer than two closes)
    last_close: Decimal | None
    prev_close: Decimal | None
    change: Decimal | None
    change_pct: Decimal | None
    bar_date: date | None  # date label of the last daily bar (exchange local)
    fetched_at: datetime  # UTC


def _to_decimal(value: float) -> Decimal:
    return Decimal(repr(float(value)))


def _close_series(df: Any, symbol: str, single: bool) -> Any | None:
    """Find the Close column for `symbol` in any yfinance column layout."""
    cols = df.columns
    if getattr(cols, "nlevels", 1) > 1:
        if symbol in cols.get_level_values(0):
            sub = df[symbol]
            return sub["Close"] if "Close" in sub.columns else None
        if "Close" in cols.get_level_values(0):
            sub = df["Close"]
            return sub[symbol] if symbol in sub.columns else None
        return None
    if single and "Close" in cols:
        return df["Close"]
    return None


def _quote(symbol: str, closes: Any, fetched_at: datetime) -> StripQuote:
    clean = closes.dropna() if closes is not None else None
    clean = clean[[not math.isinf(float(v)) for v in clean]] if clean is not None else None
    if clean is None or len(clean) < 2:
        return StripQuote(symbol, "missing", None, None, None, None, None, fetched_at)
    last = _to_decimal(clean.iloc[-1])
    prev = _to_decimal(clean.iloc[-2])
    change = last - prev
    pct = (change / prev * 100) if prev != 0 else None
    idx = clean.index[-1]
    bar_date = idx.date() if hasattr(idx, "date") else None
    return StripQuote(symbol, "ok", last, prev, change, pct, bar_date, fetched_at)


def fetch_strip(
    symbols: Sequence[str] = DEFAULT_STRIP_SYMBOLS,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[StripQuote]:
    """One batched daily download; last close, previous close and change per symbol."""
    syms = list(symbols)
    if not syms:
        return []
    failed = False
    try:
        df = yfinance.download(
            tickers=syms,
            period="10d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
    except Exception:  # noqa: BLE001 - library errors vary; message is dropped
        failed = True
    if failed:
        raise YFinanceError("download-failed")
    if df is None or getattr(df, "empty", True):
        raise YFinanceError("empty")
    fetched_at = now()
    single = len(syms) == 1
    return [_quote(s, _close_series(df, s, single), fetched_at) for s in syms]


async def fetch_strip_async(symbols: Sequence[str] = DEFAULT_STRIP_SYMBOLS) -> list[StripQuote]:
    """Async wrapper: runs fetch_strip in a worker thread."""
    return await asyncio.to_thread(fetch_strip, symbols)


class Backoff:
    """Error backoff: 2 -> 4 -> 8 ... capped at 900 seconds; reset() on success."""

    def __init__(self, initial: float = 2.0, maximum: float = 900.0, factor: float = 2.0) -> None:
        self._initial = initial
        self._maximum = maximum
        self._factor = factor
        self._current = initial

    def next_delay(self) -> float:
        delay = self._current
        self._current = min(self._current * self._factor, self._maximum)
        return delay

    def reset(self) -> None:
        self._current = self._initial
