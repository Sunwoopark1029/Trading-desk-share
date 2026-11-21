"""Toss client tests: fixtures from the official spec examples, no network."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from app.data import toss
from app.data.toss import (
    RateLimiter,
    TossAuthError,
    TossClient,
    TossError,
    TossIpNotAllowed,
    TossPathNotAllowed,
    TossRateLimited,
    TossTokenRevoked,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://toss.test"
SECRET = "client-secret-value-xyz"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler, clock: FakeClock | None = None) -> tuple[TossClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    clock = clock or FakeClock()

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    client = TossClient(BASE, "client-id", SECRET, http=http, clock=clock, sleep=clock.sleep)
    return client, seen


def token_ok(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/oauth2/token":
        return httpx.Response(200, json=load("oauth2_example.json"))
    return None


def routes(api: dict[str, Any]) -> Handler:
    """Token endpoint OK; API paths map to fixture names."""

    def handler(request: httpx.Request) -> httpx.Response:
        tok = token_ok(request)
        if tok is not None:
            return tok
        return httpx.Response(200, json=load(api[request.url.path]))

    return handler


def api_requests(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in seen if r.url.path != "/oauth2/token"]


# ---------- parsing ----------

@pytest.mark.anyio
async def test_candles_1m_parsed_exact_decimal_and_offset() -> None:
    client, seen = make_client(routes({"/api/v1/candles": "candles_1m.json"}))
    page = await client.candles("SOXS", "1m", count=5)
    first = page.candles[0]
    assert first.timestamp == datetime(2026, 3, 25, 9, 32, tzinfo=timezone(timedelta(hours=9)))
    assert first.open == Decimal("72000") and first.close == Decimal("72050")
    assert first.volume == Decimal("15200") and first.currency == "KRW"
    assert page.next_before == datetime.fromisoformat("2026-03-25T09:31:00+09:00")
    req = api_requests(seen)[0]
    assert req.url.params["symbol"] == "SOXS"
    assert req.url.params["interval"] == "1m"
    assert req.url.params["count"] == "5"


@pytest.mark.anyio
async def test_prices_decimal_exact_and_millis() -> None:
    client, seen = make_client(routes({"/api/v1/prices": "prices.json"}))
    prices = await client.prices(["005930", "AAPL"])
    aapl = prices[1]
    assert aapl.last_price == Decimal("185.70")
    assert str(aapl.last_price) == "185.70"
    assert aapl.timestamp is not None and aapl.timestamp.microsecond == 456000
    assert api_requests(seen)[0].url.params["symbols"] == "005930,AAPL"


@pytest.mark.anyio
async def test_prices_limits() -> None:
    client, _ = make_client(routes({}))
    with pytest.raises(TossError):
        await client.prices([])
    with pytest.raises(TossError):
        await client.prices(["A"] * 201)
    with pytest.raises(TossError):
        await client.prices(["BAD SYMBOL"])


@pytest.mark.anyio
async def test_market_calendar_business_day_and_holiday() -> None:
    client, seen = make_client(routes({"/api/v1/market-calendar/US": "market_calendar_us_business_day.json"}))
    cal = await client.market_calendar(date(2026, 3, 25))
    assert cal.today.date == date(2026, 3, 25)
    assert cal.today.regular_market is not None
    assert cal.today.regular_market.end == datetime.fromisoformat("2026-03-26T05:00:00+09:00")
    assert cal.previous_business_day.date == date(2026, 3, 24)
    assert api_requests(seen)[0].url.params["date"] == "2026-03-25"

    client2, _ = make_client(routes({"/api/v1/market-calendar/US": "market_calendar_us_holiday.json"}))
    hol = await client2.market_calendar()
    assert hol.today.pre_market is None and hol.today.regular_market is None
    assert hol.next_business_day.date == date(2026, 7, 6)


@pytest.mark.anyio
async def test_exchange_rate_fields() -> None:
    client, seen = make_client(routes({"/api/v1/exchange-rate": "exchange_rate_down.json"}))
    fx = await client.exchange_rate()
    assert fx.mid_rate == Decimal("1375") and fx.rate == Decimal("1372.3")
    assert fx.basis_point == Decimal("-19.6")
    assert fx.valid_from == datetime.fromisoformat("2026-03-25T09:30:00+09:00")
    params = api_requests(seen)[0].url.params
    assert params["baseCurrency"] == "USD" and params["quoteCurrency"] == "KRW"


@pytest.mark.anyio
async def test_stocks_all() -> None:
    client, seen = make_client(routes({"/api/v1/stocks/all": "stocks_all_nasdaq.json"}))
    stocks = await client.stocks_all("NASDAQ")
    assert stocks[0].symbol == "AAPL" and stocks[0].is_common_share is True
    assert api_requests(seen)[0].url.params["market"] == "NASDAQ"
    with pytest.raises(TossError):
        await client.stocks_all("MOON")


def test_timestamp_without_offset_rejected() -> None:
    bad = {"candles": [{"timestamp": "2026-03-25T09:32:00", "openPrice": "1", "highPrice": "1",
                        "lowPrice": "1", "closePrice": "1", "volume": "1", "currency": "USD"}],
           "nextBefore": None}
    with pytest.raises(TossError):
        toss.parse_candle_page(bad)


def test_utc_offset_kept() -> None:
    page = toss.parse_candle_page({"candles": [{"timestamp": "2026-03-25T13:32:00Z", "openPrice": "1",
                                                "highPrice": "1", "lowPrice": "1", "closePrice": "1",
                                                "volume": "1", "currency": "USD"}], "nextBefore": None})
    assert page.candles[0].timestamp.utcoffset() == timedelta(0)


@pytest.mark.anyio
async def test_malformed_payload_is_toss_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return token_ok(request) or httpx.Response(200, json={"result": {"candles": [{"x": 1}]}})

    client, _ = make_client(handler)
    with pytest.raises(TossError) as ei:
        await client.candles("SOXS", "1m")
    assert ei.value.code == "parse-error"


# ---------- before cursor and paging ----------

@pytest.mark.anyio
async def test_before_plus_encoded_as_2b() -> None:
    client, seen = make_client(routes({"/api/v1/candles": "candles_1m.json"}))
    await client.candles("SOXS", "1m", before=datetime.fromisoformat("2026-03-25T09:31:00+09:00"))
    raw = api_requests(seen)[0].url.raw_path.decode()
    assert "%2B09%3A00" in raw
    assert "+" not in raw
    assert api_requests(seen)[0].url.params["before"] == "2026-03-25T09:31:00+09:00"


@pytest.mark.anyio
async def test_candles_history_pages_via_next_before_and_dedupes() -> None:
    pages = [load("candles_1d.json"), load("candles_last_page.json")]
    befores: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tok = token_ok(request)
        if tok is not None:
            return tok
        befores.append(request.url.params.get("before"))
        return httpx.Response(200, json=pages[len(befores) - 1])

    client, _ = make_client(handler)
    bars = await client.candles_history("SOXS", "1d", total=10)
    assert befores == [None, "2026-03-24T00:00:00+09:00"]
    assert [b.timestamp.day for b in bars] == [20, 24, 25]  # oldest first


@pytest.mark.anyio
async def test_candle_argument_validation() -> None:
    client, seen = make_client(routes({}))
    for kwargs in ({"interval": "5m"}, {"interval": "1m", "count": 0}, {"interval": "1m", "count": 201}):
        with pytest.raises(TossError):
            await client.candles("SOXS", **kwargs)
    with pytest.raises(TossError):
        await client.candles("SOXS", "1m", before=datetime(2026, 3, 25, 9, 0))
    assert seen == []


# ---------- allowlist and headers ----------

@pytest.mark.anyio
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/orders"),                         # send: never allowed in the read client
        ("POST", "/api/v1/orders/abc/modify"),
        ("POST", "/api/v1/orders/abc/cancel"),
        ("POST", "/api/v1/conditional-orders"),
        ("DELETE", "/api/v1/conditional-orders/abc"),
        ("POST", "/api/v1/conditional-orders/abc/modify"),
        ("PUT", "/api/v1/orders"),
        ("GET", "/api/v1/orders/abc/../../holdings"),
        ("POST", "/api/v1/candles"),
        ("GET", "/oauth2/token"),
        ("GET", "/api/v1/candles/../orders"),
    ],
)
async def test_allowlist_blocks_other_paths(method: str, path: str) -> None:
    client, seen = make_client(routes({}))
    with pytest.raises(TossPathNotAllowed):
        await client._send(method, path)
    if method == "GET":
        with pytest.raises(TossPathNotAllowed):
            await client._api_get(path)
    assert seen == []


@pytest.mark.anyio
async def test_no_account_header_and_bearer_used() -> None:
    client, seen = make_client(routes({"/api/v1/prices": "prices.json"}))
    await client.prices(["AAPL"])
    for req in seen:
        assert not any(h.lower() == "x-tossinvest-account" for h in req.headers.keys())
    tok_req, api_req = seen
    assert tok_req.method == "POST"
    form = dict(x.split("=", 1) for x in tok_req.content.decode().split("&"))
    assert form["grant_type"] == "client_credentials"
    assert api_req.headers["authorization"].startswith("Bearer eyJ")


# ---------- token lifecycle ----------

@pytest.mark.anyio
async def test_token_lazy_and_reused() -> None:
    client, seen = make_client(routes({"/api/v1/prices": "prices.json"}))
    assert seen == []  # nothing at construction
    await client.prices(["AAPL"])
    await client.prices(["AAPL"])
    assert [r.url.path for r in seen].count("/oauth2/token") == 1


@pytest.mark.anyio
async def test_token_reissued_near_expiry() -> None:
    clock = FakeClock()
    client, seen = make_client(routes({"/api/v1/prices": "prices.json"}), clock)
    await client.prices(["AAPL"])
    clock.now += 86400 - 299  # inside the 5 min margin
    await client.prices(["AAPL"])
    assert [r.url.path for r in seen].count("/oauth2/token") == 2


@pytest.mark.anyio
@pytest.mark.parametrize("fixture", ["error_expired_token.json", "error_invalid_token.json"])
async def test_401_expired_or_invalid_reissues_once(fixture: str) -> None:
    calls = {"api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        tok = token_ok(request)
        if tok is not None:
            return tok
        calls["api"] += 1
        if calls["api"] == 1:
            return httpx.Response(401, json=load(fixture))
        return httpx.Response(200, json=load("prices.json"))

    client, seen = make_client(handler)
    prices = await client.prices(["AAPL"])
    assert prices[1].symbol == "AAPL"
    assert [r.url.path for r in seen].count("/oauth2/token") == 2


@pytest.mark.anyio
async def test_401_expired_twice_gives_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return token_ok(request) or httpx.Response(401, json=load("error_expired_token.json"))

    client, seen = make_client(handler)
    with pytest.raises(TossAuthError) as ei:
        await client.prices(["AAPL"])
    assert ei.value.code == "expired-token"
    assert [r.url.path for r in seen].count("/oauth2/token") == 2


@pytest.mark.anyio
async def test_token_revoked_stops_until_reset() -> None:
    state = {"revoked": True}

    def handler(request: httpx.Request) -> httpx.Response:
        tok = token_ok(request)
        if tok is not None:
            return tok
        if state["revoked"]:
            return httpx.Response(401, json=load("error_token_revoked.json"))
        return httpx.Response(200, json=load("prices.json"))

    client, seen = make_client(handler)
    with pytest.raises(TossTokenRevoked) as ei:
        await client.prices(["AAPL"])
    assert ei.value.user_message == "token replaced elsewhere (second copy running?)"
    assert client.revoked
    count = len(seen)
    with pytest.raises(TossTokenRevoked):
        await client.prices(["AAPL"])
    assert len(seen) == count  # no reissue, no network
    state["revoked"] = False
    client.reset_revoked()
    await client.prices(["AAPL"])
    assert [r.url.path for r in seen].count("/oauth2/token") == 2


@pytest.mark.anyio
async def test_403_ip_not_allowed_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return token_ok(request) or httpx.Response(403, json=load("error_ip_not_allowed.json"))

    client, _ = make_client(handler)
    with pytest.raises(TossIpNotAllowed) as ei:
        await client.prices(["AAPL"])
    assert ei.value.user_message == "Register this PC's IP in Toss WTS (Settings > Open API)"
    assert ei.value.code == "ip-not-allowed"


@pytest.mark.anyio
async def test_403_access_denied_on_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=load("oauth2_error_access_denied.json"))

    client, _ = make_client(handler)
    with pytest.raises(TossIpNotAllowed) as ei:
        await client.prices(["AAPL"])
    assert (ei.value.status, ei.value.code) == (403, "access_denied")


@pytest.mark.anyio
async def test_invalid_client_on_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=load("oauth2_error_invalid_client.json"))

    client, _ = make_client(handler)
    with pytest.raises(TossAuthError) as ei:
        await client.prices(["AAPL"])
    assert ei.value.code == "invalid_client"
    assert SECRET not in str(ei.value) and SECRET not in repr(ei.value)


@pytest.mark.anyio
async def test_error_envelope_keeps_only_status_and_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return token_ok(request) or httpx.Response(400, json=load("error_invalid_request.json"))

    client, _ = make_client(handler)
    with pytest.raises(TossError) as ei:
        await client.candles("SOXS", "1m")
    err = ei.value
    assert (err.status, err.code) == (400, "invalid-request")
    for text in (str(err), repr(err)):
        assert "toss.test" not in text and "01HXYZ" not in text and "SOXS" not in text


@pytest.mark.anyio
async def test_transport_error_has_no_url_or_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {request.url}", request=request)

    client, _ = make_client(handler)
    with pytest.raises(TossError) as ei:
        await client.prices(["AAPL"])
    err = ei.value
    assert (err.status, err.code) == (None, "transport")
    assert err.__context__ is None and err.__cause__ is None
    for text in (str(err), repr(err)):
        assert "toss.test" not in text and SECRET not in text


@pytest.mark.anyio
async def test_unparseable_error_code_falls_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return token_ok(request) or httpx.Response(502, text="<html>bad gateway https://x?k=1</html>")

    client, _ = make_client(handler)
    with pytest.raises(TossError) as ei:
        await client.prices(["AAPL"])
    assert ei.value.code == "http-502"


# ---------- 429 ----------

@pytest.mark.anyio
async def test_429_retry_after_then_one_retry() -> None:
    clock = FakeClock()
    calls = {"api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        tok = token_ok(request)
        if tok is not None:
            return tok
        calls["api"] += 1
        if calls["api"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json=load("error_rate_limit.json"))
        return httpx.Response(200, json=load("prices.json"))

    client, _ = make_client(handler, clock)
    await client.prices(["AAPL"])
    assert calls["api"] == 2
    assert 3.0 in clock.sleeps


@pytest.mark.anyio
async def test_429_twice_raises() -> None:
    calls = {"api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        tok = token_ok(request)
        if tok is not None:
            return tok
        calls["api"] += 1
        return httpx.Response(429, headers={"Retry-After": "1"}, json=load("error_rate_limit.json"))

    client, _ = make_client(handler)
    with pytest.raises(TossRateLimited):
        await client.prices(["AAPL"])
    assert calls["api"] == 2


# ---------- rate limiter (fake clock) ----------

@pytest.mark.anyio
async def test_limiter_spacing_with_fake_clock() -> None:
    clock = FakeClock()
    limiter = RateLimiter({"MARKET_INFO": 3, "MARKET_DATA_CHART": 20}, 0.8, clock, clock.sleep)
    assert limiter.interval("MARKET_INFO") == pytest.approx(1 / 2.4)
    start = clock.now
    for _ in range(5):
        await limiter.acquire("MARKET_INFO")
    # first is immediate, then 4 waits of 1/2.4 s
    assert clock.now - start == pytest.approx(4 / 2.4)
    assert len(clock.sleeps) == 4


@pytest.mark.anyio
async def test_limiter_groups_independent_and_idle_resets() -> None:
    clock = FakeClock()
    limiter = RateLimiter({"A": 1, "B": 1}, 1.0, clock, clock.sleep)
    await limiter.acquire("A")
    await limiter.acquire("B")
    assert clock.sleeps == []
    clock.now += 10
    await limiter.acquire("A")
    assert clock.sleeps == []
    with pytest.raises(TossError):
        await limiter.acquire("UNKNOWN")


def test_limiter_rejects_bad_safety() -> None:
    with pytest.raises(ValueError):
        RateLimiter({"A": 1}, 0)


def test_documented_limits_cover_allowlist() -> None:
    for group in toss.ALLOWED_PATHS.values():
        assert group in toss.DOCUMENTED_LIMITS
    for _m, _pattern, group in toss.ALLOWED_PATTERNS:
        assert group in toss.DOCUMENTED_LIMITS


def test_httpx_loggers_quiet() -> None:
    import logging

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


# ---------- phase 4: read-only account data ----------

ORDER_JSON = {"orderId": "0d5QIH-x_y", "symbol": "SOXS", "side": "BUY", "orderType": "LIMIT", "timeInForce": "DAY",
              "status": "PARTIAL_FILLED", "price": "57.47", "quantity": "10", "orderAmount": None, "currency": "USD",
              "orderedAt": "2026-03-25T23:31:00+09:00", "canceledAt": None,
              "execution": {"filledQuantity": "4", "averageFilledPrice": "57.45", "filledAmount": "229.80",
                            "commission": "0.23", "tax": "0", "filledAt": "2026-03-25T23:31:05+09:00",
                            "settlementDate": "2026-03-27"}}
COND_JSON = {"conditionalOrderId": "c1", "type": "OCO", "status": "WATCHING", "symbol": "SOXS", "market": "US",
             "quantity": "10", "orderType": "LIMIT", "expireDate": "2026-04-30",
             "first": {"type": "STOP", "status": "WATCHING", "orderSide": "SELL", "triggerPrice": "60",
                       "targetProfitRate": None, "orderPrice": "59.9", "triggeredOrderId": None},
             "second": {"type": "STOP", "status": "WATCHING", "orderSide": "SELL", "triggerPrice": "55",
                        "targetProfitRate": None, "orderPrice": "54.9", "triggeredOrderId": None},
             "createdAt": "2026-03-25T23:00:00+09:00"}


def account_routes(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    if p == "/oauth2/token":
        return httpx.Response(200, json=load("oauth2_example.json"))
    if p == "/api/v1/accounts":
        return httpx.Response(200, json={"result": [{"accountNo": "12345678901", "accountSeq": 7,
                                                     "accountType": "BROKERAGE"}]})
    if p == "/api/v1/buying-power":
        cur = request.url.params["currency"]
        return httpx.Response(200, json={"result": {"currency": cur,
                                                    "cashBuyingPower": "1234.56" if cur == "USD" else "50000"}})
    if p == "/api/v1/orders":
        return httpx.Response(200, json={"result": {"orders": [ORDER_JSON], "nextCursor": None, "hasNext": False}})
    if p.startswith("/api/v1/orders/"):
        return httpx.Response(200, json={"result": ORDER_JSON})
    if p == "/api/v1/conditional-orders":
        return httpx.Response(200, json={"result": {"conditionalOrders": [COND_JSON], "nextCursor": "n1",
                                                    "hasNext": True}})
    if p == "/api/v1/holdings":
        return httpx.Response(200, json={"result": {"totalPurchaseAmount": {"krw": "100", "usd": "1"}, "items": [
            {"symbol": "SOXS", "name": "x", "marketCountry": "US", "currency": "USD", "quantity": "10",
             "lastPrice": "57.9", "averagePurchasePrice": "56.1", "marketValue": {}, "profitLoss": {},
             "dailyProfitLoss": {}, "cost": {"commission": "0.5"}}]}})
    return httpx.Response(404, json={"error": {"code": "not-found", "message": ""}})


@pytest.mark.anyio
async def test_account_reads_send_account_header_only_there() -> None:
    client, seen = make_client(account_routes)
    accts = await client.accounts()
    assert accts[0].account_seq == 7 and accts[0].masked() == "*******8901"
    assert await client.buying_power(7, "USD") == Decimal("1234.56")
    o = (await client.open_orders(7))[0]
    assert o.status == "PARTIAL_FILLED" and o.execution.filled_quantity == Decimal("4") and o.price == Decimal("57.47")
    assert (await client.order(7, "0d5QIH-x_y")).order_id == "0d5QIH-x_y"
    conds, cursor = await client.conditional_orders(7)
    assert conds[0].second.trigger_price == Decimal("55") and cursor == "n1"
    h = await client.holdings(7)
    assert h.items[0].quantity == Decimal("10") and h.items[0].average_purchase_price == Decimal("56.1")
    by_path = {r.url.path: r for r in seen}
    assert "x-tossinvest-account" not in by_path["/api/v1/accounts"].headers
    for path in ("/api/v1/buying-power", "/api/v1/orders", "/api/v1/holdings", "/api/v1/conditional-orders"):
        assert by_path[path].headers["x-tossinvest-account"] == "7"
    assert all(r.method in ("GET", "POST") for r in seen)
    assert [r.url.path for r in seen if r.method == "POST"] == ["/oauth2/token"]   # nothing written


@pytest.mark.anyio
async def test_order_id_is_url_quoted_and_odd_ids_rejected() -> None:
    client, seen = make_client(account_routes)
    await client.order(7, "a b/c")             # quoted to a single safe segment
    assert seen[-1].url.raw_path.decode().startswith("/api/v1/orders/a%20b%2Fc")
    with pytest.raises(TossError):
        await client.buying_power(7, "EUR")


# ---------- phase 6 part A: rankings, sectors, stock basics (fixtures = spec 1.2.28 examples) ----------

PHASE6_ROUTES = {
    "/api/v1/rankings": "rankings_us_toss_volume.json",
    "/api/v1/sectors": "sectors.json",
    "/api/v1/sectors/rankings": "sector_rankings_top_gainers.json",
    "/api/v1/sectors/100": "sector_detail.json",
    "/api/v1/sectors/100/stocks": "sector_stocks_us.json",
    "/api/v1/sectors/100/etfs": "sector_etfs_kr.json",
    "/api/v1/stocks": "stock_infos_multiple.json",
}


def test_phase6_paths_allowed_with_documented_groups() -> None:
    expect = {"/api/v1/rankings": "RANKING", "/api/v1/sectors": "SECTOR", "/api/v1/sectors/rankings": "SECTOR_RANKING",
              "/api/v1/sectors/100": "SECTOR", "/api/v1/sectors/100/stocks": "SECTOR",
              "/api/v1/sectors/100/etfs": "SECTOR", "/api/v1/stocks": "STOCK"}
    for path, group in expect.items():
        assert toss.route_group("GET", path) == group
        assert toss.DOCUMENTED_LIMITS[group] == 5               # overview.md: 5 per second for all four groups
    for bad in ("/api/v1/sectors/../orders", "/api/v1/sectors/1.2", "/api/v1/sectors/100/holdings",
                "/api/v1/sectors/" + "x" * 41, "/api/v1/sectors/100/stocks/extra", "/api/v1/rankings/investor-trading"):
        assert toss.route_group("GET", bad) is None
    for method in ("POST", "PUT", "DELETE"):
        assert toss.route_group(method, "/api/v1/rankings") is None


@pytest.mark.anyio
async def test_rankings_parsed_exact_and_params() -> None:
    client, seen = make_client(routes(PHASE6_ROUTES))
    page = await client.rankings("TOSS_SECURITIES_TRADING_VOLUME", "1d")
    assert page.ranked_at == datetime.fromisoformat("2026-06-10T14:30:00+09:00")
    first, second = page.rows
    assert (first.rank, first.symbol, first.currency) == (1, "NVDA", "USD")
    assert first.last_price == Decimal("131.38") and first.base_price == Decimal("128.45")
    assert first.change_rate == Decimal("0.0228") and second.change_rate == Decimal("-0.0107")
    assert first.trading_volume == Decimal("342100") and first.trading_amount == Decimal("44942580")
    q = api_requests(seen)[0].url.params
    assert dict(q) == {"type": "TOSS_SECURITIES_TRADING_VOLUME", "marketCountry": "US", "duration": "1d", "count": "100"}
    await client.rankings("MARKET_TRADING_AMOUNT", "realtime", count=20, exclude_investment_caution=True)
    q = api_requests(seen)[1].url.params
    assert q["excludeInvestmentCaution"] == "true" and q["count"] == "20" and q["duration"] == "realtime"


@pytest.mark.anyio
async def test_rankings_empty_and_null_change_rate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return token_ok(request) or httpx.Response(200, json=load("rankings_empty.json"))

    client, _ = make_client(handler)
    page = await client.rankings("TOP_GAINERS", "1w")
    assert page.ranked_at is None and page.rows == ()
    row = dict(load("rankings_kr_amount.json")["result"]["rankings"][0])
    row["price"] = {**row["price"], "changeRate": None}          # spec: null when basePrice is 0
    assert toss.parse_rankings({"rankedAt": None, "rankings": [row]}).rows[0].change_rate is None


@pytest.mark.anyio
async def test_rankings_argument_validation_before_network() -> None:
    client, seen = make_client(routes({}))
    bad = [("TOP_GAINERS", "realtime", {}), ("TOP_LOSERS", "realtime", {}), ("TOP_MOVERS", "1d", {}),
           ("TOP_GAINERS", "2d", {}), ("MARKET_TRADING_VOLUME", "1d", {"count": 0}),
           ("MARKET_TRADING_VOLUME", "1d", {"count": 101}), ("MARKET_TRADING_VOLUME", "1d", {"count": True}),
           ("MARKET_TRADING_VOLUME", "1d", {"market_country": "JP"})]
    codes = []
    for t, d, kw in bad:
        with pytest.raises(TossError) as ei:
            await client.rankings(t, d, **kw)
        codes.append(ei.value.code)
    assert codes == ["unsupported-ranking-duration"] * 2 + ["bad-ranking-type", "bad-duration"] + ["bad-count"] * 3 \
        + ["bad-market-country"]
    assert seen == []


@pytest.mark.anyio
async def test_rankings_server_unsupported_duration_keeps_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return token_ok(request) or httpx.Response(400, json=load("rankings_unsupported_duration.json"))

    client, _ = make_client(handler)
    with pytest.raises(TossError) as ei:
        await client.rankings("MARKET_TRADING_AMOUNT", "1d")
    assert (ei.value.status, ei.value.code) == (400, "unsupported-ranking-duration")


@pytest.mark.anyio
async def test_sectors_list_and_rankings() -> None:
    client, seen = make_client(routes(PHASE6_ROUTES))
    tree = await client.sectors()
    assert [(s.sector_id, s.depth, s.parent_sector_id) for s in tree] == [
        ("10", 0, None), ("42", 1, "10"), ("100", 1, "10"), ("103", 2, "42")]
    assert tree[0].name == "IT 하드웨어"
    ranks = await client.sector_rankings("TOP_GAINERS", "1d")
    assert ranks[0].sector_id == "100" and ranks[0].change_rate == Decimal("0.0179")
    assert ranks[0].market_cap == Decimal("612000000000000") and ranks[0].currency == "KRW"
    assert ranks[1].trading_amount is None                      # null when no data
    assert dict(api_requests(seen)[1].url.params) == {"type": "TOP_GAINERS", "marketCountry": "US", "duration": "1d"}
    for t, d in (("TOP_LOSERS", "1d"), ("MARKET_TRADING_VOLUME", "1d"), ("TOP_GAINERS", "realtime")):
        with pytest.raises(TossError):
            await client.sector_rankings(t, d)                  # not offered for sectors
    assert len(api_requests(seen)) == 2


@pytest.mark.anyio
async def test_sector_detail_stocks_etfs() -> None:
    client, seen = make_client(routes(PHASE6_ROUTES))
    d = await client.sector("100")
    assert (d.name, d.depth, d.parent_sector_id, d.summary) == ("반도체", 1, "10", "반도체 설계·제조 전문 회사들")
    assert [r[0] for r in d.change_rates] == ["1d", "1w", "1mo", "3mo", "6mo", "1y"]
    assert dict(d.change_rates)["6mo"] is None and dict(d.change_rates)["1mo"] == Decimal("-0.0231")
    stocks = await client.sector_stocks("100")
    nv = stocks[0]
    assert (nv.symbol, nv.name, nv.currency) == ("NVDA", "엔비디아", "KRW")   # US amounts are KRW-converted
    assert nv.market_cap == Decimal("4703000000000000") and nv.volume == Decimal("182400500")
    etfs = await client.sector_etfs("100", "KR")
    assert [(e.symbol, e.name) for e in etfs] == [("091160", "KODEX 반도체"), ("396500", "TIGER Fn반도체TOP10")]
    assert [r.url.params["marketCountry"] for r in api_requests(seen)] == ["US", "US", "KR"]
    assert toss.parse_sector_stocks(load("sector_stocks_empty.json")["result"]) == []


@pytest.mark.anyio
async def test_sector_id_validated_and_not_found_code() -> None:
    client, seen = make_client(routes({}))
    for bad in ("rankings", "../orders", "1.2", "a/b", "", "x" * 41, "100%2F", "100\n", "rankings\n"):
        with pytest.raises(TossError) as ei:
            await client.sector(bad)
        assert ei.value.code == "bad-sector-id"
    with pytest.raises(TossError):
        await client.sector_stocks("100", "JP")
    assert seen == []

    def handler(request: httpx.Request) -> httpx.Response:
        return token_ok(request) or httpx.Response(404, json=load("sector_not_found.json"))

    client, _ = make_client(handler)
    with pytest.raises(TossError) as ei:
        await client.sector_etfs("999")
    assert (ei.value.status, ei.value.code) == (404, "sector-not-found")


@pytest.mark.anyio
async def test_stock_infos_parsed_and_validated() -> None:
    client, seen = make_client(routes(PHASE6_ROUTES))
    infos = await client.stock_infos(["005930", "AAPL"])
    assert api_requests(seen)[0].url.params["symbols"] == "005930,AAPL"
    kr, us = infos
    assert (us.symbol, us.english_name, us.market, us.security_type, us.currency) == (
        "AAPL", "APPLE INC", "NASDAQ", "STOCK", "USD")
    assert us.shares_outstanding == Decimal("14702703000") and us.leverage_factor is None
    assert us.list_date == date(1980, 12, 12) and us.delist_date is None and us.is_common_share is True
    assert kr.name == "삼성전자" and kr.status == "ACTIVE"
    for bad in ([], ["A"] * 201, ["SO XS"], ["SOXS,SOXX"], ["$X"], ["AAPL\n"], "AAPL"):
        with pytest.raises(TossError):
            await client.stock_infos(bad)
    assert len(api_requests(seen)) == 1


def test_stock_info_etf_example_without_english_name() -> None:
    """The spec's own ETF example omits englishName (schema says required): parse it anyway."""
    etf = toss.parse_stock_infos(load("stock_infos_etf.json")["result"])[0]
    assert (etf.symbol, etf.security_type, etf.english_name) == ("069500", "ETF", None)
    assert etf.leverage_factor == Decimal("1")


def test_stock_info_unknown_enum_values_kept() -> None:
    row = dict(load("stock_infos_multiple.json")["result"][1])
    row.update(market="NEW_MARKET", securityType="SPAC", status="HALTED", currency="EUR", leverageFactor="-3")
    info = toss.parse_stock_infos([row])[0]
    assert (info.market, info.security_type, info.status, info.currency) == ("NEW_MARKET", "SPAC", "HALTED", "EUR")
    assert info.leverage_factor == Decimal("-3")


def test_phase6_trailing_newline_never_matches() -> None:
    """Python's $ matches before a final newline; the new patterns use \\Z so it never slips through."""
    for path in ("/api/v1/sectors/100\n", "/api/v1/sectors/rankings\n", "/api/v1/sectors/100/stocks\n",
                 "/api/v1/sectors/100/etfs\n"):
        assert toss.route_group("GET", path) is None


def test_phase6_documented_nulls_parse() -> None:
    """Fields the spec marks nullable come back as None, never as the text 'None' or a parse error."""
    stock = toss.parse_sector_stocks({"stocks": [{"symbol": "NVDA", "name": "엔비디아", "volume": None,
                                                  "tradingAmount": None, "marketCapitalization": None,
                                                  "currency": "KRW"}]})[0]
    assert (stock.volume, stock.trading_amount, stock.market_cap) == (None, None, None)
    row = toss.parse_sector_rankings({"rankings": [{"rank": 1, "sectorId": "100", "name": "반도체", "changeRate": None,
                                                    "tradingAmount": None, "marketCapitalization": None,
                                                    "currency": "KRW"}]})[0]
    assert (row.change_rate, row.trading_amount, row.market_cap) == (None, None, None)
    top = toss.parse_sector_detail({"sectorId": "10", "name": "IT 하드웨어", "depth": 0, "parentSectorId": None,
                                    "summary": None, "performance": {"changeRates": []}})
    assert top.parent_sector_id is None and top.summary is None
    with pytest.raises(TossError):
        toss.parse_sectors({"sectors": [{"sectorId": "10", "name": "x", "depth": "0", "parentSectorId": None}]})
