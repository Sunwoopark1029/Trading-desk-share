"""Upcoming events from every source, cached per source, refreshed in the background (never blocks a page).

Sources and refresh times (config event_calendar):
- BLS bls.ics (CPI, jobs, PPI, JOLTS, ECI; ET times)          24 h
- FRED release dates (GDP, PCE; date only)                    24 h
- FOMC dates (fomc.yaml)                                       static
- Toss market calendar: holidays / early closes per weekday   12 h
- Toss sector stock lists -> top N US stocks by market cap    24 h
- Finnhub earnings for the watched tickers                     6 h
- monthly options expiry (calculated), your own events (Settings page + config.yaml)

A failing source keeps its last events and is retried after 10 minutes; its error shows in `sources`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

import httpx

from app.events import model
from app.events.model import Event
from app.timeutil import KST, NY, UTC, parse_iso_with_offset

log = logging.getLogger("desk.events")

BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"
UA = {"User-Agent": "personal-trading-desk/1.0 (event calendar)"}
RETRY_S = 600
NORMAL_CLOSE_NY = (16, 0)


# share classes of one company without a dot in the ticker (dotted ones like BRK.A / BRK.B are matched by base)
SHARE_CLASS_PAIRS = {"GOOGL": "GOOG", "FOXA": "FOX", "NWSA": "NWS", "UAA": "UA"}
CLASS_SUFFIX = re.compile(r"(\s+(?i:class|cl\.?|series|클래스)\s*[A-C]|\s+[A-C]|\s*\((?i:class\s+)?[A-C]\))$")


def company_key(symbol: str, name: str = "") -> str:
    """One key per company: share classes (GOOG / GOOGL, BRK.A / BRK.B, 'X Class A' / 'X Class C') share it."""
    sym = symbol.upper()
    sym = SHARE_CLASS_PAIRS.get(sym, sym)
    base = re.split(r"[.\-/]", sym)[0]
    nm = CLASS_SUFFIX.sub("", name.strip()).strip().lower()
    return f"name:{nm}" if len(nm) >= 3 and nm != name.strip().lower() else f"sym:{base}"


def top_companies(caps: dict[str, Any], names: dict[str, str], n: int) -> list[str]:
    """Top n tickers by market cap, one per company (the larger-cap share class is kept)."""
    out: list[str] = []
    seen_sym: set[str] = set()
    seen_name: set[str] = set()
    for sym in sorted(caps, key=lambda k: -caps[k]):
        key = company_key(sym, names.get(sym, ""))
        base = "sym:" + re.split(r"[.\-/]", SHARE_CLASS_PAIRS.get(sym.upper(), sym.upper()))[0]
        if base in seen_sym or key in seen_name:
            continue
        seen_sym.add(base)
        seen_name.add(key)
        out.append(sym)
        if len(out) >= n:
            break
    return out


@dataclass
class SourceState:
    events: list[Event] = field(default_factory=list)
    updated: float | None = None
    error: str | None = None
    retry_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


class EventsService:
    def __init__(self, svc: Any, *, http: httpx.AsyncClient | None = None, finnhub: Any = None,
                 clock: Callable[[], float] = _time.time) -> None:
        self.svc, self.cfg, self.clock = svc, svc.config.event_calendar, clock
        self._http = http
        self._finnhub = finnhub
        self.state: dict[str, SourceState] = {k: SourceState() for k in
                                              ("bls", "fred", "fomc", "market", "top_cap", "earnings")}
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ── plumbing ─────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30, follow_redirects=True)
        return self._http

    def _fh(self) -> Any:
        if self._finnhub is None:
            from app.data.finnhub_fund import FinnhubFund, gate_of
            from app.settings import get_secrets

            self._finnhub = FinnhubFund(get_secrets().FINNHUB_API_KEY.get_secret_value(), gate_of(self.svc))
        return self._finnhub

    def _ttl(self, name: str) -> float:
        hours = {"bls": self.cfg.macro_ttl_hours, "fred": self.cfg.macro_ttl_hours, "fomc": 24 * 365,
                 "market": self.cfg.market_ttl_hours, "top_cap": self.cfg.macro_ttl_hours,
                 "earnings": self.cfg.earnings_ttl_hours}[name]
        return hours * 3600

    def _due(self, name: str) -> bool:
        st, now = self.state[name], self.clock()
        if now < st.retry_at:
            return False
        return st.updated is None or now - st.updated >= self._ttl(name)

    async def _run(self, name: str, fn: Callable[[], Awaitable[list[Event]]]) -> None:
        st = self.state[name]
        try:
            st.events, st.updated, st.error = await fn(), self.clock(), None
        except Exception as e:  # noqa: BLE001 - one source failing never breaks the others
            # own RuntimeErrors carry a short fixed code (never a URL: the FRED URL holds the key)
            code = getattr(e, "code", None) or (str(e) if type(e) is RuntimeError else type(e).__name__)
            st.error, st.retry_at = code, self.clock() + RETRY_S
            log.warning("events: %s failed (%s); keeping %d old events", name, st.error, len(st.events))

    def today_ny(self) -> date:
        return datetime.fromtimestamp(self.clock(), UTC).astimezone(NY).date()

    # ── sources ──────────────────────────────────────────────────────────

    async def _bls(self) -> list[Event]:
        r = await self._client().get(BLS_ICS_URL, headers=UA)
        if r.status_code != 200:
            raise RuntimeError(f"http-{r.status_code}")
        return model.parse_bls_ics(r.text)

    async def _fred(self) -> list[Event]:
        key = getattr(self.svc, "_fred_key", "")
        if not key:
            raise RuntimeError("no-fred-key")
        today = self.today_ny()
        out: list[Event] = []
        for rid in model.FRED_RELEASES:
            r = await self._client().get(FRED_RELEASE_DATES_URL, params={
                "api_key": key, "file_type": "json", "release_id": rid, "include_release_dates_with_no_data": "true",
                "realtime_start": today.isoformat(), "realtime_end": (today + timedelta(days=120)).isoformat(),
                "sort_order": "asc"})
            if r.status_code != 200:
                raise RuntimeError(f"http-{r.status_code}")          # never the URL (it holds the key)
            out += model.parse_fred_dates(r.json(), rid, today)
        return out

    async def _fomc(self) -> list[Event]:
        return model.load_fomc()

    async def _market(self) -> list[Event]:
        """Holidays and early closes on the coming weekdays (Toss calendar per US date)."""
        toss = getattr(self.svc, "toss", None)
        if toss is None:
            raise RuntimeError("no-toss-key")
        today, out, closed = self.today_ny(), [], set()
        for i in range(1, self.cfg.days + 1):
            d = today + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            cal = await toss.market_calendar(d)
            if cal.today.date != d:                    # answer is not about the day asked: don't guess
                continue
            reg = cal.today.regular_market
            if reg is None:
                closed.add(d)
                out.append(Event(f"mkt:closed:{d:%Y%m%d}", "market", "US market closed (holiday)",
                                 "미국 증시 휴장", d, None, "Toss market calendar", "high"))
            else:
                end = reg.end.astimezone(NY)
                if (end.hour, end.minute) < NORMAL_CLOSE_NY:
                    out.append(Event(f"mkt:early:{d:%Y%m%d}", "market", f"US market closes early ({end:%H:%M} ET)",
                                     f"미국 증시 조기 폐장 ({end:%H:%M} ET)", d, reg.end, "Toss market calendar", "high"))
        self.state["market"].extra["closed"] = sorted(closed)
        return out

    async def _top_cap(self) -> list[Event]:
        """No events: builds the top-N-by-market-cap list used for earnings (Toss sector lists, daily caps)."""
        n = self.cfg.top_market_cap
        toss = getattr(self.svc, "toss", None)
        if n <= 0:
            self.state["top_cap"].extra["symbols"] = []
            return []
        if toss is None:
            raise RuntimeError("no-toss-key")
        caps: dict[str, Any] = {}
        names: dict[str, str] = {}
        tops = [s for s in await toss.sectors() if s.depth == 0]
        for s in tops:
            for st in await toss.sector_stocks(s.sector_id, "US"):
                if st.market_cap is not None and st.symbol:
                    caps[st.symbol] = max(caps.get(st.symbol, 0), st.market_cap)
                    names[st.symbol] = str(getattr(st, "name", "") or "")
        ranked = top_companies(caps, names, n)
        self.state["top_cap"].extra.update(symbols=ranked, sectors=len(tops), stocks=len(caps))
        return []

    def watched(self) -> dict[str, str]:
        """Ticker -> why it is watched for earnings (first reason wins)."""
        out: dict[str, str] = {}

        def add(syms: Any, why: str) -> None:
            for s in syms or []:
                s = str(s).strip().upper()
                if s and s not in out and len(out) < self.cfg.max_symbols:
                    out[s] = why

        acct = getattr(self.svc, "account", None)
        try:
            add([h.symbol for h in acct.state.holdings.items], "holding")   # type: ignore[union-attr]
        except AttributeError:
            pass
        add(list(getattr(self.svc, "_chart_symbols", {}) or {}), "screen")
        add([self.svc.config.tickers.default], "screen")
        try:
            from app.watchlist import Watchlist

            add([s for f in Watchlist(self.svc).load().get("folders", []) for s in f.get("symbols", [])], "watchlist")
        except Exception:  # noqa: BLE001 - a broken watchlist file never stops the calendar
            pass
        add(self.cfg.big_semis, "big semiconductors")
        add(self.state["top_cap"].extra.get("symbols", []), f"top {self.cfg.top_market_cap} by market cap")
        return out

    async def _earnings(self) -> list[Event]:
        fh = self._fh()
        if not getattr(fh, "configured", True):
            raise RuntimeError("no-finnhub-key")
        today = self.today_ny()
        end = today + timedelta(days=self.cfg.earnings_days)
        out: list[Event] = []
        errors = 0
        for sym, why in self.watched().items():
            try:
                rows = await fh.earnings_calendar(sym, today, end)
            except Exception as e:  # noqa: BLE001 - one ticker failing doesn't drop the rest
                errors += 1
                if getattr(e, "status", None) == 429 or errors >= 5:
                    raise
                continue
            for row in rows:
                if row.date is not None and row.date >= today:
                    imp = "high" if why in ("holding", "screen") else "medium"
                    out.append(model.earnings_event(sym, row.date, row.hour, imp, why))
        return out

    # ── refresh / views ──────────────────────────────────────────────────

    async def refresh(self) -> None:
        async with self._lock:
            jobs = [(n, f) for n, f in (("bls", self._bls), ("fred", self._fred), ("fomc", self._fomc),
                                       ("market", self._market), ("top_cap", self._top_cap)) if self._due(n)]
            await asyncio.gather(*(self._run(n, f) for n, f in jobs))
            if self._due("earnings"):                  # after top_cap: the watched list needs it
                await self._run("earnings", self._earnings)

    def kick(self) -> None:
        """Start a background refresh when something is due (called from page requests; never waits)."""
        if not self.cfg.enabled or (self._task is not None and not self._task.done()):
            return
        if any(self._due(n) for n in self.state):
            try:
                self._task = asyncio.get_running_loop().create_task(self.refresh())
            except RuntimeError:                       # no running loop (sync caller in a test)
                pass

    def _user_events(self) -> list[Event]:
        out: list[Event] = []
        items: list[tuple[str, Any, str | None, str]] = [
            (e.name, e.time, e.note, "config.yaml") for e in getattr(self.svc.config, "events", [])]
        us = getattr(self.svc, "user_settings", None)
        for e in getattr(getattr(us, "events", None), "items", []) or []:
            items.append((e.name, e.time, e.note, "my events"))
        for i, (name, when, note, src) in enumerate(items):
            if isinstance(when, datetime):
                at: datetime | None = when
            else:
                text = str(when).strip()
                at = None if len(text) == 10 else parse_iso_with_offset(text)
            day = at.astimezone(NY).date() if at else date.fromisoformat(str(when).strip())
            out.append(Event(f"user:{src}:{i}:{day:%Y%m%d}", "user", name, name, day, at, src, "high", note=note))
        return out

    def upcoming(self, days: int | None = None) -> list[Event]:
        now = datetime.fromtimestamp(self.clock(), UTC)
        end = now + timedelta(days=days or self.cfg.days)
        today = self.today_ny()
        closed = set(self.state["market"].extra.get("closed", []))
        pool = [e for st in self.state.values() for e in st.events]
        pool += model.opex_events(today, today + timedelta(days=(days or self.cfg.days) + 1), closed)
        try:
            pool += self._user_events()
        except (ValueError, TypeError) as e:
            log.warning("events: a user event could not be read (%s)", type(e).__name__)
        return model.merge(pool, now, end)

    def sources_view(self) -> list[dict[str, Any]]:
        out = []
        for name, st in self.state.items():
            out.append({"name": name, "ok": st.error is None and st.updated is not None, "error": st.error,
                        "updated_kst": None if st.updated is None else
                        datetime.fromtimestamp(st.updated, UTC).astimezone(KST).isoformat(),
                        "count": len(st.events), **({"symbols": len(st.extra.get("symbols", []))}
                                                    if name == "top_cap" else {})})
        return out

    def view(self, days: int | None = None) -> dict[str, Any]:
        self.kick()
        return {"events": [e.view() for e in self.upcoming(days)], "sources": self.sources_view(),
                "watched": self.watched(), "as_of_kst": datetime.fromtimestamp(self.clock(), UTC).astimezone(KST).isoformat(),
                "days": days or self.cfg.days}

    def next_event(self) -> dict[str, Any] | None:
        """For the top strip: the next upcoming event, plus how many there are in the window."""
        self.kick()
        ups = self.upcoming()
        if not ups:
            return None
        e = ups[0]
        return {"name": e.title, "name_ko": e.title_ko, "time": None if e.at is None else e.at.isoformat(),
                "date": e.day.isoformat(), "note": e.note, "kind": e.kind, "source": e.source,
                "importance": e.importance, "count": len(ups)}


def events_of(svc: Any) -> EventsService:
    ev = getattr(svc, "events_service", None)
    if ev is None:
        ev = EventsService(svc)
        svc.events_service = ev
    return ev
