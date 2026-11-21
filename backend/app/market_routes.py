"""Phase 3 routes and background loops: orderbook/trades (live stream), news feed, AI tags.

/api/orderbook?symbol=          snapshot view (ladder, spread, session, tape, stream status)
/api/stream/market?symbol=      Server-Sent Events: the same view whenever it changes (throttled)
/api/news?symbols=&priority=    news items with tags + feed/tagging status
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import store
from app.market import MarketHub
from app.news.feed import NewsFeed, NewsStore
from app.news.tagger import NewsTagger
from app.services import InputError, Services, jsonable

log = logging.getLogger("desk.market")
router = APIRouter(prefix="/api")
WATCH_TTL_S = 600     # a ticker shown on screen in the last 10 min gets news
LOOP_SECONDS = 30


def _svc(request: Request) -> Services:
    return request.app.state.services


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


# ── setup ────────────────────────────────────────────────────────────────

def watched_symbols(svc: Services) -> list[str]:
    """Screen tickers (last 10 min) + held tickers + the default ticker. Not a fixed list."""
    now = svc.clock()
    out = [s for s, t in sorted(svc._chart_symbols.items(), key=lambda kv: -kv[1]) if now - t < WATCH_TTL_S]
    try:
        for t in store.read_positions(svc.positions_path).get("trades", []):
            s = str(t.get("symbol", "")).upper() if isinstance(t, dict) else ""
            if s and s not in out:
                out.append(s)
    except store.StoreError:
        pass
    for s in svc.pinned_symbols():                 # phase 6: the pinned watchlist folder
        if s not in out:
            out.append(s)
    d = svc.config.tickers.default.upper()
    if d not in out:
        out.append(d)
    return out


def holdings(svc: Services) -> list[dict[str, str]]:
    """What the user holds (qty > 0), with names, for the tagger's helps/hurts call."""
    try:
        res = svc._compute_pnl(store.read_positions(svc.positions_path), "all")
    except Exception:  # noqa: BLE001 - tagging works without holdings
        return []
    out = []
    for row in res.symbols:
        if row.qty > 0:
            s = svc.stocks.get(row.symbol)
            out.append({"symbol": row.symbol, "name": s.name if s else "", "qty": format(row.qty, "f")})
    return out


def build(svc: Services, chat: Any, finnhub_key: str) -> None:
    """Attach market hub, news feed, tagger and account data to the services object."""
    from app.orders.account import AccountService
    oc = svc.config.orders
    svc.account = AccountService(svc.toss, account_seq=oc.account_seq, refresh_seconds=oc.account_refresh_seconds)
    ms, nc = svc.config.market_stream, svc.config.news
    svc.market = MarketHub(svc, stream_enabled=ms.enabled, rest_poll_seconds=ms.rest_poll_seconds,
                           ping_seconds=ms.ping_seconds, backoff_start=ms.backoff_start_seconds,
                           backoff_max=ms.backoff_max_seconds)
    from app.rules.routes import engine
    engine(svc)                       # automatic rules: loads logs/rules.json (armed rules keep watching)
    news_store = NewsStore()
    from app.data.finnhub_fund import gate_of       # one Finnhub gate for news + fundamentals
    svc.news = NewsFeed(nc, finnhub_key, news_store, watch=lambda: watched_symbols(svc), gate=gate_of(svc))
    if chat is not None:
        svc.tagger = NewsTagger(chat, news_store, nc.tag_batch_size, nc.max_age_hours,
                                holdings=lambda: holdings(svc), clock=time.time,
                                enabled=lambda: svc.config.news.tag_enabled)


async def news_loop(svc: Services) -> None:
    while True:
        try:
            if svc.news is not None:
                await svc.news.poll_once()
            if svc.tagger is not None:
                while await svc.tagger.tag_once():   # drain the backlog in batches
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never die
            log.warning("news loop: %s", type(exc).__name__)
        await asyncio.sleep(LOOP_SECONDS)


def start(svc: Services) -> list[asyncio.Task[None]]:
    tasks = []
    if svc.market is not None:
        svc.market.start()
    if svc.account is not None:
        svc.account.start()
    if getattr(svc, "rules", None) is not None:
        svc.rules.start()
    if svc.news is not None:
        tasks.append(asyncio.create_task(news_loop(svc), name="news"))
    return tasks


async def stop(svc: Services, tasks: list[asyncio.Task[None]]) -> None:
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if svc.market is not None:
        await svc.market.stop()
    if svc.account is not None:
        await svc.account.stop()
    if getattr(svc, "rules", None) is not None:
        await svc.rules.stop()
    if svc.news is not None:
        await svc.news.aclose()
    if getattr(svc, "_research", None) is not None:   # phase 6: SEC / Finnhub clients of the Company view
        await svc._research.aclose()


# ── routes ───────────────────────────────────────────────────────────────

async def _view(svc: Services, sym: str) -> dict[str, Any]:
    hub: MarketHub = svc.market
    hub.interest(sym)
    st = hub.states.get(sym)
    if st is not None and st.book is None and st.last_rest == 0.0:
        await hub.snapshot(sym)   # first open: don't wait for the background loop
    return jsonable(hub.view(sym, svc.config.spread.warn_pct))


@router.get("/tickers/symbols")
async def ticker_symbols(request: Request) -> JSONResponse:
    """Every listed US ticker (Toss stock list) so the screen can link tickers in chat answers to Research."""
    svc = _svc(request)
    return JSONResponse({"symbols": sorted(svc.stocks), "count": len(svc.stocks)})


@router.get("/events")
async def events(request: Request, days: int = 0) -> JSONResponse:
    """Upcoming events (macro releases, FOMC, earnings, holidays / early closes, options expiry, your own events)."""
    from app.events.service import events_of

    svc = _svc(request)
    if not svc.config.event_calendar.enabled:
        return _fail(404, "event calendar switched off (config event_calendar.enabled)")
    return JSONResponse(jsonable(events_of(svc).view(min(max(days, 0), 60) or None)))


@router.get("/options")
async def options(request: Request, symbol: str = "") -> JSONResponse:
    """Options-implied context (expected moves, IV, skew ...), calculated in code; yfinance ~15 min."""
    from app.options_view import options_view

    svc = _svc(request)
    try:
        sym = svc.validate_symbol(symbol)
    except InputError as exc:
        return _fail(400, str(exc))
    return JSONResponse(jsonable(await options_view(svc, sym)))


@router.get("/orderbook")
async def orderbook(request: Request, symbol: str = "") -> JSONResponse:
    svc = _svc(request)
    if svc.market is None:
        return _fail(404, "orderbook not available")
    try:
        sym = svc.validate_symbol(symbol)
    except InputError as exc:
        return _fail(400, str(exc))
    return JSONResponse(await _view(svc, sym))


@router.get("/stream/market")
async def stream_market(request: Request, symbol: str = "") -> Any:
    svc = _svc(request)
    if svc.market is None:
        return _fail(404, "orderbook not available")
    try:
        sym = svc.validate_symbol(symbol)
    except InputError as exc:
        return _fail(400, str(exc))
    hub: MarketHub = svc.market
    gap = 1.0 / svc.config.market_stream.push_per_second

    async def events() -> AsyncIterator[bytes]:
        seen = -1
        while not await request.is_disconnected():
            v = await hub.wait_change(seen, 15.0)
            if v == seen:
                yield b": keep-alive\n\n"
                hub.interest(sym)
                continue
            seen = v
            yield f"data: {json.dumps(await _view(svc, sym), ensure_ascii=False)}\n\n".encode()
            await asyncio.sleep(gap)   # throttle: at most push_per_second updates

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/account")
async def account(request: Request, refresh: bool = False) -> JSONResponse:
    """Read-only account data (buying power, holdings, open orders). Account number masked."""
    svc = _svc(request)
    if svc.account is None:
        return _fail(404, "account data not available")
    if refresh or svc.account.state.updated is None:
        await svc.account.refresh_now()
    return JSONResponse(jsonable(svc.account.view(svc.config.orders.dry_run)))


@router.get("/news")
async def news(request: Request, symbols: str = "", priority: str = "") -> JSONResponse:
    svc = _svc(request)
    feed: NewsFeed | None = svc.news
    if feed is None:
        return _fail(404, "news not available")
    wanted = {s.strip().upper() for s in symbols.split(",") if s.strip()} or None
    items = feed.items(wanted)
    if priority in ("high", "medium", "low"):
        items = [i for i in items if i.tag and i.tag.get("priority") == priority]
    tagger = svc.tagger
    return JSONResponse(jsonable({
        "items": [i.view() for i in items[:200]],
        "status": {
            "provider": "Finnhub", "key": "set" if feed.key else "empty",
            "watching": feed.watch_list(),
            "symbols": {s: {"last_ok": st.last_ok, "error": st.error, "count": st.count}
                        for s, st in feed.status.items()},
            "tagging": tagger.status if tagger else "off",
            "tagging_error": tagger.error if tagger else None,
            "max_age_hours": feed.cfg.max_age_hours,
        },
    }))
