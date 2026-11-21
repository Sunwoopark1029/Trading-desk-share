"""Finnhub fundamentals (phase 6 part B, read-only) + the gate shared with the news feed.

Facts from Finnhub's official spec, pinned as docs/specs/finnhub_swagger.json (read 2026-11-21):
- Key: "All GET request require a token parameter token=apiKey in the URL or a header X-Finnhub-Token": the header
  is used, so URLs never contain the key. Limits: "On top of all plan's limit, there is a 30 API calls/ second
  limit"; over a limit -> HTTP 429. The free plan's own quota is not in the spec (UNVERIFIED): every Finnhub call in
  this app (news + fundamentals) waits on ONE FinnhubGate spaced by news.finnhub_min_interval_seconds, and a 429
  pauses all of them.
- /stock/profile2 (free): name, country, currency ("used in company filings"), exchange, ipo (date), finnhubIndustry,
  logo, weburl. Its marketCapitalization / shareOutstanding carry NO unit in the spec, so they are not used
  (market cap is calculated in code from the Toss price and Toss shares outstanding).
- /stock/earnings (free tier: "Last 4 quarters"): actual, estimate, surprise, surprisePercent, period (date), year,
  quarter. Top-level JSON array.
- /calendar/earnings (free tier: "1 month of historical earnings and new updates"): {"earningsCalendar": [...]} with
  date, hour (bmo / amc / dmh), year, quarter, epsEstimate, epsActual, revenueEstimate, revenueActual. The spec says
  these EPS / revenue numbers are NON-GAAP.
- /stock/recommendation (free): array of buy, hold, sell, strongBuy, strongSell, period.
- /stock/price-target is "Premium required." -> not used (targets come from yfinance, labelled unofficial).
- /stock/financials-reported repackages SEC filings -> not used (SEC EDGAR is read directly).
Numbers are parsed as exact decimals from the JSON text. Errors carry only a status and a short code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from app.news.feed import finnhub_tls_context

log = logging.getLogger("desk.finnhub")
BASE = "https://finnhub.io/api/v1"
PATHS = {"/stock/profile2", "/stock/earnings", "/calendar/earnings", "/stock/recommendation", "/company-news"}


class FinnhubError(Exception):
    def __init__(self, status: int | None, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(f"finnhub error status={status} code={code}")

    def __repr__(self) -> str:
        return f"FinnhubError(status={self.status!r}, code={self.code!r})"


class FinnhubGate:
    """One spacing gate for every Finnhub call in the app (news feed + fundamentals); a 429 pauses all of them."""

    def __init__(self, min_interval_s: float, cooldown_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.min_interval_s = min_interval_s
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._paused_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def paused(self) -> bool:
        return self._clock() < self._paused_until

    async def acquire(self) -> None:
        """Wait for the next free slot. Raises FinnhubError(cooling-down) while paused after a 429."""
        if self.paused:
            raise FinnhubError(None, "cooling-down")
        async with self._lock:
            now = self._clock()
            slot = max(now, self._next)
            self._next = slot + self.min_interval_s
        if slot > now:
            await self._sleep(slot - now)
        if self.paused:                                  # a 429 arrived while this call waited in the queue
            raise FinnhubError(None, "cooling-down")

    def hit_429(self) -> None:
        self._paused_until = self._clock() + self.cooldown_s
        log.warning("Finnhub rate limit (429): pausing all Finnhub calls for %.0f s", self.cooldown_s)


@dataclass(frozen=True)
class Profile:
    name: str | None
    country: str | None
    currency: str | None          # currency used in the company's filings
    exchange: str | None
    ipo: date | None
    industry: str | None          # Finnhub's own industry classification
    logo: str | None
    weburl: str | None


@dataclass(frozen=True)
class EarningsSurprise:
    period: date | None
    year: int | None
    quarter: int | None
    actual: Decimal | None        # EPS
    estimate: Decimal | None
    surprise: Decimal | None
    surprise_pct: Decimal | None


@dataclass(frozen=True)
class EarningsEvent:
    date: date | None
    hour: str | None              # bmo / amc / dmh
    year: int | None
    quarter: int | None
    eps_estimate: Decimal | None  # non-GAAP (spec)
    eps_actual: Decimal | None
    revenue_estimate: Decimal | None
    revenue_actual: Decimal | None


@dataclass(frozen=True)
class RecommendationTrend:
    period: str | None
    strong_buy: int | None
    buy: int | None
    hold: int | None
    sell: int | None
    strong_sell: int | None


def _s(v: Any) -> str | None:
    return v.strip() or None if isinstance(v, str) else None


def _i(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _dec(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, Decimal) and v.is_finite():
        return v
    return None


def _date(v: Any) -> date | None:
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def parse_profile(raw: Any) -> Profile | None:
    if not isinstance(raw, dict):
        raise FinnhubError(200, "bad-shape")
    if not raw:
        return None                                    # Finnhub answers {} for a symbol it does not cover
    return Profile(_s(raw.get("name")), _s(raw.get("country")), _s(raw.get("currency")), _s(raw.get("exchange")),
                   _date(raw.get("ipo")), _s(raw.get("finnhubIndustry")), _s(raw.get("logo")), _s(raw.get("weburl")))


def parse_earnings(raw: Any) -> list[EarningsSurprise]:
    if not isinstance(raw, list):
        raise FinnhubError(200, "bad-shape")
    out = [EarningsSurprise(_date(r.get("period")), _i(r.get("year")), _i(r.get("quarter")), _dec(r.get("actual")),
                            _dec(r.get("estimate")), _dec(r.get("surprise")), _dec(r.get("surprisePercent")))
           for r in raw if isinstance(r, dict)]
    return sorted(out, key=lambda e: e.period or date.min, reverse=True)


def parse_calendar(raw: Any) -> list[EarningsEvent]:
    if not isinstance(raw, dict) or not isinstance(raw.get("earningsCalendar"), list):
        raise FinnhubError(200, "bad-shape")
    out = [EarningsEvent(_date(r.get("date")), _s(r.get("hour")), _i(r.get("year")), _i(r.get("quarter")),
                         _dec(r.get("epsEstimate")), _dec(r.get("epsActual")), _dec(r.get("revenueEstimate")),
                         _dec(r.get("revenueActual")))
           for r in raw["earningsCalendar"] if isinstance(r, dict)]
    return sorted(out, key=lambda e: e.date or date.max)


def parse_recommendations(raw: Any) -> list[RecommendationTrend]:
    if not isinstance(raw, list):
        raise FinnhubError(200, "bad-shape")
    out = [RecommendationTrend(_s(r.get("period")), _i(r.get("strongBuy")), _i(r.get("buy")), _i(r.get("hold")),
                               _i(r.get("sell")), _i(r.get("strongSell"))) for r in raw if isinstance(r, dict)]
    return sorted(out, key=lambda r: r.period or "", reverse=True)


class FinnhubFund:
    def __init__(self, key: str, gate: FinnhubGate, *, http: httpx.AsyncClient | None = None,
                 timeout_s: float = 15) -> None:
        self._key = (key or "").strip()
        self.gate = gate
        self._http = http or httpx.AsyncClient(timeout=timeout_s, verify=finnhub_tls_context())

    @property
    def configured(self) -> bool:
        return bool(self._key)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if path not in PATHS:
            raise FinnhubError(None, "path-not-allowed")
        if not self._key:
            raise FinnhubError(None, "no-key")
        await self.gate.acquire()
        failed = False
        try:
            resp = await self._http.get(BASE + path, params=params, headers={"X-Finnhub-Token": self._key})
        except httpx.HTTPError:
            failed = True
        if failed:                                     # outside except: no chained httpx error carrying the URL
            raise FinnhubError(None, "transport")
        if resp.status_code == 429:
            self.gate.hit_429()
            raise FinnhubError(429, "rate-limited")
        if resp.status_code in (401, 403):
            raise FinnhubError(resp.status_code, "not-allowed")    # bad key, or a premium endpoint
        if resp.status_code != 200:
            raise FinnhubError(resp.status_code, f"http-{resp.status_code}")
        try:
            return json.loads(resp.content, parse_float=Decimal)
        except (ValueError, UnicodeDecodeError):
            raise FinnhubError(200, "bad-json") from None

    async def profile(self, symbol: str) -> Profile | None:
        return parse_profile(await self._get("/stock/profile2", {"symbol": symbol}))

    async def earnings(self, symbol: str) -> list[EarningsSurprise]:
        """Last quarters' EPS actual vs estimate (free plan: last 4 quarters), newest first."""
        return parse_earnings(await self._get("/stock/earnings", {"symbol": symbol}))

    async def earnings_calendar(self, symbol: str, start: date, end: date) -> list[EarningsEvent]:
        """Earnings dates between start and end (free plan: 1 month back + upcoming), oldest first."""
        return parse_calendar(await self._get("/calendar/earnings",
                                              {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat()}))

    async def company_news(self, symbol: str, start: date, end: date) -> list[Any]:
        """Raw /company-news items (same endpoint and rules as the news feed, longer window). Array or error."""
        raw = await self._get("/company-news", {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat()})
        if not isinstance(raw, list):
            raise FinnhubError(200, "bad-shape")
        return raw

    async def recommendations(self, symbol: str) -> list[RecommendationTrend]:
        """Analyst rating counts per month, newest first."""
        return parse_recommendations(await self._get("/stock/recommendation", {"symbol": symbol}))


def gate_of(svc: Any) -> FinnhubGate:
    """The one Finnhub gate of this app (shared by the news feed and the fundamentals client)."""
    g = getattr(svc, "_finnhub_gate", None)
    if g is None:
        g = FinnhubGate(svc.config.news.finnhub_min_interval_seconds)
        svc._finnhub_gate = g
    return g
