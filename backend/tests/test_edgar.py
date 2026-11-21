"""SEC EDGAR client (phase 6 step 5): no network; httpx MockTransport and a temp cache folder."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from app.data.edgar import EdgarClient, EdgarError, parse_tickers, sec_ticker

UA = "Trading Desk tester@example.com"
TICKERS = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
           "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
           "2": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
           "3": {"cik_str": "bad", "ticker": "X", "title": "skipped"}}
FACTS = {"cik": 320193, "entityName": "Apple Inc.", "facts": {"us-gaap": {"Revenues": {"units": {"USD": []}}}}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self) -> None:
        self.now = time.time()
        self.mono = 1000.0
        self.sleeps: list[float] = []

    def wall(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.mono += s


def make(tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response], ua: str = UA,
         clock: Clock | None = None) -> tuple[EdgarClient, list[httpx.Request], Clock]:
    seen: list[httpx.Request] = []
    clock = clock or Clock()

    def record(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    c = EdgarClient(ua, tmp_path / "edgar", http=httpx.AsyncClient(transport=httpx.MockTransport(record)),
                    clock=clock.wall, mono=clock.monotonic, sleep=clock.sleep)
    return c, seen, clock


def ok(req: httpx.Request) -> httpx.Response:
    if req.url.host == "www.sec.gov":
        return httpx.Response(200, json=TICKERS)
    return httpx.Response(200, json=FACTS)


def test_ticker_map_and_share_classes() -> None:
    m = parse_tickers(TICKERS)
    assert m["AAPL"].cik == 320193 and m["BRK-B"].title == "BERKSHIRE HATHAWAY INC" and "X" not in m
    assert sec_ticker("brk.b") == "BRK-B"
    for bad in ([], {}, {"0": {"cik_str": True, "ticker": "A"}}):
        with pytest.raises(EdgarError):
            parse_tickers(bad)


@pytest.mark.anyio
async def test_user_agent_required_and_sent(tmp_path: Path) -> None:
    c, seen, _ = make(tmp_path, ok, ua="  ")
    with pytest.raises(EdgarError) as ei:
        await c.tickers()
    assert ei.value.code == "no-user-agent" and seen == []                # never calls SEC without one
    c, seen, _ = make(tmp_path, ok)
    await c.tickers()
    r = seen[0]
    assert r.headers["user-agent"] == UA and "gzip" in r.headers["accept-encoding"]
    assert str(r.url) == "https://www.sec.gov/files/company_tickers.json"


@pytest.mark.anyio
async def test_facts_url_padded_cik_no_key_and_cached(tmp_path: Path) -> None:
    c, seen, clock = make(tmp_path, ok)
    entry = await c.cik_for("AAPL")
    assert entry is not None and entry.cik == 320193
    assert (await c.cik_for("BRK.B")).cik == 1067983 and await c.cik_for("ZZZZ") is None
    res = await c.company_facts(entry.cik)
    facts_req = seen[-1]
    assert str(facts_req.url) == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    assert not facts_req.url.params and "authorization" not in facts_req.headers   # no key of any kind
    assert res.data["entityName"] == "Apple Inc." and res.stale is False
    assert (tmp_path / "edgar" / "CIK0000320193.json").exists()
    n = len(seen)
    again = await c.company_facts(320193)                                # within 24 h: from disk
    assert len(seen) == n and again.data == res.data
    for bad in (0, -1, 10_000_000_000, True, "320193"):
        with pytest.raises(EdgarError):
            await c.company_facts(bad)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_stale_copy_on_error_after_ttl(tmp_path: Path) -> None:
    state = {"fail": False}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops") if state["fail"] else ok(req)

    c, seen, clock = make(tmp_path, handler)
    await c.company_facts(320193)
    p = tmp_path / "edgar" / "CIK0000320193.json"
    old = time.time() - 2 * 86400
    os.utime(p, (old, old))                                              # cache older than 24 h
    state["fail"] = True
    res = await c.company_facts(320193)
    assert res.stale is True and res.error_code == "http-500" and res.data["cik"] == 320193
    p.unlink()
    with pytest.raises(EdgarError) as ei:                                # nothing cached: the error comes through
        await c.company_facts(320193)
    assert ei.value.code == "http-500"


@pytest.mark.anyio
@pytest.mark.parametrize("status,code", [(403, "blocked"), (429, "rate-limited")])
async def test_block_pauses_all_sec_calls(tmp_path: Path, status: int, code: str) -> None:
    c, seen, clock = make(tmp_path, lambda r: httpx.Response(status, text="Undeclared Automated Tool"))
    with pytest.raises(EdgarError) as ei:
        await c.company_facts(320193)
    assert (ei.value.status, ei.value.code) == (status, code) and c.cooling_down
    n = len(seen)
    with pytest.raises(EdgarError) as ei:
        await c.tickers()
    assert ei.value.code == "cooling-down" and len(seen) == n            # no network while paused
    clock.mono += 601
    assert not c.cooling_down


@pytest.mark.anyio
async def test_bad_json_and_not_found_never_overwrite_cache(tmp_path: Path) -> None:
    state = {"mode": "ok"}

    def handler(req: httpx.Request) -> httpx.Response:
        if state["mode"] == "bad":
            return httpx.Response(200, content=b"<html>not json")
        if state["mode"] == "404":
            return httpx.Response(404, text="")
        return ok(req)

    c, _, _ = make(tmp_path, handler)
    await c.company_facts(320193)
    p = tmp_path / "edgar" / "CIK0000320193.json"
    good = p.read_bytes()
    old = time.time() - 2 * 86400
    os.utime(p, (old, old))
    state["mode"] = "bad"
    res = await c.company_facts(320193)
    assert res.stale and res.error_code == "bad-json" and p.read_bytes() == good
    state["mode"] = "404"
    with pytest.raises(EdgarError) as ei:
        await c.company_facts(1045810)                                   # no XBRL facts for this filer
    assert ei.value.code == "not-found"


@pytest.mark.anyio
async def test_rate_limit_spacing(tmp_path: Path) -> None:
    c, _, clock = make(tmp_path, ok)
    for cik in (1, 2, 3):
        await c.company_facts(cik)
    assert clock.sleeps and all(abs(s - 0.2) < 1e-9 for s in clock.sleeps)   # 5 requests/second


@pytest.mark.anyio
async def test_errors_never_contain_url_or_user_agent(tmp_path: Path) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {req.url} as {req.headers['user-agent']}")

    c, _, _ = make(tmp_path, boom)
    with pytest.raises(EdgarError) as ei:
        await c.company_facts(320193)
    text = f"{ei.value} {ei.value!r} {ei.value.__cause__} {ei.value.__context__}"
    assert ei.value.code == "transport" and "sec.gov" not in text and "tester@example.com" not in text


def test_only_two_url_shapes_allowed() -> None:
    from app.data.edgar import ALLOWED_URL

    assert ALLOWED_URL.match("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json")
    for bad in ("https://data.sec.gov/api/xbrl/companyfacts/CIK320193.json",
                "https://data.sec.gov/submissions/CIK0000320193.json",
                "http://www.sec.gov/files/company_tickers.json",
                "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json\n",
                "https://evil.example/files/company_tickers.json"):
        assert not ALLOWED_URL.match(bad)


@pytest.mark.anyio
async def test_broken_tickers_cache_falls_back_and_old_map_kept(tmp_path: Path) -> None:
    state = {"fail": False}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500) if state["fail"] else ok(req)

    c, seen, clock = make(tmp_path, handler)
    await c.tickers()
    clock.now += 2 * 86400
    state["fail"] = True
    m = await c.tickers()                                                # SEC down: the map in memory is kept
    assert "AAPL" in m
    (tmp_path / "edgar" / "company_tickers.json").write_text("{broken", encoding="utf-8")
    c2, _, _ = make(tmp_path, handler)
    with pytest.raises(EdgarError):
        await c2.tickers()                                               # broken file + SEC down: a clear error
    assert json.loads(json.dumps(TICKERS))  # fixture untouched


@pytest.mark.anyio
@pytest.mark.parametrize("ua", ["트레이딩 데스크 tester@example.com",
                                "Desk tester@example.com" + chr(13) + chr(10) + "X-Evil: 1"])
async def test_non_ascii_or_control_user_agent_rejected_without_leaking(tmp_path: Path, ua: str) -> None:
    c, seen, _ = make(tmp_path, ok)
    await c.company_facts(320193)                                        # a good copy on disk first
    p = tmp_path / "edgar" / "CIK0000320193.json"
    old = time.time() - 2 * 86400
    os.utime(p, (old, old))
    bad, seen2, _ = make(tmp_path, ok, ua=ua)
    res = await bad.company_facts(320193)                                # stale copy, not a crash
    assert res.stale and res.error_code == "bad-user-agent" and seen2 == []
    with pytest.raises(EdgarError) as ei:
        await bad.tickers()
    text = f"{ei.value} {ei.value!r} {ei.value.__cause__} {ei.value.__context__}"
    assert ei.value.code == "bad-user-agent" and "tester@example.com" not in text
