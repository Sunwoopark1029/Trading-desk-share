"""P&L from the Toss account: fills -> trades, applied FX, opening positions, mismatches, end to end."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.data.toss import Execution, HoldingItem, Holdings, Order
from app.orders.account import AccountService
from app.orders.fills import mismatches, opening_positions, to_trades
from tests.test_api import NOW, make_services

D = Decimal
T0 = datetime(2026, 3, 20, 14, 0, tzinfo=UTC)


def order(oid: str, side: str, qty: str | None, price: str | None, day: int, comm: str = "0.10",
          settle: str = "2026-03-24") -> Order:
    ex = None if qty is None else Execution(D(qty), D(price), D(qty) * D(price), D(comm), D("0"),
                                            T0.replace(day=day), settle)
    return Order(oid, "SOXS", side, "LIMIT", "DAY", "FILLED", D(price or "1"), D(qty or "1"), None, "USD",
                 T0.replace(day=day), None, ex)


def holding(qty: str, avg: str, last: str = "60") -> HoldingItem:
    return HoldingItem("SOXS", "Bear 3X", "US", "USD", D(qty), D(last), D(avg), {}, {}, {}, {})


def test_to_trades_fees_fx_and_skips_unfilled() -> None:
    orders = [order("b1", "BUY", "10", "50", 20), order("s1", "SELL", "4", "55", 21, settle="2026-03-25"),
              order("x", "BUY", None, None, 22)]
    txs = [{"transactionType": "BUY", "symbol": "SOXS", "quantity": "10", "settlementDate": "2026-03-24",
            "exchangeRate": "1450.5", "isCancellation": False}]
    tr = to_trades(orders, txs)
    assert [t["side"] for t in tr] == ["buy", "sell"] and tr[0]["fee"] == D("0.10")
    assert tr[0]["fx_rate"] == D("1450.5") and tr[1]["fx_rate"] is None     # unmatched -> est. later


def test_opening_positions_for_shares_before_history() -> None:
    tr = to_trades([order("s1", "SELL", "4", "55", 21)], [])
    opens, notes = opening_positions(tr, [holding("6", "48")])
    assert opens[0]["qty"] == D("10") and opens[0]["price"] == D("48")      # 4 sold + 6 still held
    assert opens[0]["time"] < tr[0]["time"] and "predate" in notes[0]
    ok, n = opening_positions(to_trades([order("b", "BUY", "6", "48", 20)], []), [holding("6", "48")])
    assert ok == [] and n == []


def test_fractional_oversell_not_held_does_not_break_pnl() -> None:
    """Real case: buys 4 TSLA in the history, sold 4.004338, holds none now."""
    b = order("b1", "BUY", "4", "250", 20)
    s = order("s1", "SELL", "4.004338", "260", 21)
    tr = to_trades([b, s], [])
    opens, notes = opening_positions(tr, [])
    assert opens[0]["qty"] == D("0.004338") and opens[0]["price"] == D("260")
    assert "not in the order history" in notes[0]
    from app import pnl as pnl_mod
    from datetime import datetime as dt
    res = pnl_mod.compute_pnl([{**t, "symbol": "TSLA"} for t in opens + tr], {}, pnl_mod.FxQuote(D("1400"), None),
                              lambda _t: D("1400"), pnl_mod.PnlConfig(D("0.001"), D("10")), dt.now(UTC))
    assert res.symbols[0].qty == 0                                       # no oversell error any more
    assert res.symbols[0].realized_usd.value.quantize(D("0.01")) == D("40.00")   # (260-250)*4, sliver ~0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def toss_account(svc: Any, orders: list[Order], held: list[HoldingItem]) -> None:
    acct = AccountService(None, account_seq=None, refresh_seconds=10)
    acct.state.holdings = Holdings(None, None, None, None, tuple(held))
    acct.fills.orders = {o.order_id: o for o in orders}
    acct.fills.loaded = True
    svc.account = acct


@pytest.mark.anyio
async def test_pnl_view_uses_toss_fills_and_avg(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    await svc.refresh_fx()
    toss_account(svc, [order("b1", "BUY", "10", "50", 20), order("s1", "SELL", "4", "55", 21)],
                 [holding("6", "50", last="60")])
    out = svc.pnl_view("all")
    assert out["source"] == "toss"
    row = out["symbols"][0]
    assert row["qty"] == "6" and row["avg_source"] == "toss" and row["avg_usd"]["status"] == "confirmed"
    assert D(out["total"]["realized_usd"]["value"]) == D("20")                # (55-50)*4
    assert D(out["total"]["unrealized_usd"]["value"]) == D("60")              # 6*(60-50), Toss last price
    assert D(out["total"]["fees_confirmed_usd"]["value"]) == D("0.20")
    assert not [n for n in out["notes"] if "vs Toss" in n]
    assert out["names"] == {"SOXS": "Bear 3X"}                                  # name from Toss holdings
    lines = svc.positions_view()["overlays"]
    assert any(l["label"].startswith("Avg (Toss)") for l in lines)


@pytest.mark.anyio
async def test_mismatch_is_reported_not_hidden(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    await svc.refresh_fx()
    toss_account(svc, [order("b1", "BUY", "10", "50", 20)], [holding("10", "52")])
    notes = svc.pnl_view("all")["notes"]
    assert any("average from fills 50" in n and "Toss 52" in n for n in notes)
    rows = [SimpleNamespace(symbol="SOXS", qty=D("9"), avg_usd_computed=SimpleNamespace(value=D("52")))]
    assert mismatches(rows, [holding("10", "52")]) == ["SOXS: quantity from fills 9 vs Toss 10"]


@pytest.mark.anyio
async def test_sold_outside_history_is_closed_at_cost(tmp_path: Path) -> None:
    """Real case: KR/US buys in the history whose sells aren't; Toss holds only SOXS."""
    svc = make_services(tmp_path)
    await svc.refresh_fx()
    other = Order("k1", "252670", "BUY", "LIMIT", "DAY", "FILLED", D("303"), D("150000"), None, "KRW", T0, None,
                  Execution(D("150000"), D("303"), None, D("0"), D("0"), T0, "2026-03-24"))
    toss_account(svc, [order("b1", "BUY", "78", "56.91", 20), other], [holding("78", "56.91", last="57.53")])
    out = svc.pnl_view("all")
    held = [r for r in out["symbols"] if D(r["qty"]) > 0]
    assert [r["symbol"] for r in held] == ["SOXS"]                         # no phantom 150,000 shares
    assert D(out["total"]["unrealized_usd"]["value"]).quantize(D("0.01")) == D("48.36")   # 78 * 0.62
    assert out["total"]["net_usd"]["status"] != "missing"
    assert D(out["total"]["fees_est_usd"]["value"]) == 0                   # balancing entries add no fees
    assert any("closed at cost" in n for n in out["notes"])


@pytest.mark.anyio
async def test_manual_mode_without_account(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    assert svc.pnl_view("all")["source"] == "manual"


@pytest.mark.anyio
async def test_fill_history_paging_full_then_recent(tmp_path: Path) -> None:
    from tests.test_toss import ORDER_JSON, account_routes

    calls: list[httpx.Request] = []
    filled = {**ORDER_JSON, "status": "FILLED"}

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        p, q = req.url.path, req.url.params
        if p == "/api/v1/orders" and q.get("status") == "CLOSED":
            if q.get("cursor") == "c2":
                return httpx.Response(200, json={"result": {"orders": [{**filled, "orderId": "o2"}],
                                                            "nextCursor": None, "hasNext": False}})
            return httpx.Response(200, json={"result": {"orders": [filled], "nextCursor": "c2", "hasNext": True}})
        if p == "/api/v1/transactions":
            return httpx.Response(200, json={"result": {"transactions": [{"transactionType": "BUY"}],
                                                        "nextCursor": None, "hasNext": False}})
        return account_routes(req)

    svc = make_services(tmp_path, handler=h)
    acct = AccountService(svc.toss, account_seq=None, refresh_seconds=10, clock=lambda: NOW)
    await acct.refresh_now()
    await acct.refresh_fills(force=True)
    closed = [c for c in calls if c.url.path == "/api/v1/orders" and c.url.params.get("status") == "CLOSED"]
    assert len(closed) == 2 and "from" not in closed[0].url.params            # first time: whole history
    assert set(acct.fills.orders) == {"0d5QIH-x_y", "o2"} and acct.fills.loaded
    calls.clear()
    await acct.refresh_fills(force=True)
    again = [c for c in calls if c.url.path == "/api/v1/orders"]
    assert again[0].url.params["from"] == "2026-03-22"                       # then: last 3 KST days
    assert all(c.method == "GET" for c in calls)


def test_current_holding_trades_keeps_only_the_current_portfolio() -> None:
    from app.pnl import current_holding_trades

    def tr(i: str, sym: str, side: str, qty: str, day: int) -> dict[str, Any]:
        return {"id": i, "symbol": sym, "side": side, "qty": qty, "price": "10", "currency": "USD",
                "time": T0.replace(day=day).isoformat()}

    trades = [tr("a1", "SOXS", "buy", "10", 2), tr("a2", "SOXS", "sell", "10", 3),      # old round trip: out
              tr("a3", "SOXS", "buy", "5", 4), tr("a4", "SOXS", "sell", "2", 5),        # current holding: in
              tr("a5", "SOXS", "buy", "1", 6),
              tr("b1", "TSLA", "buy", "3", 2), tr("b2", "TSLA", "sell", "3", 7)]        # sold out: out
    assert [t["id"] for t in current_holding_trades(trades)] == ["a3", "a4", "a5"]


@pytest.mark.anyio
async def test_all_period_is_current_portfolio_only(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    await svc.refresh_fx()
    toss_account(svc, [order("b0", "BUY", "5", "40", 16), order("s0", "SELL", "5", "45", 17),    # earlier round trip
                       order("b1", "BUY", "10", "50", 20), order("s1", "SELL", "4", "55", 21)],
                 [holding("6", "50", last="60")])
    out = svc.pnl_view("all")
    assert D(out["total"]["realized_usd"]["value"]) == D("20")       # only the partial sell of the current holding
    assert D(out["total"]["fees_confirmed_usd"]["value"]) == D("0.20")
    assert [r["symbol"] for r in out["symbols"]] == ["SOXS"]
