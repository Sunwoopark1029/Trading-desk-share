"""Orderbook + trade tape per ticker: Toss WebSocket when connected, REST polling otherwise.

Tickers follow what the screen is looking at (any US stock/ETF), not a fixed list. The stream
has no snapshot and can drop frames, so a REST snapshot is loaded on every (re)connect and when
a ticker is first opened. Read-only: no order code here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.data.toss import Orderbook, TradeTick
from app.data.toss_ws import Backoff, Frame, TossStream
from app.timeutil import KST, to_epoch

log = logging.getLogger("desk.market")

ACTIVE_TTL_S = 60          # a ticker stays subscribed this long after the screen last asked
MAX_ACTIVE = 10
TAPE = 100                 # trades kept per ticker
SESSION_NAMES = ("day", "pre", "regular", "after")


def spread(book: Orderbook | None) -> dict[str, Any] | None:
    """Best ask - best bid, in price units and as % of the mid price. None if a side is empty."""
    if book is None or not book.asks or not book.bids:
        return None
    ask, bid = book.asks[0].price, book.bids[0].price
    mid = (ask + bid) / 2
    gap = ask - bid
    pct = (gap / mid * 100) if mid > 0 else None
    return {"bid": bid, "ask": ask, "mid": mid, "abs": gap,
            "pct": pct.quantize(Decimal("0.0001")) if pct is not None else None,
            "crossed": gap < 0}


def session_at(epoch: float, sessions: list[Any]) -> str | None:
    """Name of the session (day/pre/regular/after) containing epoch, from the Toss calendar."""
    for s in sessions:
        if s.start <= epoch < s.end:
            return s.name
    return None


@dataclass
class TickerState:
    book: Orderbook | None = None
    book_source: str | None = None        # "stream" | "rest"
    book_updated: float | None = None
    tape: deque[TradeTick] = field(default_factory=lambda: deque(maxlen=TAPE))
    tape_updated: float | None = None
    last_rest: float = 0.0
    error: str | None = None


def _tick_key(t: TradeTick) -> tuple[Any, ...]:
    return (t.timestamp, t.price, t.volume)


class MarketHub:
    def __init__(self, svc: Any, *, stream_enabled: bool = True, rest_poll_seconds: float = 2.0,
                 ping_seconds: float = 60.0, backoff_start: float = 1.0, backoff_max: float = 60.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.svc = svc
        self.clock = clock
        self.rest_poll_seconds = rest_poll_seconds
        self.states: dict[str, TickerState] = {}
        self.active: dict[str, float] = {}
        self.version = 0
        self._last_stream_state: tuple[Any, ...] | None = None
        self._changed = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self.stream: TossStream | None = None
        if stream_enabled and svc.toss is not None:
            self.stream = TossStream(svc.toss, on_frame=self.on_frame, on_connected=self.on_connected,
                                     ping_seconds=ping_seconds, backoff=Backoff(backoff_start, backoff_max))

    # ── interest ─────────────────────────────────────────────────────────

    def interest(self, symbol: str) -> None:
        self.active[symbol] = self.clock()
        self.states.setdefault(symbol, TickerState())

    def active_symbols(self) -> list[str]:
        now = self.clock()
        live = sorted((s for s, t in self.active.items() if now - t < ACTIVE_TTL_S),
                      key=lambda s: -self.active[s])[:MAX_ACTIVE]
        for s in [s for s in self.active if s not in live]:
            del self.active[s]
        return live

    # ── updates ──────────────────────────────────────────────────────────

    def _bump(self) -> None:
        self.version += 1
        self._changed.set()

    def on_frame(self, f: Frame) -> None:
        if not f.symbol:
            return
        st = self.states.setdefault(f.symbol, TickerState())
        now = self.clock()
        if f.kind == "orderbook" and f.orderbook is not None:
            st.book, st.book_source, st.book_updated = f.orderbook, "stream", now
        elif f.kind == "trade" and f.trade is not None:
            self.add_trades(st, [f.trade])
            st.tape_updated = now
        self._bump()

    @staticmethod
    def add_trades(st: TickerState, ticks: list[TradeTick]) -> None:
        """Merge without duplicates (a REST snapshot overlaps stream ticks), keep time order."""
        seen = {_tick_key(t) for t in st.tape}
        merged = list(st.tape) + [t for t in ticks if _tick_key(t) not in seen]
        merged.sort(key=lambda t: t.timestamp)
        st.tape.clear()
        st.tape.extend(merged[-TAPE:])

    async def snapshot(self, symbol: str) -> None:
        """REST orderbook + last 50 trades. Errors are recorded, never raised."""
        toss = self.svc.toss
        if toss is None:
            return
        st = self.states.setdefault(symbol, TickerState())
        st.last_rest = self.clock()
        try:
            book = await toss.orderbook(symbol)
            ticks = await toss.trades(symbol)
        except Exception as exc:  # noqa: BLE001
            from app.services import error_info
            st.error = error_info(exc)[1]
            self._bump()
            return
        now = self.clock()
        st.error = None
        if st.book_source != "stream" or st.book_updated is None or now - st.book_updated > 5:
            st.book, st.book_source, st.book_updated = book, "rest", now
        self.add_trades(st, ticks)
        st.tape_updated = now
        self._bump()

    async def on_connected(self) -> None:
        for sym in self.active_symbols():
            await self.snapshot(sym)

    # ── loops ────────────────────────────────────────────────────────────

    def stream_connected(self) -> bool:
        return self.stream is not None and self.stream.status == "connected"

    async def manage_once(self) -> None:
        if self.stream is not None:  # connection status changes are pushed to the screen too
            state = (self.stream.status, self.stream.message, self.stream.retry_in)
            if state != self._last_stream_state:
                self._last_stream_state = state
                self._bump()
        syms = self.active_symbols()
        if self.stream is not None:
            await self.stream.set_subscriptions(("us", s) for s in syms)
        now = self.clock()
        for s in syms:
            st = self.states.setdefault(s, TickerState())
            first = st.book is None and st.last_rest == 0.0
            if first or (not self.stream_connected() and now - st.last_rest >= self.rest_poll_seconds):
                await self.snapshot(s)

    async def _manage_loop(self) -> None:
        while True:
            try:
                await self.manage_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never die
                log.warning("market manager: %s", type(exc).__name__)
            await asyncio.sleep(0.5)

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._manage_loop(), name="market-manage"))
        if self.stream is not None:
            self._tasks.append(asyncio.create_task(self.stream.run(), name="market-stream"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def wait_change(self, seen: int, timeout: float) -> int:
        if self.version == seen:
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout)
            except TimeoutError:
                pass
        return self.version

    # ── views ────────────────────────────────────────────────────────────

    def last_trade(self, symbol: str) -> TradeTick | None:
        st = self.states.get(symbol)
        return st.tape[-1] if st and st.tape else None

    def spread_pct(self, symbol: str) -> Decimal | None:
        st = self.states.get(symbol)
        sp = spread(st.book) if st else None
        return sp["pct"] if sp and not sp["crossed"] else None

    def stream_status(self) -> dict[str, Any]:
        if self.stream is None:
            return {"status": "off", "message": "Toss keys not set" if self.svc.toss is None else "stream disabled",
                    "retry_in": None, "rejected": []}
        return {"status": self.stream.status, "message": self.stream.message,
                "retry_in": self.stream.retry_in, "rejected": self.stream.rejected}

    def view(self, symbol: str, warn_pct: Decimal) -> dict[str, Any]:
        st = self.states.get(symbol) or TickerState()
        book = st.book
        sp = spread(book)
        now = self.clock()

        def kst(epoch: float | None) -> str | None:
            return None if epoch is None else datetime.fromtimestamp(epoch, UTC).astimezone(KST).isoformat()

        return {
            "symbol": symbol,
            "currency": (book.currency if book else None) or "USD",
            "asks": [{"price": lv.price, "volume": lv.volume} for lv in (book.asks if book else ())],
            "bids": [{"price": lv.price, "volume": lv.volume} for lv in (book.bids if book else ())],
            "levels_per_side": {"asks": len(book.asks) if book else 0, "bids": len(book.bids) if book else 0},
            "book_time": book.timestamp.astimezone(KST).isoformat() if book and book.timestamp else None,
            "book_source": st.book_source,
            "book_updated": kst(st.book_updated),
            "spread": None if sp is None else {**sp, "wide": bool(sp["pct"] is not None and sp["pct"] > warn_pct)},
            "spread_warn_pct": warn_pct,
            "trades": [{"price": t.price, "volume": t.volume, "time": t.timestamp.astimezone(KST).isoformat()}
                       for t in reversed(list(st.tape)[-50:])],
            "session": session_at(now, self.svc.sessions()),
            "stream": self.stream_status(),
            "error": st.error,
        }


def trade_epoch(t: TradeTick) -> int:
    return to_epoch(t.timestamp)
