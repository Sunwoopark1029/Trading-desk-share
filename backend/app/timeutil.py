"""Time helpers. Store UTC epoch seconds; display Korea time (KST)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

# A US trading date runs 20:00 -> 20:00 America/New_York (covers Toss day market,
# pre-market, regular session and after-hours of that date).
TRADING_DAY_ROLLOVER = time(20, 0)


def _require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("datetime has no UTC offset (naive); refusing to guess a timezone")
    return dt


def parse_iso_with_offset(text: str) -> datetime:
    """Parse ISO 8601 text that must include an offset (or 'Z')."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("time must be a non-empty ISO 8601 string")
    try:
        dt = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(f"not an ISO 8601 time: {text!r}") from exc
    return _require_aware(dt)


def to_epoch(value: datetime | str) -> int:
    """Aware datetime or ISO string with offset -> UTC epoch seconds."""
    dt = parse_iso_with_offset(value) if isinstance(value, str) else _require_aware(value)
    return int(dt.timestamp())


def from_epoch(epoch: int | float) -> datetime:
    """Epoch seconds -> aware UTC datetime."""
    return datetime.fromtimestamp(epoch, tz=UTC)


def to_kst(dt: datetime) -> datetime:
    """Aware datetime (any zone, incl. pandas Timestamp) -> KST datetime."""
    return _require_aware(dt).astimezone(KST)


def epoch_to_kst_iso(epoch: int | float) -> str:
    """Epoch seconds -> e.g. '2026-03-09T22:30:00+09:00'."""
    return from_epoch(int(epoch)).astimezone(KST).isoformat()


def us_trading_date(epoch: int | float) -> date:
    """US trading date of an instant: [20:00 ET day D-1, 20:00 ET day D) -> D.

    For end-stamped bars pass the bar start (or end - 1) so the last
    after-hours bar ending at exactly 20:00 stays on its own date.
    Weekends/holidays are not checked here (use the market calendar).
    """
    ny = from_epoch(epoch).astimezone(NY)
    d = ny.date()
    return d + timedelta(days=1) if ny.time() >= TRADING_DAY_ROLLOVER else d


def ny_datetime(d: date, t: time) -> datetime:
    """Wall-clock time in New York on date d -> aware datetime (DST handled)."""
    return datetime.combine(d, t, tzinfo=NY)
