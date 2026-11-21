"""Toss WebSocket stream for orderbook + trades (official AsyncAPI, read-only).

Facts from the docs: same bearer token as REST on the handshake; one JSON array per message
REPLACES the whole subscription set; text "PING" -> {"type":"pong"}; server closes after 180 s
without client traffic; no initial snapshot, no sequence numbers, frames can be dropped (so the
caller loads a REST snapshot after every (re)connect); max 2 connections and 100 subscriptions
per account; reconnect with exponential backoff + jitter.

The token is only ever put in the handshake header. It is never logged or put in errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from app.data.toss import (
    WS_URL, Orderbook, TossClient, TossError, TossIpNotAllowed, TossTokenRevoked, TradeTick,
    parse_orderbook, parse_trade,
)

log = logging.getLogger("desk.ws")
logging.getLogger("websockets").setLevel(logging.WARNING)  # handshake logs could show headers

MAX_SUBSCRIPTIONS = 100


class Backoff:
    """1 s, 2 s, 4 s ... up to max, each x [0.8, 1.2] jitter. reset() after a good connection."""

    def __init__(self, start: float = 1.0, maximum: float = 60.0, rand: Callable[[], float] = random.random) -> None:
        self.start, self.maximum, self.rand = start, maximum, rand
        self.attempt = 0

    def next_delay(self) -> float:
        base = min(self.maximum, self.start * (2 ** self.attempt))
        self.attempt += 1
        return base * (0.8 + 0.4 * self.rand())

    def reset(self) -> None:
        self.attempt = 0


def declaration(subs: Iterable[tuple[str, str]], req_id: str) -> str:
    """[(market, symbol)] -> one full-replace declaration for orderbook + trades."""
    by_market: dict[str, list[str]] = {}
    for market, sym in subs:
        by_market.setdefault(market, []).append(sym)
    msg: list[dict[str, Any]] = [{"id": req_id}]
    for market, syms in sorted(by_market.items()):
        syms = sorted(set(syms))
        msg.append({"type": f"orderbook:{market}", "codes": syms})
        msg.append({"type": f"trade:{market}", "codes": syms})
    return json.dumps(msg, separators=(",", ":"))


@dataclass
class Frame:
    kind: str                     # orderbook | trade | ack | pong | error | other
    market: str | None = None
    symbol: str | None = None
    orderbook: Orderbook | None = None
    trade: TradeTick | None = None
    code: str | None = None       # error code / rejected codes
    rejected: list[dict[str, Any]] = field(default_factory=list)


def parse_frame(raw: str | bytes) -> Frame:
    """One incoming frame. Never raises: bad frames come back as kind "other"."""
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return Frame("other")
    if not isinstance(msg, dict):
        return Frame("other")
    t = msg.get("type")
    if t == "pong":
        return Frame("pong")
    if t == "subscriptions":
        rej = msg.get("rejected") if isinstance(msg.get("rejected"), list) else []
        return Frame("ack", rejected=[r for r in rej if isinstance(r, dict)])
    if t == "error":
        return Frame("error", code=str(msg.get("code") or (msg.get("error") or {}).get("code") or "error"))
    if t == "message":
        topic = str(msg.get("topic") or "")
        parts = topic.split(":")
        data = msg.get("data")
        if len(parts) != 3 or not isinstance(data, dict):
            return Frame("other")
        channel, market, symbol = parts
        try:
            if channel == "orderbook":
                return Frame("orderbook", market, symbol, orderbook=parse_orderbook(data))
            if channel == "trade":
                return Frame("trade", market, symbol, trade=parse_trade(data))
        except (KeyError, TypeError, ValueError, TossError):
            return Frame("other")
    return Frame("other")


Connect = Callable[..., Any]


def _default_connect(url: str, token: str) -> Any:
    from websockets.asyncio.client import connect

    return connect(url, additional_headers={"Authorization": f"Bearer {token}"},
                   ping_interval=30, ping_timeout=30, close_timeout=5, max_size=2 ** 20)


class TossStream:
    """Keeps one connection open, re-declares subscriptions, reconnects with backoff.

    Callbacks: on_frame(Frame) for data; on_connected() after each (re)connect (load REST
    snapshots there); status is readable from .status / .retry_at / .message.
    """

    def __init__(self, toss: TossClient, *, on_frame: Callable[[Frame], None],
                 on_connected: Callable[[], Awaitable[None]], url: str = WS_URL,
                 ping_seconds: float = 60.0, backoff: Backoff | None = None,
                 connect: Callable[[str, str], Any] = _default_connect,
                 clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.toss = toss
        self.on_frame = on_frame
        self.on_connected = on_connected
        self.url = url
        self.ping_seconds = ping_seconds
        self.backoff = backoff or Backoff()
        self._connect = connect
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._sleep = sleep
        self._subs: set[tuple[str, str]] = set()
        self._ws: Any = None
        self._req = 0
        self.status = "starting"        # connected | disconnected | stopped
        self.message: str | None = None  # safe, short
        self.retry_in: float | None = None
        self.rejected: list[dict[str, Any]] = []
        self.frames = 0

    # ── subscriptions ────────────────────────────────────────────────────

    def subscriptions(self) -> set[tuple[str, str]]:
        return set(self._subs)

    async def set_subscriptions(self, subs: Iterable[tuple[str, str]]) -> None:
        new = set(list(subs)[: MAX_SUBSCRIPTIONS // 2])  # orderbook + trade = 2 per symbol
        if new == self._subs:
            return
        self._subs = new
        if self._ws is not None:
            await self._declare()

    async def _declare(self) -> None:
        self._req += 1
        try:
            await self._ws.send(declaration(self._subs, f"req-{self._req}"))
        except Exception:  # noqa: BLE001 - the read loop notices the broken socket
            pass

    # ── main loop ────────────────────────────────────────────────────────

    async def run(self) -> None:
        while True:
            token: str | None = None
            try:
                token, _serial = await self.toss.stream_token()
                async with self._connect(self.url, token) as ws:
                    self._ws = ws
                    self.status, self.message, self.retry_in = "connected", None, None
                    await self._declare()
                    await self.on_connected()
                    pinger = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            self.backoff.reset()
                            self._handle(raw)
                    finally:
                        pinger.cancel()
                self.message = "closed by server"
            except asyncio.CancelledError:
                self.status = "stopped"
                raise
            except TossTokenRevoked:
                self.message = "token replaced elsewhere (second copy running?)"
            except TossIpNotAllowed:
                self.message = "Register this PC's IP in Toss WTS (Settings > Open API)"
            except TossError as exc:
                self.message = f"Toss error: {exc.code}"
            except Exception as exc:  # noqa: BLE001 - never str(exc): handshake errors may echo headers
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 401 and token:
                    await self.toss.drop_stream_token(token)
                self.message = f"stream error ({type(exc).__name__}{f' {status}' if status else ''})"
            finally:
                self._ws = None
            self.status = "disconnected"
            delay = self.backoff.next_delay()
            self.retry_in = delay
            log.info("toss stream: %s; retry in %.1fs", self.message, delay)
            await self._sleep(delay)

    def _handle(self, raw: str | bytes) -> None:
        f = parse_frame(raw)
        if f.kind in ("orderbook", "trade"):
            self.frames += 1
            self.on_frame(f)
        elif f.kind == "ack":
            self.rejected = f.rejected
        elif f.kind == "error":
            # auth-revoked after our own token renewal is normal: the socket closes, we reconnect
            self.message = f"server: {f.code}"

    async def _ping_loop(self, ws: Any) -> None:
        while True:
            await self._sleep(self.ping_seconds)
            try:
                await ws.send("PING")
            except Exception:  # noqa: BLE001
                return
