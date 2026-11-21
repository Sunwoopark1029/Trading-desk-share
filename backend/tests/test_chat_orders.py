"""Orders in chat become DRAFTS: the user's numbers exactly, numbers the AI picks flagged "AI-proposed"
(prices only from code levels; user choice 2026-11-20); rules are proposed, never armed; the AI never sends."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.chat.tools import ToolContext, ToolError, check_verbatim, run_tool, TOOL_DEFS
from app.chat.scenarios import CostModel
from app.timeutil import KST
from tests.test_api import NOW
from tests.test_chat_engine import FakeClient, collect, make_engine, ready_services, usage
from tests.test_orders_submit import ORDER_WRITES

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

ARGS = {"kind": "BUY", "symbol": "SOXS", "order_type": "LIMIT", "price": "4.25", "size_mode": "shares",
        "quantity": "10", "percent": None, "amount": None, "time_in_force": "DAY", "legs": None, "expire_date": None}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_strict_tool_schemas_stay_small() -> None:
    """All strict tools compile into one grammar; too much and the API refuses every chat request (400
    "compiled grammar is too large", 2026-11-20, after propose_rule was added as strict). This total worked."""
    import json
    strict = [t for t in TOOL_DEFS if t.get("strict")]
    assert "propose_rule" not in [t["name"] for t in strict]
    assert sum(len(json.dumps(t["input_schema"])) for t in strict) <= 4300


def test_no_send_or_approve_tool_exists_for_the_ai() -> None:
    names = [t["name"] for t in TOOL_DEFS]
    assert "create_order_draft" in names and "propose_rule" in names and "arm_rules" in names
    assert not any(w in n for n in names for w in ("send", "submit", "approve"))


@pytest.mark.parametrize("text,ok", [
    ("SOXS 10주 4.25 지정가로 오늘 매수해줘", True),
    ("buy 10 SOXS at 4.25 limit for today", True),
    ("buy 10 SOXS at 4.26 today", False),          # 4.25 is neither the user's price nor a code level
    ("buy SOXS at 4.25 today", True),              # size picked by the AI: allowed, flagged
    ("buy 10 SOXS at 4.25", True),                 # validity picked by the AI: allowed, flagged
    ("buy 11 shares of SOXS at 4.25 today", False),  # the user said 11 shares: 10 changes their number
])
def test_users_numbers_exact_ai_numbers_allowed(text: str, ok: bool) -> None:
    assert (check_verbatim(ARGS, text) == []) is ok


def test_ai_prices_must_be_code_levels_and_are_flagged() -> None:
    from app.chat.tools import ai_fields

    assert check_verbatim(ARGS, "buy SOXS", [D("4.2512")]) == []           # 4.25 = level 4.2512 at the price tick
    assert any("code-calculated level" in p for p in check_verbatim(ARGS, "buy SOXS", [D("4.40")]))
    assert ai_fields(ARGS, "buy SOXS") == ["price", "quantity", "time_in_force"]
    assert ai_fields(ARGS, "SOXS 10주 4.25 오늘 매수") == []
    assert any("percent" in p for p in check_verbatim({**ARGS, "size_mode": "percent", "quantity": None,
                                                       "percent": "30"}, "buy SOXS at 4.25 today"))


COND = {**ARGS, "kind": "COND_SELL", "price": None, "time_in_force": None,
        "legs": [{"trigger_price": "54.80", "order_price": None}], "order_type": "MARKET", "expire_date": "2026-11-22"}


def test_reserved_order_always_has_an_expiry() -> None:
    """User choice 2026-11-20: the user's date, or one the AI picks (flagged on the draft); never none."""
    from app.chat.tools import expiry_typed

    assert check_verbatim(COND, "54.80 되면 10주 시장가 예약 매도, 11월 22일까지") == []
    assert check_verbatim(COND, "54.80 되면 10주 시장가 예약 매도") == []          # AI-picked date allowed
    assert any("expiry date missing" in p for p in check_verbatim({**COND, "expire_date": None}, "54.80 10주"))
    for kind in ("COND_BUY", "OCO", "OTO"):
        assert any("expiry" in p for p in check_verbatim({**COND, "kind": kind, "expire_date": None}, "54.80 10주"))
    assert expiry_typed("2026-11-22", "11월 22일까지") and expiry_typed("2026-11-02", "11/2")
    assert not expiry_typed("2026-11-22", "54.80 10주")


@pytest.mark.parametrize("qty,ok", [("10", True), ("1,000", True), ("2.5", False), ("0.5", False)])
def test_shares_are_whole_numbers(qty: str, ok: bool) -> None:
    probs = check_verbatim({**ARGS, "quantity": qty}, f"SOXS {qty}주 4.25 지정가 오늘 매수")
    assert (not any("whole number" in p for p in probs)) is ok


@pytest.mark.anyio
async def test_ai_chosen_expiry_is_flagged_on_the_draft(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    now = datetime.fromtimestamp(NOW, UTC)
    exp = (now.date() + timedelta(days=7)).isoformat()
    ctx = ToolContext(svc, "SOXS", {}, [], CostModel(D("0.001"), None), user_text="54.80 되면 10주 시장가 예약 매도")
    out = await run_tool("create_order_draft", {**COND, "expire_date": exp}, ctx, now)
    assert out["expire_date_source"] == "ai_chosen" and "expire_date_ai" in out["warnings"]
    view = svc.drafts.get(out["draft_id"]).view(NOW)
    assert {"code": "expire_date_ai", "params": {"date": exp}} in view["warnings"]
    m, d = exp[5:7].lstrip("0"), exp[8:10].lstrip("0")
    ctx.user_text = f"54.80 되면 10주 시장가 예약 매도, {m}월 {d}일까지"
    mine = await run_tool("create_order_draft", {**COND, "expire_date": exp}, ctx, now)
    assert mine["expire_date_source"] == "user" and "expire_date_ai" not in mine["warnings"]
    assert getattr(svc, "sender").simulated == [] and ORDER_WRITES == []


@pytest.mark.anyio
async def test_tool_creates_draft_only(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    ctx = ToolContext(svc, "SOXS", {}, [], CostModel(D("0.001"), None), user_text="SOXS 10주 4.25 당일 매수")
    out = await run_tool("create_order_draft", ARGS, ctx, datetime.fromtimestamp(NOW, UTC))
    assert out["status"] == "draft" and out["dry_run"] is True and "approve" in out["note"]
    assert out["summary_ko"] == "SOXS 10주 4.25 USD 지정가 매수 (당일)"
    assert svc.drafts.get(out["draft_id"]).source == "chat"
    assert ctx.events == [{"type": "draft_created", "id": out["draft_id"]}]
    assert getattr(svc, "sender").simulated == [] and ORDER_WRITES == []      # nothing sent, not even simulated
    with pytest.raises(ToolError, match="use exactly that quantity"):        # never another count than the user's
        await run_tool("create_order_draft", {**ARGS, "quantity": "11"}, ctx, datetime.fromtimestamp(NOW, UTC))
    with pytest.raises(ToolError, match="code-calculated level"):            # made-up price
        await run_tool("create_order_draft", {**ARGS, "price": "4.31"}, ctx, datetime.fromtimestamp(NOW, UTC))


@pytest.mark.anyio
async def test_ai_proposed_numbers_are_flagged_on_the_draft(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    ctx = ToolContext(svc, "SOXS", {}, [D("4.25")], CostModel(D("0.001"), None), user_text="SOXS 사줘")
    out = await run_tool("create_order_draft", ARGS, ctx, datetime.fromtimestamp(NOW, UTC))
    assert out["status"] == "draft" and out["ai_proposed"] == ["price", "quantity", "time_in_force"]
    view = svc.drafts.get(out["draft_id"]).view(NOW)
    assert {"code": "ai_proposed", "params": {"fields": "price, quantity, time_in_force"}} in view["warnings"]
    assert getattr(svc, "sender").simulated == [] and ORDER_WRITES == []


def rule_args(**change: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "SOXS", "touch": {"dir": "up", "price": "4.30"},
        "trigger": {"dir": "down", "price": "4.26", "confirm": "close_5m"}, "window_min": 15,
        "order": {"side": "SELL", "order_type": "LIMIT", "price": "4.25", "quantity": "10", "time_in_force": "DAY"},
        "sessions": "any", "expires": datetime.fromtimestamp(NOW + 3600, UTC).astimezone(KST).strftime("%Y-%m-%dT%H:%M"),
        "basis": "swing_high_1 / vwap_5m", "note": "rejection at the swing high"}
    base.update(change)
    return base


@pytest.mark.anyio
async def test_ai_rule_is_only_proposed_and_flagged(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    svc.config.rules.max_proposed = 30
    ctx = ToolContext(svc, "SOXS", {}, [D("4.30"), D("4.26"), D("4.25")], CostModel(D("0.001"), None),
                      user_text="4.30 찍고 다시 내려오면 팔게 규칙 만들어줘")
    out = await run_tool("propose_rule", rule_args(), ctx, datetime.fromtimestamp(NOW, UTC))
    assert out["status"] == "proposed" and "Arm" in out["note"]
    assert out["ai_proposed"] == ["trigger.price", "order.price", "order.quantity", "window_min", "expires"]
    rule = next(iter(svc.rules.rules.values()))
    assert rule.source == "ai" and rule.status == "proposed" and rule.armed_at is None and rule.basis
    assert ctx.events == [{"type": "rule_created", "id": rule.id}]
    listed = await run_tool("list_rules", {}, ctx, datetime.fromtimestamp(NOW, UTC))
    assert listed["rules"][0]["status"] == "proposed" and listed["rules"][0]["source"] == "ai"
    with pytest.raises(ToolError, match="code-calculated level"):
        await run_tool("propose_rule", rule_args(touch={"dir": "up", "price": "4.37"}), ctx,
                       datetime.fromtimestamp(NOW, UTC))
    ctx.user_text = "4.30 찍고 내려오면 7주 팔아"
    with pytest.raises(ToolError, match="use exactly that quantity"):
        await run_tool("propose_rule", rule_args(), ctx, datetime.fromtimestamp(NOW, UTC))


@pytest.mark.parametrize("text,ok", [
    ("규칙 걸어줘", True), ("4개 다 활성화해줘", True), ("arm them", True), ("Arm해줘", True),
    ("please activate the rules", True), ("규칙 켜줘", True),
    ("규칙 만들어줘", False), ("어떻게 생각해?", False), ("don't arm yet", False), ("아직 걸지 마", False),
    ("set an alarm", False), ("army of buyers", False),
])
def test_arm_only_when_the_user_asks(text: str, ok: bool) -> None:
    from app.chat.tools import asked_to_arm
    assert asked_to_arm(text) is ok


@pytest.mark.anyio
async def test_ai_arms_on_request_and_cleans_up_replaced_rules(tmp_path: Path) -> None:
    from app.orders.account import AccountService
    from app.data.toss import HoldingItem, Holdings

    svc = await ready_services(tmp_path)
    acct = AccountService(None, account_seq=None, refresh_seconds=10, clock=svc.clock)
    item = HoldingItem("SOXS", "SOXS", "US", "USD", D("55"), D("4.25"), D("4.00"), {}, {}, {}, {})
    acct.state.holdings = Holdings(None, None, None, None, (item,))
    acct.state.buying_power_usd, acct.state.updated = D("1000"), NOW
    svc.account = acct
    ctx = ToolContext(svc, "SOXS", {}, [D("4.30"), D("4.26"), D("4.25")], CostModel(D("0.001"), None),
                      user_text="5분봉 보고 익절 규칙 만들어줘")
    now = datetime.fromtimestamp(NOW, UTC)
    a = (await run_tool("propose_rule", rule_args(), ctx, now))["rule_id"]
    b = (await run_tool("propose_rule", rule_args(), ctx, now))["rule_id"]
    with pytest.raises(ToolError, match="does not ask to arm"):            # user only asked to MAKE rules
        await run_tool("arm_rules", {"arm": [a], "disarm": [], "delete": []}, ctx, now)
    assert svc.rules.find(a).status == "proposed"
    ctx.user_text = "그거 걸어줘"
    out = await run_tool("arm_rules", {"arm": [a], "disarm": [], "delete": [b, "zzzzzzzz"]}, ctx, now)
    res = {(r["action"], r["id"]): r for r in out["results"]}
    assert res[("arm", a)]["ok"] and res[("delete", b)]["ok"] and not res[("delete", "zzzzzzzz")]["ok"]
    rule = svc.rules.find(a)
    assert rule.status == "armed" and rule.armed_by == "ai" and svc.rules.find(b) is None
    assert svc.rules.view(rule)["armed_by"] == "ai"
    out = await run_tool("arm_rules", {"arm": [], "disarm": [], "delete": [a]}, ctx, now)
    assert out["results"][0]["error"] == "only unarmed rules can be deleted"        # armed: disarm first
    ctx.user_text = "규칙 설명해줘"
    out = await run_tool("arm_rules", {"arm": [], "disarm": [a], "delete": []}, ctx, now)  # disarm needs no words
    assert out["results"][0]["ok"] and rule.status == "proposed" and rule.armed_by is None
    log = (tmp_path / "logs" / "trades.jsonl").read_text(encoding="utf-8")
    assert '"armed_by":"ai"' in log
    assert getattr(svc, "sender").simulated == [] and ORDER_WRITES == []
    assert getattr(svc, "sender").simulated == [] and ORDER_WRITES == []


@pytest.mark.anyio
async def test_chat_turn_creates_draft_event(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    client = FakeClient([
        ([{"type": "tool_use", "id": "t1", "name": "create_order_draft", "input": ARGS}], "tool_use", usage()),
        ([{"type": "text", "text": "초안을 만들었습니다. 주문 탭에서 확인 후 승인하세요."}], "end_turn", usage()),
    ])
    eng = make_engine(svc, tmp_path, client)
    cid = eng.store.create()
    events = await collect(eng, cid, "SOXS 10주 4.25 지정가 당일 매수")
    assert any(e["type"] == "draft_created" for e in events)
    assert len(svc.drafts.active("SOXS")) == 1 and ORDER_WRITES == []


def test_quick_analysis_prompt_never_arms_rules() -> None:
    """The chat's built-in ⚡ prompt (frontend i18n, EN + KO) asks for rules to be proposed, never armed."""
    import re as _re
    from app.chat.tools import asked_to_arm

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "i18n.tsx").read_text(encoding="utf-8")
    prompts = _re.findall(r'"chat\.quickPrompt": "((?:[^"\\]|\\.)*)"', src)
    assert len(prompts) == 2
    for text in prompts:
        assert "1m" in text or "1분봉" in text
        assert not asked_to_arm(text.replace("\\n", "\n"))
