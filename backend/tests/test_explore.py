"""Explore views and routes (phase 6 part A): fake Toss with the spec 1.2.28 examples, no network."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.explore.views import ExploreService, explore_of, pct
from app.main import create_app
from app.services import InputError
from tests.test_api import LOCAL, NOW, make_services, toss_handler

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Toss:
    """toss_handler + phase 6 paths; counts calls per path and can fail on demand."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path != "/oauth2/token":
            self.calls.append(path)
        if path in self.fail:
            return httpx.Response(self.fail[path], json={"error": {"code": "internal-error", "message": "x"}})
        if path == "/api/v1/rankings":
            return httpx.Response(200, json=load("rankings_us_toss_volume.json"))
        if path == "/api/v1/stocks":
            rows = []
            for s in request.url.params["symbols"].split(","):
                base = load("stock_infos_multiple.json")["result"][1]
                rows.append({**base, "symbol": s, "englishName": f"{s} INC", "name": f"{s}-ko"})
            return httpx.Response(200, json={"result": rows})
        if path == "/api/v1/sectors":
            return httpx.Response(200, json=load("sectors.json"))
        if path == "/api/v1/sectors/rankings":
            return httpx.Response(200, json=load("sector_rankings_top_gainers.json"))
        if path == "/api/v1/sectors/100":
            return httpx.Response(200, json=load("sector_detail.json"))
        if path == "/api/v1/sectors/100/stocks":
            return httpx.Response(200, json=load("sector_stocks_us.json"))
        if path == "/api/v1/sectors/100/etfs":
            return httpx.Response(200, json={"result": {"etfs": [{"symbol": "SOXX", "name": "iShares Semi"}]}})
        if path.startswith("/api/v1/sectors/"):
            return httpx.Response(404, json=load("sector_not_found.json"))
        return toss_handler(request)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def setup(tmp_path: Path, clock: Any = None) -> tuple[ExploreService, Toss, dict[str, float]]:
    t = Toss()
    now = {"t": NOW}
    svc = make_services(tmp_path, handler=t, clock=clock or (lambda: now["t"]))
    return explore_of(svc), t, now


def test_pct_is_code_calculated() -> None:
    assert pct(Decimal("0.0228")) == "2.28" and pct(Decimal("-0.0107")) == "-1.07"
    assert pct(Decimal("0.00125")) == "0.13" and pct(None) is None      # half-up


@pytest.mark.anyio
async def test_rankings_joined_with_names_and_cached(tmp_path: Path) -> None:
    ex, t, now = setup(tmp_path)
    out = await ex.rankings("TOSS_SECURITIES_TRADING_VOLUME", "1d", 20)
    nv = out["rows"][0]
    assert (nv["rank"], nv["symbol"], nv["name"], nv["english_name"]) == (1, "NVDA", "NVDA-ko", "NVDA INC")
    assert nv["last_price"] == Decimal("131.38") and nv["change_pct"] == "2.28" and nv["currency"] == "USD"
    assert out["volume_counts"] == "Toss executions only" and out["base_price_means"] == "previous close"
    assert out["meta"]["status"] == "ok" and out["meta"]["source"] == "toss"
    assert t.calls.count("/api/v1/rankings") == 1 and t.calls.count("/api/v1/stocks") == 1
    await ex.rankings("TOSS_SECURITIES_TRADING_VOLUME", "1d", 20)          # within 10 s: cached
    assert t.calls.count("/api/v1/rankings") == 1 and t.calls.count("/api/v1/stocks") == 1
    now["t"] += 11
    await ex.rankings("TOSS_SECURITIES_TRADING_VOLUME", "1d", 20)          # TTL over: refetched; names still cached
    assert t.calls.count("/api/v1/rankings") == 2 and t.calls.count("/api/v1/stocks") == 1
    gain = await ex.rankings("TOP_GAINERS", "1w", 20)
    assert gain["base_price_means"] == "duration start" and gain["volume_counts"] == "whole market"


@pytest.mark.anyio
async def test_rankings_input_checked_before_toss(tmp_path: Path) -> None:
    ex, t, _ = setup(tmp_path)
    for args in (("TOP_GAINERS", "realtime", 10), ("NOPE", "1d", 10), ("TOP_LOSERS", "2y", 10),
                 ("MARKET_TRADING_VOLUME", "1d", 0), ("MARKET_TRADING_VOLUME", "1d", 101)):
        with pytest.raises(InputError):
            await ex.rankings(*args)
    assert t.calls == []


@pytest.mark.anyio
async def test_error_keeps_last_good_value(tmp_path: Path) -> None:
    ex, t, now = setup(tmp_path)
    first = await ex.rankings("MARKET_TRADING_VOLUME", "1d", 10)
    assert first["rows"]
    now["t"] += 11
    t.fail["/api/v1/rankings"] = 500
    again = await ex.rankings("MARKET_TRADING_VOLUME", "1d", 10)
    assert again["rows"] == first["rows"]                                  # last good rows still shown
    assert again["meta"]["status"] == "error" and again["meta"]["error_code"] == "internal-error"
    assert "http" not in (again["meta"]["error"] or "")


@pytest.mark.anyio
async def test_names_fall_back_to_stock_list(tmp_path: Path) -> None:
    ex, t, _ = setup(tmp_path)
    await ex.svc.refresh_stocks()
    t.fail["/api/v1/stocks"] = 500
    infos = await ex.names(["AAPL"])
    assert infos == {}
    assert ex._label("AAPL", infos)["name"] == "Apple"                    # Korean-list name from /stocks/all
    assert ex._label("ZZZZ", infos)["name"] is None


@pytest.mark.anyio
async def test_sectors_tree_and_rankings(tmp_path: Path) -> None:
    ex, t, now = setup(tmp_path)
    tree = await ex.sectors()
    by_id = {s["sector_id"]: s for s in tree["sectors"]}
    assert by_id["10"]["children"] == 2 and by_id["42"]["children"] == 1 and by_id["103"]["children"] == 0
    now["t"] += 3600
    await ex.sectors()
    assert t.calls.count("/api/v1/sectors") == 1                          # cached a day
    r = await ex.sector_rankings("TOP_GAINERS", "1d")
    assert r["rows"][0]["change_pct"] == "1.79" and r["rows"][0]["currency"] == "KRW"
    assert r["rows"][1]["trading_amount"] is None
    with pytest.raises(InputError):
        await ex.sector_rankings("TOP_LOSERS", "1d")


@pytest.mark.anyio
async def test_sector_detail_with_prices_for_top_n(tmp_path: Path) -> None:
    ex, t, _ = setup(tmp_path)
    out = await ex.sector("100")
    assert out["detail"]["name"] == "반도체" and out["detail"]["change_rates"][5]["change_pct"] == "35.42"
    assert out["detail"]["change_rates"][4]["change_pct"] is None
    nv = out["stocks"][0]
    assert nv["symbol"] == "NVDA" and nv["currency"] == "KRW" and nv["market_cap"] == Decimal("4703000000000000")
    assert nv["last_price"] == Decimal("4.25") and nv["price_currency"] == "USD"   # from /prices (fake)
    assert out["etfs"][0]["symbol"] == "SOXX" and out["etfs"][0]["last_price"] == Decimal("4.25")
    assert t.calls.count("/api/v1/prices") == 1
    for bad in ("rankings", "a/b", "x" * 41, "100\n"):
        with pytest.raises(InputError):
            await ex.sector(bad)


@pytest.mark.anyio
async def test_sector_prices_capped_and_said(tmp_path: Path) -> None:
    ex, t, _ = setup(tmp_path)
    ex.cfg = ex.cfg.model_copy(update={"sector_prices_top": 0})
    out = await ex.sector("100")
    assert out["stocks"][0]["last_price"] is None and "/api/v1/prices" not in t.calls
    assert any("top 0" in n for n in out["notes"])                       # the cap is stated, never silent


@pytest.mark.anyio
async def test_unknown_sector_is_an_error_status(tmp_path: Path) -> None:
    ex, _, _ = setup(tmp_path)
    out = await ex.sector("999")
    assert out["detail"] is None and out["meta"]["detail"]["error_code"] == "sector-not-found"


@pytest.mark.anyio
async def test_no_toss_keys(tmp_path: Path) -> None:
    svc = make_services(tmp_path, keys=False)
    out = await explore_of(svc).rankings("MARKET_TRADING_VOLUME", "1d", 10)
    assert out["rows"] == [] and out["meta"]["error_code"] == "no-key"


def test_routes(tmp_path: Path) -> None:
    t = Toss()
    svc = make_services(tmp_path, handler=t)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        r = c.get("/api/explore/rankings?type=TOP_GAINERS&duration=1d&count=5")
        assert r.status_code == 200 and r.json()["rows"][0]["last_price"] == "131.38"      # exact decimal text
        assert c.get("/api/explore/rankings?type=TOP_GAINERS&duration=realtime").status_code == 400
        assert c.get("/api/explore/sectors").json()["sectors"][0]["name"] == "IT 하드웨어"
        assert c.get("/api/explore/sectors/rankings?type=TOP_GAINERS&duration=1w").status_code == 200
        assert "/api/v1/sectors/rankings" in t.calls and "/api/v1/sectors/rankings/stocks" not in t.calls
        d = c.get("/api/explore/sectors/100").json()
        assert d["detail"]["sector_id"] == "100" and d["stocks"][0]["market_cap"] == "4703000000000000"
        assert c.get("/api/explore/sectors/bad.id").status_code == 400
    assert all(not p.startswith("/api/v1/orders") for p in t.calls)        # nothing order-related


@pytest.mark.anyio
async def test_failed_name_lookup_not_retried_on_every_view(tmp_path: Path) -> None:
    ex, t, now = setup(tmp_path)
    t.fail["/api/v1/stocks"] = 500
    for _ in range(5):
        await ex.rankings("MARKET_TRADING_VOLUME", "1d", 10)
    assert t.calls.count("/api/v1/stocks") == 1                         # one try, then wait
    del t.fail["/api/v1/stocks"]
    now["t"] += 301                                                     # NAMES_RETRY_SECONDS later
    out = await ex.rankings("MARKET_TRADING_VOLUME", "1d", 10)
    assert t.calls.count("/api/v1/stocks") == 2 and out["rows"][0]["english_name"] == "NVDA INC"


@pytest.mark.anyio
async def test_sector_prices_one_slot_per_sector(tmp_path: Path) -> None:
    ex, t, now = setup(tmp_path)
    await ex.sector("100")
    keys = [k for k in ex.cache.keys() if k.startswith("sector_prices")]
    assert keys == ["sector_prices:100"]
    out = await ex.sector("100")
    assert t.calls.count("/api/v1/prices") == 1                         # same list within TTL: cached
    assert out["stocks"][0]["market_cap_delay"] == "daily"


def test_sector_prices_top_bounded_by_one_prices_call() -> None:
    from pydantic import ValidationError

    from app.settings import ExploreConfig

    with pytest.raises(ValidationError):
        ExploreConfig(sector_prices_top=101)
