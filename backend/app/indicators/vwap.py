"""Session VWAP anchored at the pre-market start of each US trading date."""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from app.indicators.candles import EXTENDED_SESSIONS, Bar


@dataclass(frozen=True)
class VwapResult:
    """VWAP per input bar plus 'regular only' flags.

    `no_premarket_anchors`: anchors whose period has counted bars but no 'pre' bar.
    `regular_only`: True when the latest period with counted bars has no 'pre' bar
    (show the label "regular only").
    """

    values: list[Decimal | None]
    no_premarket_anchors: list[int] = field(default_factory=list)
    regular_only: bool = False


def vwap(bars_1m: Sequence[Bar], anchors: Sequence[int]) -> VwapResult:
    """VWAP of typical price (H+L+C)/3 weighted by volume.

    `bars_1m` must be sorted by end and session-tagged. `anchors` are the
    pre-market start epochs, one per trading date. A bar belongs to the period
    of the latest anchor strictly before its end. Only pre/regular/after bars
    count; 'day' and untagged bars get None and add nothing. Bars before the
    first anchor get None. Zero cumulative volume gives None.
    """
    anchor_list = sorted(set(anchors))
    values: list[Decimal | None] = []
    pv = Decimal(0)  # sum of (H+L+C) * V; divided by 3 at the end
    vol = Decimal(0)
    current: int | None = None
    seen: dict[int, bool] = {}  # anchor -> period had a 'pre' bar

    for b in bars_1m:
        idx = bisect.bisect_left(anchor_list, b.end) - 1
        anchor = anchor_list[idx] if idx >= 0 else None
        if anchor != current:
            current, pv, vol = anchor, Decimal(0), Decimal(0)
        if anchor is None or b.session not in EXTENDED_SESSIONS:
            values.append(None)
            continue
        seen[anchor] = seen.get(anchor, False) or b.session == "pre"
        pv += (b.high + b.low + b.close) * b.volume
        vol += b.volume
        values.append(pv / (3 * vol) if vol > 0 else None)

    missing = [a for a in sorted(seen) if not seen[a]]
    latest = max(seen) if seen else None
    return VwapResult(
        values=values,
        no_premarket_anchors=missing,
        regular_only=latest is not None and not seen[latest],
    )


def vwap_at(
    bars_1m: Sequence[Bar], values: Sequence[Decimal | None], ends: Sequence[int]
) -> list[Decimal | None]:
    """Sample VWAP at each end time (e.g. 5m bar ends).

    Uses the value of the latest 1m bar whose end is in (T-5m, T];
    None if there is no such bar.
    """
    bar_ends = [b.end for b in bars_1m]
    out: list[Decimal | None] = []
    for t in ends:
        i = bisect.bisect_right(bar_ends, t) - 1
        if i >= 0 and bar_ends[i] > t - 300:
            out.append(values[i])
        else:
            out.append(None)
    return out
