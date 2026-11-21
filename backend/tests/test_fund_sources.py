"""Finnhub fundamentals + shared gate, yfinance targets / fallback statements (phase 6 step 7). No network."""

from __future__ import annotations

import json
import math
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx
import pandas as pd
import pytest

from app.data.finnhub_fund import FinnhubError, FinnhubFund, FinnhubGate, parse_profile
from app.data.yf_fund import YFFund, YFFundError, parse_statement, parse_targets
from app.news.feed import NewsFeed, NewsStore
from app.settings import NewsConfig

FIX = Path(__file__).parent / "fixtures"
KEY = "fh-secret-key-123"
D = Decimal


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Clock:
    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def client(handler: Callable[[httpx.Request], httpx.Response], key: str = KEY,
           gate: FinnhubGate | None = None) -> tuple[FinnhubFund, list[httpx.Request], FinnhubGate]:
    seen: list[httpx.Request] = []
    clock = Clock()
    gate = gate or FinnhubGate(1.1, clock=clock, sleep=clock.sleep)

    def record(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return handler(r)

    return FinnhubFund(key, gate, http=httpx.AsyncClient(transport=httpx.MockTransport(record))), seen, gate


ROUTES = {"/api/v1/stock/profile2": "finnhub_profile2.json", "/api/v1/stock/earnings": "finnhub_earnings.json",
          "/api/v1/calendar/earnings": "finnhub_calendar.json", "/api/v1/stock/recommendation": "finnhub_recommendation.json"}


def ok(r: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=(FIX / ROUTES[r.url.path]).read_bytes())


@pytest.mark.anyio
async def test_parsed_exact_from_spec_examples() -> None:
    c, seen, _ = client(ok)
    p = await c.profile("AAPL")
    assert p is not None and (p.name, p.currency, p.ipo) == ("Apple Inc", "USD", date(1980, 12, 12))
    assert not hasattr(p, "market_cap")                                  # unit not in the spec: not used
    e = await c.earnings("AAPL")
    assert e[0].period == date(2023, 3, 31) and e[0].actual == D("1.88") and e[0].estimate == D("1.9744")
    assert e[0].surprise_pct == D("-4.7812") and e == sorted(e, key=lambda x: x.period, reverse=True)
    cal = await c.earnings_calendar("AAPL", date(2020, 1, 1), date(2020, 3, 1))
    assert [e.date for e in cal] == sorted(e.date for e in cal)          # oldest first (next one = first future)
    jan = next(e for e in cal if e.date == date(2020, 1, 28))
    assert jan.hour == "amc" and jan.revenue_actual == D("91819000000") and jan.eps_actual == D("4.99")
    rec = await c.recommendations("AAPL")
    assert (rec[0].period, rec[0].strong_buy, rec[0].buy, rec[0].hold) == ("2025-03-01", 13, 24, 7)
    q = {r.url.path: r for r in seen}["/api/v1/calendar/earnings"].url.params
    assert dict(q) == {"symbol": "AAPL", "from": "2020-01-01", "to": "2020-03-01"}


@pytest.mark.anyio
async def test_key_only_in_header_and_never_without_key() -> None:
    c, seen, _ = client(ok, key=" ")
    with pytest.raises(FinnhubError) as ei:
        await c.earnings("AAPL")
    assert ei.value.code == "no-key" and seen == []
    c, seen, _ = client(ok)
    await c.earnings("AAPL")
    r = seen[0]
    assert r.headers["x-finnhub-token"] == KEY and KEY not in str(r.url) and "token" not in r.url.params


@pytest.mark.anyio
async def test_errors_carry_no_key_or_url() -> None:
    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {r.url} with {r.headers['x-finnhub-token']}")

    c, _, _ = client(boom)
    with pytest.raises(FinnhubError) as ei:
        await c.profile("AAPL")
    text = f"{ei.value} {ei.value!r} {ei.value.__cause__} {ei.value.__context__}"
    assert ei.value.code == "transport" and KEY not in text and "finnhub.io" not in text
    for status, code in ((401, "not-allowed"), (403, "not-allowed"), (500, "http-500")):
        c, _, _ = client(lambda r, s=status: httpx.Response(s, json={"error": "x"}))
        with pytest.raises(FinnhubError) as ei:
            await c.recommendations("AAPL")
        assert ei.value.code == code
    c, _, _ = client(lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(FinnhubError) as ei:
        await c.earnings("AAPL")
    assert ei.value.code == "bad-json"
    c, _, _ = client(lambda r: httpx.Response(200, json={"not": "a list"}))
    with pytest.raises(FinnhubError) as ei:
        await c.earnings("AAPL")
    assert ei.value.code == "bad-shape"


def test_unknown_symbol_profile_is_none() -> None:
    assert parse_profile({}) is None
    with pytest.raises(FinnhubError):
        parse_profile([])


@pytest.mark.anyio
async def test_gate_spacing_and_429_pauses_everyone() -> None:
    clock = Clock()
    gate = FinnhubGate(1.1, cooldown_s=60, clock=clock, sleep=clock.sleep)
    c, seen, _ = client(ok, gate=gate)
    for _ in range(3):
        await c.earnings("AAPL")
    assert clock.sleeps == pytest.approx([1.1, 1.1])                     # spaced like the news feed
    c429, _, _ = client(lambda r: httpx.Response(429), gate=gate)
    with pytest.raises(FinnhubError) as ei:
        await c429.profile("AAPL")
    assert ei.value.code == "rate-limited" and gate.paused
    n = len(seen)
    with pytest.raises(FinnhubError) as ei:
        await c.earnings("AAPL")                                         # the other client waits too
    assert ei.value.code == "cooling-down" and len(seen) == n
    clock.t += 61
    await c.earnings("AAPL")
    assert len(seen) == n + 1


@pytest.mark.anyio
async def test_news_feed_shares_the_gate(tmp_path: Path) -> None:
    clock = Clock()
    clock.t = 1_000_000.0                                                # past the feed's poll interval
    gate = FinnhubGate(1.1, cooldown_s=60, clock=clock, sleep=clock.sleep)
    calls: list[str] = []

    def news(r: httpx.Request) -> httpx.Response:
        calls.append(r.url.params["symbol"])
        return httpx.Response(200, json=[])

    feed = NewsFeed(NewsConfig(), KEY, NewsStore(tmp_path / "n.sqlite"), watch=lambda: ["SOXS", "SOXX"],
                    http=httpx.AsyncClient(transport=httpx.MockTransport(news)), clock=clock, sleep=clock.sleep,
                    gate=gate)
    await feed.poll_once()
    assert calls == ["SOXS", "SOXX"] and clock.sleeps == pytest.approx([1.1])
    gate.hit_429()                                                       # e.g. a fundamentals call got 429
    clock.t += 1000                                                      # news poll interval passed...
    gate._paused_until = clock.t + 30                                   # ...but the app-wide pause still holds
    await feed.poll_once()
    assert calls == ["SOXS", "SOXX"]                                     # no news call while paused

    def limited(r: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    gate2 = FinnhubGate(1.1, cooldown_s=60, clock=clock, sleep=clock.sleep)
    feed2 = NewsFeed(NewsConfig(), KEY, NewsStore(tmp_path / "m.sqlite"), watch=lambda: ["SOXS"],
                     http=httpx.AsyncClient(transport=httpx.MockTransport(limited)), clock=clock, sleep=clock.sleep,
                     gate=gate2)
    await feed2.poll_once()
    assert gate2.paused                                                  # a news 429 pauses fundamentals too


# ── yfinance ─────────────────────────────────────────────────────────────

class FakeTicker:
    def __init__(self, targets: Any = None, raise_exc: Exception | None = None, frames: dict[str, Any] | None = None,
                 info: dict[str, Any] | None = None):
        self._t, self._e, self._f = targets, raise_exc, frames or {}
        self.info = info or {}

    @property
    def analyst_price_targets(self) -> Any:
        if self._e:
            raise self._e
        return self._t

    def __getattr__(self, name: str) -> Any:
        if name in ("income_stmt", "cashflow", "balance_sheet", "quarterly_income_stmt", "quarterly_cashflow",
                    "quarterly_balance_sheet"):
            if self._e:
                raise self._e
            return self._f.get(name, pd.DataFrame())
        raise AttributeError(name)


class YFRateLimitError(Exception):
    pass


def test_targets_parsing_rules() -> None:
    t = parse_targets({"current": 212.5, "low": 170.0, "high": 300.0, "mean": 245.13, "median": 250.0})
    assert t is not None and t.mean == D("245.13") and t.currency == "USD" and "unofficial" in t.source
    assert parse_targets({}) is None and parse_targets(None) is None
    assert parse_targets({"current": 212.5}) is None                     # price only: no targets
    nan = parse_targets({"current": 1.0, "low": math.nan, "high": 3.0, "mean": None, "median": "x"})
    assert nan is not None and nan.low is None and nan.high == D("3.0") and nan.median is None


@pytest.mark.anyio
async def test_targets_fetch_backoff_after_failure() -> None:
    clock = Clock()
    calls: list[str] = []

    def factory(sym: str) -> FakeTicker:
        calls.append(sym)
        return FakeTicker(raise_exc=YFRateLimitError("Too Many Requests at https://query2.finance.yahoo.com"))

    yf = YFFund(factory, clock=clock)
    with pytest.raises(YFFundError) as ei:
        await yf.price_targets("AAPL")
    assert ei.value.code == "rate-limited" and "yahoo" not in str(ei.value)
    with pytest.raises(YFFundError) as ei:
        await yf.price_targets("AAPL")
    assert ei.value.code == "backing-off" and calls == ["AAPL"]          # no second Yahoo call during backoff
    clock.t += 61
    yf._ticker = lambda s: FakeTicker(targets={"current": 1.0, "mean": 2.0})
    assert (await yf.price_targets("AAPL")).mean == D("2.0")


@pytest.mark.anyio
async def test_fallback_statements_from_dataframes() -> None:
    cols = [pd.Timestamp("2025-09-30"), pd.Timestamp("2024-09-30")]
    inc = pd.DataFrame({cols[0]: [1000.0, 7.5, math.nan], cols[1]: [900.0, 6.0, 1.0]},
                       index=["Total Revenue", "Diluted EPS", "Some Other Row"])
    bal = pd.DataFrame({cols[0]: [5000.0]}, index=["Total Assets"])
    yf = YFFund(lambda s: FakeTicker(frames={"income_stmt": inc, "balance_sheet": bal},
                                     info={"financialCurrency": "TWD"}))
    res = await yf.statements("XYZ")
    assert res.currency == "TWD" and res.source == "yfinance · unofficial"
    out = res.lines
    assert out["revenue"] == {date(2025, 9, 30): D("1000.0"), date(2024, 9, 30): D("900.0")}
    assert out["eps_diluted"][date(2025, 9, 30)] == D("7.5") and out["total_assets"][date(2025, 9, 30)] == D("5000.0")
    assert "operating_cash_flow" not in out                              # empty frame: unavailable, not 0
    assert parse_statement(pd.DataFrame(), {"Total Revenue": "revenue"}) == {}
    assert parse_statement(None, {"Total Revenue": "revenue"}) == {}


@pytest.mark.anyio
async def test_fallback_statements_unknown_currency_stays_unknown() -> None:
    yf = YFFund(lambda s: FakeTicker(frames={}, info={}))
    res = await yf.statements("XYZ")
    assert res.currency is None and res.lines == {}                      # never an assumed USD


@pytest.mark.anyio
async def test_queued_calls_stop_when_a_429_arrives() -> None:
    import asyncio

    clock = Clock()
    gate = FinnhubGate(1.1, cooldown_s=60, clock=clock, sleep=clock.sleep)
    sent: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        sent.append(r.url.params["symbol"])
        return httpx.Response(429) if len(sent) == 1 else ok(r)

    c, _, _ = client(handler, gate=gate)
    results = await asyncio.gather(c.earnings("A"), c.earnings("B"), c.recommendations("C"), return_exceptions=True)
    assert sent == ["A"]                                                  # B and C waited, then saw the pause
    assert [getattr(r, "code", None) for r in results] == ["rate-limited", "cooling-down", "cooling-down"]
