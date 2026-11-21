"""Phase 4 part 5: modify / cancel through draft -> approve -> send. Fake Toss only; DRY RUN by default."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.data.toss import ConditionalOrder, ConditionLeg, Execution, Order
from app.main import create_app
from app.orders.account import AccountService
from app.orders.manage import action_request, find_target, summarize_action, validate_action
from tests.test_api import LOCAL
from tests.test_orders_drafts import ctx
from tests.test_orders_submit import ORDER_WRITES, new_draft, setup

D = Decimal
T = datetime(2026, 3, 25, 14, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def open_order(oid: str = "o1", status: str = "PENDING", price: str = "57.47") -> Order:
    return Order(oid, "SOXS", "BUY", "LIMIT", "DAY", status, D(price), D("10"), None, "USD", T, None,
                 Execution(D("0"), None, None, None, None, None, None))


def cond(cid: str = "c1", status: str = "WATCHING") -> ConditionalOrder:
    return ConditionalOrder(cid, "OCO", status, "SOXS", "US", D("10"), "LIMIT", "2026-12-31",
                            ConditionLeg("STOP", "WATCHING", "SELL", D("60"), None, D("59.9"), None),
                            ConditionLeg("STOP", "WATCHING", "SELL", D("55"), None, D("54.9"), None), T)


def with_account(svc: Any, orders: list[Order], conds: list[ConditionalOrder]) -> None:
    acct = AccountService(None, account_seq=None, refresh_seconds=10)
    acct.state.open_orders, acct.state.open_conditional = orders, conds
    svc.account = acct


def test_find_and_validate_normal_order() -> None:
    svc = type("S", (), {})()
    with_account(svc, [open_order()], [])
    t = find_target(svc, "order", "o1")
    assert t["side"] == "BUY" and t["price"] == D("57.47") and find_target(svc, "order", "zz") is None
    ok = validate_action({"action": "modify", "order_type": "LIMIT", "price": "57.30"}, t, ctx())
    assert ok.ok and ok.normalized["price"] == D("57.30")
    codes = lambda r: [i.code for i in r.errors]
    assert "modify_price_only" in codes(validate_action({"action": "modify", "order_type": "LIMIT", "price": "57.3",
                                                        "quantity": "5"}, t, ctx()))
    assert "price_decimals" in codes(validate_action({"action": "modify", "order_type": "LIMIT", "price": "57.305"}, t, ctx()))
    assert "price_out_of_band" in codes(validate_action({"action": "modify", "order_type": "LIMIT", "price": "70"}, t, ctx()))
    assert "nothing_changed" in codes(validate_action({"action": "modify", "order_type": "LIMIT", "price": "57.47"}, t, ctx()))
    mkt = validate_action({"action": "modify", "order_type": "MARKET"}, t, ctx(session="after"))
    assert mkt.ok and mkt.warnings[0].code == "market_thin_book"
    assert validate_action({"action": "cancel"}, t, ctx()).ok
    done = find_target(type("S", (), {"account": None})(), "order", "o1")
    assert done is None
    t2 = {**t, "status": "PENDING_CANCEL"}
    assert "target_not_open" in codes(validate_action({"action": "cancel"}, t2, ctx()))
    assert summarize_action(ok.normalized)[0] == "Change the SOXS buy order (10 shares, 57.47 USD) to 57.30 USD limit"
    assert summarize_action(validate_action({"action": "cancel"}, t, ctx()).normalized)[1] == \
        "SOXS 매수 주문 (10주, 57.47 USD) 취소"


def test_conditional_modify_and_requests() -> None:
    svc = type("S", (), {})()
    with_account(svc, [open_order()], [cond()])
    t = find_target(svc, "conditional", "c1")
    assert t["kind"] == "OCO" and len(t["legs"]) == 2
    new_legs = [{"trigger_price": "61", "order_price": "60.9"}, {"trigger_price": "55", "order_price": "54.9"}]
    r = validate_action({"action": "modify", "legs": new_legs, "expire_date": "2026-12-31"}, t, ctx(held_qty=D("0")))
    assert r.ok, r.errors                                         # its own 10 held-back shares count as sellable
    same = validate_action({"action": "modify", "legs": [{"trigger_price": "60", "order_price": "59.9"},
                                                         {"trigger_price": "55", "order_price": "54.9"}],
                            "expire_date": "2026-12-31"}, t, ctx())
    assert [i.code for i in same.errors] == ["nothing_changed"]
    m, p, b = action_request(r.normalized)
    assert (m, p) == ("POST", "/api/v1/conditional-orders/c1/modify")
    assert b["type"] == "OCO" and b["first"] == {"orderSide": "SELL", "triggerPrice": "61", "orderPrice": "60.9"}
    assert b["quantity"] == "10" and b["expireDate"] == "2026-12-31"
    assert action_request(validate_action({"action": "cancel"}, t, ctx()).normalized) == \
        ("DELETE", "/api/v1/conditional-orders/c1", None)
    o = find_target(svc, "order", "o1")
    assert action_request(validate_action({"action": "cancel"}, o, ctx()).normalized) == \
        ("POST", "/api/v1/orders/o1/cancel", None)
    assert action_request(validate_action({"action": "modify", "order_type": "LIMIT", "price": "57.3"}, o, ctx()).normalized) == \
        ("POST", "/api/v1/orders/o1/modify", {"orderType": "LIMIT", "confirmHighValueOrder": False, "price": "57.3"})


@pytest.mark.anyio
async def test_dry_run_modify_then_cancel_simulated(tmp_path: Path) -> None:
    svc = await setup(tmp_path)
    d = await new_draft(svc)
    sim_id = (await svc.sender.submit(d.id))["order_id"]
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        m = c.post("/api/orders/drafts", json={"action": "modify", "target_type": "simulated", "target_id": sim_id,
                                               "order_type": "LIMIT", "price": "4.24"}).json()
        assert m["status"] == "draft" and m["summary_en"].startswith("Change the SOXS buy order")
        r = c.post("/api/orders/submit", json={"draft_id": m["id"], "approved": True}).json()
        assert r["status"] == "simulated" and r["order_id"].startswith("DRYRUN-")
        sims = c.get("/api/orders/simulated").json()["orders"]
        assert [s["status"] for s in sims] == ["OPEN", "REPLACED"] and sims[0]["values"]["price"] == "4.24"
        cx = c.post("/api/orders/drafts", json={"action": "cancel", "target_type": "simulated",
                                                "target_id": sims[0]["id"]}).json()
        assert c.post("/api/orders/submit", json={"draft_id": cx["id"], "approved": True}).json()["status"] == "simulated"
        assert c.get("/api/orders/simulated").json()["orders"][0]["status"] == "CANCELED"
        gone = c.post("/api/orders/drafts", json={"action": "cancel", "target_type": "simulated",
                                                  "target_id": sims[0]["id"]}).json()
        assert gone["status"] == "invalid" and gone["errors"][0]["code"] == "target_not_open"
        none = c.post("/api/orders/drafts", json={"action": "cancel", "target_type": "order", "target_id": "x"}).json()
        assert none["errors"][0]["code"] == "target_not_found"
    assert ORDER_WRITES == []                                     # DRY RUN: Toss never touched
    lines = [json.loads(l) for l in (tmp_path / "trades.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [l.get("action") for l in lines if l["event"] == "request"] == [None, "modify", "cancel"]


@pytest.mark.anyio
async def test_real_cancel_is_never_retried(tmp_path: Path) -> None:
    from tests.test_toss import account_routes

    calls: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/cancel"):
            calls.append(req.url.path)
            raise httpx.ConnectError("lost")
        if req.url.path == "/api/v1/orders" and req.url.params.get("status") == "OPEN":
            o = {"orderId": "o1", "symbol": "SOXS", "side": "BUY", "orderType": "LIMIT", "timeInForce": "DAY",
                 "status": "PENDING", "price": "4.20", "quantity": "10", "orderAmount": None, "currency": "USD",
                 "orderedAt": "2026-03-25T23:00:00+09:00", "canceledAt": None, "execution": None}
            return httpx.Response(200, json={"result": {"orders": [o], "nextCursor": None, "hasNext": False}})
        return account_routes(req)

    svc = await setup(tmp_path, dry_run=False)
    from tests.test_api import toss_handler
    svc.toss._http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: h(r) if h(r).status_code != 404 else toss_handler(r)))
    svc.toss.enable_order_writes()
    svc.account = AccountService(svc.toss, account_seq=None, refresh_seconds=10, clock=svc.clock)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        d = c.post("/api/orders/drafts", json={"action": "cancel", "target_type": "order", "target_id": "o1"}).json()
        assert d["status"] == "draft"
        r = c.post("/api/orders/submit", json={"draft_id": d["id"], "approved": True}).json()
    assert r["status"] == "unknown" and r["error"] == "transport"
    assert calls == ["/api/v1/orders/o1/cancel"]                  # exactly one attempt, no automatic retry


@pytest.mark.anyio
async def test_orders_and_fills_for_the_chart(tmp_path: Path) -> None:
    svc = await setup(tmp_path)
    d = await new_draft(svc)
    await svc.sender.submit(d.id)                                  # DRY RUN: a simulated open order
    with_account(svc, [open_order()], [cond()])
    filled = Order("f1", "SOXS", "SELL", "LIMIT", "DAY", "FILLED", D("4.3"), D("2"), None, "USD", T, None,
                   Execution(D("2"), D("4.31"), D("8.62"), D("0.01"), D("0"), T, None))
    svc.account.fills.orders = {"f1": filled}
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        body = c.get("/api/orders/drafts", params={"symbol": "SOXS"}).json()
    labels = sorted(l["label"] for l in body["overlays"] if l.get("group") == "orders")
    assert labels == ["DRY RUN buy 10", "Open buy 10", "Reserved sell 10 trigger", "Reserved sell 10 trigger"]
    assert all(l["dashed"] and l["type"] == "hline" for l in body["overlays"])
    assert body["fills"] == [{"symbol": "SOXS", "time": "2026-03-25T23:00:00+09:00", "side": "SELL", "qty": "2",
                              "price": "4.31", "currency": "USD"}]
    assert ORDER_WRITES == []
