"""Watchlist folders (phase 6 part A): file safety, limits, migration, routes, quotes. No network."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import InputError
from app.watchlist import MAX_FOLDERS, MAX_PER_FOLDER, WatchlistError, path_for, validate, watchlist_of
from tests.test_api import LOCAL, make_services, toss_handler


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def ready(tmp_path: Path, handler: Any = toss_handler) -> Any:
    svc = make_services(tmp_path, handler=handler)
    await svc.refresh_calendar()
    await svc.refresh_stocks()          # fake Toss list: AAPL, SOXS
    return svc


def ok_check(s: str) -> str:
    if s.upper() not in ("AAPL", "SOXS", "NVDA"):
        raise InputError(f"{s} is not in the Toss US stock list")
    return s.upper()


def test_validate_rules() -> None:
    doc = validate({"folders": [{"name": " Semis ", "symbols": ["nvda", "NVDA", "aapl"]},
                                {"name": "Later", "symbols": []}]}, ok_check)
    a, b = doc["folders"]
    assert (a["id"], a["name"], a["symbols"], a["pinned"]) == ("semis", "Semis", ["NVDA", "AAPL"], True)
    assert b["id"] == "later" and b["pinned"] is False                    # first folder pinned when none is
    two = validate({"folders": [{"name": "A"}, {"name": "A b", "id": "a"}, {"name": "c", "pinned": True}]}, ok_check)
    assert [f["id"] for f in two["folders"]] == ["a-2", "a", "c"]          # an existing id is never taken over
    assert [f["pinned"] for f in two["folders"]] == [False, False, True]
    dup = validate({"folders": [{"name": "x", "id": "k"}, {"name": "y", "id": "k"}]}, ok_check)
    assert [f["id"] for f in dup["folders"]] == ["k", "y"]
    bad = [
        {"folders": []},
        {"folders": [{"name": f"f{i}"} for i in range(MAX_FOLDERS + 1)]},
        {"folders": [{"name": "x", "symbols": ["AAPL"] * 1 + ["NVDA"] * 0 + ["ZZZZ"]}]},       # unknown ticker
        {"folders": [{"name": "x"}, {"name": "X"}]},                                         # same name
        {"folders": [{"name": "x", "pinned": True}, {"name": "y", "pinned": True}]},
        {"folders": [{"name": ""}]},
        {"folders": [{"name": "y" * 41}]},
        {"folders": [{"name": "x", "id": "Bad Id"}]},
        {"folders": [{"name": "x", "symbols": "AAPL"}]},
        {"folders": "nope"},
    ]
    for d in bad:
        with pytest.raises(InputError):
            validate(d, ok_check)


def test_per_folder_limit() -> None:
    many = [f"T{i}" for i in range(MAX_PER_FOLDER + 1)]
    with pytest.raises(InputError, match="at most"):
        validate({"folders": [{"name": "big", "symbols": many}]}, lambda s: s.upper())
    assert len(validate({"folders": [{"name": "ok", "symbols": many[:MAX_PER_FOLDER]}]},
                        lambda s: s.upper())["folders"][0]["symbols"]) == MAX_PER_FOLDER


@pytest.mark.anyio
async def test_first_run_migration_writes_pinned_favorites(tmp_path: Path) -> None:
    svc = await ready(tmp_path)
    w = watchlist_of(svc)
    assert not path_for(svc).exists()
    doc = w.load()
    fav = doc["folders"][0]
    assert (fav["id"], fav["name"], fav["pinned"]) == ("favorites", "Favorites", True)
    assert fav["symbols"][0] == "SOXS" and "SOXX" in fav["symbols"]       # default + compare from config
    assert path_for(svc).exists() and json.loads(path_for(svc).read_text(encoding="utf-8"))["folders"][0]["id"] == "favorites"
    assert w.pinned_symbols() == fav["symbols"]


@pytest.mark.anyio
async def test_bom_read_and_broken_file_never_overwritten(tmp_path: Path) -> None:
    svc = await ready(tmp_path)
    p = path_for(svc)
    p.write_text('﻿{"version": 1, "folders": [{"id": "a", "name": "A", "pinned": true, "symbols": ["AAPL"]}]}',
                 encoding="utf-8")
    assert watchlist_of(svc).load()["folders"][0]["symbols"] == ["AAPL"]   # BOM is fine
    p.write_text('{"folders": [ broken', encoding="utf-8")
    with pytest.raises(WatchlistError, match="not valid JSON"):
        watchlist_of(svc).load()
    with pytest.raises(WatchlistError):
        watchlist_of(svc).save({"folders": [{"name": "new", "symbols": ["AAPL"]}]})
    assert p.read_text(encoding="utf-8") == '{"folders": [ broken'        # untouched
    assert watchlist_of(svc).pinned_symbols() == []
    p.write_text('["not", "an", "object"]', encoding="utf-8")
    with pytest.raises(WatchlistError, match="unexpected shape"):
        watchlist_of(svc).load()


@pytest.mark.anyio
async def test_save_replaces_and_checks_toss_list(tmp_path: Path) -> None:
    svc = await ready(tmp_path)
    w = watchlist_of(svc)
    saved = w.save({"folders": [{"name": "Semis", "symbols": ["soxs"]}, {"name": "Big", "pinned": True,
                                                                          "symbols": ["AAPL"]}]})
    assert [f["id"] for f in saved["folders"]] == ["semis", "big"] and w.pinned_symbols() == ["AAPL"]
    assert w.load()["folders"][1]["pinned"] is True and saved["updated"].endswith("+09:00")
    with pytest.raises(InputError, match="not in the Toss US stock list"):
        w.save({"folders": [{"name": "x", "symbols": ["NOTREAL"]}]})
    assert w.load()["folders"][0]["id"] == "semis"                        # failed save changed nothing


@pytest.mark.anyio
async def test_quotes_change_calculated_in_code(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/oauth2/token":
            calls.append(request.url.path)
        return toss_handler(request)

    svc = await ready(tmp_path, handler)
    w = watchlist_of(svc)
    w.save({"folders": [{"name": "Mine", "symbols": ["SOXS", "AAPL"]}]})
    out = await w.quotes("mine")
    soxs = out["items"][0]
    prev_day = svc.calendar.previous_business_day.date
    prev = svc.prev_close[("SOXS", prev_day)]
    assert soxs["last_price"] == Decimal("4.25") and soxs["prev_close"] == prev
    if prev is not None:
        assert soxs["change"] == Decimal("4.25") - prev
        assert soxs["change_pct"] == ((Decimal("4.25") - prev) / prev * 100).quantize(Decimal("0.01"))
    assert out["meta"]["source"] == "toss" and out["prev_close_pending"] == 0
    n_prices = calls.count("/api/v1/prices")
    await w.quotes("mine")                                               # within the poll window: cached
    assert calls.count("/api/v1/prices") == n_prices
    with pytest.raises(InputError):
        await w.quotes("nope")


@pytest.mark.anyio
async def test_quotes_prev_close_fetch_is_capped_and_reported(tmp_path: Path) -> None:
    import app.watchlist as wl

    svc = await ready(tmp_path)
    w = watchlist_of(svc)
    w.save({"folders": [{"name": "Mine", "symbols": ["SOXS", "AAPL"]}]})
    old = wl.PREV_CLOSE_PER_REQUEST
    wl.PREV_CLOSE_PER_REQUEST = 1
    try:
        first = await w.quotes("mine")
        assert first["prev_close_pending"] == 1                          # the rest come on the next request
        second = await w.quotes("mine")
        assert second["prev_close_pending"] == 0
    finally:
        wl.PREV_CLOSE_PER_REQUEST = old


def test_routes(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        first = c.get("/api/watchlist")
        assert first.status_code == 200 and first.json()["folders"][0]["pinned"] is True
        r = c.put("/api/watchlist", json={"folders": [{"name": "Semis", "symbols": ["SOXS"]}]})
        assert r.status_code == 200 and r.json()["folders"][0]["id"] == "semis"
        assert c.put("/api/watchlist", content="folders=x",
                     headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 415
        assert c.put("/api/watchlist", json={"folders": []}).status_code == 400
        assert c.get("/api/watchlist/quotes?folder=semis").status_code == 200
        assert c.get("/api/watchlist/quotes?folder=missing").status_code == 400
        path_for(svc).write_text("{bad", encoding="utf-8")
        assert c.get("/api/watchlist").status_code == 422
        assert c.put("/api/watchlist", json={"folders": [{"name": "x"}]}).status_code == 409
        assert path_for(svc).read_text(encoding="utf-8") == "{bad"


@pytest.mark.anyio
async def test_hand_edited_bad_folder_is_a_readable_error(tmp_path: Path) -> None:
    svc = await ready(tmp_path)
    p = path_for(svc)
    for bad in ('{"folders": ["AAPL"]}', '{"folders": [{"name": "a", "symbols": "AAPL"}]}',
                '{"folders": [{"name": "a", "symbols": ["NOT A TICKER"]}]}'):
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(WatchlistError, match="the file was not changed"):
            watchlist_of(svc).load()
        assert watchlist_of(svc).pinned_symbols() == []
        assert p.read_text(encoding="utf-8") == bad
    p.write_text('{"folders": [{"name": "Hand made", "symbols": ["aapl"]}]}', encoding="utf-8")
    doc = watchlist_of(svc).load()                                      # no ids / pin in the file: filled in
    assert doc["folders"][0]["id"] == "hand-made" and doc["folders"][0]["pinned"] is True


@pytest.mark.anyio
async def test_rename_works_while_toss_list_is_loading(tmp_path: Path) -> None:
    svc = await ready(tmp_path)
    w = watchlist_of(svc)
    w.save({"folders": [{"name": "Mine", "symbols": ["AAPL"]}]})
    svc.stocks = {}                                                     # e.g. right after a restart
    renamed = w.save({"folders": [{"id": "mine", "name": "Renamed", "symbols": ["AAPL"]}]})
    assert renamed["folders"][0]["name"] == "Renamed"
    with pytest.raises(InputError):
        w.save({"folders": [{"id": "mine", "name": "Renamed", "symbols": ["AAPL", "NVDA"]}]})  # new ticker: checked


@pytest.mark.anyio
async def test_failed_prev_close_waits_before_retry(tmp_path: Path) -> None:
    candle_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/candles" and request.url.params.get("interval") == "1d":
            candle_calls.append(request.url.params["symbol"])
            return httpx.Response(500, json={"error": {"code": "internal-error", "message": "x"}})
        return toss_handler(request)

    svc = await ready(tmp_path, handler)
    w = watchlist_of(svc)
    w.save({"folders": [{"name": "Mine", "symbols": ["SOXS", "AAPL"]}]})
    candle_calls.clear()
    first = await w.quotes("mine")
    second = await w.quotes("mine")
    assert len(candle_calls) == 1                                       # stopped at the first failure, then waited
    assert first["prev_close_pending"] == second["prev_close_pending"] == 2
    assert first["items"][0]["change"] is None and first["items"][0]["last_price"] == Decimal("4.25")


@pytest.mark.anyio
async def test_pinned_folder_feeds_strip_and_news(tmp_path: Path) -> None:
    from app.market_routes import watched_symbols
    from app.services import STRIP_MAX

    svc = await ready(tmp_path)
    w = watchlist_of(svc)
    svc.pinned_provider = w.pinned_symbols
    assert [i["key"] for i in svc.strip_view()["items"] if i.get("source") == "Toss"] == ["SOXS", "SOXX"]  # first run
    w.save({"folders": [{"name": "Mine", "symbols": ["AAPL"]}]})
    keys = [i["key"] for i in svc.strip_view()["items"] if i.get("source") == "Toss"]
    assert keys == ["AAPL"]                                            # pinned folder replaces the fixed slots
    assert "AAPL" in svc.quote_symbols() and "AAPL" in watched_symbols(svc)
    many = [f"S{i}" for i in range(STRIP_MAX + 3)]
    svc.pinned_provider = lambda: many
    assert svc.strip_symbols() == many[:STRIP_MAX]
    svc.pinned_provider = lambda: []                                   # empty pinned folder: config strip
    assert svc.strip_symbols() == [s.upper() for s in svc.config.tickers.strip]

    def broken() -> list[str]:
        raise RuntimeError("x")

    svc.pinned_provider = broken
    assert svc.strip_symbols() == [s.upper() for s in svc.config.tickers.strip]


@pytest.mark.anyio
async def test_pinned_symbols_reread_only_when_file_changes(tmp_path: Path) -> None:
    import os

    svc = await ready(tmp_path)
    w = watchlist_of(svc)
    w.save({"folders": [{"name": "Mine", "symbols": ["AAPL"]}]})
    assert w.pinned_symbols() == ["AAPL"]
    reads = []
    real = w.load
    w.load = lambda: (reads.append(1), real())[1]                      # type: ignore[method-assign]
    assert w.pinned_symbols() == ["AAPL"] and reads == []             # unchanged file: cached
    w.save({"folders": [{"name": "Mine", "symbols": ["AAPL", "SOXS"]}]})
    st = path_for(svc).stat()
    os.utime(path_for(svc), ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert w.pinned_symbols() == ["AAPL", "SOXS"]
