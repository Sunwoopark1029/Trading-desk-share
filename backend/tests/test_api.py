"""API tests: FastAPI TestClient with fake sources (httpx.MockTransport). No network."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.data.toss import TossClient
from app.data.yf_source import StripQuote
from app.main import create_app
from app.services import Services
from app.settings import AppConfig, Secrets

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://toss.test"
TOSS_ID = "toss-id-XYZ987"
TOSS_SECRET = "toss-secret-QWE654"
FRED_KEY = "fred-key-ABC123"
LOCAL = "http://127.0.0.1"
# 2026-03-25 23:30 KST = regular session of US date 2026-03-25 (fixture calendar)
NOW = datetime(2026, 3, 25, 14, 30, tzinfo=UTC).timestamp()
KST_OFFSET = "+09:00"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _iso_kst(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).astimezone(
        datetime.fromisoformat("2026-01-01T00:00:00+09:00").tzinfo).isoformat()


def _minute_bars() -> list[dict[str, str]]:
    """1m bars ending 17:01 .. 23:30 KST on 2026-03-25 (pre-market + regular), oldest first."""
    start = datetime.fromisoformat("2026-03-25T17:00:00+09:00").timestamp()
    out = []
    for i in range(1, 391):
        px = Decimal("100") + Decimal(i % 17) / 10
        out.append({"timestamp": _iso_kst(start + 60 * i), "openPrice": str(px),
                    "highPrice": str(px + Decimal("0.5")), "lowPrice": str(px - Decimal("0.5")),
                    "closePrice": str(px + Decimal("0.1")), "volume": "100", "currency": "USD"})
    return out


DAILY = [{"timestamp": f"2026-03-{d:02d}T00:00:00-04:00", "openPrice": "100", "highPrice": "102",
          "lowPrice": "99", "closePrice": f"{100 + d}.5", "volume": "1000", "currency": "USD"}
         for d in range(16, 25)]


def toss_handler(request: httpx.Request) -> httpx.Response:
    path, q = request.url.path, request.url.params
    if path == "/oauth2/token":
        return httpx.Response(200, json=load("oauth2_example.json"))
    if path == "/api/v1/market-calendar/US":
        return httpx.Response(200, json=load("market_calendar_us_business_day.json"))
    if path == "/api/v1/exchange-rate":
        return httpx.Response(200, json=load("exchange_rate_up.json"))
    if path == "/api/v1/stocks/all":
        items = [] if q["market"] != "NASDAQ" else [
            {"symbol": "AAPL", "name": "Apple", "securityType": "STOCK", "isCommonShare": True,
             "isinCode": "US0378331005"},
            {"symbol": "SOXS", "name": "Direxion Semi Bear 3X", "securityType": "ETF",
             "isCommonShare": False, "isinCode": "US25460G8316"}]
        return httpx.Response(200, json={"result": items})
    if path == "/api/v1/prices":
        res = [{"symbol": s, "timestamp": "2026-03-25T23:29:59+09:00", "lastPrice": "4.25",
                "currency": "USD"} for s in q["symbols"].split(",")]
        return httpx.Response(200, json={"result": res})
    if path == "/api/v1/candles":
        bars = _minute_bars() if q["interval"] == "1m" else DAILY
        if "before" in q:
            cut = datetime.fromisoformat(q["before"])
            bars = [b for b in bars if datetime.fromisoformat(b["timestamp"]) <= cut]
        newest = list(reversed(bars))[: int(q["count"])]
        more = len(bars) > len(newest)
        return httpx.Response(200, json={"result": {
            "candles": newest, "nextBefore": newest[-1]["timestamp"] if more else None}})
    return httpx.Response(404, json={"error": {"code": "not-found", "message": ""}})


async def _no_sleep(_s: float) -> None:
    return None


def fake_yf(_symbols: Any) -> Any:
    async def run(symbols: Any) -> list[StripQuote]:
        now = datetime.fromtimestamp(NOW, UTC)
        return [StripQuote(s, "ok", Decimal("4.30") if s == "^TNX" else Decimal("100"),
                           Decimal("99"), Decimal("1"), Decimal("1.01"), date(2026, 3, 24), now)
                for s in symbols]
    return run


def make_services(tmp_path: Path, *, keys: bool = True, fred_status: int = 200,
                  handler: Any = toss_handler, clock: Any = lambda: NOW) -> Services:
    secrets = Secrets(
        _env_file=None,
        TOSS_CLIENT_ID=TOSS_ID if keys else "",
        TOSS_CLIENT_SECRET=TOSS_SECRET if keys else "",
        FRED_API_KEY=FRED_KEY if keys else "",
    )

    def fred_handler(request: httpx.Request) -> httpx.Response:
        if fred_status != 200:  # body echoes the URL (and so the key) on purpose
            return httpx.Response(fred_status, text=f"error for {request.url}")
        return httpx.Response(200, json={"observations": [
            {"date": "2026-03-24", "value": "."}, {"date": "2026-03-23", "value": "4.31"}]})

    toss = TossClient(BASE, TOSS_ID, TOSS_SECRET,
                      http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                      sleep=_no_sleep) if keys else None
    return Services(
        AppConfig(), secrets, toss=toss,
        fred_http=httpx.AsyncClient(transport=httpx.MockTransport(fred_handler)),
        yf_fetch=fake_yf(None),
        positions_path=tmp_path / "positions.json",
        overlays_path=tmp_path / "overlays.json",
        clock=clock,
    )


@pytest.fixture
def svc(tmp_path: Path) -> Services:
    return make_services(tmp_path)


@pytest.fixture
def client(svc: Services):
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        yield c


# ── health / host ────────────────────────────────────────────────────────

def test_health_shows_only_set_or_empty(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["keys"] == {"TOSS_CLIENT_ID": "set", "TOSS_CLIENT_SECRET": "set", "FRED_API_KEY": "set",
                            "ANTHROPIC_API_KEY": "empty", "FINNHUB_API_KEY": "empty", "MASSIVE_API_KEY": "empty",
                      "SEC_USER_AGENT": "empty"}
    for secret in (TOSS_ID, TOSS_SECRET, FRED_KEY):
        assert secret not in r.text


def test_health_empty_keys(tmp_path: Path) -> None:
    svc = make_services(tmp_path, keys=False)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        c.portal.call(svc.refresh_fx)
        c.portal.call(svc.refresh_fred)
        body = c.get("/api/health").json()
    assert set(body["keys"].values()) == {"empty"}
    assert body["sources"]["toss.fx"]["error_code"] == "no-key"
    assert body["sources"]["fred.dgs10"]["error"] == "no key"


def test_host_check_rejects_other_hosts(svc: Services) -> None:
    app = create_app(svc, start_pollers=False)
    with TestClient(app, base_url="http://evil.example") as c:
        assert c.get("/api/health").status_code == 400
    with TestClient(app, base_url="http://localhost:8000") as c:
        assert c.get("/api/health").status_code == 200


def test_toss_retry(client: TestClient, svc: Services) -> None:
    svc.toss._revoked = True  # type: ignore[union-attr]
    assert client.get("/api/health").json()["toss"]["revoked"] is True
    assert client.post("/api/health/toss/retry").status_code == 200
    assert svc.toss.revoked is False  # type: ignore[union-attr]


# ── FRED secrets ─────────────────────────────────────────────────────────

def test_fred_error_never_contains_key(tmp_path: Path) -> None:
    svc = make_services(tmp_path, fred_status=500)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        c.portal.call(svc.refresh_fred)
        r = c.get("/api/health")
        strip = c.get("/api/strip")
    entry = svc.cache.get("fred.dgs10")
    assert entry is not None and entry.status == "error" and entry.error_code == "http-500"
    assert FRED_KEY not in (entry.error_message or "")
    assert FRED_KEY not in r.text and FRED_KEY not in strip.text
    assert r.json()["sources"]["fred.dgs10"]["status"] == "error"


# ── tickers ──────────────────────────────────────────────────────────────

def test_invalid_ticker_400(client: TestClient, svc: Services) -> None:
    assert client.get("/api/candles", params={"symbol": "SO$XS", "tf": "5m"}).status_code == 400
    # stock list not loaded: only configured tickers pass
    assert client.get("/api/candles", params={"symbol": "AAPL", "tf": "5m"}).status_code == 400
    client.portal.call(svc.refresh_stocks)
    assert client.get("/api/candles", params={"symbol": "NOPE", "tf": "5m"}).status_code == 400
    assert client.get("/api/candles", params={"symbol": "aapl", "tf": "9m"}).status_code == 400
    items = client.get("/api/tickers", params={"q": "aa"}).json()["items"]
    assert [i["symbol"] for i in items] == ["AAPL"]


# ── candles ──────────────────────────────────────────────────────────────

def test_candles_5m_and_1m(client: TestClient, svc: Services) -> None:
    client.portal.call(svc.refresh_calendar)
    client.portal.call(svc.refresh_stocks)
    r = client.get("/api/candles", params={"symbol": "aapl", "tf": "5m"})
    assert r.status_code == 200
    body = r.json()
    bars = body["bars"]
    assert len(bars) == 78  # 390 one-minute bars -> 78 five-minute bars
    assert all(b["start"] == b["end"] - 300 for b in bars)
    assert bars[0]["session"] == "pre" and bars[-1]["session"] == "regular"
    assert set(body["ema"]) == {"9", "20", "60"} and len(body["ema"]["9"]) == len(bars)
    assert body["ema"]["9"][7] is None and body["ema"]["9"][8] is not None
    rs = body["rsi"]  # RSI(14): first value at index 14, always 0..100
    assert len(rs) == len(bars) and rs[13] is None and rs[14] is not None
    assert all(0 <= float(v) <= 100 for v in rs if v is not None)
    assert len(body["vwap"]) == len(bars) and body["vwap"][-1] is not None
    assert body["vwap_label"] is None
    assert body["fib"]["levels"][0]["ratio"] == "0" and body["fib_date"] == "2026-03-25"
    assert {s["name"] for s in body["sessions"]} >= {"pre", "regular"}
    assert body["meta"]["status"] == "ok" and body["meta"]["currency"] == "USD"
    one = client.get("/api/candles", params={"symbol": "AAPL", "tf": "1m"}).json()
    assert len(one["bars"]) == 390 and one["bars"][0]["start"] == one["bars"][0]["end"] - 60
    assert len(one["rsi"]) == 390 and one["rsi"][-1] is not None
    for view in (body, one):  # markers sit on a bar start (charts label bars by start)
        starts = {b["start"] for b in view["bars"]}
        assert view["swings"] and all(m["start"] in starts for m in view["swings"])
        assert all(p["start"] in starts for p in view["fib"]["points"])


def test_live_bar_from_quote() -> None:
    from types import SimpleNamespace

    from app.services import live_bar

    t0 = 1_774_440_000  # multiple of 300
    last = {"start": t0, "end": t0 + 60, "open": "10", "high": "10.5", "low": "9.9", "close": "10.2",
            "volume": "100", "session": "regular"}
    q = lambda ts, px: SimpleNamespace(timestamp=datetime.fromtimestamp(ts, UTC), last_price=Decimal(px))
    same = live_bar(last, q(t0 + 30, "10.7"), None, 60, t0 + 31)          # inside the newest bar
    assert same["start"] == t0 and same["high"] == Decimal("10.7") and same["low"] == Decimal("9.9")
    assert same["close"] == Decimal("10.7") and same["open"] == "10" and same["provisional"]
    edge = live_bar(last, q(t0 + 60, "10.1"), None, 60, t0 + 61)          # end stamp belongs to that bar
    assert edge["start"] == t0
    new = live_bar(last, q(t0 + 75, "10.3"), None, 60, t0 + 76)           # next minute: new forming bar
    assert new["start"] == t0 + 60 and new["open"] == new["close"] == Decimal("10.3") and new["volume"] == 0
    assert live_bar(last, q(t0 - 30, "9"), None, 60, t0) is None           # older than the newest bar
    assert live_bar(last, q(t0 + 30, "10"), None, 60, t0 + 500) is None    # stale quote
    assert live_bar(last, None, None, 60, t0) is None
    assert live_bar(last, q(t0 + 30, "10"), None, 86400, t0 + 31) is None  # daily: no live bar
    ticks = [(t0 + 10, Decimal("11.0")), (t0 + 20, Decimal("9.5")), (t0 - 5, Decimal("99"))]
    hl = live_bar(last, q(t0 + 30, "10.4"), ticks, 60, t0 + 31)      # trade ticks set high/low
    assert hl["high"] == Decimal("11.0") and hl["low"] == Decimal("9.5") and hl["close"] == Decimal("10.4")


def test_candles_1d(client: TestClient, svc: Services) -> None:
    body = client.get("/api/candles", params={"symbol": "SOXS", "tf": "1d"}).json()
    assert len(body["bars"]) == len(DAILY)
    first = body["bars"][0]
    assert first["date"] == "2026-03-16"
    assert first["start"] == int(datetime(2026, 3, 16, tzinfo=UTC).timestamp())
    assert body["vwap"] is None and body["fib"] is None
    assert len(body["rsi"]) == len(DAILY)


def test_candles_error_keeps_app_alive(tmp_path: Path) -> None:
    svc = make_services(tmp_path, keys=False)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        r = c.get("/api/candles", params={"symbol": "SOXS", "tf": "5m"})
    assert r.status_code == 200
    assert r.json()["bars"] == [] and r.json()["meta"]["error_code"] == "no-key"


# ── strip ────────────────────────────────────────────────────────────────

def test_strip(client: TestClient, svc: Services) -> None:
    for fn in (svc.refresh_calendar, svc.refresh_fx, svc.refresh_quotes, svc.refresh_yf,
               svc.refresh_fred):
        client.portal.call(fn)
    items = {i["key"]: i for i in client.get("/api/strip").json()["items"]}
    assert items["USDKRW"]["value"] == "1375" and items["USDKRW"]["source"] == "Toss mid"
    assert items["DGS10"]["value"] == "4.31"
    assert items["^TNX"]["flag"] is None
    assert items["NQ=F"]["delay"] == "yfinance (delayed)"
    soxs = items["SOXS"]
    assert soxs["value"] == "4.25" and soxs["change"] == "-120.25"  # prev close 124.5 (03-24)


# ── positions ────────────────────────────────────────────────────────────

def test_positions_invalid_json_not_overwritten(client: TestClient, svc: Services) -> None:
    broken = '{"trades": [ {"symbol": "SOXS",, }'
    svc.positions_path.write_text(broken, encoding="utf-8")
    plan = {"symbol": "SOXS", "side": "buy", "price": 3.95, "currency": "USD"}
    r = client.put("/api/positions/plans", json={"plans": [plan]})
    assert r.status_code == 409
    assert svc.positions_path.read_text(encoding="utf-8") == broken
    assert client.get("/api/positions").status_code == 422
    assert client.get("/api/pnl").status_code == 422


def test_put_plans_validates_and_keeps_trades(client: TestClient, svc: Services) -> None:
    example = (Path(__file__).parents[2] / "data" / "positions.example.json").read_text(encoding="utf-8")
    svc.positions_path.write_text(example, encoding="utf-8")
    bad = client.put("/api/positions/plans", json={"plans": [{"symbol": "SOXS", "side": "buy",
                                                              "price": 3.95}]})
    assert bad.status_code == 400  # currency missing: never defaulted
    ok = client.put("/api/positions/plans",
                    content='{"plans": [{"symbol": "SOXS", "side": "buy", "price": 3.950, '
                            '"currency": "USD"}]}',
                    headers={"Content-Type": "application/json; charset=utf-8"})
    assert ok.status_code == 200
    saved = json.loads(svc.positions_path.read_text(encoding="utf-8"), parse_float=Decimal)
    assert saved["plans"][0]["price"] == Decimal("3.950")
    assert len(saved["trades"]) == 2
    client.portal.call(svc.refresh_fx)
    body = client.get("/api/positions").json()
    ids = {o["id"]: o for o in body["overlays"]}
    assert ids["plan-0"]["type"] == "hline" and ids["plan-0"]["source"] == "user"
    assert ids["avg-SOXS"]["points"][0]["price"] == "4.10"  # typed avg_override
    assert body["fx"]["rate"] == "1375"


def test_pnl(client: TestClient, svc: Services) -> None:
    example = (Path(__file__).parents[2] / "data" / "positions.example.json").read_text(encoding="utf-8")
    svc.positions_path.write_text(example, encoding="utf-8")
    client.portal.call(svc.refresh_fx)
    client.portal.call(svc.refresh_quotes)
    body = client.get("/api/pnl", params={"period": "all"}).json()
    soxs = body["symbols"][0]
    assert soxs["symbol"] == "SOXS" and soxs["qty"] == "6"
    assert soxs["realized_usd"]["value"] == "0.92"  # (4.35 - 4.12) x 4
    assert body["total"]["ai_credits_usd"]["value"] == "0"
    assert body["fx"]["rate"] == "1375"
    assert client.get("/api/pnl", params={"period": "week"}).status_code == 400


# ── overlays ─────────────────────────────────────────────────────────────

def test_overlays_crud(client: TestClient, svc: Services) -> None:
    assert client.get("/api/overlays").json() == {"overlays": []}
    h = client.post("/api/overlays", json={
        "type": "hline", "points": [{"time": "2026-03-25T23:00:00+09:00", "price": 4.2}],
        "label": "res", "color": "#ff0000", "source": "user"}).json()
    assert h["id"] and h["points"][0]["price"] == "4.2"
    f = client.post("/api/overlays", json={
        "id": "fib1", "type": "fib", "color": "#00ff00", "source": "user",
        "points": [{"time": "2026-03-25T22:00:00+09:00", "price": 10},
                   {"time": "2026-03-25T23:00:00+09:00", "price": 20}]}).json()
    assert f["direction"] == "up" and f["levels"][3]["price"] == "15.0"
    listed = client.get("/api/overlays").json()["overlays"]
    assert {o["id"] for o in listed} == {h["id"], "fib1"}
    bad = client.post("/api/overlays", json={"type": "hline", "points": [], "color": "#fff",
                                             "source": "user"})
    assert bad.status_code == 400
    assert client.delete("/api/overlays/fib1").status_code == 200
    assert client.delete("/api/overlays/fib1").status_code == 404
    assert [o["id"] for o in client.get("/api/overlays").json()["overlays"]] == [h["id"]]


def test_overlays_filtered_by_symbol(client: TestClient) -> None:
    base = {"type": "hline", "points": [{"time": "2026-03-25T23:00:00+09:00", "price": 5}],
            "color": "#ffffff", "source": "user"}
    client.post("/api/overlays", json={**base, "id": "soxs1", "symbol": "SOXS"})
    client.post("/api/overlays", json={**base, "id": "soxx1", "symbol": "SOXX"})
    client.post("/api/overlays", json={**base, "id": "all1"})
    ids = lambda q: {o["id"] for o in client.get(f"/api/overlays{q}").json()["overlays"]}
    assert ids("?symbol=soxs") == {"soxs1", "all1"}
    assert ids("?symbol=SOXX") == {"soxx1", "all1"}
    assert ids("") == {"soxs1", "soxx1", "all1"}
    assert client.get("/api/overlays?symbol=bad%20sym").status_code == 400
    bad = client.post("/api/overlays", json={**base, "symbol": "soxs lower"})
    assert bad.status_code == 400


def test_app_module_imports_without_side_effects() -> None:
    import app.main as m

    assert m.app.title == "Personal Trading Desk"
    paths = set(m.app.openapi()["paths"])
    assert {"/api/health", "/api/candles", "/api/strip", "/api/positions", "/api/pnl",
            "/api/overlays", "/api/overlays/{overlay_id}", "/api/positions/plans",
            "/api/health/toss/retry", "/api/tickers"} <= paths
    # orders: exactly ONE send route (POST /api/orders/submit, approved drafts only); nothing else writes
    assert not any(re.search(r"send|cancel|modify", p) or ("submit" in p and p != "/api/orders/submit")
                   or ("account" in p and p != "/api/account") or "holding" in p for p in paths)
    assert {p for p in paths if "order" in p and "orderbook" not in p} == {
        "/api/orders/settings", "/api/orders/drafts", "/api/orders/drafts/{draft_id}",
        "/api/orders/submit", "/api/orders/simulated", "/api/orders/journal"}
    assert set(m.app.openapi()["paths"]["/api/orders/submit"]) == {"post"}
    assert set(m.app.openapi()["paths"]["/api/orders/journal"]) == {"get"}       # read-only journal
    assert set(m.app.openapi()["paths"]["/api/orders/settings"]) == {"get"}
    assert set(m.app.openapi()["paths"]["/api/account"]) == {"get"}   # read-only account view
    assert "/api/orderbook" in paths  # read-only orderbook is allowed; order endpoints are not


def test_ui_config_has_tickers_and_polling_only(client: TestClient) -> None:
    body = client.get("/api/ui-config").json()
    assert body["tickers"]["default"] == "SOXS"
    assert body["tickers"]["compare"] == ["SOXX"]
    assert body["polling"]["candles_seconds"] > 0
    assert set(body) == {"tickers", "polling", "user"}  # nothing else (no keys, no toss settings)
    assert set(body["user"]) == {"general", "chart", "ai"} and body["user"]["chart"]["timeframe"] == "5m"


# ── review fixes ─────────────────────────────────────────────────────────

def test_cross_site_writes_refused(client: TestClient, svc: Services) -> None:
    hline = ('{"type": "hline", "points": [{"time": "2026-03-25T23:00:00+09:00", '
             '"price": 4.2}], "color": "#ff0000", "source": "ai"}')
    r = client.post("/api/overlays", content=hline, headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    r = client.put("/api/positions/plans", content='{"plans": []}',
                   headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    evil = {"Origin": "https://evil.example", "Content-Type": "application/json"}
    assert client.post("/api/overlays", content=hline, headers=evil).status_code == 403
    svc.toss._revoked = True  # type: ignore[union-attr]
    assert client.post("/api/health/toss/retry", headers=evil).status_code == 403
    assert svc.toss.revoked is True  # type: ignore[union-attr]
    assert client.delete("/api/overlays/x", headers=evil).status_code == 403
    assert not svc.overlays_path.exists()
    ok = {"Origin": "http://127.0.0.1:5173", "Content-Type": "application/json"}
    assert client.post("/api/overlays", content=hline, headers=ok).status_code == 200
    assert client.get("/api/health", headers={"Origin": "https://evil.example"}).status_code == 200


def test_non_utf8_positions_gives_clear_error_and_quotes_keep_working(
        client: TestClient, svc: Services) -> None:
    raw = '{"trades": [{"symbol": "SOXS", "note": "\ub9e4\uc218"}]}'.encode("cp949")
    svc.positions_path.write_bytes(raw)
    r = client.get("/api/positions")
    assert r.status_code == 422 and "UTF-8" in r.json()["detail"]
    plan = {"symbol": "SOXS", "side": "buy", "price": 3.95, "currency": "USD"}
    assert client.put("/api/positions/plans", json={"plans": [plan]}).status_code == 409
    assert svc.positions_path.read_bytes() == raw
    client.portal.call(svc.refresh_quotes)
    assert svc.cache.get("toss.quotes").status == "ok"  # type: ignore[union-attr]


def test_unhandled_error_is_500_without_message(svc: Services) -> None:
    def boom() -> Any:
        raise RuntimeError("https://example.invalid/?api_key=" + FRED_KEY)
    svc.strip_view = boom  # type: ignore[method-assign]
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        r = c.get("/api/strip")
    assert r.status_code == 500 and r.json() == {"detail": "internal error (RuntimeError)"}
    assert FRED_KEY not in r.text


def test_prev_close_retried_after_error(tmp_path: Path) -> None:
    calls = {"1d": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/candles" and request.url.params["interval"] == "1d":
            calls["1d"] += 1
            if calls["1d"] <= 2:  # first poll: both strip symbols fail
                return httpx.Response(500, json={"error": {"code": "internal", "message": ""}})
        return toss_handler(request)

    svc = make_services(tmp_path, handler=flaky)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        c.portal.call(svc.refresh_calendar)
        c.portal.call(svc.refresh_quotes)
        assert not svc.prev_close  # errors are not remembered
        c.portal.call(svc.refresh_quotes)
        items = {i["key"]: i for i in c.get("/api/strip").json()["items"]}
    assert items["SOXS"]["change"] == "-120.25"


def test_daily_kst_stamp_start_before_end(tmp_path: Path) -> None:
    kst_daily = [{**b, "timestamp": b["timestamp"].replace("-04:00", "+09:00")} for b in DAILY]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/candles" and request.url.params["interval"] == "1d":
            return httpx.Response(200, json={"result": {"candles": list(reversed(kst_daily)),
                                                        "nextBefore": None}})
        return toss_handler(request)

    svc = make_services(tmp_path, handler=handler)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        body = c.get("/api/candles", params={"symbol": "SOXS", "tf": "1d"}).json()
    first = body["bars"][0]
    assert first["date"] == "2026-03-16"
    assert first["start"] == int(datetime(2026, 3, 16, tzinfo=UTC).timestamp())
    assert all(b["end"] == b["start"] + 86400 for b in body["bars"])
    starts = {b["start"] for b in body["bars"]}
    assert all(m["start"] in starts for m in body["swings"])


def test_long_refresh_gap_is_filled(tmp_path: Path) -> None:
    t0 = datetime.fromisoformat("2026-03-25T17:00:00+09:00").timestamp()
    now = {"t": t0 + 60 * 100}

    def bars_until(t: float) -> list[dict[str, str]]:
        n = int((t - t0) // 60)
        return [{"timestamp": _iso_kst(t0 + 60 * i), "openPrice": "10", "highPrice": "11",
                 "lowPrice": "9", "closePrice": "10", "volume": "5", "currency": "USD"}
                for i in range(1, n + 1)]

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        if request.url.path == "/api/v1/candles" and q["interval"] == "1m":
            bars = bars_until(now["t"])
            if "before" in q:
                cut = datetime.fromisoformat(q["before"])
                bars = [b for b in bars if datetime.fromisoformat(b["timestamp"]) <= cut]
            newest = list(reversed(bars))[: int(q["count"])]
            more = len(bars) > len(newest)
            return httpx.Response(200, json={"result": {
                "candles": newest, "nextBefore": newest[-1]["timestamp"] if more else None}})
        return toss_handler(request)

    svc = make_services(tmp_path, handler=handler, clock=lambda: now["t"])
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        c.get("/api/candles", params={"symbol": "SOXS", "tf": "1m"})
        now["t"] += 5 * 3600  # away for 5 hours: 300 new bars (> one 200-bar page)
        c.get("/api/candles", params={"symbol": "SOXS", "tf": "1m"})
    keys = sorted(svc._candles["SOXS"].bars)
    assert len(keys) == 400
    assert all(b - a == 60 for a, b in zip(keys, keys[1:]))  # no hole
