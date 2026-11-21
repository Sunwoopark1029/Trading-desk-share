"""Phase 6 part C step C2: research chat mode (own prompt + tools, <fundamentals> block, valuation card, number check)."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from app.chat.engine import TurnRequest
from app.chat.research import (
    RESEARCH_SYSTEM_PROMPT, research_tool_defs, unverified_research_numbers, valuation_card,
)
from app.chat.tools import TOOL_DEFS, ToolContext, ToolError
from tests.test_chat_engine import FakeClient, make_engine, usage
from tests.test_research import setup


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── number check ────────────────────────────────────────────────────────

def test_research_number_check_accepts_display_strings_and_flags_the_rest() -> None:
    src = ['{"display":"28.4x","id":"pe_ttm"} {"display":"+10.0%"} {"display":"USD 35.1B"} {"value":"0.18234"}',
           '{"eps":"1.6432","raw":"35100000000","median":"21.3333"}']
    ok = ("P/E 28.4x (pe_ttm), growth +10.0%, revenue USD 35.1B, margin 18.2%, EPS USD 1.64, "
          "peer median 21.3x, 351억 달러")
    assert unverified_research_numbers(ok, src, []) == []
    bad = unverified_research_numbers("P/E 30x, margin 19%, revenue 40.2B, price 123.45, 0% change", src, [])
    assert bad == ["19%", "30x", "40.2B", "123.45"]           # 0% is never flagged
    assert unverified_research_numbers("typed 123.45", src, [D("123.45")]) == []   # the user's own number


# ── valuation card ──────────────────────────────────────────────────────

def _ctx(svc: Any, pack: dict[str, Any] | None) -> ToolContext:
    from app.chat.scenarios import CostModel
    ctx = ToolContext(svc, "AAPL", {}, [], CostModel(D("0.001"), None, None))
    ctx.research.update(pack=pack, price={"last": "200", "currency": "USD"}, as_of={"statements": "x"})
    return ctx


def _args(**kw: Any) -> dict[str, Any]:
    base = {"verdicts": {"own_history": "unclear", "peers": "unclear", "growth": "fair"},
            "summary": "Fair against growth.", "basis": ["pe_ttm", "rev_yoy"], "reasoning": "r",
            "risks": ["a"], "change_view": ["b"], "reference_ids": []}
    base.update(kw)
    return base


@pytest.mark.anyio
async def test_valuation_card_checks_ids_and_fills_numbers_from_the_pack(tmp_path: Path) -> None:
    svc, rs, _, _ = await setup(tmp_path)
    pack = (await rs.metrics("AAPL"))["pack"]
    by = {v["id"]: v for v in pack["values"]}
    ctx = _ctx(svc, pack)
    card = valuation_card(_args(), ctx)
    assert card["verdicts"] == {"own_history": "unclear", "peers": "unclear", "growth": "fair"}
    assert card["basis"][0]["display"] == by["pe_ttm"]["display"] and card["symbol"] == "AAPL"
    with pytest.raises(ToolError, match="unknown pack id"):
        valuation_card(_args(basis=["pe_ttm", "made_up"]), ctx)
    with pytest.raises(ToolError, match="must be one of"):
        valuation_card(_args(verdicts={"own_history": "cheapish", "peers": "unclear", "growth": "fair"}), ctx)
    if by.get("pe_peer_median", {}).get("value") is None:      # no peer median yet: a peers verdict is refused
        with pytest.raises(ToolError, match="use 'unclear'"):
            valuation_card(_args(verdicts={"own_history": "unclear", "peers": "cheap", "growth": "fair"}), ctx)
    with pytest.raises(ToolError, match="implied-price or target id"):
        valuation_card(_args(reference_ids=["pe_ttm"]), ctx)
    tgt = next((i for i in ("target_mean", "target_median") if by.get(i, {}).get("value") is not None), None)
    if tgt:
        ref = valuation_card(_args(reference_ids=[tgt]), ctx)["reference_levels"][0]
        assert ref["price"] == by[tgt]["value"] and ref["display"] == by[tgt]["display"]
    with pytest.raises(ToolError, match="no fundamentals pack"):
        valuation_card(_args(), _ctx(svc, None))


# ── a research turn end to end (fake model) ─────────────────────────────

@pytest.mark.anyio
async def test_research_turn_uses_its_own_prompt_tools_and_blocks(tmp_path: Path) -> None:
    svc, rs, _, _ = await setup(tmp_path)
    pack = (await rs.metrics("AAPL"))["pack"]
    pe = next(v for v in pack["values"] if v["id"] == "pe_ttm")
    client = FakeClient([
        ([{"type": "tool_use", "id": "t1", "name": "create_order_draft", "input": {"symbol": "AAPL"}},
          {"type": "tool_use", "id": "t2", "name": "show_valuation", "input": _args()}],
         "tool_use", usage(2000, 80, write=1500)),
        ([{"type": "text", "text": f"Fair. P/E {pe['display']} (pe_ttm). Not 99.9x."}], "end_turn", usage(300, 60)),
        ([{"type": "text", "text": "Same data."}], "end_turn", usage(300, 20)),
    ])
    eng = make_engine(svc, tmp_path, client)
    cid = eng.store.create(symbol="AAPL", kind="research")
    req = TurnRequest(text="is AAPL cheap?", tier="normal", screen={"symbol": "AAPL"})
    events = [ev async for ev in eng.run_turn(cid, req)]

    first = client.calls[0]
    assert first["system"][0]["text"] == RESEARCH_SYSTEM_PROMPT
    names = [t["name"] for t in first["tools"]]
    assert names == [t["name"] for t in research_tool_defs()] and names[0] == "get_levels"
    assert not {"create_order_draft", "propose_rule", "arm_rules", "add_overlay"} & set(names)
    assert next(t for t in TOOL_DEFS if t["name"] == "get_levels") == first["tools"][0]   # same bytes as the Desk
    blocks = [b["text"][:14] for b in first["messages"][0]["content"]]
    assert blocks[0].startswith("<context>") and blocks[1].startswith("<fundamentals>")
    assert blocks[2] == "is AAPL cheap?"[:14] and blocks[3].startswith("<live>") and blocks[4].startswith("[Screen")
    assert pe["display"] in first["messages"][0]["content"][3]["text"] or pe["display"] in first["messages"][0]["content"][1]["text"]

    # the order tool does not exist here; the card is checked and sent to the screen
    tool_results = client.calls[1]["messages"][-1]["content"]
    assert tool_results[0]["is_error"] is True and "unknown tool" in tool_results[0]["content"]
    assert any(ev["type"] == "valuation" for ev in events)
    done = next(ev for ev in events if ev["type"] == "done")["display"]
    assert done["valuations"][0]["verdicts"]["growth"] == "fair"
    assert any("99.9x" in w for w in done["warnings"]) and not any(pe["display"] in w for w in done["warnings"])
    assert eng.usage.records()[-1]["kind"] == "research"
    assert client.calls[1]["tools"] == first["tools"] and client.calls[1]["system"] == first["system"]

    # second question: fundamentals unchanged -> not sent again (context unchanged too)
    [ev async for ev in eng.run_turn(cid, TurnRequest(text="and now?", tier="normal", screen={"symbol": "AAPL"}))]
    second = client.calls[2]["messages"][-1]["content"]
    assert [b["text"][:6] for b in second] == ["and no", "<live>", "[Scree"]
    assert client.calls[2]["messages"][: len(client.calls[1]["messages"])] == client.calls[1]["messages"][:len(client.calls[1]["messages"])]


@pytest.mark.anyio
async def test_desk_chat_is_unchanged_by_research_mode(tmp_path: Path) -> None:
    svc, _, _, _ = await setup(tmp_path)
    client = FakeClient([([{"type": "text", "text": "ok"}], "end_turn", usage())])
    eng = make_engine(svc, tmp_path, client)
    cid = eng.store.create(symbol="AAPL")
    [ev async for ev in eng.run_turn(cid, TurnRequest(text="hi", tier="normal", screen={"symbol": "AAPL"}))]
    call = client.calls[0]
    assert [t["name"] for t in call["tools"]] == [t["name"] for t in TOOL_DEFS]
    assert call["system"][0]["text"].startswith("You are the analysis assistant")
    assert not any(b["text"].startswith("<fundamentals>") for b in call["messages"][0]["content"])


def test_number_check_forms_units_dates_and_timestamps() -> None:
    src = ['{"display":"28.4x","as_of":"2026-11-21T10:37:45+09:00","id":"rev_cagr_3y"} {"display":"USD 35.1B"} '
           '{"display":"USD 6.08"} {"v":"0.18234"} {"raw":"35100000000","last":"182.35"}']
    ok = ("P/E 28.4x, 28.4배, margin 18.2%, revenue USD 35.1B, 351억, EPS USD 6.08, price USD 182, 182.35달러, "
          "다음 실적 11.21(목)")
    assert unverified_research_numbers(ok, src, []) == []
    bad = unverified_research_numbers(
        "P/E 45%, 21x, 9.0%, USD 35.1M, 97.3배, 97.3×, USD 40, PER97.3x, 40 billion", src, [])
    # 45 / 21 / 9 / 40 only appear in the timestamp; 35.1 exists but as B, not M
    assert set(bad) == {"45%", "9.0%", "21x", "97.3배", "97.3×", "97.3x", "35.1M", "40 billion", "USD 40"}


def test_fundamentals_key_ignores_timestamps() -> None:
    from app.chat.research import fundamentals_block

    def data(as_of: str, updated: str, display: str = "28.4x") -> dict[str, Any]:
        v = {"id": "pe_ttm", "display": display, "period": "TTM", "kind": "multiple", "note": None,
             "source": "calculated (SEC EDGAR)", "as_of": as_of, "live": False, "value": "28.4", "label": "P/E"}
        return {"overview": {}, "metrics": {"pack": {"values": [v], "currency": "USD", "warnings": [], "entity": "X"},
                                            "meta": {"statements": {"source": "SEC EDGAR", "updated": updated}}}}

    t1, k1 = fundamentals_block("X", data("2026-11-21T10:00:00+09:00", "2026-11-21T10:00:00+09:00"))
    t2, k2 = fundamentals_block("X", data("2026-11-21T16:00:00+09:00", "2026-11-21T16:00:00+09:00"))
    _, k3 = fundamentals_block("X", data("2026-11-21T16:00:00+09:00", "x", display="29.0x"))
    assert k1 == k2 and k1 != k3 and '"as_of"' not in t1


@pytest.mark.anyio
async def test_card_text_is_checked_and_peers_fill_in_the_same_turn(tmp_path: Path) -> None:
    svc, rs, _, _ = await setup(tmp_path)
    rs.index.ensure()
    await rs.index._task
    card = _args(verdicts={"own_history": "unclear", "peers": "cheap", "growth": "fair"},
                 summary="Upside 63.4% to fair value.", basis=["pe_ttm", "pe_peer_median"],
                 reference_ids=["fv_pe_peer_median"])
    client = FakeClient([
        ([{"type": "tool_use", "id": "p1", "name": "compare_peers", "input": {"symbol": "NVDA"}}],
         "tool_use", usage(2000, 40)),
        ([{"type": "tool_use", "id": "v1", "name": "show_valuation", "input": card}], "tool_use", usage(300, 40)),
        ([{"type": "text", "text": "Upside 63.4%."}], "end_turn", usage(300, 20)),
    ])
    eng = make_engine(svc, tmp_path, client)
    cid = eng.store.create(symbol="NVDA", kind="research")
    events = [ev async for ev in eng.run_turn(cid, TurnRequest(text="cheap vs peers?", tier="normal",
                                                               screen={"symbol": "NVDA"}))]
    done = next(ev for ev in events if ev["type"] == "done")["display"]
    assert done["valuations"][0]["verdicts"]["peers"] == "cheap"                 # peer median filled in this turn
    assert done["valuations"][0]["reference_levels"][0]["id"] == "fv_pe_peer_median"
    assert any("63.4%" in w for w in done["warnings"])                           # card text does not vouch for itself


@pytest.mark.anyio
async def test_discovery_tools_and_checked_candidates(tmp_path: Path) -> None:
    svc, rs, _, _ = await setup(tmp_path)
    client = FakeClient([
        ([{"type": "tool_use", "id": "r1", "name": "get_rankings",
           "input": {"type": "MARKET_TRADING_VOLUME", "duration": "1d", "count": 5}},
          {"type": "tool_use", "id": "r2", "name": "list_sectors", "input": {"query": None}},
          {"type": "tool_use", "id": "r3", "name": "search_tickers", "input": {"query": "AA"}},
          {"type": "tool_use", "id": "r4", "name": "show_candidates",
           "input": {"title": "Look at", "items": [{"symbol": "AAPL", "reason": "most traded"},
                                                    {"symbol": "FAKEZZ", "reason": "x"}]}}],
         "tool_use", usage(2000, 40)),
        ([{"type": "tool_use", "id": "r5", "name": "show_candidates",
           "input": {"title": "Look at", "items": [{"symbol": "aapl", "reason": "up 87.5% this week"},
                                                    {"symbol": "NVDA", "reason": "same sector"}]}}],
         "tool_use", usage(300, 40)),
        ([{"type": "text", "text": "Two to look at."}], "end_turn", usage(300, 20)),
    ])
    eng = make_engine(svc, tmp_path, client)
    cid = eng.store.create(symbol="AAPL", kind="research")
    events = [ev async for ev in eng.run_turn(cid, TurnRequest(text="what else?", tier="normal",
                                                               screen={"symbol": "AAPL"}))]
    names = [t["name"] for t in client.calls[0]["tools"]]
    assert {"get_rankings", "list_sectors", "get_sector", "search_tickers", "show_candidates"} <= set(names)
    results = client.calls[1]["messages"][-1]["content"]
    assert not results[0].get("is_error") and '"rows"' in results[0]["content"]
    assert not results[1].get("is_error") and '"sectors"' in results[1]["content"]
    assert not results[2].get("is_error") and "AAPL" in results[2]["content"]
    assert results[3]["is_error"] and "FAKEZZ" in results[3]["content"]          # unknown ticker refused
    done = next(ev for ev in events if ev["type"] == "done")["display"]
    lst = done["candidates"][0]
    assert [i["symbol"] for i in lst["items"]] == ["AAPL", "NVDA"]
    assert any("87.5%" in w for w in done["warnings"])                           # reason text is checked too
