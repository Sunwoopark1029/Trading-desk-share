"""Phase 4 parts 3-4: approve -> send. DRY RUN guard, one send per draft, final re-check, request shapes.
Fake Toss only (httpx.MockTransport): no real order is ever sent."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.data.toss import TossPathNotAllowed
from app.main import create_app
from app.orders.drafts import DraftStore, SettingsStore
from app.orders.submit import OrderSender, TradeLog, same_order, toss_request
from app.orders.validate import validate
from tests.test_api import LOCAL, make_services, toss_handler
from tests.test_orders_drafts import BUY, COND, ctx

D = Decimal
ORDER_WRITES: list[httpx.Request] = []


def guarded(req: httpx.Request) -> httpx.Response:
    """Fake Toss that records ANY order write (POST/DELETE on order paths)."""
    if req.method in ("POST", "DELETE") and "order" in req.url.path:
        ORDER_WRITES.append(req)
        return httpx.Response(200, json={"result": {"orderId": "real-123", "clientOrderId": None}})
    return toss_handler(req)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def setup(tmp_path: Path, dry_run: bool = True) -> Any:
    ORDER_WRITES.clear()
    svc = make_services(tmp_path, handler=guarded)
    await svc.refresh_stocks()
    await svc.refresh_quotes()
    svc.config.orders.dry_run = dry_run
    svc.drafts = DraftStore(clock=svc.clock)
    svc.order_settings = SettingsStore(svc.config.orders)
    svc.sender = OrderSender(svc, svc.drafts, dry_run=dry_run, log_=TradeLog(tmp_path / "trades.jsonl"))
    return svc


async def new_draft(svc: Any, **change: Any) -> Any:
    from app.orders.context import build_context
    inp = {**BUY, "price": "4.25", **change}
    return svc.drafts.create(inp, await build_context(svc, "SOXS", svc.config.orders.price_band_pct),
                             ttl=180, source="panel", dry_run=svc.sender.dry_run)


# ── DRY RUN guard ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_dry_run_never_reaches_toss_order_endpoints(tmp_path: Path) -> None:
    svc = await setup(tmp_path, dry_run=True)
    d = await new_draft(svc)
    r = await svc.sender.submit(d.id)
    assert r["ok"] and r["status"] == "simulated" and r["dry_run"] and r["order_id"].startswith("DRYRUN-")
    assert ORDER_WRITES == []                                  # nothing reached the (fake) Toss order API
    assert not svc.toss.order_writes_enabled
    with pytest.raises(TossPathNotAllowed):                    # and the client itself refuses writes
        await svc.toss.order_write("POST", "/api/v1/orders", {"x": 1}, 7)
    assert ORDER_WRITES == []
    assert svc.sender.simulated[0]["id"] == r["order_id"]
    log = [json.loads(l) for l in (tmp_path / "trades.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [l["event"] for l in log] == ["request", "result"] and all(l["dry_run"] for l in log)
    assert log[0]["body"]["clientOrderId"] == d.id and "token" not in json.dumps(log).lower()


@pytest.mark.anyio
async def test_one_send_per_draft(tmp_path: Path) -> None:
    svc = await setup(tmp_path)
    d = await new_draft(svc)
    first = await svc.sender.submit(d.id)
    second = await svc.sender.submit(d.id)                     # double click
    assert second == first and len(svc.sender.simulated) == 1
    lines = (tmp_path / "trades.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2                                     # one request + one result, not four
    assert svc.sender.log.sent_today(svc.clock()) == (1, D("42.50"))


@pytest.mark.anyio
async def test_refused_drafts(tmp_path: Path) -> None:
    svc = await setup(tmp_path)
    assert (await svc.sender.submit("nope"))["error"] == "draft_not_found"
    bad = await new_draft(svc, price="4.255")
    assert (await svc.sender.submit(bad.id))["error"] == "draft_invalid"
    d = await new_draft(svc)
    svc.drafts.clock = lambda: svc.clock() + 999               # expired
    d.expires = svc.clock() - 1
    assert (await svc.sender.submit(d.id))["error"] == "draft_expired"
    assert svc.sender.simulated == []


@pytest.mark.anyio
async def test_recheck_against_latest_price(tmp_path: Path) -> None:
    svc = await setup(tmp_path)
    d = await new_draft(svc)
    svc.config.orders.price_band_pct = D("0.1")
    from app.data.toss import Price
    from datetime import UTC, datetime
    q = svc.cache.get("toss.quotes").value
    q["SOXS"] = Price("SOXS", datetime.fromtimestamp(svc.clock(), UTC), D("4.50"), "USD")   # price moved
    r = await svc.sender.submit(d.id)
    assert not r["ok"] and r["error"] == "recheck_failed" and r["issues"][0]["code"] == "price_out_of_band"
    assert d.status == "invalid" and svc.sender.simulated == []


def test_percent_size_change_is_refused() -> None:
    a = validate({**BUY, "size_mode": "percent", "percent": "30"}, ctx()).normalized
    b = validate({**BUY, "size_mode": "percent", "percent": "30"}, ctx(buying_power_usd=D("2000"))).normalized
    assert a["quantity"] != b["quantity"] and not same_order(a, b)
    assert same_order(a, validate({**BUY, "size_mode": "percent", "percent": "30"}, ctx()).normalized)


# ── Toss request shapes (official field names) ───────────────────────────

def test_request_bodies() -> None:
    m, p, b = toss_request(validate(BUY, ctx()).normalized, "d1")
    assert (m, p) == ("POST", "/api/v1/orders")
    assert b == {"symbol": "SOXS", "side": "BUY", "orderType": "LIMIT", "clientOrderId": "d1",
                 "confirmHighValueOrder": False, "quantity": "10", "timeInForce": "DAY", "price": "57.47"}
    mkt = validate({**BUY, "order_type": "MARKET", "price": "", "size_mode": "amount", "amount": "500"}, ctx())
    _, _, b = toss_request(mkt.normalized, "d2")
    assert b["orderAmount"] == "500" and "quantity" not in b and "price" not in b and "timeInForce" not in b
    oco = validate({**COND, "kind": "OCO", "legs": [{"trigger_price": "60", "order_price": "59.9"},
                                                    {"trigger_price": "55", "order_price": "54.9"}]}, ctx())
    m, p, b = toss_request(oco.normalized, "d3")
    assert p == "/api/v1/conditional-orders" and b["type"] == "OCO" and b["expireDate"] == "2026-12-31"
    assert b["first"] == {"orderSide": "SELL", "triggerPrice": "60", "orderPrice": "59.9"}
    single = validate({**COND, "kind": "COND_BUY", "order_type": "MARKET", "legs": [{"trigger_price": "55"}]}, ctx())
    _, _, b = toss_request(single.normalized, "d4")
    assert b["type"] == "SINGLE" and b["second"] is None and b["first"] == {"orderSide": "BUY", "triggerPrice": "55"}


# ── real path (writes enabled) against the FAKE Toss ─────────────────────

@pytest.mark.anyio
async def test_real_path_retry_and_rejection(tmp_path: Path) -> None:
    from app.orders.account import AccountService
    from tests.test_toss import account_routes

    sent: list[dict[str, Any]] = []
    mode = {"n": 0, "reject": False}

    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/api/v1/orders":
            sent.append({"body": json.loads(req.content), "account": req.headers.get("x-tossinvest-account")})
            mode["n"] += 1
            if mode["reject"]:
                return httpx.Response(422, json={"error": {"code": "insufficient-buying-power", "message": ""}})
            if mode["n"] == 1:
                raise httpx.ConnectError("lost")
            return httpx.Response(200, json={"result": {"orderId": "T-1", "clientOrderId": "x"}})
        r = account_routes(req)
        return r if r.status_code != 404 else toss_handler(req)

    svc = await setup(tmp_path, dry_run=False)
    svc.toss._http = httpx.AsyncClient(transport=httpx.MockTransport(h))
    svc.toss.enable_order_writes()
    svc.account = AccountService(svc.toss, account_seq=None, refresh_seconds=10, clock=svc.clock)
    await svc.account.refresh_now()
    d = await new_draft(svc)
    r = await svc.sender.submit(d.id)
    assert r["ok"] and r["status"] == "accepted" and r["order_id"] == "T-1" and r["dry_run"] is False
    assert len(sent) == 2 and sent[0]["body"] == sent[1]["body"]                    # retried with the SAME id
    assert sent[0]["body"]["clientOrderId"] == d.id and sent[0]["account"] == "7"
    mode["reject"] = True
    d2 = await new_draft(svc, quantity="1")
    r2 = await svc.sender.submit(d2.id)
    assert not r2["ok"] and r2["status"] == "rejected" and r2["error"] == "insufficient-buying-power"
    assert d2.status == "rejected"


# ── route ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_submit_route(tmp_path: Path) -> None:
    svc = await setup(tmp_path)
    d = await new_draft(svc)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        assert c.post("/api/orders/submit", json={"draft_id": d.id}).status_code == 400       # not approved
        assert c.post("/api/orders/submit", json={"draft_id": d.id, "approved": True},
                      headers={"Origin": "https://evil.example"}).status_code == 403
        r = c.post("/api/orders/submit", json={"draft_id": d.id, "approved": True})
        assert r.status_code == 200 and r.json()["status"] == "simulated"
        listed = c.get("/api/orders/drafts").json()
        assert listed["drafts"] == [] and listed["done"][0]["result"]["status"] == "simulated"
        assert c.get("/api/orders/simulated").json()["orders"][0]["draft_id"] == d.id
    assert ORDER_WRITES == []
    assert date.today()                                      # keep import used
