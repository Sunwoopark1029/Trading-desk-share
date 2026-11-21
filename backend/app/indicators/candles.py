"""Bar model, session tagging, and 1m -> 5m aggregation.

All times are UTC epoch seconds. Bars are stamped with their END time
(as Toss does); `bar_start` gives the start time for display.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal, Sequence

SessionName = Literal["day", "pre", "regular", "after"]

FIVE_MINUTES = 300

# Sessions that count as the US trading day for VWAP and default Fib.
EXTENDED_SESSIONS: frozenset[str] = frozenset({"pre", "regular", "after"})


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar stamped with its end time (epoch seconds, UTC)."""

    end: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    session: str | None = None


@dataclass(frozen=True)
class Session:
    """A market session window (start, end] in epoch seconds."""

    name: SessionName
    start: int
    end: int


def session_for(end_t: int, sessions: Sequence[Session]) -> str | None:
    """Name of the session with start < end_t <= end, or None."""
    for s in sessions:
        if s.start < end_t <= s.end:
            return s.name
    return None


def tag_sessions(bars: Sequence[Bar], sessions: Sequence[Session]) -> list[Bar]:
    """Return copies of `bars` with `session` set (None if outside every session)."""
    return [replace(b, session=session_for(b.end, sessions)) for b in bars]


def bucket_end(end_t: int, interval_seconds: int = FIVE_MINUTES) -> int:
    """Interval boundary T such that end_t is in (T - interval, T]."""
    return -(-end_t // interval_seconds) * interval_seconds


def aggregate_5m(bars_1m: Sequence[Bar]) -> list[Bar]:
    """Build 5m bars from 1m bars.

    1m bars with end in (T-5m, T] form the 5m bar ending at T (T on :00/:05...).
    O = first, H = max, L = min, C = last, V = sum, session = last bar's session.
    Windows with no 1m bars produce no bar (never zero-filled).
    Duplicate end times: the last one given wins.
    """
    unique = {b.end: b for b in bars_1m}
    ordered = [unique[k] for k in sorted(unique)]

    out: list[Bar] = []
    group: list[Bar] = []
    group_t: int | None = None
    for b in ordered:
        t = bucket_end(b.end)
        if group and t != group_t:
            out.append(_merge(group, group_t))  # type: ignore[arg-type]
            group = []
        group.append(b)
        group_t = t
    if group:
        out.append(_merge(group, group_t))  # type: ignore[arg-type]
    return out


def _merge(group: list[Bar], end_t: int) -> Bar:
    return Bar(
        end=end_t,
        open=group[0].open,
        high=max(b.high for b in group),
        low=min(b.low for b in group),
        close=group[-1].close,
        volume=sum((b.volume for b in group), Decimal(0)),
        session=group[-1].session,
    )


def bar_start(bar: Bar, interval_seconds: int) -> int:
    """Start time of an intraday bar (end - interval)."""
    return bar.end - interval_seconds
