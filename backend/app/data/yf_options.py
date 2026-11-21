"""yfinance option chains -> Snapshot (app/indicators/options.py), with cache, backoff and stale fallback.

Source facts (live check 2026-11-21, ROADMAP Data sources): unofficial Yahoo endpoint, ~15 min delayed,
expirations `YYYY-MM-DD`, one request for the expiration list + one per expiry. Values are normalized here:
NaN -> None, non-positive bid / ask -> None, lastTradeDate -> epoch seconds. Nothing is calculated here.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time as _time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from app.data.yf_source import Backoff
from app.indicators.options import Chain, Contract, Snapshot

log = logging.getLogger(__name__)

SOURCE = "yfinance · ~15 min"


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def _pos(v: Any) -> float | None:
    x = _num(v)
    return x if x is not None and x > 0 else None


def _epoch(v: Any) -> float | None:
    try:
        ts = v.timestamp()                      # pandas Timestamp / datetime (tz-aware UTC from yfinance)
    except AttributeError:
        return _num(v)
    except ValueError:                          # NaT (no trade yet)
        return None
    return None if ts != ts else float(ts)      # NaT -> NaN


def contracts_from_rows(rows: list[dict[str, Any]]) -> tuple[Contract, ...]:
    out = []
    for r in rows:
        strike = _num(r.get("strike"))
        if strike is None:
            continue
        out.append(Contract(
            contract=str(r.get("contractSymbol") or ""), strike=strike,
            bid=_pos(r.get("bid")), ask=_pos(r.get("ask")), last=_pos(r.get("lastPrice")),
            volume=_num(r.get("volume")), oi=_num(r.get("openInterest")), iv=_pos(r.get("impliedVolatility")),
            last_trade=_epoch(r.get("lastTradeDate"))))
    return tuple(out)


def _rows(df: Any) -> list[dict[str, Any]]:
    return [] if df is None else df.to_dict("records")


def fetch_snapshot(symbol: str, max_expiries: int = 6) -> Snapshot:
    """Blocking fetch (run in a thread): the first `max_expiries` chains of `symbol`."""
    import yfinance as yf

    tk = yf.Ticker(symbol)
    exps = list(tk.options)
    if not exps:
        raise LookupError("no_options")
    chains: list[Chain] = []
    under: dict[str, Any] = {}
    for exp in exps[:max_expiries]:
        oc = tk.option_chain(exp)
        under = under or (oc.underlying or {})
        chains.append(Chain(date.fromisoformat(exp), contracts_from_rows(_rows(oc.calls)),
                            contracts_from_rows(_rows(oc.puts))))
    price = _pos(under.get("regularMarketPrice"))
    if price is None:
        raise LookupError("no_underlying_price")
    return Snapshot(symbol=symbol.upper(), price=price, price_time=_num(under.get("regularMarketTime")),
                    market_state=under.get("marketState"), post_price=_pos(under.get("postMarketPrice")),
                    pre_price=_pos(under.get("preMarketPrice")), chains=tuple(chains), fetched_at=_time.time(),
                    expiries_all=tuple(date.fromisoformat(e) for e in exps))


@dataclass
class OptionsResult:
    snapshot: Snapshot | None
    error: str | None = None      # last fetch error code (data may still be there, stale)
    stale: bool = False           # snapshot older than its refresh time because fetching failed


@dataclass
class _Entry:
    snap: Snapshot | None = None
    at: float = 0.0
    error: str | None = None
    retry_at: float = 0.0
    backoff: Backoff | None = None


class OptionsService:
    """Per-symbol cache: refresh after `ttl_live` s while the regular session is live, `ttl_closed` s otherwise.
    Errors back off (2 s doubling to 15 min) and keep serving the last snapshot marked stale."""

    def __init__(self, fetch: Callable[[str, int], Snapshot] = fetch_snapshot, *, max_expiries: int = 6,
                 ttl_live: float = 300, ttl_closed: float = 1800, max_symbols: int = 8,
                 clock: Callable[[], float] = _time.time) -> None:
        self.fetch, self.max_expiries = fetch, max_expiries
        self.ttl_live, self.ttl_closed, self.max_symbols, self.clock = ttl_live, ttl_closed, max_symbols, clock
        self._cache: OrderedDict[str, _Entry] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    def _ttl(self, snap: Snapshot | None) -> float:
        return self.ttl_live if snap is not None and (snap.market_state or "").upper() == "REGULAR" else self.ttl_closed

    async def get(self, symbol: str) -> OptionsResult:
        sym = symbol.strip().upper()
        lock = self._locks.setdefault(sym, asyncio.Lock())
        async with lock:
            e = self._cache.get(sym) or _Entry(backoff=Backoff(maximum=900))
            self._cache[sym] = e
            self._cache.move_to_end(sym)
            while len(self._cache) > self.max_symbols:
                old, _ = self._cache.popitem(last=False)
                self._locks.pop(old, None)
            now = self.clock()
            fresh = e.snap is not None and now - e.at < self._ttl(e.snap)
            if not fresh and now >= e.retry_at:
                try:
                    e.snap = await asyncio.to_thread(self.fetch, sym, self.max_expiries)
                    e.at, e.error = self.clock(), None
                    assert e.backoff is not None
                    e.backoff.reset()
                except Exception as ex:  # noqa: BLE001 - unofficial source: any failure -> back off, keep old data
                    code = str(ex) if isinstance(ex, LookupError) else type(ex).__name__
                    assert e.backoff is not None
                    e.error, e.retry_at = code, now + e.backoff.next_delay()
                    log.warning("options %s: fetch failed (%s); next try in %.0f s", sym, code, e.retry_at - now)
            stale = e.snap is not None and self.clock() - e.at >= self._ttl(e.snap)
            return OptionsResult(e.snap, e.error, stale)
