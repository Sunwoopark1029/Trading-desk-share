"""Peer set and peer medians (phase 6 part B, decision D7: top 5 by market cap in the same Toss sector).

Toss sector stock lists come sorted by market cap (descending, spec); the company itself is excluded. Medians are
taken only over peers that have the metric (a loss-making peer has no P/E and is left out, and the count says so).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

DEFAULT_PEERS = 5


@dataclass(frozen=True)
class PeerMedian:
    metric_id: str
    value: Decimal | None
    count: int                 # peers that had the metric
    of: int                    # peers in the set
    note: str | None = None


def pick_peers(sector_symbols: Iterable[str], self_symbol: str, n: int = DEFAULT_PEERS,
               allowed: set[str] | None = None) -> tuple[list[str], str | None]:
    """First n symbols (already in market-cap order) other than the company itself; `allowed` filters to tickers the
    app can use (e.g. SEC filers). Returns (peers, note when fewer than n)."""
    me = self_symbol.upper()
    out: list[str] = []
    for s in sector_symbols:
        u = s.upper()
        if u == me or u in out or (allowed is not None and u not in allowed):
            continue
        out.append(u)
        if len(out) == n:
            break
    note = None if len(out) == n else f"only {len(out)} of {n} peers available in this sector"
    return out, note


def peer_medians(peer_metrics: dict[str, dict[str, Decimal | None]], metric_ids: Iterable[str]) -> list[PeerMedian]:
    """{peer: {metric id: value}} -> median per metric over peers that have it."""
    out = []
    n = len(peer_metrics)
    for mid in metric_ids:
        vals = [m[mid] for m in peer_metrics.values() if m.get(mid) is not None]
        if not vals:
            out.append(PeerMedian(mid, None, 0, n, "no peer has this metric"))
            continue
        out.append(PeerMedian(mid, Decimal(median(vals)), len(vals), n,
                              None if len(vals) == n else f"{len(vals)} of {n} peers have it"))
    return out
