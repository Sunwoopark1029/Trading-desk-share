"""SEC EDGAR client (phase 6 part B, read-only, official and free).

Facts from SEC's own pages (read 2026-11-20):
- https://www.sec.gov/search-filings/edgar-application-programming-interfaces
  companyfacts: https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json, "the entity's 10-digit central
  index key (CIK), including leading zeros"; "These APIs do not require any authentication or API keys";
  XBRL APIs "updated with a typical processing delay of under a minute".
- https://www.sec.gov/os/accessing-edgar-data and https://www.sec.gov/privacy.htm
  "Current max request rate: 10 requests/second" per user; "Please declare your user agent in request headers"
  (sample: "Sample Company Name AdminContact@<sample company domain>.com"); over the limit, requests "may be
  limited for a brief period" and access resumes "once the rate of requests has dropped below the threshold
  for 10 minutes". No HTTP status code is documented, so any 403 / 429 is treated as a block: every SEC call
  then pauses for `cooldown` (default 10 min) without touching the network.
- https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
  https://www.sec.gov/files/company_tickers.json: ticker, CIK, EDGAR conformed name; SEC updates it
  "periodically" and does "not guarantee accuracy or scope". Share classes use '-' (BRK-B).

The User-Agent contains the user's contact address: it is never logged and never put in an error.
Only the two URL shapes below can be requested. companyfacts files can be several MB: they are cached on disk
(data/cache/edgar/, gitignored) and parsed in a worker thread, and they are never sent to the AI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.data.toss import RateLimiter

log = logging.getLogger("desk.edgar")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ALLOWED_URL = re.compile(r"^(https://www\.sec\.gov/files/company_tickers\.json"
                         r"|https://data\.sec\.gov/api/xbrl/companyfacts/CIK[0-9]{10}\.json)\Z")
GROUP = "SEC"
MAX_CIK = 9_999_999_999


class EdgarError(Exception):
    """SEC call failed. Carries only an HTTP status and a short code (never the URL or User-Agent)."""

    def __init__(self, status: int | None, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(f"sec error status={status} code={code}")

    def __repr__(self) -> str:
        return f"EdgarError(status={self.status!r}, code={self.code!r})"


@dataclass(frozen=True)
class TickerEntry:
    cik: int
    ticker: str            # as SEC writes it (BRK-B)
    title: str             # EDGAR conformed name (may carry suffixes like "/DE", "/ADR")


@dataclass(frozen=True)
class FactsResult:
    cik: int
    data: dict[str, Any]   # the companyfacts JSON as SEC sent it
    fetched_at: float      # epoch seconds of the download
    stale: bool            # True: SEC failed and this is the last good copy from disk
    error_code: str | None = None


def sec_ticker(symbol: str) -> str:
    """Toss-style symbol -> SEC style (share classes: BRK.B -> BRK-B)."""
    return symbol.strip().upper().replace(".", "-")


def parse_tickers(raw: Any) -> dict[str, TickerEntry]:
    """company_tickers.json: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}."""
    if not isinstance(raw, dict):
        raise EdgarError(200, "bad-tickers")
    out: dict[str, TickerEntry] = {}
    for row in raw.values():
        if not isinstance(row, dict):
            continue
        cik, ticker, title = row.get("cik_str"), row.get("ticker"), row.get("title")
        if isinstance(cik, bool) or not isinstance(cik, int) or not 0 < cik <= MAX_CIK:
            continue
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        t = ticker.strip().upper()
        out.setdefault(t, TickerEntry(cik, t, str(title or "")))   # first row wins on a duplicate ticker
    if not out:
        raise EdgarError(200, "bad-tickers")
    return out


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        cache_dir: Path,
        *,
        http: httpx.AsyncClient | None = None,
        requests_per_second: float = 5,
        facts_ttl_s: float = 86400,
        tickers_ttl_s: float = 86400,
        cooldown_s: float = 600,
        timeout_s: float = 30,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._ua = (user_agent or "").strip()
        self.cache_dir = cache_dir
        self._http = http or httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)
        self._limiter = RateLimiter({GROUP: requests_per_second}, 1.0, mono, sleep)
        self.facts_ttl_s = facts_ttl_s
        self.tickers_ttl_s = tickers_ttl_s
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._mono = mono
        self._blocked_until = 0.0
        self._tickers: tuple[float, dict[str, TickerEntry]] | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def configured(self) -> bool:
        return bool(self._ua)

    @property
    def cooling_down(self) -> bool:
        return self._mono() < self._blocked_until

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── one request ──────────────────────────────────────────────────────

    async def _get(self, url: str) -> bytes:
        if not ALLOWED_URL.match(url):
            raise EdgarError(None, "url-not-allowed")
        if not self._ua:
            raise EdgarError(None, "no-user-agent")       # SEC requires a declared user agent: never call without
        if not (self._ua.isascii() and self._ua.isprintable()):
            raise EdgarError(None, "bad-user-agent")      # headers are ASCII only; never echo the value anywhere
        if self.cooling_down:
            raise EdgarError(None, "cooling-down")
        await self._limiter.acquire(GROUP)
        headers = {"User-Agent": self._ua, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"}
        failed = False
        try:
            resp = await self._http.get(url, headers=headers)
        except httpx.HTTPError:
            failed = True
        if failed:                                       # raised outside except: no chained httpx error (URL)
            raise EdgarError(None, "transport")
        if resp.status_code in (403, 429):
            self._blocked_until = self._mono() + self.cooldown_s
            log.warning("SEC refused a request (%s): pausing SEC calls for %.0f min", resp.status_code,
                        self.cooldown_s / 60)
            raise EdgarError(resp.status_code, "blocked" if resp.status_code == 403 else "rate-limited")
        if resp.status_code == 404:
            raise EdgarError(404, "not-found")
        if resp.status_code != 200:
            raise EdgarError(resp.status_code, f"http-{resp.status_code}")
        return resp.content

    # ── disk cache ───────────────────────────────────────────────────────

    def _path(self, name: str) -> Path:
        return self.cache_dir / name

    def _read_cache(self, name: str) -> tuple[float, bytes] | None:
        p = self._path(name)
        try:
            return p.stat().st_mtime, p.read_bytes()
        except OSError:
            return None

    def _write_cache(self, name: str, body: bytes) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=self.cache_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            os.replace(tmp, self._path(name))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    async def _parse(body: bytes) -> Any:
        try:
            return await asyncio.to_thread(json.loads, body)
        except (ValueError, UnicodeDecodeError):
            raise EdgarError(200, "bad-json") from None

    # ── public ───────────────────────────────────────────────────────────

    async def tickers(self) -> dict[str, TickerEntry]:
        """SEC ticker -> CIK map (memory, then disk, then SEC; refreshed after tickers_ttl)."""
        now = self._clock()
        if self._tickers is not None and now - self._tickers[0] < self.tickers_ttl_s:
            return self._tickers[1]
        async with self._locks.setdefault("tickers", asyncio.Lock()):
            if self._tickers is not None and now - self._tickers[0] < self.tickers_ttl_s:
                return self._tickers[1]
            cached = self._read_cache("company_tickers.json")
            if cached is not None and now - cached[0] < self.tickers_ttl_s:
                try:
                    parsed = parse_tickers(await self._parse(cached[1]))
                    self._tickers = (cached[0], parsed)
                    return parsed
                except EdgarError:
                    pass                                  # broken cache file: download again
            try:
                body = await self._get(TICKERS_URL)
                parsed = parse_tickers(await self._parse(body))
            except EdgarError:
                if self._tickers is not None:
                    return self._tickers[1]               # keep the old map in memory
                if cached is not None:
                    try:
                        parsed = parse_tickers(await self._parse(cached[1]))   # old disk copy beats nothing
                        self._tickers = (cached[0], parsed)
                        return parsed
                    except EdgarError:
                        pass
                raise
            self._write_cache("company_tickers.json", body)
            self._tickers = (now, parsed)
            return parsed

    async def cik_for(self, symbol: str) -> TickerEntry | None:
        """SEC entry for a US ticker (BRK.B and BRK-B both work), or None when SEC doesn't list it."""
        table = await self.tickers()
        return table.get(sec_ticker(symbol)) or table.get(symbol.strip().upper())

    async def company_facts(self, cik: int) -> FactsResult:
        """companyfacts JSON for one CIK: disk cache for facts_ttl, else SEC; on SEC failure the last good copy
        comes back with stale=True (never silently); with no copy at all the EdgarError is raised."""
        if isinstance(cik, bool) or not isinstance(cik, int) or not 0 < cik <= MAX_CIK:
            raise EdgarError(None, "bad-cik")
        name = f"CIK{cik:010d}.json"
        now = self._clock()
        async with self._locks.setdefault(name, asyncio.Lock()):
            cached = self._read_cache(name)
            if cached is not None and now - cached[0] < self.facts_ttl_s:
                try:
                    return FactsResult(cik, await self._parse(cached[1]), cached[0], stale=False)
                except EdgarError:
                    cached = None                         # broken cache file: download again
            try:
                body = await self._get(FACTS_URL.format(cik=cik))
                data = await self._parse(body)
                if not isinstance(data, dict) or not isinstance(data.get("facts"), dict):
                    raise EdgarError(200, "bad-facts")
            except EdgarError as exc:
                if cached is not None:
                    try:
                        old = await self._parse(cached[1])
                        log.info("SEC facts failed (%s): using the cached copy", exc.code)
                        return FactsResult(cik, old, cached[0], stale=True, error_code=exc.code)
                    except EdgarError:
                        pass
                raise
            self._write_cache(name, body)
            return FactsResult(cik, data, now, stale=False)
