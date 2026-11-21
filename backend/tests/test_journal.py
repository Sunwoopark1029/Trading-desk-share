"""Phase 4 part 7: trade journal = round trips that include an order sent by this program."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.chat.usage import UsageLog
from app.data.toss import ConditionalOrder, ConditionLeg
from app.orders.drafts import DraftStore
from app.orders.journal import program_order_ids, round_trips
from app.orders.submit import OrderSender, TradeLog
from tests.test_pnl_toss import T0, holding, make_services, order, toss_account

D = Decimal


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def tr(i: str, side: str, qty: str, day: int, sym: str = "SOXS") -> dict[str, Any]:
    return {"id": i, "order_id": i, "symbol": sym, "side": side, "qty": qty, "price": "10", "currency": "USD",
            "time": T0.replace(day=day).isoformat()}


def test_round_trips_split_at_zero() -> None:
    trips = round_trips([tr("a", "buy", "10", 2), tr("b", "sell", "4", 3), tr("c", "sell", "6", 4),
                         tr("d", "buy", "5", 5), tr("e", "buy", "1", 1, "TSLA")])
    assert sorted([r["id"] for r in t] for t in trips) == [["a", "b", "c"], ["d"], ["e"]]


def test_program_ids_from_log_and_triggered_conditionals() -> None:
    log = [
        {"event": "result", "status": "accepted", "dry_run": False, "order_id": "o1", "path": "/api/v1/orders"},
        {"event": "result", "status": "simulated", "dry_run": True, "order_id": "DRYRUN-x"},       # never fills
        {"event": "result", "status": "rejected", "dry_run": False, "order_id": None},
        {"event": "result", "status": "accepted", "dry_run": False, "action": "modify", "order_id": "o2",
         "path": "/api/v1/orders/o1/modify"},                                                   # modify: new id
        {"event": "result", "status": "accepted", "dry_run": False, "conditional_order_id": "c1",
         "path": "/api/v1/conditional-orders"},
        {"event": "request", "order_id": "zz"},
    ]
    leg = ConditionLeg("STOP", "ORDERED", "SELL", D("60"), None, D("59.9"), "o3")
    other = ConditionLeg("STOP", "ORDERED", "SELL", D("60"), None, D("59.9"), "o9")
    conds = [ConditionalOrder("c1", "SINGLE", "COMPLETED", "SOXS", "US", D("1"), "LIMIT", None, leg, None, T0),
             ConditionalOrder("c9", "SINGLE", "COMPLETED", "SOXS", "US", D("1"), "LIMIT", None, other, None, T0)]
    assert program_order_ids(log, conds) == {"o1", "o2", "o3"}


def sent(path: Path, *ids: str) -> TradeLog:
    log = TradeLog(path)
    for i in ids:
        log.append({"event": "result", "status": "accepted", "dry_run": False, "order_id": i,
                    "path": "/api/v1/orders"})
    return log


@pytest.mark.anyio
async def test_journal_only_program_round_trips(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    await svc.refresh_fx()
    toss_account(svc, [
        order("old-b", "BUY", "5", "40", 10), order("old-s", "SELL", "5", "45", 11),   # unrelated: left out
        order("b1", "BUY", "10", "50", 16),                                            # bought outside the program
        order("p-s1", "SELL", "4", "55", 17), order("p-s2", "SELL", "6", "56", 18),    # sold with the program
        order("p-b2", "BUY", "3", "52", 20),                                           # open, program buy
    ], [holding("3", "52", last="54")])
    svc.drafts = DraftStore(clock=svc.clock)
    svc.sender = OrderSender(svc, svc.drafts, dry_run=True, log_=sent(tmp_path / "trades.jsonl", "p-s1", "p-s2", "p-b2"))
    usage = UsageLog(tmp_path / "usage.jsonl")
    for day, cost in ((17, "0.50"), (19, "0.25"), (21, "1.00")):     # 17: during trip 1; 19: between; 21: trip 2
        usage.append({"ts": T0.replace(day=day).isoformat(), "cost_usd": cost, "cost_krw": None, "model": "m"})
    svc.ai_credits = usage

    out = await svc.journal_view()
    assert out["ready"] and [t["status"] for t in out["trips"]] == ["open", "closed"]
    op, cl = out["trips"]
    assert cl["symbol"] == "SOXS" and cl["fills"] == 3 and cl["program_fills"] == 2
    assert D(cl["buy_qty"]) == 10 and D(cl["avg_entry"]) == 50 and D(cl["avg_exit"]) == D("55.6")
    assert D(cl["realized_usd"]["value"]) == D("56")                  # 4*5 + 6*6
    assert D(cl["fees_usd"]["value"]) == D("0.30")                    # three fills x 0.10 (Toss, confirmed)
    assert cl["holding_seconds"] == int(timedelta(days=2).total_seconds())
    assert D(cl["ai_credits_usd"]["value"]) == D("0.50")              # only usage while the trade was open
    assert D(op["held_qty"]) == 3 and op["exit_time"] is None
    assert D(op["unrealized_usd"]["value"]) == D("6")                 # 3 * (54 - 52)
    assert D(op["ai_credits_usd"]["value"]) == D("1.00")
    assert D(out["total"]["ai_credits_usd"]["value"]) == D("1.50")    # each call once; day 19 not in any trade
    net = sum(D(t["net_usd"]["value"]) for t in out["trips"])
    assert D(out["total"]["net_usd"]["value"]) == net


@pytest.mark.anyio
async def test_journal_empty_in_dry_run_and_without_account(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    assert (await svc.journal_view())["ready"] is False
    await svc.refresh_fx()
    toss_account(svc, [order("b1", "BUY", "10", "50", 16)], [holding("10", "50")])
    svc.drafts = DraftStore(clock=svc.clock)
    log = TradeLog(tmp_path / "trades.jsonl")
    log.append({"event": "result", "status": "simulated", "dry_run": True, "order_id": "b1"})   # DRY RUN never counts
    svc.sender = OrderSender(svc, svc.drafts, dry_run=True, log_=log)
    out = await svc.journal_view()
    assert out["ready"] and out["trips"] == [] and out["total"]["trades"] == 0
    json.dumps(out)
