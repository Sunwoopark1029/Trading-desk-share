"""Options step 3 (2026-11-21): options view for the API / chat, <live> "options", em_* levels, get_options tool."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.data.yf_options as yf_options
from app.chat.scenarios import CostModel
from app.chat.tools import ToolContext, run_tool
from app.indicators import options as o
from app.indicators.options import Snapshot
from app.main import create_app
from app.options_view import compact, option_levels, options_view
from tests.test_api import LOCAL
from tests.test_chat_engine import SCREEN, FakeClient, collect, make_engine, ready_services, usage
from tests.test_options import chain, half_for


def live_snapshot(symbol: str = "SOXS", spread: float = 0.2, state: str = "REGULAR") -> Snapshot:
    """A chain priced for IV 60 % (weekly) / 50 % (monthly) around price 100, expiries from today."""
    now = time.time()
    today = datetime.now(UTC).date()
    e_week, e_month = today + timedelta(days=7), today + timedelta(days=30)
    chs = (chain(e_week, half_for(0.6, o.years_to_expiry(e_week, now)), sym=symbol, spread=spread),
           chain(e_month, half_for(0.5, o.years_to_expiry(e_month, now)), sym=symbol, spread=spread))
    return Snapshot(symbol, 100.0, now - 60, state, None, None, chs, now, (e_week, e_month))


@pytest.fixture
def chains(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    box: dict[str, Any] = {"snap": live_snapshot(), "calls": 0}

    def fetch(symbol: str, n: int) -> Snapshot:
        box["calls"] += 1
        return box["snap"]

    monkeypatch.setattr(yf_options, "fetch_snapshot", fetch)
    return box


@pytest.mark.anyio
async def test_view_compact_and_levels(tmp_path: Path, chains: dict[str, Any]) -> None:
    svc = await ready_services(tmp_path)
    v = await options_view(svc, "SOXS")
    assert v["available"] and v["source"] == "yfinance · ~15 min" and v["currency"] == "USD"
    assert v["week"]["iv"] == pytest.approx(0.6, abs=1e-3) and v["month"]["iv"] == pytest.approx(0.5, abs=1e-3)
    assert v["day"]["move"] == pytest.approx(100 * 0.6 / 252 ** 0.5, abs=1e-3)      # first good expiry >= 1 day
    assert v["price_time_kst"].endswith("+09:00") and "oi_unverified" in v["warnings"]
    c = compact(v)
    assert c["expected_move_day"]["levels"] == ["em_day_up", "em_day_down"] and c["session"] == "live"
    assert c["expected_move_week"]["iv"] == "60%" and c["term_structure"] == "inverted"
    lv = {x.id: x for x in option_levels(v)}
    assert set(lv) == {"em_day_up", "em_day_down", "em_week_up", "em_week_down", "em_month_up", "em_month_down"}
    assert lv["em_day_up"].price == Decimal(str(round(100 + v["day"]["move"], 2))) and lv["em_day_up"].kind == "options"
    assert "yfinance" in lv["em_week_down"].label
    await options_view(svc, "SOXS")
    assert chains["calls"] == 1                                    # cached (5 min while live)


@pytest.mark.anyio
async def test_thin_unlisted_off_and_non_us(tmp_path: Path, chains: dict[str, Any]) -> None:
    svc = await ready_services(tmp_path)
    chains["snap"] = live_snapshot(spread=6.0, state="POSTPOST")
    v = await options_view(svc, "SOXS")
    assert v["available"] and v["day"] is None and "thin_chain" in v["warnings"] and "last_session" in v["warnings"]
    c = compact(v)
    assert "expected_move_day" not in c and any("thin" in w for w in c["warnings"]) and c["session"] == "last session"
    assert option_levels(v) == []
    assert not (await options_view(svc, "005930"))["available"]    # KR ticker
    svc.options = None
    svc.config.options.enabled = False
    v = await options_view(svc, "SOXS")
    assert not v["available"] and "switched off" in v["reason"]


@pytest.mark.anyio
async def test_unlisted_by_default_in_tests(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)                           # conftest: no network, "no listed options"
    v = await options_view(svc, "SOXS")
    assert v == {"symbol": "SOXS", "source": "yfinance · ~15 min", "available": False, "reason": "no listed options"}


@pytest.mark.anyio
async def test_chat_gets_options_in_live_block_and_levels(tmp_path: Path, chains: dict[str, Any]) -> None:
    svc = await ready_services(tmp_path)
    client = FakeClient([([{"type": "text", "text": "ok"}], "end_turn", usage())])
    eng = make_engine(svc, tmp_path, client)
    cid = eng.store.create()
    await collect(eng, cid, "오늘 예상 변동폭?")
    msg = client.calls[0]["messages"][-1]["content"]
    live = next(b["text"] for b in msg if b["type"] == "text" and b["text"].startswith("<live>"))
    assert '"options"' in live and "expected_move_day" in live and "yfinance" in live
    assert '"em_day_up"' in live                                    # the bands are live levels with ids
    system = json.dumps(client.calls[0]["system"])
    assert "Expected move" in system and "never says which direction" in system
    assert any(t["name"] == "get_options" for t in client.calls[0]["tools"])


@pytest.mark.anyio
async def test_get_options_tool(tmp_path: Path, chains: dict[str, Any]) -> None:
    svc = await ready_services(tmp_path)
    ctx = ToolContext(svc, "SOXS", SCREEN, [], CostModel(Decimal("0.001"), None))
    out = await run_tool("get_options", {}, ctx, datetime.now(UTC))
    assert out["available"] and out["symbol"] == "SOXS" and len(out["expiries"]) == 2
    assert out["expiries"][0]["straddle"]["yahoo_iv"] == pytest.approx(0.5)
    ls = await ctx.levels("SOXS", "5m")                             # bands now count as known prices
    assert Decimal(str(round(out["day"]["up"], 2))) in ls.prices()


def test_options_route(tmp_path: Path, chains: dict[str, Any]) -> None:
    from tests.test_api import make_services

    svc = make_services(tmp_path)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        r = c.get("/api/options", params={"symbol": "SOXS"})
        assert r.status_code == 200 and r.json()["available"] and r.json()["week"]["quality"] == "good"
        assert c.get("/api/options", params={"symbol": "../x"}).status_code == 400
