"""Read-only account data from Toss: buying power, holdings, open orders, open conditional orders.

Refreshed every `orders.account_refresh_seconds` and right after any order action (refresh_now).
The account number is personal: only the masked form ever leaves this module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.data.toss import (
    ACCOUNT_TYPES_OK, Account, ConditionalOrder, Holdings, Order, TossClient,
)
from app.orders.fills import FillHistory
from app.timeutil import KST

log = logging.getLogger("desk.account")


@dataclass
class AccountState:
    account: Account | None = None
    note: str | None = None                 # e.g. several accounts found
    buying_power_usd: Decimal | None = None
    buying_power_krw: Decimal | None = None
    holdings: Holdings | None = None
    open_orders: list[Order] = field(default_factory=list)
    open_conditional: list[ConditionalOrder] = field(default_factory=list)
    updated: float | None = None
    error: str | None = None


def pick_account(accounts: list[Account], wanted_seq: int | None) -> tuple[Account | None, str | None]:
    """The brokerage account to use. With several, config orders.account_seq decides."""
    ok = [a for a in accounts if a.account_type in ACCOUNT_TYPES_OK] or accounts
    if wanted_seq is not None:
        hit = next((a for a in ok if a.account_seq == wanted_seq), None)
        return (hit, None) if hit else (None, f"orders.account_seq {wanted_seq} not found among your accounts")
    if not ok:
        return None, "no brokerage account found"
    if len(ok) > 1:
        return ok[0], (f"{len(ok)} accounts found; using the first ({ok[0].masked()}). "
                       "Set orders.account_seq in config.yaml to choose")
    return ok[0], None


class AccountService:
    def __init__(self, toss: TossClient | None, *, account_seq: int | None, refresh_seconds: float,
                 clock: Callable[[], float] = time.time) -> None:
        self.toss = toss
        self.account_seq = account_seq
        self.refresh_seconds = refresh_seconds
        self.clock = clock
        self.state = AccountState()
        self.fills = FillHistory()
        self.fills_every = 60.0          # seconds between fill-history refreshes (full once, then recent days)
        self._fills_at = 0.0
        self.closed_conditional: list[ConditionalOrder] = []   # for the journal (triggered order ids)
        self._closed_cond_at = 0.0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def refresh_now(self) -> AccountState:
        """One full read. Errors are recorded (safe text), never raised."""
        if self.toss is None:
            self.state.error = "Toss keys not set in .env"
            return self.state
        async with self._lock:
            st = self.state
            try:
                if st.account is None:
                    st.account, st.note = pick_account(await self.toss.accounts(), self.account_seq)
                if st.account is None:
                    st.error = st.note
                    return st
                seq = st.account.account_seq
                st.buying_power_usd = await self.toss.buying_power(seq, "USD")
                st.buying_power_krw = await self.toss.buying_power(seq, "KRW")
                st.holdings = await self.toss.holdings(seq)
                st.open_orders = await self.toss.open_orders(seq)
                conds: list[ConditionalOrder] = []
                cursor: str | None = None
                for _ in range(10):  # paged; 10 pages x 100 is far more than anyone keeps open
                    page, cursor = await self.toss.conditional_orders(seq, "OPEN", cursor)
                    conds.extend(page)
                    if not cursor:
                        break
                st.open_conditional = conds
                st.updated, st.error = self.clock(), None
            except Exception as exc:  # noqa: BLE001
                from app.services import error_info
                code, msg = error_info(exc)
                st.error = msg
                if code == "account-not-found":
                    st.account = None  # pick again next time
                log.warning("account refresh failed: %s", code)
            return st

    async def refresh_fills(self, force: bool = False) -> None:
        """Closed-order fills + transactions for P&L. Errors recorded, never raised."""
        st = self.state
        if self.toss is None or st.account is None:
            return
        now = self.clock()
        if not force and now - self._fills_at < self.fills_every:
            return
        self._fills_at = now
        try:
            await self.fills.refresh(self.toss, st.account.account_seq, now, full=not self.fills.loaded)
            self.fills.error = None
        except Exception as exc:  # noqa: BLE001
            from app.services import error_info
            self.fills.error = error_info(exc)[1]
            log.warning("fill history failed: %s", error_info(exc)[0])

    async def refresh_closed_conditional(self, every: float = 60.0) -> None:
        """Finished conditional orders (to find the orders they triggered). At most once a minute;
        errors are recorded in the log only (the journal then shows what it can)."""
        st = self.state
        now = self.clock()
        if self.toss is None or st.account is None or now - self._closed_cond_at < every:
            return
        self._closed_cond_at = now
        try:
            out: list[ConditionalOrder] = []
            cursor: str | None = None
            for _ in range(10):
                page, cursor = await self.toss.conditional_orders(st.account.account_seq, "CLOSED", cursor)
                out.extend(page)
                if not cursor:
                    break
            self.closed_conditional = out
        except Exception as exc:  # noqa: BLE001
            from app.services import error_info
            log.warning("closed conditional orders failed: %s", error_info(exc)[0])

    async def _loop(self) -> None:
        while True:
            await self.refresh_now()
            await self.refresh_fills()
            await asyncio.sleep(self.refresh_seconds)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="account")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def held_quantity(self, symbol: str) -> Decimal:
        h = self.state.holdings
        return sum((i.quantity for i in (h.items if h else ()) if i.symbol == symbol), Decimal(0))

    def status(self) -> str:
        """'pending' (never loaded), 'error' (never loaded, failed), 'stale' (last good read is older
        than 6 refresh periods, at least 60 s), else 'ok'."""
        st = self.state
        if st.updated is None:
            return "error" if st.error else "pending"
        if self.clock() - st.updated > max(60.0, 6 * self.refresh_seconds):
            return "stale"
        return "ok"

    def cash_snapshot(self) -> dict[str, Any]:
        """Buying power (USD + KRW) for the chat's <live> block. Read-only, no Toss call."""
        st = self.state
        status = self.status()
        if st.updated is None:
            return {"available": False, "status": status,
                    "message": st.error or "Toss account data not loaded yet"}
        return {
            "available": True, "status": status, "source": "Toss buying power: room for NEW orders, open buy orders already deducted (not the cash balance)",
            "buying_power_usd": None if st.buying_power_usd is None else format(st.buying_power_usd, "f"),
            "buying_power_krw": None if st.buying_power_krw is None else format(st.buying_power_krw, "f"),
            "as_of_kst": datetime.fromtimestamp(st.updated, UTC).astimezone(KST).replace(microsecond=0).isoformat(),
            "last_refresh_error": st.error,
        }

    def orders_snapshot(self) -> dict[str, Any]:
        """Open regular orders + open reserved (conditional) orders for the chat's <live> block.
        Compact, read-only, no Toss call. Order ids are left out (the AI cannot act on them)."""
        st = self.state
        status = self.status()
        if st.updated is None:
            return {"available": False, "status": status,
                    "message": st.error or "Toss account data not loaded yet"}

        def d(v: Decimal | None) -> str | None:
            return None if v is None else format(v, "f")

        def kst(t: datetime | None) -> str | None:
            return None if t is None else t.astimezone(KST).replace(microsecond=0).isoformat()

        def leg(lg: Any) -> dict[str, Any]:
            return {"side": lg.order_side, "trigger_price": d(lg.trigger_price),
                    "target_profit_rate_pct": d(lg.target_profit_rate),
                    "order_price": d(lg.order_price), "order_price_note": None if lg.order_price is not None
                    else "market order when triggered", "status": lg.status}

        return {
            "available": True, "status": status,
            "as_of_kst": datetime.fromtimestamp(st.updated, UTC).astimezone(KST).replace(microsecond=0).isoformat(),
            "open_orders": [{
                "symbol": o.symbol, "side": o.side, "order_type": o.order_type, "currency": o.currency,
                "price": d(o.price), "quantity": d(o.quantity), "order_amount": d(o.order_amount),
                "filled_quantity": d(o.execution.filled_quantity) if o.execution else "0",
                "time_in_force": o.time_in_force, "status": o.status, "ordered_at_kst": kst(o.ordered_at),
            } for o in st.open_orders],
            "reserved_orders": [{
                "symbol": c.symbol, "type": c.type, "order_type": c.order_type,
                "currency": "USD" if c.market == "US" else "KRW" if c.market == "KR" else c.market,
                "quantity": d(c.quantity), "legs": [leg(c.first)] + ([leg(c.second)] if c.second else []),
                "expire_date": c.expire_date, "status": c.status,
            } for c in st.open_conditional],
        }

    def view(self, dry_run: bool) -> dict[str, Any]:
        st = self.state

        def kst(e: float | None) -> str | None:
            return None if e is None else datetime.fromtimestamp(e, UTC).astimezone(KST).isoformat()

        h = st.holdings
        return {
            "dry_run": dry_run,
            "account": None if st.account is None else {"masked": st.account.masked(), "type": st.account.account_type},
            "note": st.note,
            "buying_power": {"usd": st.buying_power_usd, "krw": st.buying_power_krw},
            "holdings": None if h is None else {
                "total_purchase_amount": h.total_purchase_amount, "market_value": h.market_value,
                "profit_loss": h.profit_loss, "daily_profit_loss": h.daily_profit_loss,
                "items": [vars(i) for i in h.items]},
            "open_orders": [_order_view(o) for o in st.open_orders],
            "open_conditional": [_cond_view(c) for c in st.open_conditional],
            "updated": kst(st.updated),
            "status": self.status(),
            "error": st.error,
        }


def _order_view(o: Order) -> dict[str, Any]:
    d = {k: v for k, v in vars(o).items() if k != "execution"}
    d["execution"] = None if o.execution is None else vars(o.execution)
    return d


def _cond_view(c: ConditionalOrder) -> dict[str, Any]:
    d = {k: v for k, v in vars(c).items() if k not in ("first", "second")}
    d["first"] = vars(c.first)
    d["second"] = None if c.second is None else vars(c.second)
    return d
