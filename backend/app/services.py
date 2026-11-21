"""Runtime state for the API: cache, data sources, background pollers, view builders.

No order code here. Toss tokens are issued lazily by TossClient on its first call.
Errors are recorded in the Cache (short code + safe message) and never crash a route.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from types import SimpleNamespace
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from app import overlays as overlay_mod
from app import pnl as pnl_mod
from app import store
from app.cache import Cache, CacheEntry
from app.data.fred_source import FredError, latest_dgs10
from app.data.toss import (
    IP_NOT_ALLOWED_MESSAGE,
    SYMBOL_RE,
    TOKEN_REVOKED_MESSAGE,
    CalendarDay,
    Candle,
    ListedStock,
    MarketCalendar,
    TossClient,
    TossError,
    TossIpNotAllowed,
    TossTokenRevoked,
)
from app.data.yf_source import Backoff, StripQuote, YFinanceError, fetch_strip_async
from app.indicators.candles import Bar, Session, aggregate_5m, tag_sessions
from app.indicators.ema import ema
from app.indicators.rsi import rsi
from app.indicators.fib import FibPoint, FibResult, default_fib, fib_levels
from app.indicators.swings import swings
from app.indicators.vwap import vwap, vwap_at
from app.settings import OPTIONAL_SECRETS, AppConfig, Secrets, key_status
from app.timeutil import KST, to_epoch, us_trading_date

log = logging.getLogger("desk")

KEY_CALENDAR = "toss.calendar"
KEY_STOCKS = "toss.stocks"
KEY_FX = "toss.fx"
KEY_QUOTES = "toss.quotes"
KEY_YF = "yfinance.strip"
KEY_FRED = "fred.dgs10"
SOURCE_KEYS = (KEY_CALENDAR, KEY_STOCKS, KEY_FX, KEY_QUOTES, KEY_YF, KEY_FRED)

US_MARKETS = ("NYSE", "NASDAQ", "AMEX", "US_ETC")
HISTORY_1M = 3000        # ~3 trading days of 1m bars (5m view + EMA60 warm-up)
VIEW_1M = 2000           # ~2 trading days shown on the 1m chart
HISTORY_1D = 260         # ~200 daily bars shown + EMA60 warm-up
FULL_REFRESH_AFTER_S = 240  # older than this: re-fetch a full page instead of count=5
EMA_PERIODS = (9, 20, 60)
SESSION_FIELDS = (("day", "day_market"), ("pre", "pre_market"),
                  ("regular", "regular_market"), ("after", "after_market"))
EXTENDED_HOURS_NOTE = ("Toss 1m bars include day, pre, regular and after-hours trades "
                       "(verified 2026-11-16)")
FRED_ATTRIBUTION = ("This product uses the FRED® API but is not endorsed or certified "
                    "by the Federal Reserve Bank of St. Louis.")
YF_LABELS = {"NQ=F": "NQ", "ES=F": "ES", "^ICESEMI": "ICESEMI", "^VIX": "VIX",
             "^TNX": "10Y ^TNX", "KRW=X": "USD/KRW"}
YF_UNITS = {"NQ=F": "pts", "ES=F": "pts", "^ICESEMI": "pts", "^VIX": "pts",
            "^TNX": "%", "KRW=X": "KRW"}
COLOR_BUY, COLOR_SELL, COLOR_AVG = "#26a69a", "#ef5350", "#f5a623"
CHART_SYMBOL_TTL_S = 120     # keep polling the quote of a ticker the chart showed in the last 2 min
LIVE_QUOTE_MAX_AGE_S = 120   # older quotes don't update the forming candle


STRIP_MAX = 8   # stock slots in the top strip (pinned folder: first 8; the rest are counted)

class InputError(ValueError):
    """Bad user input (message is safe to show) -> HTTP 400."""


# ── JSON helper ──────────────────────────────────────────────────────────

def jsonable(obj: Any) -> Any:
    """Decimal -> exact string, datetime -> KST ISO, dataclasses/models -> dicts."""
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, float):
        return obj
    if isinstance(obj, datetime):
        return obj.astimezone(KST).isoformat() if obj.tzinfo else obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, pnl_mod.Tagged):
        return {"value": jsonable(obj.value), "status": obj.status}
    if isinstance(obj, BaseModel):
        return jsonable(obj.model_dump())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in obj]
    return str(obj)


def epoch_kst(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC).astimezone(KST).isoformat()


def error_info(exc: BaseException) -> tuple[str, str]:
    """(code, safe message). Never uses str(exc) of library errors."""
    if isinstance(exc, TossTokenRevoked):
        return exc.code, TOKEN_REVOKED_MESSAGE
    if isinstance(exc, TossIpNotAllowed):
        return exc.code, IP_NOT_ALLOWED_MESSAGE
    if isinstance(exc, TossError):
        status = f" {exc.status}" if exc.status else ""
        return exc.code, f"Toss error{status}: {exc.code}"
    if isinstance(exc, FredError):
        return exc.code, f"FRED error: {exc.code}"
    if isinstance(exc, YFinanceError):
        return exc.code, f"yfinance error: {exc.code}"
    if isinstance(exc, pnl_mod.PnlError):
        return "pnl", str(exc)
    return "unexpected", f"unexpected error ({type(exc).__name__})"


def _bar_dict(b: Bar, start: int, **extra: Any) -> dict[str, Any]:
    return {"start": start, "end": b.end, "open": b.open, "high": b.high, "low": b.low,
            "close": b.close, "volume": b.volume, "session": b.session, **extra}


def bucket_start(end: int, interval: int) -> int:
    """Start of the chart bar (of `interval` s) that holds a 1m bar ending at `end`."""
    return (end - 1) // interval * interval


def live_bar(last: dict[str, Any] | None, quote: Any, ticks: list[tuple[int, Decimal]] | None,
             interval: int, now: float) -> dict[str, Any] | None:
    """The forming candle, updated with the latest Toss quote (display only; indicators use
    finished bars). Same bucket rule as the bars: a price at time t belongs to the bar that
    ends at the next interval boundary at or after t. None if no fresh quote."""
    if quote is None or getattr(quote, "timestamp", None) is None or interval >= 86400:
        return None
    qt = to_epoch(quote.timestamp)
    if now - qt > LIVE_QUOTE_MAX_AGE_S:
        return None
    start = bucket_start(qt, interval)
    p = quote.last_price
    inside = [px for t, px in (ticks or []) if bucket_start(t, interval) == start]
    if last is not None and last["start"] == start:
        return {**last, "high": max([Decimal(str(last["high"])), p, *inside]),
                "low": min([Decimal(str(last["low"])), p, *inside]),
                "close": p, "provisional": True, "quote_time": epoch_kst(qt)}
    if last is not None and start < last["start"]:
        return None  # quote older than the newest bar
    return {"start": start, "end": start + interval, "open": inside[0] if inside else p,
            "high": max([p, *inside]), "low": min([p, *inside]), "close": p,
            "volume": Decimal(0), "session": last["session"] if last else None,
            "provisional": True, "quote_time": epoch_kst(qt)}


def fib_dict(res: FibResult | None, interval: int = 60) -> dict[str, Any] | None:
    if res is None:
        return None
    return {
        "type": "fib", "source": "indicator", "direction": res.direction,
        "low": res.low, "high": res.high,
        "points": [{"time": epoch_kst(p.time), "epoch": p.time,
                    "start": bucket_start(p.time, interval), "price": p.price}
                   for p in (res.p1, res.p2)],
        "levels": [{"ratio": lv.ratio, "price": lv.price} for lv in res.levels],
    }


@dataclasses.dataclass
class _CandleSet:
    bars: dict[int, Bar] = dataclasses.field(default_factory=dict)  # 1m by end epoch
    daily: dict[int, Candle] = dataclasses.field(default_factory=dict)  # 1d by stamp epoch
    fetched_1m: float | None = None
    fetched_1d: float | None = None


# ── services ─────────────────────────────────────────────────────────────

class Services:
    """Everything the routes need. Build one per app; tests inject fakes."""

    def __init__(
        self,
        config: AppConfig,
        secrets: Secrets,
        *,
        toss: TossClient | None = None,
        fred_http: httpx.AsyncClient | None = None,
        yf_fetch: Callable[[Sequence[str]], Awaitable[list[StripQuote]]] | None = None,
        positions_path: Path = store.POSITIONS_PATH,
        overlays_path: Path = store.OVERLAYS_PATH,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.keys = key_status(secrets)
        self.clock = clock
        self.cache = Cache(config.polling.stale_after_factor, clock)
        self.positions_path = positions_path
        self.overlays_path = overlays_path
        self._fred_key = secrets.FRED_API_KEY.get_secret_value().strip()
        if toss is None and self.keys["TOSS_CLIENT_ID"] == "set" and self.keys["TOSS_CLIENT_SECRET"] == "set":
            t = config.toss
            toss = TossClient(  # no network here; token is issued on the first call
                t.base_url,
                secrets.TOSS_CLIENT_ID.get_secret_value().strip(),
                secrets.TOSS_CLIENT_SECRET.get_secret_value().strip(),
                group_limits=t.rate_limits_per_second,
                safety=t.rate_limit_safety,
                token_margin_s=t.token_refresh_margin_seconds,
                timeout_s=t.timeout_seconds,
            )
        self.toss = toss
        self._fred_http = fred_http
        self._owns_fred_http = fred_http is None
        self._yf_fetch = yf_fetch or fetch_strip_async
        self._yf_backoff = Backoff(config.yfinance.backoff_start_seconds,
                                   config.yfinance.backoff_max_seconds)
        self.calendar: MarketCalendar | None = None
        self.calendars: dict[date, CalendarDay] = {}
        self.stocks: dict[str, ListedStock] = {}
        self.prev_close: dict[tuple[str, date], Decimal | None] = {}
        self._candles: dict[str, _CandleSet] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self.ai_credits: Any = None  # chat UsageLog (phase 2); None = no AI spend counted
        self._chart_symbols: dict[str, float] = {}  # symbol -> last time a chart asked for it
        self.market: Any = None   # MarketHub (phase 3): orderbook + trades
        self.news: Any = None     # NewsFeed (phase 3)
        self.tagger: Any = None   # NewsTagger (phase 3)
        self.account: Any = None  # AccountService (phase 4, read-only account data)
        self.drafts: Any = None   # DraftStore (phase 4): proposals only, nothing is sent from here
        self.order_settings: Any = None
        self.rules: Any = None    # RuleEngine: automatic rules (fire only after the user armed them)
        self.events_service: Any = None  # EventsService: next event + event list (created on first use)
        self.options: Any = None  # OptionsService (options-implied context; created on first use)
        self.sender: Any = None   # OrderSender: the only send path (DRY RUN unless config says otherwise)
        self.user_settings: Any = None      # Settings page values (user_settings.json over config.yaml)
        self.settings_defaults: Any = None
        self.settings_file: Any = None

    # ── small helpers ────────────────────────────────────────────────────

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    def _ok(self, key: str, value: Any, poll: float, source: str = "toss",
            delay: str = "real-time") -> None:
        self.cache.set_ok(key, value, source=source, delay_label=delay, poll_seconds=poll)

    def _err(self, key: str, exc: BaseException | None, poll: float, source: str = "toss",
             delay: str = "real-time", code: str | None = None, message: str | None = None) -> None:
        if exc is not None:
            code, message = error_info(exc)
            log.warning("%s failed: %s", key, code)
        self.cache.set_error(key, source=source, delay_label=delay, poll_seconds=poll,
                             error_code=code or "error", error_message=message)

    def _need_toss(self, key: str, poll: float) -> TossClient | None:
        if self.toss is None:
            self._err(key, None, poll, code="no-key", message="Toss keys not set in .env")
        return self.toss

    def fx_rate(self) -> tuple[Decimal | None, datetime | None, Decimal | None]:
        """(Toss mid, its time, basisPoint) from the cache, or Nones."""
        e = self.cache.get(KEY_FX)
        if e is None or e.value is None:
            return None, None, None
        return e.value.mid_rate, e.value.valid_from, e.value.basis_point

    def configured_symbols(self) -> list[str]:
        t = self.config.tickers
        return list(dict.fromkeys(s.upper() for s in [t.default, *t.compare, *self.strip_symbols()]))

    def pinned_symbols(self) -> list[str]:
        """The pinned watchlist folder (phase 6), via a provider set by the app; [] when none."""
        provider = getattr(self, "pinned_provider", None)
        if provider is None:
            return []
        try:
            return [s.upper() for s in provider()]
        except Exception as exc:  # noqa: BLE001 - a watchlist problem never breaks quotes
            log.info("pinned folder unavailable: %s", type(exc).__name__)
            return []

    def strip_symbols(self) -> list[str]:
        """Stock slots in the top strip: the pinned folder's first STRIP_MAX tickers, else config tickers.strip."""
        pinned = self.pinned_symbols()
        return pinned[:STRIP_MAX] if pinned else [s.upper() for s in self.config.tickers.strip]

    # ── refreshers (one cycle each; never raise) ─────────────────────────

    async def refresh_calendar(self) -> None:
        poll = self.config.polling.calendar_seconds
        toss = self._need_toss(KEY_CALENDAR, poll)
        if toss is None:
            return
        try:
            cal = await toss.market_calendar()
        except Exception as exc:  # noqa: BLE001
            self._err(KEY_CALENDAR, exc, poll)
            return
        self.calendar = cal
        for day in (cal.previous_business_day, cal.today, cal.next_business_day):
            self.calendars[day.date] = day
        try:  # one more trading day back for 5m history; old-date support UNVERIFIED
            older = await toss.market_calendar(cal.previous_business_day.date)
            self.calendars.setdefault(older.previous_business_day.date, older.previous_business_day)
        except Exception as exc:  # noqa: BLE001 - no shading for that day
            log.info("old-date calendar unavailable: %s", error_info(exc)[0])
        self._ok(KEY_CALENDAR, {"today": cal.today.date.isoformat()}, poll)

    async def refresh_stocks(self) -> None:
        poll = self.config.polling.stock_list_seconds
        toss = self._need_toss(KEY_STOCKS, poll)
        if toss is None:
            return
        merged: dict[str, ListedStock] = {}
        try:
            for market in US_MARKETS:
                for s in await toss.stocks_all(market):
                    merged.setdefault(s.symbol.upper(), s)
        except Exception as exc:  # noqa: BLE001 - keep the old list
            self._err(KEY_STOCKS, exc, poll)
            return
        self.stocks = merged
        self._ok(KEY_STOCKS, {"count": len(merged)}, poll)

    async def refresh_fx(self) -> None:
        poll = self.config.polling.fx_seconds
        toss = self._need_toss(KEY_FX, poll)
        if toss is None:
            return
        try:
            self._ok(KEY_FX, await toss.exchange_rate("USD", "KRW"), poll)
        except Exception as exc:  # noqa: BLE001
            self._err(KEY_FX, exc, poll)

    def quote_symbols(self) -> list[str]:
        syms = self.configured_symbols()
        try:
            for t in store.read_positions(self.positions_path).get("trades", []):
                s = str(t.get("symbol", "")).upper() if isinstance(t, dict) else ""
                if SYMBOL_RE.match(s):
                    syms.append(s)
        except store.StoreError:
            pass  # shown by /api/positions
        now = self.clock()
        syms += [s for s, t in self._chart_symbols.items() if now - t < CHART_SYMBOL_TTL_S]
        return list(dict.fromkeys(syms))

    async def refresh_quotes(self) -> None:
        poll = self.config.polling.quotes_seconds
        toss = self._need_toss(KEY_QUOTES, poll)
        if toss is None:
            return
        try:
            prices = await toss.prices(self.quote_symbols())
        except Exception as exc:  # noqa: BLE001
            self._err(KEY_QUOTES, exc, poll)
            return
        self._ok(KEY_QUOTES, {p.symbol.upper(): p for p in prices}, poll)
        if self.calendar is not None:
            prev_day = self.calendar.previous_business_day.date
            for sym in self.strip_symbols():
                if (sym.upper(), prev_day) not in self.prev_close:
                    try:
                        self.prev_close[(sym.upper(), prev_day)] = await self._fetch_prev_close(
                            toss, sym.upper(), prev_day)
                    except Exception as exc:  # noqa: BLE001 - not stored: retried next poll
                        log.info("prev close %s failed: %s", sym, error_info(exc)[0])

    async def _fetch_prev_close(self, toss: TossClient, sym: str, day: date) -> Decimal | None:
        """Close of the 1d bar whose date = previous business day (None if no such bar).

        Fetch errors propagate so the caller can retry on the next poll.
        """
        page = await toss.candles(sym, "1d", 5)
        return next((c.close for c in page.candles if c.timestamp.date() == day), None)

    async def refresh_yf(self) -> float:
        """Returns seconds until the next try (backoff after errors)."""
        poll = self.config.polling.yfinance_seconds
        try:
            quotes = await self._yf_fetch(self.config.yfinance.symbols)
        except Exception as exc:  # noqa: BLE001
            self._err(KEY_YF, exc, poll, source="yfinance", delay="delayed")
            return self._yf_backoff.next_delay()
        self._yf_backoff.reset()
        self._ok(KEY_YF, {q.symbol: q for q in quotes}, poll, source="yfinance", delay="delayed")
        return poll

    async def refresh_fred(self) -> None:
        poll = self.config.polling.fred_seconds
        if not self._fred_key:
            self._err(KEY_FRED, None, poll, source="fred", delay="daily",
                      code="no-key", message="no key")
            return
        if self._fred_http is None:
            self._fred_http = httpx.AsyncClient(timeout=self.config.toss.timeout_seconds)
        try:
            day, value = await latest_dgs10(self._fred_key, self._fred_http)
        except Exception as exc:  # noqa: BLE001
            self._err(KEY_FRED, exc, poll, source="fred", delay="daily")
            return
        self._ok(KEY_FRED, {"date": day, "value": value}, poll, source="fred", delay="daily")

    # ── pollers ──────────────────────────────────────────────────────────

    async def _loop(self, name: str, fn: Callable[[], Awaitable[Any]], interval: float) -> None:
        while True:
            try:
                delay = await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a poller must never die
                log.warning("poller %s: %s", name, type(exc).__name__)
                delay = None
            await asyncio.sleep(delay if isinstance(delay, (int, float)) and delay > 0 else interval)

    def start(self) -> None:
        """Start background pollers (call inside the running event loop)."""
        p = self.config.polling
        jobs = [
            ("calendar", self.refresh_calendar, p.calendar_seconds),
            ("stocks", self.refresh_stocks, p.stock_list_seconds),
            ("fx", self.refresh_fx, p.fx_seconds),
            ("quotes", self.refresh_quotes, p.quotes_seconds),
            ("yfinance", self.refresh_yf, p.yfinance_seconds),
            ("fred", self.refresh_fred, p.fred_seconds),
        ]
        self._tasks = [asyncio.create_task(self._loop(n, f, i), name=f"poll-{n}") for n, f, i in jobs]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.toss is not None:
            await self.toss.aclose()
        if self._fred_http is not None and self._owns_fred_http:
            await self._fred_http.aclose()

    def reset_toss(self) -> bool:
        if self.toss is None:
            return False
        self.toss.reset_revoked()
        return True

    # ── health ───────────────────────────────────────────────────────────

    @staticmethod
    def entry_meta(e: CacheEntry | None) -> dict[str, Any]:
        if e is None:
            return {"status": "pending", "updated": None, "source": None, "delay": None,
                    "error_code": None, "error": None}
        return {"status": e.status, "updated": epoch_kst(e.updated_at), "source": e.source,
                "delay": e.delay_label, "error_code": e.error_code, "error": e.error_message}

    def health(self) -> dict[str, Any]:
        sources = {k: self.entry_meta(self.cache.get(k)) for k in SOURCE_KEYS}
        for k in self.cache.keys():
            sources.setdefault(k, self.entry_meta(self.cache.get(k)))
        revoked = bool(self.toss is not None and self.toss.revoked)
        # candles are loaded on demand (only while a chart shows them): idle ones are not a problem
        bad = any(s["status"] in ("error", "stale") for k, s in sources.items() if not k.startswith("toss.candles."))
        return {
            "status": "degraded" if bad else "ok",
            "keys": dict(self.keys),
            "optional_keys": list(OPTIONAL_SECRETS),
            "toss": {"configured": self.toss is not None, "revoked": revoked,
                     "message": TOKEN_REVOKED_MESSAGE if revoked else None},
            "sources": sources,
            "fred_attribution": FRED_ATTRIBUTION,
            "deploy_role": self.config.deploy.role,
        }

    # ── tickers ──────────────────────────────────────────────────────────

    def validate_symbol(self, raw: str | None) -> str:
        if not raw or len(raw) > 20 or not SYMBOL_RE.match(raw):
            raise InputError("ticker must be letters, digits, '.' or '-'")
        sym = raw.upper()
        if self.stocks:
            if sym not in self.stocks:
                raise InputError(f"{sym} is not in the Toss US stock list")
        elif sym not in self.configured_symbols():
            raise InputError("stock list not loaded yet; only tickers from config.yaml are allowed")
        return sym

    def search_tickers(self, q: str, limit: int = 20) -> dict[str, Any]:
        q = (q or "").strip()
        meta = self.entry_meta(self.cache.get(KEY_STOCKS))
        if not q:
            return {"items": [], "meta": meta}
        qu = q.upper()
        exact = [s for k, s in self.stocks.items() if k == qu]
        prefix = sorted((s for k, s in self.stocks.items() if k.startswith(qu) and k != qu),
                        key=lambda s: s.symbol)
        names = [s for s in self.stocks.values()
                 if q.lower() in s.name.lower() and not s.symbol.upper().startswith(qu)]
        items = (exact + prefix + names)[:limit]
        return {"items": [{"symbol": s.symbol, "name": s.name, "security_type": s.security_type}
                          for s in items], "meta": meta}

    # ── candles ──────────────────────────────────────────────────────────

    def sessions(self) -> list[Session]:
        out: list[Session] = []
        for day in sorted(self.calendars):
            cd = self.calendars[day]
            for name, attr in SESSION_FIELDS:
                w = getattr(cd, attr)
                if w is not None:
                    out.append(Session(name, to_epoch(w.start), to_epoch(w.end)))  # type: ignore[arg-type]
        return out

    async def _load(self, symbol: str, interval: str) -> CacheEntry | None:
        """Fetch/merge candles for symbol if older than the poll interval."""
        key = f"toss.candles.{symbol}.{interval}"
        poll = self.config.polling.candles_seconds
        async with self._lock(key):
            cs = self._candles.setdefault(symbol, _CandleSet())
            store_ = cs.bars if interval == "1m" else cs.daily
            fetched = cs.fetched_1m if interval == "1m" else cs.fetched_1d
            age = None if fetched is None else self.clock() - fetched
            if store_ and age is not None and age < poll:
                return self.cache.get(key)
            toss = self._need_toss(key, poll)
            if toss is None:
                return self.cache.get(key)
            total = HISTORY_1M if interval == "1m" else HISTORY_1D
            try:
                if not store_:
                    candles = await toss.candles_history(symbol, interval, total)
                elif age is not None and age < FULL_REFRESH_AFTER_S:
                    candles = (await toss.candles(symbol, interval, 5)).candles
                else:
                    candles = await self._fill_gap(toss, symbol, interval, max(store_), total)
            except Exception as exc:  # noqa: BLE001 - keep last good bars
                self._err(key, exc, poll)
                return self.cache.get(key)
            for c in candles:  # upsert by timestamp (replace)
                ts = to_epoch(c.timestamp)
                if interval == "1m":
                    cs.bars[ts] = Bar(ts, c.open, c.high, c.low, c.close, c.volume)
                else:
                    cs.daily[ts] = c
            for k in sorted(store_)[:-total]:
                del store_[k]
            if interval == "1m":
                cs.fetched_1m = self.clock()
            else:
                cs.fetched_1d = self.clock()
            self._ok(key, {"bars": len(store_)}, poll)
            return self.cache.get(key)

    @staticmethod
    async def _fill_gap(toss: TossClient, symbol: str, interval: str, newest_stored: int,
                        total: int) -> list[Candle]:
        """Page back from now until a page reaches the newest stored bar (no hole left).

        Stops after `total` bars; older stored bars are then trimmed by the caller.
        """
        out: list[Candle] = []
        cursor: datetime | None = None
        while len(out) < total:
            page = await toss.candles(symbol, interval, 200, cursor)
            out.extend(page.candles)
            if (not page.candles or page.next_before is None
                    or min(to_epoch(c.timestamp) for c in page.candles) <= newest_stored):
                break
            cursor = page.next_before
        return out

    def _fib_target_date(self, now_epoch: int) -> date:
        cal = self.calendar
        if cal is None:
            return us_trading_date(now_epoch)
        pre = cal.today.pre_market
        if pre is not None and now_epoch >= to_epoch(pre.start):
            return cal.today.date
        return cal.previous_business_day.date

    async def candles_view(self, symbol: str, tf: str) -> dict[str, Any]:
        if tf not in ("1m", "5m", "1d"):
            raise InputError("tf must be 1m, 5m or 1d")
        self._chart_symbols[symbol] = self.clock()
        if self.market is not None:
            self.market.interest(symbol)  # stream its trades: the forming candle moves tick by tick
        if tf == "1d":
            return await self._daily_view(symbol)
        entry = await self._load(symbol, "1m")
        cs = self._candles.get(symbol, _CandleSet())
        sessions = self.sessions()
        tagged = tag_sessions([cs.bars[k] for k in sorted(cs.bars)], sessions)
        anchors = sorted({s.start for s in sessions if s.name == "pre"})
        vres = vwap(tagged, anchors)
        if tf == "1m":
            interval, series, vw = 60, tagged, list(vres.values)
        else:
            interval, series = 300, aggregate_5m(tagged)
            vw = vwap_at(tagged, vres.values, [b.end for b in series])
        emas = {str(n): ema([b.close for b in series], n) for n in EMA_PERIODS}
        rs = rsi([b.close for b in series])
        if tf == "1m" and len(series) > VIEW_1M:  # EMA / RSI warmed up on the full series
            cut = len(series) - VIEW_1M
            series, vw, rs = series[cut:], vw[cut:], rs[cut:]
            emas = {k: v[cut:] for k, v in emas.items()}
        target = self._fib_target_date(int(self.clock()))
        fib = default_fib([b for b in tagged if us_trading_date(b.end - 1) == target])
        first = series[0].end - interval if series else None
        last = series[-1].end if series else None
        shading = [{"name": s.name, "start": s.start, "end": s.end} for s in sessions
                   if first is not None and last is not None and s.end > first and s.start < last]
        return {
            "symbol": symbol, "tf": tf, "interval_seconds": interval,
            "bars": [_bar_dict(b, b.end - interval) for b in series],
            "ema": emas, "vwap": vw, "rsi": rs,
            "vwap_label": "regular only" if vres.regular_only else None,
            "fib": fib_dict(fib, interval), "fib_date": target,
            "swings": [{**m, "start": m["end"] - interval} for m in swings(series, 3)],
            "sessions": shading,
            "live_bar": live_bar(_bar_dict(series[-1], series[-1].end - interval) if series else None,
                                 *self._live_inputs(symbol), interval, self.clock()),
            "meta": {**self.entry_meta(entry), "currency": self._currency(symbol),
                     "extended_hours_note": EXTENDED_HOURS_NOTE,
                     "time_label": "bar start (end also given)"},
        }

    async def _daily_view(self, symbol: str) -> dict[str, Any]:
        entry = await self._load(symbol, "1d")
        cs = self._candles.get(symbol, _CandleSet())
        candles = [cs.daily[k] for k in sorted(cs.daily)]
        bars: list[Bar] = []
        out_bars = []
        for c in candles:
            d = c.timestamp.date()  # US trading date (stamped at local midnight, any offset)
            start = int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())
            b = Bar(start + 86400, c.open, c.high, c.low, c.close, c.volume)  # end = start + 1 day
            bars.append(b)
            out_bars.append(_bar_dict(b, start, date=d.isoformat()))
        return {
            "symbol": symbol, "tf": "1d", "interval_seconds": 86400,
            "bars": out_bars,
            "ema": {str(n): ema([b.close for b in bars], n) for n in EMA_PERIODS},
            "rsi": rsi([b.close for b in bars]),
            "vwap": None, "vwap_label": None, "fib": None, "fib_date": None,
            "swings": [{**m, "start": m["end"] - 86400} for m in swings(bars, 3)],
            "sessions": [], "live_bar": None,
            "meta": {**self.entry_meta(entry), "currency": self._currency(symbol),
                     "time_label": "US trading date (start = 00:00 UTC of that date, "
                                   "end = start + 1 day)"},
        }

    def _live_inputs(self, symbol: str) -> tuple[Any, list[tuple[int, Decimal]]]:
        """(latest price source, trade ticks) for the forming candle: the newer of the Toss quote
        and the last streamed trade; ticks give the candle's high/low between redraws."""
        q = self._quote(symbol)
        ticks: list[tuple[int, Decimal]] = []
        st = self.market.states.get(symbol) if self.market is not None else None
        if st is not None and st.tape:
            ticks = [(to_epoch(t.timestamp), t.price) for t in st.tape]
            lt = st.tape[-1]
            if q is None or q.timestamp is None or lt.timestamp > q.timestamp:
                q = SimpleNamespace(timestamp=lt.timestamp, last_price=lt.price)
        return q, ticks

    def _quote(self, symbol: str) -> Any:
        q_e = self.cache.get(KEY_QUOTES)
        return (q_e.value or {}).get(symbol) if q_e and q_e.value else None

    def _currency(self, symbol: str) -> str | None:
        cs = self._candles.get(symbol)
        if cs and cs.daily:
            return next(iter(cs.daily.values())).currency
        return "USD" if self.stocks or symbol in self.configured_symbols() else None

    # ── strip ────────────────────────────────────────────────────────────

    def strip_view(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        yf_e = self.cache.get(KEY_YF)
        yf_q: dict[str, StripQuote] = (yf_e.value or {}) if yf_e else {}
        yf_meta = self.entry_meta(yf_e)
        delays = self.config.yfinance.delay_minutes

        def yf_item(sym: str, label: str | None = None) -> dict[str, Any]:
            q = yf_q.get(sym)
            n = delays.get(sym)
            return {
                "key": sym, "label": label or YF_LABELS.get(sym, sym),
                "value": q.last_close if q else None, "change": q.change if q else None,
                "change_pct": q.change_pct if q else None, "unit": YF_UNITS.get(sym),
                "source": "yfinance", "delay": f"yfinance ~{n} min" if n else "yfinance (delayed)",
                "updated": q.fetched_at if q else yf_meta["updated"],
                "bar_date": q.bar_date if q else None,
                "status": "missing" if q is not None and q.status != "ok" else yf_meta["status"],
                "error": yf_meta["error"], "flag": None,
            }

        mid, fx_time, _bp = self.fx_rate()
        fx_meta = self.entry_meta(self.cache.get(KEY_FX))
        if mid is not None or "KRW=X" not in yf_q:
            items.append({"key": "USDKRW", "label": "USD/KRW", "value": mid, "change": None,
                          "change_pct": None, "unit": "KRW", "source": "Toss mid",
                          "delay": "refreshes ~1 min", "updated": fx_time or fx_meta["updated"],
                          "status": fx_meta["status"], "error": fx_meta["error"], "flag": None})
        else:
            items.append(yf_item("KRW=X"))
        for sym in self.config.yfinance.symbols:
            if sym == "^TNX":
                fred_e = self.cache.get(KEY_FRED)
                fmeta = self.entry_meta(fred_e)
                fv = fred_e.value if fred_e and fred_e.value else None
                items.append({"key": "DGS10", "label": "10Y FRED", "value": fv["value"] if fv else None,
                              "change": None, "change_pct": None, "unit": "%", "source": "FRED DGS10",
                              "delay": "daily", "updated": fmeta["updated"],
                              "bar_date": fv["date"] if fv else None, "status": fmeta["status"],
                              "error": fmeta["error"], "flag": None,
                              "attribution": FRED_ATTRIBUTION})
                tnx = yf_item(sym)
                if fv and tnx["value"] is not None and (
                        abs(tnx["value"] - fv["value"]) > self.config.yfinance.tnx_vs_fred_warn_points):
                    tnx["flag"] = "unit?"
                items.append(tnx)
            elif sym != "KRW=X":
                items.append(yf_item(sym))
        q_e = self.cache.get(KEY_QUOTES)
        q_meta = self.entry_meta(q_e)
        quotes = (q_e.value or {}) if q_e else {}
        prev_day = self.calendar.previous_business_day.date if self.calendar else None
        for sym in self.strip_symbols():
            p = quotes.get(sym)
            prev = self.prev_close.get((sym, prev_day)) if prev_day else None
            value = p.last_price if p else None
            change = value - prev if value is not None and prev is not None else None
            pct = change / prev * 100 if change is not None and prev else None
            items.append({"key": sym, "label": sym, "value": value, "change": change,
                          "change_pct": pct, "unit": p.currency if p else "USD", "source": "Toss",
                          "delay": "real-time", "updated": (p.timestamp if p and p.timestamp
                                                            else q_meta["updated"]),
                          "status": q_meta["status"] if p or q_e else "pending",
                          "error": q_meta["error"], "flag": None,
                          "prev_close_date": prev_day})
        return {"items": items, "next_event": self.next_event()}

    def next_event(self) -> dict[str, Any] | None:
        if self.config.event_calendar.enabled:
            from app.events.service import events_of
            return events_of(self).next_event()      # macro, FOMC, earnings, holidays, expiry, your own events
        now = self.now()
        future = sorted((e for e in self.config.events if e.time > now), key=lambda e: e.time)
        if not future:
            return None
        e = future[0]
        return {"name": e.name, "time": e.time, "note": e.note}

    # ── positions / P&L ──────────────────────────────────────────────────

    def _fx_block(self) -> dict[str, Any]:
        mid, t, _ = self.fx_rate()
        return {"rate": mid, "time": t, "source": "Toss mid", "pair": "USD/KRW"}

    def positions_view(self) -> dict[str, Any]:
        data = store.read_positions(self.positions_path)  # StoreError -> caller
        mid, fx_time, _ = self.fx_rate()
        plans_out: list[dict[str, Any]] = []
        lines: list[dict[str, Any]] = []
        now_iso = self.now().astimezone(KST).isoformat()
        for i, raw in enumerate(data.get("plans", [])):
            try:
                p = store.validate_plan(raw)
            except store.StoreError as exc:
                plans_out.append({"raw": raw, "error": str(exc)})
                continue
            usd = p["price"] if p["currency"] == "USD" else (p["price"] / mid if mid else None)
            krw = p["price"] if p["currency"] == "KRW" else (p["price"] * mid if mid else None)
            plans_out.append({**p, "price_usd": usd, "price_krw": krw,
                              "converted": p["currency"] != "USD",
                              "fx": {"rate": mid, "time": fx_time}})
            if usd is not None:
                lines.append(self._hline(p.get("id") or f"plan-{i}", p["symbol"], usd,
                                         f"Plan {p['side']} {p['currency']} {p['price']}",
                                         COLOR_BUY if p["side"] == "buy" else COLOR_SELL, now_iso))
        notes: list[str] = []
        try:
            result = self._compute_pnl(data, "all")
            for row in result.symbols:
                if row.qty > 0 and row.avg_usd.value is not None:
                    label = {"typed": "Avg (typed)", "toss": "Avg (Toss)"}.get(row.avg_source, "Avg")
                    lines.append(self._hline(f"avg-{row.symbol}", row.symbol, row.avg_usd.value,
                                             f"{label} USD {row.avg_usd.value:.4f}", COLOR_AVG, now_iso))
        except pnl_mod.PnlError as exc:
            notes.append(f"average line not shown: {exc}")
        return {"trades": data.get("trades", []), "plans": plans_out,
                "avg_override": data.get("avg_override", {}), "overlays": lines,
                "fx": self._fx_block(), "notes": notes}

    @staticmethod
    def _hline(oid: str, symbol: str, price: Decimal, label: str, color: str, t: str) -> dict[str, Any]:
        return {"id": oid, "type": "hline", "points": [{"time": t, "price": price}], "label": label,
                "color": color, "source": "user", "visible": True, "note": None,
                "symbol": symbol, "derived": True}

    def toss_pnl_ready(self) -> bool:
        acct = self.account
        return bool(acct is not None and acct.state.holdings is not None and acct.fills.loaded)

    def _compute_pnl(self, data: dict[str, Any], period: str) -> pnl_mod.PnlResult:
        """P&L from the Toss account when available (fills + holdings); else positions.json."""
        if self.toss_pnl_ready():
            return self._compute_pnl_toss(period)
        return self._compute_pnl_manual(data, period)

    def _toss_trades(self) -> tuple[list[dict[str, Any]], list[str], dict[str, Any], dict[str, Decimal]]:
        """(trade records from Toss fills incl. balancing entries, notes, Toss avg overrides, live prices)."""
        from app.orders.fills import closing_positions, opening_positions, to_trades

        acct = self.account
        items = acct.state.holdings.items
        trades = to_trades(acct.fills.orders.values(), acct.fills.transactions)
        opens, notes = opening_positions(trades, items)
        overrides = {h.symbol: pnl_mod.AvgOverride(h.average_purchase_price, "USD" if h.currency != "KRW" else "KRW",
                                                   "toss")
                     for h in items if h.average_purchase_price and h.quantity > 0}
        mid, _t, _bp = self.fx_rate()
        live: dict[str, Decimal] = {}
        for h in items:   # Toss's last price; KRW stocks converted to USD at the Toss mid (est.)
            if h.last_price is not None and h.currency == "USD":
                live[h.symbol] = h.last_price
            elif h.last_price is not None and h.currency == "KRW" and mid:
                live[h.symbol] = h.last_price / mid
        first = self._compute_pnl_manual({"trades": opens + trades, "avg_override": overrides}, "all",
                                         live_extra=live)
        currencies = {t["symbol"]: t["currency"] for t in trades} | {h.symbol: h.currency for h in items}
        closes, close_notes = closing_positions(first.symbols, items, currencies, self.now())
        return opens + trades + closes, notes + close_notes, overrides, live

    def _compute_pnl_toss(self, period: str) -> pnl_mod.PnlResult:
        from app.orders.fills import mismatches

        all_trades, notes, overrides, live = self._toss_trades()
        result = self._compute_pnl_manual({"trades": all_trades, "avg_override": overrides}, period,
                                          live_extra=live)
        result.notes[:0] = notes + mismatches(result.symbols, self.account.state.holdings.items)
        return result

    async def journal_view(self) -> dict[str, Any]:
        """Trade journal: round trips that include an order sent by this program (app.orders.journal)."""
        from app.orders.journal import build_journal, program_order_ids

        dry = self.config.orders.dry_run
        if not self.toss_pnl_ready():
            return {"ready": False, "dry_run": dry, "trips": [], "total": None, "notes": [],
                    "names": {}, "fx": self._fx_block()}
        acct = self.account
        sender = getattr(self, "sender", None)
        log = sender.log.records() if sender is not None else []
        if any(r.get("event") == "result" and r.get("conditional_order_id") and not r.get("dry_run") for r in log):
            await acct.refresh_closed_conditional()
        ids = program_order_ids(log, [*acct.state.open_conditional, *acct.closed_conditional])
        trades, _notes, _ov, live_extra = self._toss_trades()
        mid, fx_time, bp = self.fx_rate()
        q_e = self.cache.get(KEY_QUOTES)
        quotes = (q_e.value or {}) if q_e else {}
        live = {**live_extra, **{s: p.last_price for s, p in quotes.items() if p.currency == "USD"}}
        cfg = self.config.pnl
        pcfg = pnl_mod.PnlConfig(cfg.fee_rate, cfg.fee_free_below_usd, cfg.fx_spread_bp, bp)
        now = self.now()

        def engine(trip: list[dict[str, Any]]) -> pnl_mod.PnlResult:
            return pnl_mod.compute_pnl(trip, live, pnl_mod.FxQuote(mid, fx_time), lambda _t: mid, pcfg, now,
                                       {}, "all")

        usage = self.ai_credits.records() if self.ai_credits is not None else []
        out = build_journal(trades, ids, usage, engine, now, mid)
        out.update(ready=True, dry_run=dry, names=self.display_names([r["symbol"] for r in out["trips"]]),
                   fx=self._fx_block(), sent_orders=len(ids))
        return jsonable(out)

    def _compute_pnl_manual(self, data: dict[str, Any], period: str,
                            live_extra: dict[str, Decimal] | None = None) -> pnl_mod.PnlResult:
        mid, fx_time, bp = self.fx_rate()
        q_e = self.cache.get(KEY_QUOTES)
        quotes = (q_e.value or {}) if q_e else {}
        live = {**(live_extra or {}), **{s: p.last_price for s, p in quotes.items() if p.currency == "USD"}}
        cfg = self.config.pnl
        pcfg = pnl_mod.PnlConfig(cfg.fee_rate, cfg.fee_free_below_usd, cfg.fx_spread_bp, bp)
        trades = data.get("trades", [])
        since = None
        if period == "all":   # "all" = the current portfolio only (user choice 2026-11-17)
            trades = pnl_mod.current_holding_trades(trades)
            since = min((pnl_mod.parse_trade(t).time for t in trades), default=None)
        ai = self._ai_credits(period, mid, since)
        result = pnl_mod.compute_pnl(
            trades, live, pnl_mod.FxQuote(mid, fx_time),
            lambda _t: mid,  # historical reference rate not available: current mid (est.)
            pcfg, self.now(), data.get("avg_override") or {}, period,  # type: ignore[arg-type]
            ai_credits=ai,
        )
        if trades and mid is not None and any(
                (t.get("fx_rate") if isinstance(t, dict) else None) is None for t in trades):
            result.notes.append("Trades without an applied FX rate use the current Toss mid (est.)")
        return result

    def _ai_credits(self, period: str, mid: Decimal | None,
                    since: datetime | None = None) -> tuple[pnl_mod.Tagged, pnl_mod.Tagged] | None:
        """AI spend from logs/usage.jsonl for the period. USD is confirmed (from API usage x
        config prices); KRW uses the rate logged with each call, else the current mid (est.)."""
        if self.ai_credits is None:
            return None
        today, _month, total = self.ai_credits.totals(self.now())
        if period == "today":
            t = today
        elif since is None:     # nothing held: no current-portfolio period, so no AI spend in it
            t = type(total)()
        else:                   # since the oldest current holding was bought
            t = self.ai_credits.since(since)
        krw, est = t.krw_total(mid)
        usd_tag = pnl_mod.Tagged(t.usd, "confirmed")
        krw_tag = pnl_mod.MISSING if krw is None else pnl_mod.Tagged(krw, "est." if est else "confirmed")
        return usd_tag, krw_tag

    def display_names(self, symbols: list[str]) -> dict[str, str]:
        """Names for the screen: Toss holdings first (has Korean names for KR stocks), then the US list."""
        names: dict[str, str] = {}
        acct = self.account
        if acct is not None and acct.state.holdings is not None:
            names.update({h.symbol: h.name for h in acct.state.holdings.items if h.name})
        for s in symbols:
            if s not in names and s in self.stocks:
                names[s] = self.stocks[s].name
        return {s: names[s] for s in symbols if s in names}

    def symbol_currencies(self, symbols: list[str]) -> dict[str, str]:
        """Trading currency per symbol (KRW for Korean stocks) from Toss holdings / fills."""
        cur: dict[str, str] = {}
        acct = self.account
        if acct is not None:
            for o in acct.fills.orders.values():
                cur.setdefault(o.symbol, o.currency)
            if acct.state.holdings is not None:
                cur.update({h.symbol: h.currency for h in acct.state.holdings.items if h.currency})
        return {s: cur.get(s, "USD") for s in symbols}

    def pnl_view(self, period: str) -> dict[str, Any]:
        if period not in ("today", "all"):
            raise InputError("period must be today or all")
        data = store.read_positions(self.positions_path)
        result = self._compute_pnl(data, period)
        out = jsonable(result)
        out["source"] = "toss" if self.toss_pnl_ready() else "manual"
        out["names"] = self.display_names([r.symbol for r in result.symbols])
        out["currencies"] = self.symbol_currencies([r.symbol for r in result.symbols])
        out["fx"] = self._fx_block()
        out["quotes"] = self.entry_meta(self.cache.get(KEY_QUOTES))
        return out

    def save_plans(self, plans: Any) -> list[dict[str, Any]]:
        """Bad plan input -> InputError; a broken positions.json -> StoreError (file untouched)."""
        if not isinstance(plans, list):
            raise InputError("plans must be a list")
        try:
            for p in plans:
                store.validate_plan(p)
        except store.StoreError as exc:
            raise InputError(str(exc)) from None
        return store.save_plans(plans, self.positions_path)

    # ── overlays ─────────────────────────────────────────────────────────

    @staticmethod
    def overlay_out(o: overlay_mod.Overlay) -> dict[str, Any]:
        d = jsonable(o.model_dump())
        d["timeframe"] = overlay_mod.effective_timeframe(o)
        if o.type == "fib":
            p1, p2 = (FibPoint(to_epoch(p.time), p.price) for p in o.points)
            res = fib_levels(p1, p2)
            d["levels"] = [{"ratio": lv.ratio, "price": lv.price} for lv in res.levels]
            d["direction"] = res.direction
        return jsonable(d)

    def list_overlays(self, symbol: str | None = None, timeframe: str | None = None) -> list[dict[str, Any]]:
        """All saved overlays, or only those for `symbol` plus ticker-less ones, and (with timeframe) only
        those drawn on that timeframe plus timeframe-less ones."""
        items = overlay_mod.list_overlays(self.overlays_path)
        if symbol is not None:
            items = [o for o in items if o.symbol in (None, symbol)]
        if timeframe is not None:
            items = [o for o in items if overlay_mod.effective_timeframe(o) in (None, timeframe)]
        return [self.overlay_out(o) for o in items]

    def save_overlay(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise overlay_mod.OverlayError("overlay must be a JSON object")
        return self.overlay_out(overlay_mod.save_overlay(data, self.overlays_path))

    def delete_overlay(self, overlay_id: str) -> bool:
        return overlay_mod.delete_overlay(overlay_id, self.overlays_path)
