"""Company view service + routes (phase 6 step 9a). Every source is faked: no network, no real keys."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.data.edgar import EdgarClient
from app.data.finnhub_fund import FinnhubFund, FinnhubGate
from app.data.toss import ListedStock
from app.data.yf_fund import YFFund
from app.main import create_app
from app.research.sector_index import SectorIndex
from app.research.service import ResearchService
from tests.test_api import LOCAL, make_services, toss_handler

FIX = Path(__file__).parent / "fixtures"
D = Decimal


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def info(sym: str, sec_type: str = "STOCK", shares: str = "15000000000", cur: str = "USD") -> dict[str, Any]:
    return {"symbol": sym, "name": f"{sym}-ko", "englishName": f"{sym} INC", "isinCode": "X", "market": "NASDAQ",
            "securityType": sec_type, "isCommonShare": True, "status": "ACTIVE", "currency": cur,
            "listDate": "2000-01-01", "delistDate": None, "sharesOutstanding": shares,
            "leverageFactor": "-3" if sec_type == "ETF" else None, "koreanMarketDetail": None}


INFOS = {"AAPL": info("AAPL"), "NVDA": info("NVDA", shares="24000000000"), "SOXS": info("SOXS", "ETF", "50000000"),
         "TSM": info("TSM", "DEPOSITARY_RECEIPT", "5186474000"), "ZZZ": info("ZZZ", shares="1000")}
SECTOR_MEMBERS = {"100": ["NVDA", "AAPL", "TSM", "ZZZ"], "103": ["AAPL"]}


class Toss:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, r: httpx.Request) -> httpx.Response:
        p = r.url.path
        if p != "/oauth2/token":
            self.calls.append(p)
        if p == "/api/v1/stocks":
            return httpx.Response(200, json={"result": [INFOS[s] for s in r.url.params["symbols"].split(",") if s in INFOS]})
        if p == "/api/v1/sectors":
            return httpx.Response(200, json=load("sectors.json"))
        if p.startswith("/api/v1/sectors/") and p.endswith("/stocks"):
            sid = p.split("/")[4]
            return httpx.Response(200, json={"result": {"stocks": [
                {"symbol": s, "name": s, "volume": None, "tradingAmount": None, "marketCapitalization": None,
                 "currency": "KRW"} for s in SECTOR_MEMBERS.get(sid, [])]}})
        return toss_handler(r)


def tsm_facts() -> dict[str, Any]:
    rows = [{"start": f"{y}-01-01", "end": f"{y}-12-31", "val": v, "accn": f"t{y}", "form": "20-F", "fp": "FY",
             "fy": y, "filed": f"{y + 1}-04-15"} for y, v in ((2023, 2161735800000), (2024, 2894307700000))]
    ni = [{**r, "val": r["val"] // 2} for r in rows]
    return {"cik": 1046179, "entityName": "TSMC", "facts": {"ifrs-full": {
        "Revenue": {"units": {"TWD": rows}}, "ProfitLossAttributableToOwnersOfParent": {"units": {"TWD": ni}}}}}


def sec_handler(r: httpx.Request) -> httpx.Response:
    if r.url.host == "www.sec.gov":
        return httpx.Response(200, json={"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
                                         "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA"},
                                         "2": {"cik_str": 1046179, "ticker": "TSM", "title": "TSMC"}})
    if r.url.path.endswith("CIK0001046179.json"):
        return httpx.Response(200, json=tsm_facts())
    return httpx.Response(200, content=(FIX / "edgar_aapl_trimmed.json").read_bytes())


FH = {"/api/v1/stock/profile2": "finnhub_profile2.json", "/api/v1/stock/earnings": "finnhub_earnings.json",
      "/api/v1/calendar/earnings": "finnhub_calendar.json", "/api/v1/stock/recommendation": "finnhub_recommendation.json"}


class Finnhub:
    def __init__(self) -> None:
        self.fail = False

    def __call__(self, r: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(500)
        if r.url.path == "/api/v1/company-news":
            return httpx.Response(200, json=[{"category": "company", "datetime": 1795000000, "headline": "Apple news",
                                              "id": 1, "image": "", "related": "AAPL", "source": "Wire",
                                              "summary": "s", "url": "https://example.com/a"}])
        return httpx.Response(200, content=(FIX / FH[r.url.path]).read_bytes())


class FakeTicker:
    def __init__(self, sym: str) -> None:
        self.sym = sym
        self.info = {"financialCurrency": "USD"}

    @property
    def analyst_price_targets(self) -> Any:
        return {"current": 4.3, "low": 3.0, "high": 6.0, "mean": 4.8, "median": 4.9} if self.sym != "NVDA" else {}

    def __getattr__(self, name: str) -> Any:
        if name in ("income_stmt", "cashflow", "balance_sheet"):
            cols = [pd.Timestamp("2025-12-31"), pd.Timestamp("2024-12-31")]
            if name == "income_stmt":
                return pd.DataFrame({cols[0]: [100.0, 10.0], cols[1]: [80.0, 8.0]}, index=["Total Revenue", "Net Income"])
            return pd.DataFrame()
        if name.startswith("quarterly_"):
            return pd.DataFrame()
        raise AttributeError(name)


async def setup(tmp_path: Path) -> tuple[Any, ResearchService, Toss, Finnhub]:
    t = Toss()
    svc = make_services(tmp_path, handler=t)
    await svc.refresh_calendar()
    await svc.refresh_stocks()
    for s in ("NVDA", "TSM", "ZZZ"):
        svc.stocks[s] = ListedStock(s, s, "STOCK", True, "X")
    fh = Finnhub()
    edgar = EdgarClient("Test Desk tester@example.com", tmp_path / "edgar",
                        http=httpx.AsyncClient(transport=httpx.MockTransport(sec_handler)), requests_per_second=1000)
    fund = FinnhubFund("fh-test-key", FinnhubGate(0.0), http=httpx.AsyncClient(transport=httpx.MockTransport(fh)))
    rs = ResearchService(svc, edgar=edgar, fund=fund, yf=YFFund(lambda s: FakeTicker(s)),
                         index=SectorIndex(svc, tmp_path / "sector_index.json", 86400, svc.clock))
    svc._research = rs
    return svc, rs, t, fh


@pytest.mark.anyio
async def test_financials_from_sec(tmp_path: Path) -> None:
    _, rs, _, _ = await setup(tmp_path)
    out = await rs.financials("AAPL", "annual")
    t = out["table"]
    assert out["meta"]["source"] == "SEC EDGAR" and out["meta"]["status"] == "ok" and t["currency"] == "USD"
    assert len(t["periods"]) <= 5 and t["periods"][0]["label"].startswith("FY2026, ended")
    rev = next(r for r in t["rows"] if r["id"] == "revenue")
    assert rev["cells"][t["periods"][0]["key"]]["value"] == D("478834000000")
    q = await rs.financials("AAPL", "quarterly")
    assert len(q["table"]["periods"]) == 8 and q["table"]["periods"][0]["key"] == "FY2026Q4"


@pytest.mark.anyio
async def test_metrics_pack_live_price_targets_and_statuses(tmp_path: Path) -> None:
    _, rs, _, _ = await setup(tmp_path)
    out = await rs.metrics("AAPL")
    pk = {v["id"]: v for v in out["pack"]["values"]}
    assert pk["mcap"]["value"] == D("4.25") * D("15000000000")           # Toss price x Toss shares
    assert pk["pe_ttm"]["display"].endswith("x") and pk["ttm.revenue"]["display"] == "USD 478.8B"
    assert pk["target_mean"]["value"] == D("4.8") and "unofficial" in pk["target_mean"]["source"]
    assert pk["pe_peer_median"]["value"] is None                         # peers not computed yet
    assert out["meta"]["statements"]["source"] == "SEC EDGAR" and out["meta"]["price"]["source"] == "Toss"
    assert len(pk) == len(out["pack"]["values"])                         # ids unique


@pytest.mark.anyio
async def test_etf_has_no_statements(tmp_path: Path) -> None:
    _, rs, _, _ = await setup(tmp_path)
    fin = await rs.financials("SOXS")
    assert fin["table"] is None and fin["meta"]["reason"] == "etf" and "ETF" in fin["meta"]["note"]
    assert (await rs.metrics("SOXS"))["pack"] is None
    ov = await rs.overview("SOXS")
    assert ov["kind"] == "etf" and ov["basics"]["leverage_factor"] == D("-3")


@pytest.mark.anyio
async def test_foreign_filer_no_multiples(tmp_path: Path) -> None:
    _, rs, _, _ = await setup(tmp_path)
    out = await rs.metrics("TSM")
    pk = {v["id"]: v for v in out["pack"]["values"]}
    assert out["pack"]["currency"] == "TWD" and "no verified exchange rate" in pk["pe_ttm"]["display"]
    assert pk["rev_yoy"]["value"] is not None
    ov = await rs.overview("TSM")
    assert ov["kind"] == "depositary_receipt" and "ADS" in ov["basics"]["shares_note"]


@pytest.mark.anyio
async def test_yfinance_fallback_when_sec_has_nothing(tmp_path: Path) -> None:
    _, rs, _, _ = await setup(tmp_path)
    fin = await rs.financials("ZZZ")
    assert fin["meta"]["fallback"] is True and "unofficial" in fin["meta"]["source"]
    rev = next(r for r in fin["table"]["rows"] if r["id"] == "revenue")
    assert rev["cells"]["FY2025"]["value"] == D("100.0") and rev["cells"]["FY2025"]["note"] == "unofficial"


@pytest.mark.anyio
async def test_overview_earnings_analysts(tmp_path: Path) -> None:
    _, rs, _, _ = await setup(tmp_path)
    ov = await rs.overview("AAPL")
    assert ov["profile"]["name"] == "Apple Inc" and ov["price"]["last"] == D("4.25") and ov["kind"] == "stock"
    assert ov["meta"]["profile"]["status"] == "ok"
    e = await rs.earnings("AAPL")
    assert e["surprises"][0]["actual"] == D("1.88") and any("non-GAAP" in n for n in e["notes"])
    assert e["sec_eps_history"] and e["sec_eps_history"][0]["currency"] == "USD"
    a = await rs.analysts("AAPL")
    assert a["ratings"][0]["strong_buy"] == 13 and a["targets"]["median"] == D("4.9")
    assert (await rs.analysts("NVDA"))["targets"] is None                 # no targets: unavailable, not 0


@pytest.mark.anyio
async def test_error_keeps_last_value_and_says_so(tmp_path: Path) -> None:
    svc, rs, _, fh = await setup(tmp_path)
    first = await rs.analysts("AAPL")
    assert first["ratings"]
    rs.memo.clock = lambda: svc.clock() + 10 ** 6                         # every cache entry expired
    fh.fail = True
    again = await rs.analysts("AAPL")
    assert again["ratings"] == first["ratings"] and again["meta"]["ratings"]["status"] == "error"
    assert "http-500" in again["meta"]["ratings"]["error_code"] and "fh-test-key" not in json.dumps(
        again["meta"], default=str)


@pytest.mark.anyio
async def test_sector_index_and_peers(tmp_path: Path) -> None:
    svc, rs, t, _ = await setup(tmp_path)
    first = await rs.peers("AAPL")                                        # starts the background build
    assert first["sector"] is None and first["sector_index"]["status"] in ("building", "ok")
    await rs.index._task
    assert rs.index.status == "ok" and rs.index.sector_for("AAPL").sector_id == "103"   # deepest leaf
    assert rs.index.sector_for("NVDA").sector_id == "100"
    out = await rs.peers("NVDA")
    assert [p["symbol"] for p in out["peers"]] == ["AAPL", "TSM"]         # self and non-SEC ZZZ excluded
    assert out["note"] == "only 2 of 5 peers available in this sector"
    med = {m["metric_id"]: m for m in out["medians"]}
    assert med["pe_ttm"]["count"] == 1 and med["pe_ttm"]["of"] == 2      # TSM (TWD) has no P/E
    assert (tmp_path / "sector_index.json").exists()
    reloaded = SectorIndex(svc, tmp_path / "sector_index.json", 86400, svc.clock)
    assert reloaded.status == "ok" and reloaded.sector_for("AAPL").sector_id == "103"    # no rebuild after restart
    pk = {v["id"]: v for v in (await rs.metrics("NVDA"))["pack"]["values"]}
    assert pk["pe_peer_median"]["value"] is not None                     # cached peer median now used


@pytest.mark.anyio
async def test_news_longer_window(tmp_path: Path) -> None:
    _, rs, _, _ = await setup(tmp_path)
    out = await rs.news("AAPL", 30)
    assert out["days"] == 30 and out["items"][0]["headline"] == "Apple news" and out["meta"]["status"] == "ok"
    assert (await rs.news("AAPL", 999))["days"] == 30                    # capped by config


def test_routes(tmp_path: Path) -> None:
    import asyncio

    svc, rs, _, _ = asyncio.run(setup(tmp_path))
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        svc._research = rs
        r = c.get("/api/research/AAPL/financials?freq=annual")
        assert r.status_code == 200 and r.json()["table"]["currency"] == "USD"
        assert isinstance(r.json()["table"]["rows"][0]["cells"], dict)
        assert c.get("/api/research/AAPL/financials?freq=weekly").status_code == 400
        assert c.get("/api/research/NOTREAL/overview").status_code == 400
        m = c.get("/api/research/AAPL/metrics").json()
        assert isinstance(m["pack"]["values"][0]["value"], (str, type(None)))      # exact decimals as text
        for path in ("overview", "earnings", "analysts", "peers", "news"):
            assert c.get(f"/api/research/AAPL/{path}").status_code == 200, path


@pytest.mark.anyio
async def test_sec_error_is_shown_not_swapped_for_yfinance(tmp_path: Path) -> None:
    _, rs, _, _ = await setup(tmp_path)

    def down(r: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    rs.edgar._http = httpx.AsyncClient(transport=httpx.MockTransport(down))
    fin = await rs.financials("AAPL")
    assert fin["table"] is None and fin["meta"]["status"] == "error" and fin["meta"]["reason"] == "sec-error"
    assert "fallback" not in fin["meta"]                                  # no unofficial figures for a SEC filer


@pytest.mark.anyio
async def test_etf_detected_from_stock_list_when_basics_miss(tmp_path: Path) -> None:
    svc, rs, _, _ = await setup(tmp_path)
    saved = INFOS.pop("SOXS")
    try:
        svc.stocks["SOXS"] = ListedStock("SOXS", "SOXS", "ETF", False, "X")
        assert (await rs.financials("SOXS"))["meta"]["reason"] == "etf"
        assert (await rs.overview("SOXS"))["kind"] == "etf"
    finally:
        INFOS["SOXS"] = saved


def test_yf_fallback_one_column_per_year() -> None:
    from datetime import date

    from app.data.yf_fund import YFStatements
    from app.research.service import from_yf

    st = from_yf(YFStatements({"revenue": {date(2024, 1, 31): D("1"), date(2024, 12, 31): D("2")}}, "USD"), None)
    assert [p.key for p in st.annual] == ["FY2024"] and st.rows["revenue"]["FY2024"].value == D("2")
    assert st.rows["revenue"]["TTM"].value == D("2")


@pytest.mark.anyio
async def test_peers_one_company_per_cik_and_stale_median_dropped(tmp_path: Path) -> None:
    svc, rs, _, _ = await setup(tmp_path)
    SECTOR_MEMBERS["100"] = ["NVDA", "AAPL", "AAPL.B", "TSM"]
    INFOS["AAPL.B"] = info("AAPL.B")
    svc.stocks["AAPL.B"] = ListedStock("AAPL.B", "AAPL.B", "STOCK", True, "X")
    orig = sec_handler

    def tickers_with_class(r: httpx.Request) -> httpx.Response:
        if r.url.host == "www.sec.gov":
            return httpx.Response(200, json={"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
                                             "1": {"cik_str": 320193, "ticker": "AAPL-B", "title": "Apple"},
                                             "2": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA"},
                                             "3": {"cik_str": 1046179, "ticker": "TSM", "title": "TSMC"}})
        return orig(r)

    rs.edgar._http = httpx.AsyncClient(transport=httpx.MockTransport(tickers_with_class))
    try:
        rs.index.ensure()
        await rs.index._task
        out = await rs.peers("NVDA")
        assert [p["symbol"] for p in out["peers"]] == ["AAPL", "TSM"]   # AAPL.B is the same company as AAPL
        assert rs._cached_peer_median("NVDA")[0] is not None
        later = svc.clock() + 10 ** 6                                    # peers entry now older than its TTL
        rs.memo.clock = rs.svc.clock = lambda: later
        assert rs._cached_peer_median("NVDA") == (None, None)
    finally:
        SECTOR_MEMBERS["100"] = ["NVDA", "AAPL", "TSM", "ZZZ"]
        INFOS.pop("AAPL.B", None)


@pytest.mark.anyio
async def test_sector_index_skips_a_failing_sector_and_throttles_failed_builds(tmp_path: Path) -> None:
    svc, rs, t, _ = await setup(tmp_path)
    real = t.__call__

    def flaky(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/api/v1/sectors/103/stocks":
            return httpx.Response(500, json={"error": {"code": "internal-error", "message": "x"}})
        return real(r)

    svc.toss._http = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    rs.index.ensure()
    await rs.index._task
    assert rs.index.status == "ok" and rs.index.skipped == 1 and rs.index.sector_for("NVDA").sector_id == "100"

    def broken(r: httpx.Request) -> httpx.Response:
        return real(r) if r.url.path == "/oauth2/token" else httpx.Response(500, json={"error": {"code": "x"}})

    idx = SectorIndex(svc, tmp_path / "other.json", 86400, svc.clock)
    svc.toss._http = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    idx.ensure()
    await idx._task
    assert idx.status == "error"
    first = idx._task
    idx.ensure()
    assert idx._task is first                                             # no immediate rebuild after a failure


@pytest.mark.anyio
async def test_sector_index_without_toss_keys_shows_error_and_waits(tmp_path: Path) -> None:
    class NoToss:
        toss = None
        clock = staticmethod(lambda: 1000.0)

    idx = SectorIndex(NoToss(), tmp_path / "none.json", 86400, NoToss.clock)
    idx.ensure()
    await idx._task
    assert idx.status == "error" and idx.error == "Toss keys not set in .env"
    first = idx._task
    idx.ensure()                                                          # within 10 min: stays "error", no rebuild
    assert idx._task is first and idx.status == "error"
