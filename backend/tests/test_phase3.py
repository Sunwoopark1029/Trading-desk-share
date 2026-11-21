"""Phase 3: orderbook/trades (REST + stream), spread, reconnect backoff, news feed, AI tags, routes.
No network: fake Toss transport, fake WebSocket, fake Finnhub transport, fake Claude client."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.data.toss import BookLevel, Orderbook, TradeTick, parse_orderbook, parse_trades
from app.data.toss_ws import Backoff, TossStream, declaration, parse_frame
from app.main import create_app
from app.market import MarketHub, TickerState, session_at, spread
from app.news.feed import (
    NewsFeed, NewsItem, NewsStore, dedupe, finnhub_tls_context, is_fresh, normalize_finnhub, parse_epoch_seconds,
    title_key, url_key,
)
from app.news.tagger import NewsTagger, validate_tags
from app.settings import NewsConfig
from tests.test_api import LOCAL, NOW, make_services, toss_handler

D = Decimal
BOOK = {"timestamp": "2026-03-25T23:29:58+09:00", "currency": "USD",
        "asks": [{"price": "4.27", "volume": "500"}, {"price": "4.26", "volume": "300"}, {"price": "x", "volume": "1"}],
        "bids": [{"price": "4.24", "volume": "200"}, {"price": "4.25", "volume": "100"}]}
TRADES = [{"price": "4.25", "volume": "10", "timestamp": "2026-03-25T23:29:50.100+09:00", "currency": "USD"},
          {"price": "4.26", "volume": "5", "timestamp": "2026-03-25T23:29:40+09:00", "currency": "USD"},
          {"price": "bad", "volume": "5", "timestamp": "2026-03-25T23:29:41+09:00", "currency": "USD"}]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/orderbook":
        return httpx.Response(200, json={"result": BOOK})
    if request.url.path == "/api/v1/trades":
        return httpx.Response(200, json={"result": TRADES})
    return toss_handler(request)


# ── orderbook, trades, spread ────────────────────────────────────────────

def test_parse_orderbook_sorts_and_skips_bad_levels() -> None:
    b = parse_orderbook(BOOK)
    assert [lv.price for lv in b.asks] == [D("4.26"), D("4.27")]   # cheapest first
    assert [lv.price for lv in b.bids] == [D("4.25"), D("4.24")]   # highest first
    assert b.currency == "USD" and b.timestamp is not None
    t = parse_trades(TRADES)
    assert [x.price for x in t] == [D("4.26"), D("4.25")]          # oldest first, bad one dropped


def test_spread_math() -> None:
    b = parse_orderbook(BOOK)
    sp = spread(b)
    assert sp["bid"] == D("4.25") and sp["ask"] == D("4.26") and sp["abs"] == D("0.01")
    assert sp["pct"] == (D("0.01") / D("4.255") * 100).quantize(D("0.0001"))   # ~0.235% of mid
    assert not sp["crossed"]
    assert spread(Orderbook(None, "USD", (), b.bids)) is None and spread(None) is None
    crossed = Orderbook(None, "USD", (BookLevel(D("4.20"), D("1")),), (BookLevel(D("4.21"), D("1")),))
    assert spread(crossed)["crossed"] is True


def test_session_at() -> None:
    sess = [SimpleNamespace(name="pre", start=100, end=200), SimpleNamespace(name="regular", start=200, end=300)]
    assert session_at(150, sess) == "pre" and session_at(200, sess) == "regular" and session_at(300, sess) is None


# ── stream frames, backoff ───────────────────────────────────────────────

def test_parse_frames_never_raise() -> None:
    ob = parse_frame(json.dumps({"type": "message", "topic": "orderbook:us:SOXS", "data": BOOK}))
    assert ob.kind == "orderbook" and ob.symbol == "SOXS" and ob.market == "us" and ob.orderbook.asks
    tr = parse_frame(json.dumps({"type": "message", "topic": "trade:us:SOXS", "data": TRADES[0]}))
    assert tr.kind == "trade" and tr.trade.price == D("4.25")
    assert parse_frame('{"type":"pong"}').kind == "pong"
    ack = parse_frame(json.dumps({"type": "subscriptions", "rejected": [{"target": "X", "code": "stock-not-found"}]}))
    assert ack.kind == "ack" and ack.rejected[0]["code"] == "stock-not-found"
    assert parse_frame('{"type":"error","code":"auth-revoked"}').code == "auth-revoked"
    for junk in ("not json", "[]", '{"type":"message","topic":"trade:us:X","data":{"price":"x"}}',
                 '{"type":"message","topic":"bad"}'):
        assert parse_frame(junk).kind == "other"


def test_declaration_full_replace() -> None:
    msg = json.loads(declaration([("us", "SOXX"), ("us", "SOXS")], "req-1"))
    assert msg[0] == {"id": "req-1"}
    assert {"type": "orderbook:us", "codes": ["SOXS", "SOXX"]} in msg
    assert {"type": "trade:us", "codes": ["SOXS", "SOXX"]} in msg
    assert json.loads(declaration([], "r")) == [{"id": "r"}]   # clears everything


def test_backoff_doubles_with_jitter_and_resets() -> None:
    b = Backoff(1, 10, rand=lambda: 0.5)   # 0.5 -> jitter factor 1.0
    assert [b.next_delay() for _ in range(6)] == [1, 2, 4, 8, 10, 10]
    b.reset()
    assert b.next_delay() == 1
    lo, hi = Backoff(1, 10, rand=lambda: 0.0), Backoff(1, 10, rand=lambda: 1.0)
    assert lo.next_delay() == pytest.approx(0.8) and hi.next_delay() == pytest.approx(1.2)


class FakeWS:
    def __init__(self, frames: list[str]) -> None:
        self.frames, self.sent = list(frames), []

    async def __aenter__(self) -> FakeWS:
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def send(self, m: str) -> None:
        self.sent.append(m)

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for f in self.frames:
                yield f
        return gen()


class FakeToss:
    def __init__(self) -> None:
        self.token_serial, self.dropped = 1, []

    async def stream_token(self) -> tuple[str, int]:
        return "tok-secret", self.token_serial

    async def drop_stream_token(self, t: str) -> None:
        self.dropped.append(t)


@pytest.mark.anyio
async def test_stream_reconnects_with_backoff_and_resubscribes() -> None:
    frames = [json.dumps({"type": "message", "topic": "trade:us:SOXS", "data": TRADES[0]})]
    sockets: list[FakeWS] = []
    sleeps: list[float] = []
    got: list[Any] = []
    connected = 0

    class Handshake401(Exception):
        response = SimpleNamespace(status_code=401)

    def connect(url: str, token: str) -> Any:
        assert token == "tok-secret"
        if len(sockets) == 1:
            sockets.append(None)  # second attempt: handshake refused
            raise Handshake401()
        ws = FakeWS(frames)
        sockets.append(ws)
        return ws

    async def on_connected() -> None:
        nonlocal connected
        connected += 1

    async def sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    toss = FakeToss()
    st = TossStream(toss, on_frame=got.append, on_connected=on_connected, connect=connect,
                    backoff=Backoff(1, 60, rand=lambda: 0.5), sleep=sleep, ping_seconds=1000)
    await st.set_subscriptions([("us", "SOXS")])
    with pytest.raises(asyncio.CancelledError):
        await st.run()
    assert connected == 2 and len(got) == 2              # data from both good connections
    assert json.loads(sockets[0].sent[0])[1] == {"type": "orderbook:us", "codes": ["SOXS"]}
    assert toss.dropped == ["tok-secret"]                # 401 -> fresh token next time
    assert sleeps == [1, 2, 1]   # good socket -> 1 s; failed handshake doubles; good socket resets
    assert "tok-secret" not in (st.message or "") and st.status in ("disconnected", "stopped")


# ── market hub ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_hub_snapshot_stream_merge_and_rest_fallback(tmp_path: Path) -> None:
    svc = make_services(tmp_path, handler=handler)
    await svc.refresh_calendar()
    hub = MarketHub(svc, stream_enabled=False, clock=lambda: NOW)
    hub.interest("SOXS")
    await hub.manage_once()                                # first open: REST snapshot
    st = hub.states["SOXS"]
    assert st.book_source == "rest" and len(st.tape) == 2
    tick = TradeTick(D("4.27"), D("3"), datetime(2026, 3, 25, 14, 30, 5, tzinfo=UTC), "USD")
    hub.on_frame(SimpleNamespace(kind="trade", symbol="SOXS", trade=tick, orderbook=None))
    hub.add_trades(st, parse_trades(TRADES))              # overlapping REST snapshot: no duplicates
    assert len(st.tape) == 3 and st.tape[-1].price == D("4.27")
    v = hub.view("SOXS", D("0.20"))
    assert v["spread"]["wide"] is True and v["levels_per_side"] == {"asks": 2, "bids": 2}
    assert v["session"] == "regular" and v["trades"][0]["price"] == D("4.27")   # newest first
    assert v["stream"]["status"] == "off"
    assert hub.spread_pct("SOXS") == v["spread"]["pct"]


@pytest.mark.anyio
async def test_hub_never_crashes_on_toss_error(tmp_path: Path) -> None:
    svc = make_services(tmp_path, handler=lambda r: httpx.Response(500, json={"error": {"code": "internal-error"}}))
    hub = MarketHub(svc, stream_enabled=False, clock=lambda: NOW)
    hub.interest("SOXS")
    await hub.manage_once()
    v = hub.view("SOXS", D("0.2"))
    assert v["asks"] == [] and v["spread"] is None and "internal-error" in v["error"]


# ── news ─────────────────────────────────────────────────────────────────

def test_dates_urls_titles() -> None:
    now = 1_774_440_000.0
    assert parse_epoch_seconds(1_774_400_000, now) == 1_774_400_000
    assert parse_epoch_seconds(1_774_400_000_000, now) is None     # ms or garbage: not guessed
    assert parse_epoch_seconds("1774400000", now) is None and parse_epoch_seconds(True, now) is None
    assert parse_epoch_seconds(now + 2 * 86400, now) is None        # far future
    assert url_key("https://WWW.Example.com/a/?utm_source=x&id=3#top") == "https://example.com/a?id=3"
    assert title_key("NVIDIA beats; shares +5%!") == title_key("Nvidia Beats, Shares 5")


def test_normalize_dedupe_fresh_and_kst() -> None:
    now = 1_774_440_000.0
    raw = {"headline": "Nvidia beats", "url": "https://x.com/a?utm_medium=1", "source": "Reuters",
           "datetime": 1_774_436_400, "related": "NVDA,AMD, bad ticker!", "summary": "s"}
    a = normalize_finnhub(raw, "SOXS", now)
    assert a.tickers == ["AMD", "NVDA", "SOXS"] and a.date_verified
    assert a.view()["published_kst"] == "2026-03-25T20:00:00+09:00"   # 11:00 UTC -> 20:00 KST
    assert normalize_finnhub({**raw, "url": "javascript:alert(1)"}, "SOXS", now) is None
    assert normalize_finnhub({**raw, "headline": ""}, "SOXS", now) is None
    b = normalize_finnhub({**raw, "url": "https://www.x.com/a", "related": "TSM"}, "SOXX", now)
    c = normalize_finnhub({**raw, "url": "https://y.com/z", "headline": "NVIDIA Beats!", "datetime": None}, "MU", now)
    assert not c.date_verified and c.view()["published_kst"] is None
    out = dedupe([a, b, c])
    assert len(out) == 1 and out[0].tickers == ["AMD", "MU", "NVDA", "SOXS", "SOXX", "TSM"]
    assert is_fresh(a, now, 48) and not is_fresh(a, now + 49 * 3600, 48)


def test_store_upsert_merge_and_tags(tmp_path: Path) -> None:
    s = NewsStore(tmp_path / "n.sqlite")
    now = 1_774_440_000.0
    a = NewsItem("fh:1", "Head", "src", "https://x.com/a", now - 60, ["SOXS"], first_seen=now)
    assert s.upsert([a]) == 1
    assert s.upsert([NewsItem("fh:2", "Head", "src", "https://x.com/a/", now - 60, ["NVDA"], first_seen=now)]) == 0
    assert s.recent(now - 3600)[0].tickers == ["NVDA", "SOXS"]
    assert [i.id for i in s.untagged(now - 3600, 10)] == ["fh:1"]
    s.set_tags({"fh:1": {"priority": "high", "direction": "hurts", "reason_en": "r", "reason_ko": "ㄹ"}})
    assert s.untagged(now - 3600, 10) == [] and s.recent(now - 3600)[0].tag["priority"] == "high"


@pytest.mark.anyio
async def test_feed_polls_finnhub_with_header_key(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    now = 1_774_440_000.0

    def fh(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.params["symbol"] == "AMD":
            return httpx.Response(429, json={})
        return httpx.Response(200, json=[{"headline": f"{req.url.params['symbol']} news", "url": "https://n.com/1",
                                          "source": "S", "datetime": int(now - 100), "related": ""}])

    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    cfg = NewsConfig(related={"SOXS": ["NVDA", "AMD", "MU"]}, finnhub_min_interval_seconds=1.0)
    feed = NewsFeed(cfg, "fh-secret-key", NewsStore(tmp_path / "n.sqlite"), watch=lambda: ["SOXS"],
                    http=httpx.AsyncClient(transport=httpx.MockTransport(fh)), clock=lambda: now, sleep=sleep)
    assert feed.watch_list() == ["SOXS", "NVDA", "AMD", "MU"]
    n = await feed.poll_once()
    assert n == 1                                          # SOXS + NVDA same url -> merged
    assert [c.url.params["symbol"] for c in calls] == ["SOXS", "NVDA", "AMD"]   # stops at 429
    assert all(c.headers["X-Finnhub-Token"] == "fh-secret-key" and "fh-secret" not in str(c.url) for c in calls)
    assert feed.status["AMD"].error == "rate-limited" and slept    # min interval respected
    assert feed.items({"NVDA"})[0].tickers == ["NVDA", "SOXS"]
    assert await feed.poll_once() == 0                     # not due yet
    nokey = NewsFeed(cfg, "", NewsStore(tmp_path / "m.sqlite"), watch=lambda: ["SOXS"])
    assert await nokey.poll_once() == 0


# ── tagging ──────────────────────────────────────────────────────────────

def test_validate_tags() -> None:
    data = {"items": [{"id": "a", "priority": "high", "direction": "hurts", "reason_en": "x", "reason_ko": "y"},
                      {"id": "zzz", "priority": "high", "direction": "hurts", "reason_en": "", "reason_ko": ""},
                      {"id": "b", "priority": "urgent", "direction": "hurts", "reason_en": "", "reason_ko": ""}]}
    assert list(validate_tags(data, {"a", "b"})) == ["a"]
    assert validate_tags("junk", {"a"}) == {}


class FakeEngine:
    def __init__(self, text: str, over: bool = False) -> None:
        self.key_set, self.records, self.calls = True, [], []
        self._text = text
        self.usage = SimpleNamespace(totals=lambda now: (SimpleNamespace(usd=D("9") if over else D("0")),
                                                         SimpleNamespace(usd=D("0")), None))
        self.svc = SimpleNamespace(config=SimpleNamespace(budget=SimpleNamespace(
            daily_usd=D("5"), monthly_usd=D("100"), warn_at=D("0.8"))))

    def tier(self, _n: str) -> Any:
        return SimpleNamespace(model="claude-haiku-5-5", max_tokens=4000, effort="low")

    def client(self) -> Any:
        async def create(**kw: Any) -> Any:
            self.calls.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)], stop_reason="end_turn")
        return SimpleNamespace(messages=SimpleNamespace(create=create))

    def record_usage(self, msg: Any, **kw: Any) -> None:
        self.records.append(kw)


@pytest.mark.anyio
async def test_tagger_batches_logs_usage_and_pauses_on_budget(tmp_path: Path) -> None:
    now = 1_774_440_000.0
    s = NewsStore(tmp_path / "n.sqlite")
    s.upsert([NewsItem("a", "NVDA up", "S", "https://x/a", now, ["NVDA"], first_seen=now),
              NewsItem("b", "MU down", "S", "https://x/b", now, ["MU"], first_seen=now)])
    eng = FakeEngine(json.dumps({"items": [{"id": "a", "priority": "high", "direction": "hurts",
                                            "reason_en": "bullish for chips, bad for SOXS", "reason_ko": "반도체 강세"}]}))
    hold = [{"symbol": "SOXS", "name": "Direxion Daily Semiconductor Bear 3X", "qty": "10"}]
    t = NewsTagger(eng, s, 20, 48, holdings=lambda: hold, clock=lambda: now)
    assert await t.tag_once() == 2
    assert eng.records[0]["purpose"] == "news_tag"
    kw = eng.calls[0]
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["model"] == "claude-haiku-5-5"
    assert "Bear 3X" in kw["messages"][0]["content"]
    tags = {i.id: i.tag for i in s.recent(now - 10)}
    assert tags["a"]["direction"] == "hurts" and tags["b"]["reason_en"] == "not labelled"   # no retry loop
    assert await t.tag_once() == 0
    s.upsert([NewsItem("c", "new", "S", "https://x/c", now, ["SOXS"], first_seen=now)])
    paused = NewsTagger(FakeEngine("{}", over=True), s, 20, 48, holdings=list, clock=lambda: now)
    assert await paused.tag_once() == 0 and paused.status == "paused: budget"


# ── chat: search_news + spread in scenario costs ─────────────────────────

def test_search_news_tool_and_spread_cost(tmp_path: Path) -> None:
    from app.chat.scenarios import CostModel, evaluate_table
    from app.chat.tools import search_news

    now = 1_774_440_000.0
    s = NewsStore(tmp_path / "n.sqlite")
    s.upsert([NewsItem("a", "Nvidia export curbs", "Reuters", "https://x/a", now - 60, ["NVDA"], first_seen=now),
              NewsItem("b", "AMD event", "S", "https://x/b", now - 30, ["AMD"], first_seen=now),
              NewsItem("c", "Old NVDA thing", "S", "https://x/c", None, ["NVDA"], first_seen=now)])
    s.set_tags({"a": {"priority": "high", "direction": "hurts", "reason_en": "r", "reason_ko": "r"}})
    feed = NewsFeed(NewsConfig(), "k", s, watch=lambda: ["SOXS"], clock=lambda: now)
    out = search_news(SimpleNamespace(news=feed), "any important news on NVDA?")
    assert out["available"] and [i["url"] for i in out["items"]] == ["https://x/a", "https://x/c"]
    assert out["items"][0]["priority"] == "high" and out["items"][1]["published_kst"] == "date unverified"
    assert search_news(SimpleNamespace(news=None), "NVDA")["available"] is False

    row = {"direction": "long", "entry": 4.0, "invalidation": 3.8, "target": 4.5, "entry_basis": "a",
           "invalidation_basis": "b", "target_basis": "c", "reasoning": "d"}
    r = evaluate_table([row], [D("4.0"), D("3.8"), D("4.5")], CostModel(D("0.001"), D("10"), D("0.25")))[0]
    assert r["costs_pct_est"] == "0.650" and r["spread_pct_est"] == "0.250"   # 0.2 fees + 0.2 FX + 0.25 spread
    assert r["net_move_pct_est"] == "11.85"


# ── routes ───────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_routes_orderbook_and_news(tmp_path: Path) -> None:
    svc = make_services(tmp_path, handler=handler)
    await svc.refresh_calendar()
    await svc.refresh_stocks()
    svc.market = MarketHub(svc, stream_enabled=False, clock=lambda: NOW)
    now = 1_774_440_000.0
    st = NewsStore(tmp_path / "n.sqlite")
    st.upsert([NewsItem("a", "SOXS headline", "S", "https://x/a", now, ["SOXS"], first_seen=now)])
    svc.news = NewsFeed(NewsConfig(), "", st, watch=lambda: ["SOXS"], clock=lambda: now)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        ob = c.get("/api/orderbook", params={"symbol": "SOXS"}).json()
        assert ob["asks"][0] == {"price": "4.26", "volume": "300"} and ob["spread"]["abs"] == "0.01"
        assert c.get("/api/orderbook", params={"symbol": "NOPE!"}).status_code == 400
        nw = c.get("/api/news", params={"symbols": "SOXS"}).json()
        assert nw["items"][0]["headline"] == "SOXS headline" and nw["status"]["key"] == "empty"
        assert c.get("/api/news", params={"priority": "high"}).json()["items"] == []
        assert "fh-secret" not in json.dumps(nw)


# ── phase 4 part 1: read-only account route ──────────────────────────────

@pytest.mark.anyio
async def test_account_route_masks_number_and_only_reads(tmp_path: Path) -> None:
    from app.orders.account import AccountService, pick_account
    from tests.test_toss import account_routes

    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return account_routes(req)

    svc = make_services(tmp_path, handler=h)
    svc.account = AccountService(svc.toss, account_seq=None, refresh_seconds=10, clock=lambda: NOW)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        body = c.get("/api/account").json()
    assert body["dry_run"] is True and body["account"]["masked"] == "*******8901"
    assert "12345678901" not in json.dumps(body)
    assert body["buying_power"]["usd"] == "1234.56" and body["open_orders"][0]["status"] == "PARTIAL_FILLED"
    assert body["open_conditional"][0]["type"] == "OCO" and body["holdings"]["items"][0]["symbol"] == "SOXS"
    assert svc.account.held_quantity("SOXS") == D("10") and svc.account.held_quantity("NVDA") == 0
    assert all(r.method == "GET" or r.url.path == "/oauth2/token" for r in seen)
    from app.data.toss import Account
    two = [Account("1111", 1, "BROKERAGE"), Account("2222", 2, "BROKERAGE")]
    assert pick_account(two, None)[0].account_seq == 1 and "account_seq" in pick_account(two, None)[1]
    assert pick_account(two, 2)[0].account_seq == 2 and pick_account(two, 9)[0] is None


def test_finnhub_tls_uses_os_store_but_still_verifies():
    """Finnhub uses the OS certificate list (company proxy), never 'skip verification'."""
    import ssl

    import truststore

    ctx = finnhub_tls_context()
    assert isinstance(ctx, truststore.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
