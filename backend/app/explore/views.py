"""Explore views (phase 6 part A): Toss stock rankings and sectors, fetched on demand and cached briefly.

Nothing here polls in the background: a view is fetched when the screen asks and kept for a short TTL
(config `explore:`), so Toss is only called while someone is looking. US market only in this phase.
All numbers come from Toss as exact decimals; this module only joins and labels them:
- ranking rows have no name -> names come from Toss stock basics (`/stocks`, cached a day per symbol),
  falling back to the Korean name in the daily stock list;
- sector stock / ETF rows have no price -> last prices from Toss `/prices` for the top N (config);
- sector amounts are KRW-converted by Toss, also for US stocks: kept as KRW with the currency label.
Errors keep the last good value and are reported as status "error" with a short code (never a URL).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.cache import Cache, CacheEntry
from app.data import toss as toss_mod
from app.data.toss import StockInfo
from app.services import InputError, Services, error_info

log = logging.getLogger("desk.explore")
MARKET = "US"
SOURCE = "toss"
PCT = Decimal("0.01")
NAMES_RETRY_SECONDS = 300      # a failed / missing stock-basics lookup is retried after 5 min, not on every view


def pct(rate: Decimal | None) -> str | None:
    """Toss fraction (0.0125) -> percent text with 2 decimals ("1.25"), calculated here, never by the AI."""
    return None if rate is None else format((rate * 100).quantize(PCT, rounding=ROUND_HALF_UP), "f")


class ExploreService:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.cfg = svc.config.explore
        self.cache = Cache(svc.config.polling.stale_after_factor, svc.clock)
        self._locks: dict[str, asyncio.Lock] = {}
        self._names: dict[str, tuple[StockInfo, float]] = {}   # symbol -> (basics, fetched at)
        self._names_tried: dict[str, float] = {}                # symbol -> last lookup attempt (ok or not)
        self._names_lock = asyncio.Lock()

    # ── generic on-demand fetch ──────────────────────────────────────────

    async def _fetch(self, key: str, ttl: float, delay: str,
                     load: Callable[[toss_mod.TossClient], Awaitable[Any]],
                     valid: Callable[[Any], bool] | None = None) -> CacheEntry:
        """Cached value if younger than ttl (and `valid(value)` if given), else one Toss call."""
        fresh = self._fresh(key, ttl, valid)
        if fresh is not None:
            return fresh
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:                      # one Toss call per key at a time; others wait for its result
            fresh = self._fresh(key, ttl, valid)
            if fresh is not None:
                return fresh
            client = self.svc.toss
            if client is None:
                self.cache.set_error(key, source=SOURCE, delay_label=delay, poll_seconds=ttl,
                                     error_code="no-key", error_message="Toss keys not set in .env")
            else:
                try:
                    self.cache.set_ok(key, await load(client), source=SOURCE, delay_label=delay, poll_seconds=ttl)
                except Exception as exc:  # noqa: BLE001 - shown as status "error", last good value kept
                    code, msg = error_info(exc)
                    log.warning("explore %s failed: %s", key.split(":")[0], code)
                    self.cache.set_error(key, source=SOURCE, delay_label=delay, poll_seconds=ttl,
                                         error_code=code, error_message=msg)
        entry = self.cache.get(key)
        assert entry is not None
        return entry

    def _fresh(self, key: str, ttl: float, valid: Callable[[Any], bool] | None = None) -> CacheEntry | None:
        e = self.cache.get(key)
        if e is None or e.error_code is not None or e.updated_at is None:
            return None
        if valid is not None and not valid(e.value):
            return None
        return e if self.svc.clock() - e.updated_at < ttl else None

    def meta(self, e: CacheEntry) -> dict[str, Any]:
        m = self.svc.entry_meta(e)
        if e.error_code is None and e.updated_at is not None:
            m["status"] = "ok"                  # on-demand views are refreshed on read, never "stale" while shown
        return m

    # ── names (Toss stock basics, cached a day per symbol) ───────────────

    async def names(self, symbols: list[str]) -> dict[str, StockInfo]:
        """Stock basics for the given symbols; missing / expired ones fetched in batches of 200."""
        want = list(dict.fromkeys(s.upper() for s in symbols if toss_mod.STOCKS_SYMBOLS_RE.match(s or "")))
        client = self.svc.toss
        async with self._names_lock:          # concurrent views share one lookup
            now = self.svc.clock()
            stale = [s for s in want
                     if (s not in self._names or now - self._names[s][1] >= self.cfg.names_ttl_seconds)
                     and now - self._names_tried.get(s, float("-inf")) >= NAMES_RETRY_SECONDS]
            if stale and client is not None:
                for i in range(0, len(stale), toss_mod.MAX_SYMBOLS):
                    batch = stale[i:i + toss_mod.MAX_SYMBOLS]
                    for sym in batch:
                        self._names_tried[sym] = now     # failed or missing symbols wait NAMES_RETRY_SECONDS
                    try:
                        for info in await client.stock_infos(batch):
                            self._names[info.symbol.upper()] = (info, now)
                    except Exception as exc:  # noqa: BLE001 - fall back to the daily stock list's Korean name
                        log.info("explore names failed: %s", error_info(exc)[0])
        return {s: self._names[s][0] for s in want if s in self._names}

    def _label(self, symbol: str, infos: dict[str, StockInfo]) -> dict[str, Any]:
        info = infos.get(symbol.upper())
        listed = self.svc.stocks.get(symbol.upper()) if getattr(self.svc, "stocks", None) else None
        return {
            "name": info.name if info else (listed.name if listed else None),
            "english_name": info.english_name if info else None,
            "security_type": info.security_type if info else (listed.security_type if listed else None),
            "leverage_factor": info.leverage_factor if info else None,
        }

    # ── rankings ─────────────────────────────────────────────────────────

    async def rankings(self, ranking_type: str, duration: str, count: int = 50) -> dict[str, Any]:
        if ranking_type not in toss_mod.RANKING_TYPES:
            raise InputError(f"type must be one of {', '.join(toss_mod.RANKING_TYPES)}")
        if duration not in toss_mod.RANKING_DURATIONS:
            raise InputError(f"duration must be one of {', '.join(toss_mod.RANKING_DURATIONS)}")
        if duration == "realtime" and ranking_type in toss_mod.NO_REALTIME_TYPES:
            raise InputError("gainers / losers have no realtime ranking: pick 1d or longer")
        if not 1 <= count <= toss_mod.MAX_RANKING_COUNT:
            raise InputError("count must be 1-100")
        key = f"rankings:{ranking_type}:{duration}:{count}"
        e = await self._fetch(key, self.cfg.rankings_ttl_seconds, "real-time",
                              lambda c: c.rankings(ranking_type, duration, MARKET, count))
        page = e.value
        rows: list[dict[str, Any]] = []
        if page is not None:
            infos = await self.names([r.symbol for r in page.rows])
            for r in page.rows:
                rows.append({"rank": r.rank, "symbol": r.symbol, **self._label(r.symbol, infos),
                             "currency": r.currency, "last_price": r.last_price, "base_price": r.base_price,
                             "change_rate": r.change_rate, "change_pct": pct(r.change_rate),
                             "trading_volume": r.trading_volume, "trading_amount": r.trading_amount})
        return {"type": ranking_type, "duration": duration, "market": MARKET,
                "ranked_at": page.ranked_at if page is not None else None,
                "base_price_means": "duration start" if ranking_type in toss_mod.NO_REALTIME_TYPES else "previous close",
                "volume_counts": "Toss executions only" if ranking_type.startswith("TOSS_SECURITIES") else "whole market",
                "rows": rows, "meta": self.meta(e)}

    # ── sectors ──────────────────────────────────────────────────────────

    async def sectors(self) -> dict[str, Any]:
        e = await self._fetch("sectors", self.cfg.sector_list_ttl_seconds, "daily", lambda c: c.sectors())
        items = e.value or []
        kids: dict[str, int] = {}
        for s in items:
            if s.parent_sector_id:
                kids[s.parent_sector_id] = kids.get(s.parent_sector_id, 0) + 1
        return {"sectors": [{"sector_id": s.sector_id, "name": s.name, "depth": s.depth,
                             "parent_sector_id": s.parent_sector_id, "children": kids.get(s.sector_id, 0)}
                            for s in items], "meta": self.meta(e)}

    async def sector_rankings(self, ranking_type: str, duration: str) -> dict[str, Any]:
        if ranking_type not in toss_mod.SECTOR_RANKING_TYPES:
            raise InputError(f"type must be one of {', '.join(toss_mod.SECTOR_RANKING_TYPES)}")
        if duration not in toss_mod.SECTOR_DURATIONS:
            raise InputError(f"duration must be one of {', '.join(toss_mod.SECTOR_DURATIONS)}")
        key = f"sector_rankings:{ranking_type}:{duration}"
        e = await self._fetch(key, self.cfg.rankings_ttl_seconds, "real-time",
                              lambda c: c.sector_rankings(ranking_type, duration, MARKET))
        rows = [{"rank": r.rank, "sector_id": r.sector_id, "name": r.name, "change_rate": r.change_rate,
                 "change_pct": pct(r.change_rate), "trading_amount": r.trading_amount, "market_cap": r.market_cap,
                 "currency": r.currency} for r in (e.value or [])]
        return {"type": ranking_type, "duration": duration, "market": MARKET, "rows": rows,
                "amounts_note": "Toss converts sector amounts to KRW, also for US stocks", "meta": self.meta(e)}

    async def sector(self, sector_id: str) -> dict[str, Any]:
        if not isinstance(sector_id, str) or not toss_mod.SECTOR_ID_RE.match(sector_id) or sector_id == "rankings":
            raise InputError("sector id must be letters, digits, '_' or '-' (1-40)")
        ttl = self.cfg.sector_ttl_seconds
        detail = await self._fetch(f"sector:{sector_id}", ttl, "real-time", lambda c: c.sector(sector_id, MARKET))
        stocks = await self._fetch(f"sector_stocks:{sector_id}", ttl, "real-time",
                                   lambda c: c.sector_stocks(sector_id, MARKET))
        etfs = await self._fetch(f"sector_etfs:{sector_id}", ttl, "real-time",
                                 lambda c: c.sector_etfs(sector_id, MARKET))
        stock_rows = list(stocks.value or [])
        etf_rows = list(etfs.value or [])
        top = self.cfg.sector_prices_top
        priced = [s.symbol for s in stock_rows[:top]] + [x.symbol for x in etf_rows[:top]]
        priced = list(dict.fromkeys(priced))[:toss_mod.MAX_SYMBOLS]
        prices_e = None
        last: dict[str, Any] = {}
        if priced:
            want = tuple(priced)

            async def load_prices(c: toss_mod.TossClient) -> tuple[tuple[str, ...], list[Any]]:
                return want, await c.prices(list(want))

            # one slot per sector; a changed symbol list (market-cap order moved) refetches into the same slot
            prices_e = await self._fetch(f"sector_prices:{sector_id}", ttl, "real-time", load_prices,
                                         valid=lambda v: v is not None and v[0] == want)
            got = prices_e.value[1] if prices_e.value is not None and prices_e.value[0] == want else []
            last = {p.symbol.upper(): p for p in got}
        d = detail.value
        return {
            "market": MARKET,
            "detail": None if d is None else {
                "sector_id": d.sector_id, "name": d.name, "depth": d.depth, "parent_sector_id": d.parent_sector_id,
                "summary": d.summary,
                "change_rates": [{"duration": k, "change_rate": v, "change_pct": pct(v)} for k, v in d.change_rates]},
            "stocks": [{"symbol": s.symbol, "name": s.name, "market_cap": s.market_cap, "market_cap_delay": "daily",
                        "volume": s.volume, "trading_amount": s.trading_amount, "currency": s.currency,
                        **self._price(last.get(s.symbol.upper()))} for s in stock_rows],
            "etfs": [{"symbol": x.symbol, "name": x.name, **self._price(last.get(x.symbol.upper()))} for x in etf_rows],
            "prices_for_top": top,
            "notes": [n for n in (
                f"Last prices only for the top {top} stocks by market cap and the top {top} ETFs"
                if len(stock_rows) > top or len(etf_rows) > top else None,
                "Toss gives no change % in sector lists",
                "market cap: updated daily by Toss; market cap / amount: KRW-converted by Toss, also for US stocks")
                if n],
            "meta": {"detail": self.meta(detail), "stocks": self.meta(stocks), "etfs": self.meta(etfs),
                     "prices": self.meta(prices_e) if prices_e is not None else None},
        }

    @staticmethod
    def _price(p: Any) -> dict[str, Any]:
        if p is None:
            return {"last_price": None, "price_currency": None, "price_time": None}
        return {"last_price": p.last_price, "price_currency": p.currency, "price_time": p.timestamp}


def explore_of(svc: Services) -> ExploreService:
    """One ExploreService per app (created on first use)."""
    ex = getattr(svc, "_explore", None)
    if ex is None:
        ex = ExploreService(svc)
        svc._explore = ex  # type: ignore[attr-defined]
    return ex
