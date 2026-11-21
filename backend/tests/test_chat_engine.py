"""Chat engine, tools and routes with a fake Claude client (no network, no real API calls)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.chat.engine import ChatEngine, TurnRequest, echo_content, unverified_numbers
from app.chat.levels import compute_levels, shown_price
from app.chat.scenarios import CostModel, evaluate_table
from app.chat.store import ChatStore
from app.chat.tools import ToolContext, ToolError, run_tool, user_numbers, validate_ai_overlay
from app.chat.usage import UsageLog
from app.main import create_app
from app.settings import CandlesConfig, load_config
from app.timeutil import KST as KST_TZ
from tests.test_api import LOCAL, NOW, make_services

D = Decimal
SCREEN = {"symbol": "SOXS", "timeframe": "5m", "toggles": {"ema": True}}
NOW_DT = datetime.fromtimestamp(NOW, UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── fake Anthropic client ────────────────────────────────────────────────

def usage(inp: int = 100, out: int = 50, read: int = 0, write: int = 0) -> dict[str, Any]:
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write}


class FakeStream:
    def __init__(self, msg: Any) -> None:
        self.msg = msg

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for b in self.msg.content:
                if b["type"] == "text":
                    yield SimpleNamespace(type="text", text=b["text"])
                elif b["type"] == "tool_use":
                    yield SimpleNamespace(type="content_block_start",
                                          content_block=SimpleNamespace(type="tool_use", name=b["name"]))
        return gen()

    async def get_final_message(self) -> Any:
        return self.msg


class FakeClient:
    def __init__(self, replies: list[tuple[list[dict[str, Any]], str, dict[str, Any]]]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params: Any) -> FakeStream:
        self.calls.append(copy.deepcopy(params))
        content, stop, use = self.replies.pop(0)
        return FakeStream(SimpleNamespace(content=content, stop_reason=stop, usage=use,
                                          model=params["model"], stop_details=None,
                                          input_transformations=[]))


async def ready_services(tmp_path: Path) -> Any:
    svc = make_services(tmp_path)
    await svc.refresh_calendar()
    await svc.refresh_stocks()
    await svc.refresh_fx()
    await svc.refresh_quotes()
    return svc


def make_engine(svc: Any, tmp_path: Path, client: FakeClient, **llm_update: Any) -> ChatEngine:
    llm = load_config().llm
    assert llm is not None
    llm_update.setdefault("candles", CandlesConfig(mode="A"))  # tests pick the mode explicitly
    if llm_update:
        llm = llm.model_copy(update=llm_update)
    return ChatEngine(svc, llm, api_key="sk-test-not-real", store=ChatStore(tmp_path / "chat.sqlite"),
                      usage=UsageLog(tmp_path / "usage.jsonl"), client_factory=lambda _k: client,
                      clock=lambda: NOW_DT)


async def collect(engine: ChatEngine, cid: str, text: str, tier: str = "normal") -> list[dict[str, Any]]:
    return [ev async for ev in engine.run_turn(cid, TurnRequest(text=text, tier=tier, screen=SCREEN))]


def lv(levels: Any, lid: str) -> Any:
    return next(x for x in levels.all if x.id == lid)


# ── tools: overlay validation ────────────────────────────────────────────

@pytest.mark.anyio
async def test_ai_overlay_validation(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    ctx = ToolContext(svc, "SOXS", SCREEN, user_numbers(["draw a line at 4.50"]), CostModel(D("0.001"), None))
    with pytest.raises(ToolError, match="get_levels first"):
        validate_ai_overlay({"type": "hline", "points": [{"time": None, "price": 4.5}]}, ctx, NOW_DT)
    levels = await ctx.levels("SOXS", "5m")
    vwap = lv(levels, "vwap_5m").price
    ok = validate_ai_overlay({"type": "hline", "points": [{"time": None, "price": float(vwap)}],
                              "label": "VWAP", "color": "javascript:1", "note": "vwap_5m"}, ctx, NOW_DT)
    assert ok["source"] == "ai" and ok["symbol"] == "SOXS" and ok["color"] == "#29b6f6"
    assert validate_ai_overlay({"type": "hline", "points": [{"time": None, "price": 4.5}]}, ctx, NOW_DT)
    with pytest.raises(ToolError, match="not a code-calculated level"):
        validate_ai_overlay({"type": "hline", "points": [{"time": None, "price": 77.77}]}, ctx, NOW_DT)
    with pytest.raises(ToolError, match="exactly 2"):
        validate_ai_overlay({"type": "zone", "points": [{"time": None, "price": float(vwap)}]}, ctx, NOW_DT)
    hi, lo = lv(levels, "swing_high_2"), lv(levels, "swing_low_2")  # bars that are not both
    tl = validate_ai_overlay({"type": "trendline", "points": [
        {"time": hi.time, "price": float(hi.price)}, {"time": lo.time, "price": float(lo.price)}],
        "label": "t", "color": None, "note": ""}, ctx, NOW_DT)
    assert tl["points"][0] == {"time": hi.time, "price": shown_price(hi.price)}
    with pytest.raises(ToolError, match="not a swing point"):
        validate_ai_overlay({"type": "fib", "points": [
            {"time": hi.time, "price": float(lo.price)}, {"time": lo.time, "price": float(lo.price)}]}, ctx, NOW_DT)
    with pytest.raises(ToolError, match="need the swing point's time"):
        validate_ai_overlay({"type": "trendline", "points": [
            {"time": None, "price": float(hi.price)}, {"time": lo.time, "price": float(lo.price)}]}, ctx, NOW_DT)


@pytest.mark.anyio
async def test_ai_can_remove_only_ai_lines(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    user = svc.save_overlay({"type": "hline", "points": [{"time": "2026-03-25T23:00:00+09:00", "price": 4}],
                             "color": "#ffffff", "source": "user", "symbol": "SOXS"})
    ctx = ToolContext(svc, "SOXS", SCREEN, [], CostModel(D("0.001"), None))
    with pytest.raises(ToolError, match="belongs to the user"):
        await run_tool("remove_overlay", {"id": user["id"]}, ctx, NOW_DT)
    with pytest.raises(ToolError, match="no line"):
        await run_tool("remove_overlay", {"id": "nope"}, ctx, NOW_DT)
    assert svc.list_overlays("SOXS")[0]["id"] == user["id"]
    news = await run_tool("search_news", {"query": "SOXS"}, ctx, NOW_DT)
    assert news["available"] is False


def test_scenarios_computed_in_code() -> None:
    known = [D("4.00"), D("3.80"), D("4.50")]
    rows = evaluate_table([
        {"direction": "long", "entry": 4.0, "invalidation": 3.8, "target": 4.5, "entry_basis": "vwap_5m",
         "invalidation_basis": "swing_low_1", "target_basis": "fib_0.618", "reasoning": "overlap"},
        {"direction": "short", "entry": 4.0, "invalidation": 3.8, "target": 4.5, "entry_basis": "a",
         "invalidation_basis": "b", "target_basis": "c", "reasoning": "d"},
        {"direction": "long", "entry": 4.0, "invalidation": 3.8, "target": 4.66, "entry_basis": "a",
         "invalidation_basis": "b", "target_basis": "c", "reasoning": "d"},
    ], known, CostModel(D("0.001"), D("10")))
    r = rows[0]
    assert r["valid"] and r["risk_reward"] == "2.50"           # 0.5 / 0.2
    assert r["gross_move_pct"] == "12.50"
    assert r["costs_pct_est"] == "0.400"                        # 0.1%*2 fees + 10bp*2 FX
    assert r["net_move_pct_est"] == "12.10"
    assert r["net_per_share_usd_est"] == "0.4915"               # 0.5 - 0.001*(4+4.5)
    assert not rows[1]["valid"] and "short needs" in rows[1]["problems"][0]
    assert rows[2]["valid"] and rows[2]["problems"] == []          # not a level: allowed, flagged (2026-11-20)
    assert len(rows[2]["price_warnings"]) == 1 and "target 4.66 is AI-proposed" in rows[2]["price_warnings"][0]
    assert r["price_warnings"] == []


def test_echo_content_after_mid_output_fallback() -> None:
    content = [{"type": "thinking", "thinking": "", "signature": "a"}, {"type": "text", "text": "par"},
               {"type": "tool_use", "id": "x", "name": "get_levels", "input": {}},
               {"type": "fallback", "from": {"model": "m1"}, "to": {"model": "m2"}},
               {"type": "thinking", "thinking": "", "signature": "b"}, {"type": "text", "text": "tial"}]
    assert [b["type"] for b in echo_content(content)] == ["text", "fallback", "thinking", "text"]
    plain = [{"type": "thinking", "thinking": "", "signature": "a"}, {"type": "text", "text": "x"}]
    assert echo_content(plain) == plain


def test_vs_last_side_computed_in_code() -> None:
    from app.chat.levels import Level, LevelSet, vs_last

    assert vs_last(D("57.98"), D("57.90")) == "above"     # the case the model got wrong
    assert vs_last(D("57.72"), D("57.90")) == "below"
    assert vs_last(D("57.92"), D("57.90")) == "at"         # within 0.05%
    ls = LevelSet("SOXS", "5m", "USD", live=[Level("last", "Last", D("57.90"), "last"),
                                             Level("ema20_5m", "EMA 20", D("57.98"), "ema")])
    out = ls.as_dict("live")
    assert out["last_price"] == "57.90"
    assert "vs_last" not in out["levels"][0] and out["levels"][1]["vs_last"] == "above"
    assert out["levels"][1]["diff_from_last"] == "0.08" and out["levels"][1]["diff_from_last_pct"] == "0.14"


def test_unverified_numbers() -> None:
    text = "VWAP is 4.12 and EMA20 4.10. Target 9.99 gives +12.50% and R/R 2.50R."
    assert unverified_numbers(text, ['{"price":"4.12"}'], [D("4.1")]) == ["9.99"]


# ── engine: a full turn ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_turn_with_tools_logs_usage_and_keeps_history_append_only(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    levels = await compute_levels(svc, "SOXS", "5m")
    vwap = lv(levels, "vwap_5m").price
    client = FakeClient([
        ([{"type": "thinking", "thinking": "", "signature": "s1"},
          {"type": "tool_use", "id": "t1", "name": "get_levels", "input": {"symbol": "SOXS", "timeframe": "5m"}}],
         "tool_use", usage(3000, 80, write=2500)),
        ([{"type": "tool_use", "id": "t2", "name": "add_overlay", "input": {
            "type": "hline", "points": [{"time": None, "price": float(vwap)}], "label": "VWAP support",
            "color": None, "note": "vwap_5m"}},
          {"type": "tool_use", "id": "t3", "name": "add_overlay", "input": {
              "type": "hline", "points": [{"time": None, "price": 55.55}], "label": "made up",
              "color": None, "note": ""}}],
         "tool_use", usage(400, 120, read=5500)),
        ([{"type": "text", "text": f"Support at VWAP {vwap} USD (vwap_5m). Also 9.87 maybe."}],
         "end_turn", usage(300, 60, read=6200)),
    ])
    eng = make_engine(svc, tmp_path, client)
    svc.ai_credits = eng.usage
    cid = eng.store.create()
    events = await collect(eng, cid, "where are the support levels on SOXS 5m?")
    types = [e["type"] for e in events]
    assert types[0] == "start" and types[-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "text").startswith("Support at VWAP")
    assert [e for e in events if e["type"] == "overlay_changed"][0]["action"] == "added"
    err = [e for e in events if e["type"] == "tool" and e["status"] == "error"]
    assert len(err) == 1 and "not a code-calculated level" in err[0]["message"]
    ai_lines = [o for o in svc.list_overlays("SOXS") if o["source"] == "ai"]
    assert len(ai_lines) == 1 and ai_lines[0]["points"][0]["price"] == shown_price(vwap)

    # usage: one line per API call, costs present
    recs = eng.usage.records()
    assert len(recs) == 3 and all(r["purpose"] == "chat" and r["model"] == "claude-sonnet-5-5" for r in recs)
    assert recs[1]["cache_read_tokens"] == 5500 and recs[1]["cost_usd"]
    done = events[-1]["display"]
    assert done["cost"]["calls"] == 3 and Decimal(done["cost"]["usd"]) > 0
    assert any("9.87" in w for w in done["warnings"]) and not any(str(vwap) in w for w in done["warnings"])

    # request shape: fixed tools/system, cache points, adaptive thinking with drop_block, fallbacks
    p0 = client.calls[0]
    assert p0["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert p0["cache_control"] == {"type": "ephemeral"}
    assert p0["thinking"]["block_binding"]["prefix_mismatch_behavior"] == "drop_block"
    assert "thinking-binding-controls-2026-08-01" in p0["betas"] and p0["fallbacks"] == "default"
    assert p0["output_config"] == {"effort": "medium"}
    assert all(c["tools"] == p0["tools"] and c["system"] == p0["system"] for c in client.calls)
    assert not any(t.get("name") == "web_search" for t in p0["tools"])
    first_user = p0["messages"][0]["content"]
    assert first_user[0]["text"].startswith("<context>") and first_user[-2]["text"].startswith("<live>")
    assert first_user[-1]["text"] == "[Screen language: English. Answer in English.]"

    # append-only: every request starts with the whole previous request
    for a, b in zip(client.calls, client.calls[1:]):
        assert b["messages"][: len(a["messages"])] == a["messages"]
    assert client.calls[1]["messages"][1]["content"][0]["signature"] == "s1"  # thinking replayed as-is

    # second turn (in Korean): context not re-sent (same ticker/day), previous history is an exact prefix
    client.replies.append(([{"type": "text", "text": "ok"}], "end_turn", usage(200, 10, read=7000)))
    [ev async for ev in eng.run_turn(cid, TurnRequest(text="and resistance?", tier="normal", screen=SCREEN, lang="ko"))]
    last = client.calls[-1]
    assert last["messages"][: len(client.calls[2]["messages"])] == client.calls[2]["messages"]
    new_user = last["messages"][-1]["content"]
    assert [b["text"][:6] for b in new_user] == ["and re", "<live>", "[Scree"]
    assert new_user[-1]["text"] == "[Screen language: Korean. Answer in Korean.]"
    assert last["system"] == p0["system"]  # language switch does not touch the cached system prompt

    # P&L counts AI credits and subtracts them from net
    with TestClient(create_app(svc, start_pollers=False, chat=eng), base_url=LOCAL) as c:
        body = c.get("/api/pnl", params={"period": "all"}).json()
    # "all" = the current portfolio only: nothing is held, so no period and no AI spend in it
    assert D(body["total"]["ai_credits_usd"]["value"]) == 0 and D(body["total"]["net_usd"]["value"]) == 0
    assert sum(D(r["cost_usd"]) for r in eng.usage.records()) > 0
    since = eng.usage.since(datetime(2000, 1, 1, tzinfo=UTC))
    assert since.usd == sum(D(r["cost_usd"]) for r in eng.usage.records()) and since.calls == len(eng.usage.records())


@pytest.mark.anyio
async def test_mode_b_sends_candles_and_only_new_5m_bars(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    client = FakeClient([([{"type": "text", "text": "a"}], "end_turn", usage()),
                         ([{"type": "text", "text": "b"}], "end_turn", usage())])
    eng = make_engine(svc, tmp_path, client, candles=CandlesConfig(mode="B", last_1m_bars=5))
    cid = eng.store.create()
    await collect(eng, cid, "q1")
    first = client.calls[0]["messages"][0]["content"]
    assert [b["text"][:6] for b in first] == ["<conte", "q1", "<live>", "<candl", "[Scree"]
    assert "5m bars since session open" in first[-2]["text"] and "last 5 1m bars" in first[-2]["text"]
    await collect(eng, cid, "q2")
    second = client.calls[1]["messages"][-1]["content"]
    assert "5m bars since session open" not in second[-2]["text"]  # no new 5m bar: nothing re-sent
    assert "last 5 1m bars" in second[-2]["text"]


@pytest.mark.anyio
async def test_refusal_and_cut_off_tool_call(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    client = FakeClient([
        ([{"type": "text", "text": "par"}], "refusal", usage()),
        ([{"type": "text", "text": "long"}, {"type": "tool_use", "id": "z", "name": "get_levels", "input": {}}],
         "max_tokens", usage()),
    ])
    eng = make_engine(svc, tmp_path, client)
    cid = eng.store.create()
    ev = await collect(eng, cid, "q1")
    assert "declined" in ev[-1]["display"]["warnings"][0]
    assert [m["role"] for m in eng.store.history(cid)] == ["user"]  # refused output not stored
    ev = await collect(eng, cid, "q2")
    assert "cut off" in ev[-1]["display"]["warnings"][0]
    hist = eng.store.history(cid)
    assert hist[-1]["role"] == "assistant" and [b["type"] for b in hist[-1]["content"]] == ["text"]
    assert len(client.calls) == 2  # the cut-off tool call was never run


@pytest.mark.anyio
async def test_chat_remembers_only_the_last_turns(tmp_path: Path) -> None:
    """User choice 2026-11-21: the AI sees the last N earlier turns (no Haiku summary); older ones stay on disk."""
    svc = await ready_services(tmp_path)
    client = FakeClient([
        ([{"type": "thinking", "thinking": "", "signature": "a"}, {"type": "text", "text": "A1"}], "end_turn", usage()),
        ([{"type": "thinking", "thinking": "", "signature": "b"}, {"type": "text", "text": "A2"}], "end_turn",
         usage(70000, 10)),
        ([{"type": "text", "text": "A3"}], "end_turn", usage()),
        ([{"type": "text", "text": "A4"}], "end_turn", usage()),
    ])
    eng = make_engine(svc, tmp_path, client, memory_turns=2)
    cid = eng.store.create()
    await collect(eng, cid, "Q1")
    await collect(eng, cid, "Q2")
    second = client.calls[1]["messages"]
    assert "Q1" in json.dumps(second) and second[0]["content"][0]["text"].startswith("<context>")  # append-only so far
    await collect(eng, cid, "Q3")                      # 2 earlier turns: nothing dropped yet
    assert "Q1" in json.dumps(client.calls[2]["messages"])
    await collect(eng, cid, "Q4")                      # 3 earlier turns > 2: Q1 left out
    hist = client.calls[3]["messages"]
    text = json.dumps(hist, ensure_ascii=False)
    assert "Q1" not in text and "Q2" in text and "Q3" in text
    assert all(b["type"] != "thinking" for m in hist for b in m["content"])          # kept turns stripped
    assert sum(1 for m in hist for b in m["content"]
               if b.get("type") == "text" and b["text"].startswith("<context>")) == 1  # one fresh context ...
    assert hist[-1]["content"][0]["text"].startswith("<context>")                    # ... in the new message
    assert all(r["model"] != "claude-haiku-5-5" for r in eng.usage.records())         # no summary call
    assert len(client.calls) == 4
    shown = [r["display"]["text"] for r in eng.store.rows(cid) if r.get("display") and r["display"].get("kind") == "question"]
    assert shown == ["Q1", "Q2", "Q3", "Q4"]                                          # all still on screen / disk


# ── routes: key, budget ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_routes_key_and_budget(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    eng = make_engine(svc, tmp_path, FakeClient([]))
    with TestClient(create_app(svc, start_pollers=False, chat=eng), base_url=LOCAL) as c:
        st = c.get("/api/chat/status").json()
        assert st["key"] == "set" and [t["name"] for t in st["tiers"]] == ["normal", "deep", "very_deep"]
        assert "sk-test-not-real" not in json.dumps(st)
        cid = c.post("/api/chat/conversations").json()["id"]
        eng.usage.append({"ts": NOW_DT.isoformat(), "cost_usd": "5.01", "cost_krw": None, "model": "x"})
        send = {"text": "hi", "tier": "deep", "screen": SCREEN}
        r = c.post(f"/api/chat/conversations/{cid}/messages", json=send)
        assert r.status_code == 403 and "blocked" in r.json()["detail"]
        r = c.post(f"/api/chat/conversations/{cid}/messages", json={**send, "tier": "normal"})
        assert r.status_code == 409 and r.json()["needs_confirm"] is True
        summ = c.get("/api/usage/summary").json()
        assert summ["budget"]["state"] == "over" and summ["today"]["usd"] == "5.01"
    eng._key = ""
    with TestClient(create_app(svc, start_pollers=False, chat=eng), base_url=LOCAL) as c:
        r = c.post(f"/api/chat/conversations/{cid}/messages", json={"text": "hi", "tier": "normal"})
        assert r.status_code == 409 and "key not set" in r.json()["detail"]
        assert c.get("/api/chat/status").json()["key"] == "empty"


# ── A/B test helpers (no API calls) ──────────────────────────────────────

@pytest.mark.anyio
async def test_ab_cases_and_answer_check(tmp_path: Path) -> None:
    from app.chat.ab_test import build_cases, check_answer

    svc = await ready_services(tmp_path)
    cases = await build_cases(svc, [{"symbol": "SOXS", "timeframe": "5m", "text": "support?"}], 5)
    a, b = cases
    assert (a["variant"], b["variant"]) == ("A", "B")
    assert len(b["messages"][0]["content"]) == len(a["messages"][0]["content"]) + 1  # + candles
    assert "<candles" in b["messages"][0]["content"][1]["text"]
    assert a["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    vwap = next(lv for lv in (await compute_levels(svc, "SOXS", "5m")).all if lv.id == "vwap_5m")
    chk = check_answer(f"VWAP {shown_price(vwap.price)} (vwap_5m), 12.34 (ema99_5m)", a["allowed"], a["ids"])
    assert chk["prices_cited"] == 2 and chk["prices_wrong"] == ["12.34"] and chk["ids_unknown"] == ["ema99_5m"]


def test_scenario_plan_decides_order_buttons() -> None:
    """Combined orders (OTO / OCO) only when the AI marks the row as entry + exits or exits only."""
    base = {"direction": "long", "entry": 4.0, "invalidation": 3.8, "target": 4.5, "entry_basis": "a",
            "invalidation_basis": "b", "target_basis": "c", "reasoning": "d"}
    rows = evaluate_table([base, {**base, "plan": "entry_exits"}, {**base, "plan": "exits"},
                           {**base, "plan": "send_now"}], [D("4.0"), D("3.8"), D("4.5")], CostModel(D("0.001"), None))
    assert [r["plan"] for r in rows] == ["entry", "entry_exits", "exits", "entry"]   # missing / unknown -> entry only
    from app.chat.tools import TOOL_DEFS
    schema = next(t for t in TOOL_DEFS if t["name"] == "show_scenarios")["input_schema"]
    assert schema["properties"]["rows"]["items"]["properties"]["plan"]["enum"] == ["entry", "entry_exits", "exits"]


# ── scenario quantity (user choice 2026-11-20: AI may suggest one, only when asked) ──

QBASE = {"direction": "long", "entry": 4.0, "invalidation": 3.8, "target": 4.5, "entry_basis": "a",
         "invalidation_basis": "b", "target_basis": "c", "reasoning": "d", "plan": "entry"}
QKNOWN = [D("4.0"), D("3.8"), D("4.5")]


def test_scenario_quantity_money_computed_in_code() -> None:
    from app.chat.scenarios import AccountLimits

    acct = AccountLimits(buying_power_usd=D("30"), held_quantity=D("5"))
    rows = evaluate_table([{**QBASE, "quantity": 10}, {**QBASE, "quantity": 5},
                           {**QBASE, "direction": "short", "entry": 4.0, "invalidation": 4.5, "target": 3.8,
                            "quantity": 10},
                           {**QBASE, "plan": "exits", "quantity": 5}, {**QBASE, "quantity": None}],
                          QKNOWN, CostModel(D("0.001"), None), acct)
    r = rows[0]
    assert r["quantity"] == 10 and r["quantity_source"] == "ai_suggested"
    assert r["position_usd"] == "40.00"                         # 10 x 4.00
    assert r["loss_at_stop_usd_est"] == "2.08"                  # 10 x (0.20 + 0.001 x (4.0 + 3.8))
    assert r["net_at_target_usd_est"] == "4.92"                 # 10 x 0.4915
    assert r["valid"] and r["quantity_warnings"] == ["cost 40.00 USD is more than buying power 30.00 USD"]
    assert rows[1]["quantity_warnings"] == []                   # 20.00 USD fits
    assert rows[2]["quantity_warnings"] == ["quantity 10 is more than the 5 shares held"]   # sell side
    assert rows[3]["quantity_warnings"] == []                   # exits: sells 5 of 5 held
    assert rows[4]["quantity"] is None and rows[4]["position_usd"] is None and rows[4]["quantity_warnings"] == []
    unknown = evaluate_table([{**QBASE, "quantity": 3}], QKNOWN, CostModel(D("0.001"), None))
    assert unknown[0]["quantity_warnings"] == ["buying power unknown (Toss account data not loaded)"]


@pytest.mark.parametrize("bad", [2.5, 0, -3, True, "abc", 10_000_001])
def test_scenario_quantity_must_be_whole_shares(bad: Any) -> None:
    from app.chat.scenarios import ScenarioError

    with pytest.raises(ScenarioError, match="quantity"):
        evaluate_table([{**QBASE, "quantity": bad}], QKNOWN, CostModel(D("0.001"), None))


@pytest.mark.anyio
async def test_scenario_quantity_only_when_user_asked(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    ctx = ToolContext(svc, "SOXS", SCREEN, QKNOWN, CostModel(D("0.001"), None), user_text="SOXS 시나리오 보여줘")
    await ctx.levels("SOXS", "5m")
    with pytest.raises(ToolError, match="did not ask for a quantity"):
        await run_tool("show_scenarios", {"rows": [{**QBASE, "quantity": 10}]}, ctx, NOW_DT)
    plain = await run_tool("show_scenarios", {"rows": [{**QBASE, "quantity": None}]}, ctx, NOW_DT)
    assert plain["rows"][0]["quantity"] is None
    ctx.user_text = "SOXS 시나리오, 수량도 정해줘"
    out = await run_tool("show_scenarios", {"rows": [{**QBASE, "quantity": 10}]}, ctx, NOW_DT)
    assert out["rows"][0]["quantity"] == 10 and out["rows"][0]["position_usd"] == "40.00"
    assert ctx.events[-1]["table"]["rows"][0]["quantity"] == 10


@pytest.mark.anyio
async def test_get_account_tool_read_only(tmp_path: Path) -> None:
    from app.data.toss import Holdings, HoldingItem
    from app.orders.account import AccountService

    svc = await ready_services(tmp_path)
    ctx = ToolContext(svc, "SOXS", SCREEN, [], CostModel(D("0.001"), None))
    svc.account = None
    assert (await run_tool("get_account", {"symbol": "SOXS"}, ctx, NOW_DT))["available"] is False
    acct = AccountService(None, account_seq=None, refresh_seconds=10)
    svc.account = acct
    assert (await run_tool("get_account", {"symbol": "SOXS"}, ctx, NOW_DT))["available"] is False   # not loaded
    item = HoldingItem("SOXS", "SOXS", "US", "USD", D("12"), D("4.10"), D("3.95"), {}, {}, {}, {})
    acct.state.holdings = Holdings(None, None, None, None, (item,))
    acct.state.buying_power_usd, acct.state.updated = D("1234.5"), NOW
    out = await run_tool("get_account", {"symbol": "soxs"}, ctx, NOW_DT)
    assert out["available"] and out["buying_power_usd"] == "1234.5" and out["held_quantity"] == "12"
    assert out["average_price_usd"] == "3.95" and out["as_of_kst"].endswith("+09:00")
    assert "account" not in json.dumps(out).replace("account data", "")   # no account number


def test_cash_in_live_block() -> None:
    from app.chat.prompt import cash_snapshot, live_block
    from app.orders.account import AccountService

    assert cash_snapshot(SimpleNamespace(account=None))["available"] is False
    clock = [float(NOW)]
    acct = AccountService(None, account_seq=None, refresh_seconds=10, clock=lambda: clock[0])
    svc = SimpleNamespace(account=acct)
    out = cash_snapshot(svc)
    assert out["available"] is False and out["status"] == "pending"          # not loaded yet
    acct.state.error = "Toss keys not set in .env"
    assert cash_snapshot(svc)["status"] == "error" and cash_snapshot(svc)["message"] == acct.state.error
    acct.state.error = None
    acct.state.buying_power_usd, acct.state.buying_power_krw = D("1234.50"), D("250000")
    acct.state.updated = float(NOW)
    out = cash_snapshot(svc)
    assert out["available"] and out["status"] == "ok"
    assert out["buying_power_usd"] == "1234.50" and out["buying_power_krw"] == "250000"
    assert out["as_of_kst"].endswith("+09:00")
    assert "account" not in json.dumps(out)                                  # no account number
    clock[0] += 61                                                           # > max(60, 6 x 10 s)
    assert cash_snapshot(svc)["status"] == "stale"

    lv = live_block(SimpleNamespace(as_dict=lambda kind: []), NOW_DT, SCREEN, "en", cash_snapshot(svc))
    data = json.loads(lv.removeprefix("<live>\n").removesuffix("\n</live>"))
    assert data["cash"]["buying_power_usd"] == "1234.50" and data["cash"]["status"] == "stale"
    lv0 = live_block(SimpleNamespace(as_dict=lambda kind: []), NOW_DT, SCREEN, "en")
    assert "cash" not in lv0


def test_orders_in_live_block() -> None:
    from app.chat.prompt import live_block, orders_snapshot
    from app.data.toss import ConditionalOrder, ConditionLeg, Execution, Order
    from app.orders.account import AccountService

    assert orders_snapshot(SimpleNamespace(account=None))["available"] is False
    acct = AccountService(None, account_seq=None, refresh_seconds=10, clock=lambda: float(NOW))
    svc = SimpleNamespace(account=acct)
    assert orders_snapshot(svc)["available"] is False                        # not loaded yet
    t = datetime(2026, 11, 20, 14, 30, tzinfo=UTC)                           # 23:30 KST
    acct.state.open_orders = [Order("ORD-SECRET-1", "SOXS", "BUY", "LIMIT", "DAY", "OPEN", D("3.50"), D("100"),
                                    None, "USD", t, None,
                                    Execution(D("40"), D("3.50"), None, None, None, None, None))]
    stop = ConditionLeg("STOP", "WATCHING", "SELL", D("3.20"), None, None, None)
    tp = ConditionLeg("STOP", "WATCHING", "SELL", D("4.10"), None, D("4.05"), None)
    acct.state.open_conditional = [ConditionalOrder("COND-SECRET-1", "OCO", "WATCHING", "SOXS", "US", D("50"),
                                                    "LIMIT", "2026-11-27", tp, stop, t)]
    acct.state.updated = float(NOW)
    out = orders_snapshot(svc)
    assert out["available"] and out["status"] == "ok" and out["as_of_kst"].endswith("+09:00")
    o = out["open_orders"][0]
    assert (o["symbol"], o["side"], o["price"], o["quantity"], o["filled_quantity"], o["currency"]) == \
        ("SOXS", "BUY", "3.50", "100", "40", "USD")
    assert o["ordered_at_kst"] == "2026-11-20T23:30:00+09:00"
    r = out["reserved_orders"][0]
    assert r["type"] == "OCO" and r["currency"] == "USD" and r["quantity"] == "50" and r["expire_date"] == "2026-11-27"
    assert [lg["trigger_price"] for lg in r["legs"]] == ["4.10", "3.20"]
    assert r["legs"][0]["order_price"] == "4.05" and r["legs"][1]["order_price"] is None
    assert r["legs"][1]["order_price_note"] == "market order when triggered"
    text = json.dumps(out)
    assert "SECRET" not in text and "account" not in text                    # no order ids, no account number
    lv = live_block(SimpleNamespace(as_dict=lambda kind: []), NOW_DT, SCREEN, "en", None, out)
    assert json.loads(lv.removeprefix("<live>\n").removesuffix("\n</live>"))["orders"]["reserved_orders"][0]["type"] == "OCO"


def test_scenario_quantity_schema() -> None:
    from app.chat.tools import TOOL_DEFS

    items = next(t for t in TOOL_DEFS if t["name"] == "show_scenarios")["input_schema"]["properties"]["rows"]["items"]
    assert items["properties"]["quantity"]["type"] == ["integer", "null"] and "quantity" in items["required"]
    assert any(t["name"] == "get_account" for t in TOOL_DEFS)


@pytest.mark.anyio
async def test_ai_hline_starts_at_its_levels_bar(tmp_path: Path) -> None:
    """Chart lines float from where their level comes from (Toss style): a swing level keeps the swing bar's time;
    a level without a bar (VWAP) or a typed price keeps the drawing time."""
    svc = await ready_services(tmp_path)
    ctx = ToolContext(svc, "SOXS", SCREEN, user_numbers(["line at 4.50"]), CostModel(D("0.001"), None))
    levels = await ctx.levels("SOXS", "5m")
    sw = next(x for x in levels.all if x.kind == "swing" and x.time)
    now_iso = NOW_DT.astimezone(KST_TZ).replace(microsecond=0).isoformat()
    on_swing = validate_ai_overlay({"type": "hline", "points": [{"time": None, "price": float(sw.price)}]}, ctx, NOW_DT)
    same = [x.time for x in levels.all if x.time and x.price == sw.price]      # earliest bar with that level
    assert on_swing["points"][0]["time"] == min(same, key=lambda s: datetime.fromisoformat(s))
    vwap = lv(levels, "vwap_5m")
    if vwap.time is None and not any(x.time and x.price == vwap.price for x in levels.all):
        on_vwap = validate_ai_overlay({"type": "hline", "points": [{"time": None, "price": float(vwap.price)}]}, ctx, NOW_DT)
        assert on_vwap["points"][0]["time"] == now_iso
    typed = validate_ai_overlay({"type": "hline", "points": [{"time": None, "price": 4.5}]}, ctx, NOW_DT)
    assert typed["points"][0]["time"] == now_iso


# ── lines belong to one timeframe (user choice 2026-11-20) ──

def test_old_line_timeframe_guessed_from_label() -> None:
    from app.overlays import infer_timeframe

    assert infer_timeframe("AI 1분 피보나치") == "1m" and infer_timeframe("AI 5분 EMA20 67.20") == "5m"
    assert infer_timeframe("일봉 추세선") == "1d" and infer_timeframe("1 minute support") == "1m"
    assert infer_timeframe("AI 15분 저항") is None            # 15분 is not 5분
    assert infer_timeframe("AI 저항대 68.01~68.23") is None    # no timeframe named: every timeframe
    assert infer_timeframe("1분 / 5분 비교") is None           # two named: don't guess


@pytest.mark.anyio
async def test_overlays_listed_per_timeframe(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    pt = [{"time": "2026-03-25T23:00:00+09:00", "price": 4}]
    base = {"type": "hline", "points": pt, "color": "#ffffff", "source": "ai", "symbol": "SOXS"}
    svc.save_overlay({**base, "label": "a", "timeframe": "1m"})
    svc.save_overlay({**base, "label": "AI 5분 저항"})              # old line: guessed 5m
    svc.save_overlay({**base, "label": "AI 지지"})                  # old line, no timeframe: everywhere
    names = lambda tf: sorted(o["label"] for o in svc.list_overlays("SOXS", tf))
    assert names("1m") == ["AI 지지", "a"]
    assert names("5m") == ["AI 5분 저항", "AI 지지"]
    assert names(None) == ["AI 5분 저항", "AI 지지", "a"]
    assert next(o for o in svc.list_overlays("SOXS") if o["label"] == "AI 5분 저항")["timeframe"] == "5m"
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as client:
        assert client.get("/api/overlays?symbol=SOXS&timeframe=15m").status_code == 400
        r = client.get("/api/overlays?symbol=SOXS&timeframe=1m").json()
        assert sorted(o["label"] for o in r["overlays"]) == ["AI 지지", "a"]


@pytest.mark.anyio
async def test_ai_line_gets_the_timeframe_it_was_built_from(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    ctx = ToolContext(svc, "SOXS", {**SCREEN, "timeframe": "1m"}, user_numbers(["line at 4.50"]),
                      CostModel(D("0.001"), None))
    five = await ctx.levels("SOXS", "5m")
    hi, lo = lv(five, "swing_high_2"), lv(five, "swing_low_2")
    tl = validate_ai_overlay({"type": "trendline", "points": [
        {"time": hi.time, "price": float(hi.price)}, {"time": lo.time, "price": float(lo.price)}],
        "label": "t", "color": None, "note": ""}, ctx, NOW_DT)
    assert tl["timeframe"] == "5m"                              # 5m swing points, though the screen shows 1m
    typed = validate_ai_overlay({"type": "hline", "points": [{"time": None, "price": 4.5}]}, ctx, NOW_DT)
    assert typed["timeframe"] == "1m"                           # typed price: the chart on screen
