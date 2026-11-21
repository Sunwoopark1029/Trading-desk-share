"""Automatic rules: rule checks, the touch / trigger state machine, and the engine (arming, firing once
through the normal send path, DRY RUN, stale data, restart). Fake Toss only: no real order is ever sent."""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.data.toss import HoldingItem, Holdings, TradeTick
from app.orders.account import AccountService
from app.rules.engine import RuleEngine, RuleError
from app.rules.model import build_rule, step_bar, step_tick
from app.timeutil import KST
from tests.test_api import NOW, toss_handler
from tests.test_orders_drafts import ctx
from tests.test_orders_submit import ORDER_WRITES, setup

D = Decimal


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def kst_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).astimezone(KST).isoformat()


def sell_rule(**change: Any) -> dict[str, Any]:
    """Touch 4.30 or higher, then back to 4.26 or lower within 15 min -> sell 14 at 4.25 limit."""
    base: dict[str, Any] = {
        "symbol": "SOXS", "touch": {"dir": "up", "price": "4.30"},
        "trigger": {"dir": "down", "price": "4.26", "confirm": "tick"}, "window_min": 15,
        "order": {"side": "SELL", "order_type": "LIMIT", "price": "4.25", "quantity": "14", "time_in_force": "DAY"},
        "expires": kst_iso(NOW + 3600)}
    base.update(change)
    return base


def buy_rule(**change: Any) -> dict[str, Any]:
    """Touch 4.20 or lower, then back up to 4.25 or higher within 10 min -> buy 10 at 4.25 limit."""
    base: dict[str, Any] = {
        "symbol": "SOXS", "touch": {"dir": "down", "price": "4.20"},
        "trigger": {"dir": "up", "price": "4.25", "confirm": "tick"}, "window_min": 10,
        "order": {"side": "BUY", "order_type": "LIMIT", "price": "4.25", "quantity": "10", "time_in_force": "DAY"},
        "expires": kst_iso(NOW + 3600)}
    base.update(change)
    return base


def built(inp: dict[str, Any], **kw: Any) -> Any:
    rule, errors, warnings = build_rule(inp, kw.pop("c", ctx(held_qty=D("20"))), now=NOW, source=kw.pop("source", "user"),
                                        max_days=30, **kw)
    return rule, [i.code for i in errors], [i.code for i in warnings]


def armed(inp: dict[str, Any]) -> Any:
    rule, errors, _ = built(inp)
    assert rule is not None, errors
    rule.status, rule.armed_at = "armed", NOW
    return rule


# ── rule checks ──────────────────────────────────────────────────────────

def test_rule_is_built_unarmed_with_exact_numbers_and_summary() -> None:
    rule, errors, warnings = built(sell_rule())
    assert errors == [] and warnings == [] and rule.status == "proposed" and rule.armed_at is None
    assert rule.touch == {"dir": "up", "price": D("4.30")} and rule.trigger["price"] == D("4.26")
    assert rule.order["price"] == "4.25" and rule.order["quantity"] == "14" and rule.order["kind"] == "SELL"
    assert "touches 4.30 USD or higher" in rule.summary_en and "within 15 min" in rule.summary_en
    assert "falls to 4.26 USD or lower" in rule.summary_en and "Sell 14 SOXS at 4.25 USD" in rule.summary_en
    assert "4.30 USD 이상" in rule.summary_ko and "15분" in rule.summary_ko
    back = type(rule).from_json(json.loads(json.dumps(rule.to_json())))
    assert back.trigger == rule.trigger and back.touch == rule.touch and back.order == rule.order


@pytest.mark.parametrize("change,code", [
    ({"trigger": {"dir": "down", "price": "4.30", "confirm": "tick"}}, "rule_levels_order"),     # Y must be below X
    ({"trigger": {"dir": "up", "price": "4.40", "confirm": "tick"}}, "rule_levels_order"),       # same direction
    ({"touch": None}, "rule_use_reserved_order"),                    # plain "price reaches Y" = Toss reserved order
    ({"window_min": None}, "rule_window"),
    ({"window_min": 0}, "rule_window"),
    ({"window_min": 3, "trigger": {"dir": "down", "price": "4.26", "confirm": "close_5m"}}, "rule_window_short"),
    ({"expires": ""}, "rule_expiry_required"),
    ({"expires": kst_iso(NOW - 1)}, "rule_expiry_past"),
    ({"expires": kst_iso(NOW + 31 * 86400)}, "rule_expiry_too_far"),
    ({"trigger": {"dir": "down", "price": "4.26", "confirm": "close_15m"}}, "rule_confirm"),
    ({"trigger": {"dir": "down", "price": "4.26123", "confirm": "tick"}}, "price_decimals"),
    ({"sessions": "night"}, "rule_sessions"),
    ({"order": {"side": "SELL", "order_type": "LIMIT", "price": "4.50", "quantity": "14", "time_in_force": "DAY"}},
     "price_out_of_band"),                                           # limit 5.6% away from the 4.26 trigger
    ({"order": {"side": "SELL", "order_type": "LIMIT", "price": "4.255", "quantity": "14", "time_in_force": "DAY"}},
     "price_decimals"),
    ({"order": {"side": "SELL", "order_type": "MARKET", "quantity": "1.5", "time_in_force": "DAY"}}, "quantity_whole"),
    ({"order": {"side": "SELL", "order_type": "LIMIT", "price": "4.25", "quantity": "14"}}, "tif_required"),
    ({"order": {"side": "HOLD", "order_type": "LIMIT", "price": "4.25", "quantity": "14", "time_in_force": "DAY"}},
     "rule_side"),
])
def test_rule_errors(change: dict[str, Any], code: str) -> None:
    rule, errors, _ = built(sell_rule(**change))
    assert rule is None and code in errors


def test_account_problems_are_warnings_when_written_and_market_order_is_allowed() -> None:
    rule, errors, warnings = built(sell_rule(), c=ctx(held_qty=D("5")))        # holds 5, rule sells 14
    assert rule is not None and errors == [] and "not_enough_shares" in warnings
    mkt = sell_rule(order={"side": "SELL", "order_type": "MARKET", "quantity": "14", "time_in_force": "DAY"})
    rule, errors, _ = built(mkt)
    assert rule is not None and errors == [] and "at market" in rule.summary_en


def test_candle_close_rule_without_touch_and_buy_cap_warning() -> None:
    inp = buy_rule(touch=None, window_min=None, trigger={"dir": "up", "price": "4.25", "confirm": "close_5m"})
    rule, errors, warnings = built(inp)
    assert rule is not None and errors == [] and "a 5m candle closes at 4.25 USD or higher" in rule.summary_en
    # buy 10 x 4.25 = 42.50 USD; cap 4% of 1000 USD buying power = 40 USD
    _rule, _e, warnings = built(inp, max_buy_pct=D("4"))
    assert "rule_over_buy_cap" in warnings
    _rule, _e, warnings = built(inp, max_buy_pct=D("5"))
    assert "rule_over_buy_cap" not in warnings


def test_ai_fields_kept_only_for_ai_rules() -> None:
    inp = sell_rule(ai_fields=["trigger.price", "order.quantity"], basis="vwap_5m", note="rejection at VWAP")
    assert built(inp, source="ai")[0].ai_fields == ["trigger.price", "order.quantity"]
    assert built(inp, source="user")[0].ai_fields == []


# ── state machine: trades ────────────────────────────────────────────────

def test_touch_then_fall_back_fires_once_in_order() -> None:
    r = armed(sell_rule())
    assert not step_tick(r, D("4.26"), NOW + 1) and r.status == "armed"          # trigger level before any touch
    assert not step_tick(r, D("4.29"), NOW + 2) and r.status == "armed"          # not touched yet
    assert not step_tick(r, D("4.30"), NOW + 3) and r.status == "touched"        # touch (X or higher)
    assert r.touched_at == NOW + 3 and r.touch_price == D("4.30")
    assert not step_tick(r, D("4.27"), NOW + 4)                                  # not back to Y yet
    assert step_tick(r, D("4.26"), NOW + 5)                                      # Y or lower: fire


def test_gap_through_both_levels_and_trades_before_arming() -> None:
    r = armed(sell_rule())
    assert not step_tick(r, D("4.35"), NOW - 5) and r.status == "armed"          # before arming: ignored
    assert not step_tick(r, D("4.35"), NOW + 1) and r.status == "touched"
    assert step_tick(r, D("4.10"), NOW + 2)                                      # straight through Y


def test_window_runs_out_then_needs_a_new_touch() -> None:
    r = armed(sell_rule(window_min=1))
    step_tick(r, D("4.31"), NOW + 1)
    assert r.status == "touched"
    assert not step_tick(r, D("4.26"), NOW + 62) and r.status == "armed"         # 61 s later: too late, reset
    assert not step_tick(r, D("4.25"), NOW + 63)                                 # still no new touch
    assert not step_tick(r, D("4.30"), NOW + 64) and r.status == "touched"
    assert step_tick(r, D("4.26"), NOW + 65)


def test_touch_then_bounce_buy() -> None:
    r = armed(buy_rule())
    assert not step_tick(r, D("4.25"), NOW + 1)
    assert not step_tick(r, D("4.19"), NOW + 2) and r.status == "touched"
    assert not step_tick(r, D("4.24"), NOW + 3)
    assert step_tick(r, D("4.25"), NOW + 4)


def test_unarmed_and_finished_rules_never_fire() -> None:
    r, _, _ = built(sell_rule())
    assert not step_tick(r, D("4.31"), NOW + 1) and r.status == "proposed"
    r.status = "sent"
    assert not step_tick(r, D("4.20"), NOW + 2) and not step_bar(r, bar(NOW + 300, "4.4", "4.1", "4.1"), 300)


# ── state machine: finished candles ──────────────────────────────────────

def bar(end: float, high: str, low: str, close: str) -> dict[str, Any]:
    return {"end": int(end), "high": D(high), "low": D(low), "close": D(close)}


def test_candle_confirm_ignores_a_wick_and_fires_on_the_close() -> None:
    r = armed(sell_rule(trigger={"dir": "down", "price": "4.26", "confirm": "close_5m"}))
    step_tick(r, D("4.31"), NOW + 10)
    assert r.status == "touched"
    assert not step_tick(r, D("4.20"), NOW + 20)                                 # a dip under Y is not a close
    assert not step_bar(r, bar(NOW + 300, "4.31", "4.20", "4.28"), 300)          # closed back above Y: no fire
    assert not step_bar(r, bar(NOW + 300, "4.31", "4.20", "4.20"), 300)          # same candle is never read twice
    assert not step_bar(r, bar(NOW + 360, "4.29", "4.20", "4.20"), 60)           # 1m candle: wrong timeframe
    assert step_bar(r, bar(NOW + 600, "4.29", "4.24", "4.25"), 300)              # 5m close at / under Y: fire


def test_candle_high_counts_as_the_touch_when_trades_were_missed() -> None:
    r = armed(sell_rule(trigger={"dir": "down", "price": "4.26", "confirm": "close_5m"}))
    assert step_bar(r, bar(NOW + 300, "4.32", "4.22", "4.24"), 300)              # wick to X, close under Y
    assert r.touch_price == D("4.32")
    r2 = armed(sell_rule(trigger={"dir": "down", "price": "4.26", "confirm": "close_5m"}))
    assert not step_bar(r2, bar(NOW + 300, "4.29", "4.22", "4.24"), 300)         # never reached X


def test_candle_close_without_touch_and_candles_before_arming() -> None:
    r = armed(buy_rule(touch=None, window_min=None, trigger={"dir": "up", "price": "4.25", "confirm": "close_1m"}))
    assert not step_tick(r, D("4.40"), NOW + 1)                                  # trades alone never fire it
    assert not step_bar(r, bar(NOW, "4.4", "4.3", "4.4"), 60)                    # ended when it was armed: ignored
    assert not step_bar(r, bar(NOW + 60, "4.26", "4.2", "4.24"), 60)
    assert step_bar(r, bar(NOW + 120, "4.26", "4.2", "4.25"), 60)


def test_candle_confirm_respects_the_window() -> None:
    r = armed(sell_rule(window_min=5, trigger={"dir": "down", "price": "4.26", "confirm": "close_5m"}))
    step_tick(r, D("4.31"), NOW + 10)
    assert not step_bar(r, bar(NOW + 600, "4.27", "4.2", "4.2"), 300) and r.status == "armed"   # 590 s > 5 min


# ── engine ───────────────────────────────────────────────────────────────

class FakeMarket:
    def __init__(self, clock: Any) -> None:
        self.clock, self.states, self.connected = clock, {}, True

    def interest(self, symbol: str) -> None:
        self.states.setdefault(symbol, SimpleNamespace(tape=deque(maxlen=100), error=None, last_rest=0.0))

    def stream_connected(self) -> bool:
        return self.connected

    def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def last_trade(self, symbol: str) -> Any:
        st = self.states.get(symbol)
        return st.tape[-1] if st and st.tape else None

    def trade(self, symbol: str, price: str, at: float | None = None) -> None:
        self.interest(symbol)
        ts = datetime.fromtimestamp(self.clock() if at is None else at, UTC)
        self.states[symbol].tape.append(TradeTick(D(price), D("1"), ts, "USD"))


async def make(tmp_path: Path, dry_run: bool = True, held: str = "20") -> tuple[Any, RuleEngine, list[float]]:
    svc = await setup(tmp_path, dry_run=dry_run)
    await svc.refresh_calendar()                                 # fixture calendar: NOW is in the regular session
    t = [float(NOW)]
    svc.clock = lambda: t[0]
    svc.drafts.clock = svc.clock
    svc.market = FakeMarket(svc.clock)
    acct = AccountService(None, account_seq=None, refresh_seconds=10, clock=svc.clock)
    item = HoldingItem("SOXS", "SOXS", "US", "USD", D(held), D("4.25"), D("4.00"), {}, {}, {}, {})
    acct.state.holdings = Holdings(None, None, None, None, (item,))
    acct.state.buying_power_usd, acct.state.updated = D("1000"), t[0]
    svc.account = acct
    return svc, RuleEngine(svc, tmp_path / "rules.json"), t


async def play(svc: Any, eng: RuleEngine, t: list[float], prices: list[str], step: float = 1.0) -> None:
    for p in prices:
        t[0] += step
        svc.market.trade("SOXS", p)
        await eng.run_once()


def events(tmp_path: Path) -> list[str]:
    path = tmp_path / "trades.jsonl"
    return [json.loads(l)["event"] for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.mark.anyio
async def test_dry_run_rule_fires_once_and_never_reaches_toss(tmp_path: Path) -> None:
    svc, eng, t = await make(tmp_path)
    rule, errors, _ = await eng.create(sell_rule(), "user")
    assert rule is not None and errors == [] and rule.status == "proposed"
    svc.market.trade("SOXS", "4.25")
    await eng.run_once()
    await play(svc, eng, t, ["4.31", "4.26"])                    # not armed: nothing happens
    assert rule.status == "proposed" and svc.sender.simulated == []
    await eng.arm(rule.id)
    await eng.run_once()                                        # first look: earlier trades do not count
    await play(svc, eng, t, ["4.26", "4.31"])
    assert rule.status == "touched"
    await play(svc, eng, t, ["4.27", "4.26"])
    assert rule.status == "sent", rule.result
    assert rule.result["status"] == "simulated" and rule.fire_price == D("4.26")
    assert len(svc.sender.simulated) == 1 and ORDER_WRITES == []
    sim = svc.sender.simulated[0]["values"]
    assert (sim["side"], sim["quantity"], sim["price"], sim["order_type"]) == ("SELL", D("14"), D("4.25"), "LIMIT")
    await play(svc, eng, t, ["4.31", "4.20", "4.31", "4.20"])   # keeps moving: no second order
    assert len(svc.sender.simulated) == 1 and ORDER_WRITES == []
    assert events(tmp_path) == ["rule_created", "rule_armed", "rule_touched", "rule_fired", "request", "result",
                                "rule_result"]
    assert svc.drafts.get(rule.result["draft_id"]).source == "rule"


@pytest.mark.anyio
async def test_real_mode_sends_exactly_one_order_with_the_armed_numbers(tmp_path: Path) -> None:
    from tests.test_toss import account_routes

    sent: list[dict[str, Any]] = []

    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/api/v1/orders":
            sent.append(json.loads(req.content))
            return httpx.Response(200, json={"result": {"orderId": "T-9", "clientOrderId": "x"}})
        r = account_routes(req)
        return r if r.status_code != 404 else toss_handler(req)

    svc, eng, t = await make(tmp_path, dry_run=False)
    svc.toss._http = httpx.AsyncClient(transport=httpx.MockTransport(h))
    svc.toss.enable_order_writes()
    svc.account = AccountService(svc.toss, account_seq=None, refresh_seconds=10, clock=svc.clock)
    await svc.account.refresh_now()
    rule, errors, _ = await eng.create(buy_rule(order={"side": "BUY", "order_type": "LIMIT", "price": "4.25",
                                                       "quantity": "1", "time_in_force": "DAY"}), "user")
    assert rule is not None, errors
    await eng.arm(rule.id)
    await eng.run_once()
    await play(svc, eng, t, ["4.19", "4.25", "4.19", "4.25"])
    assert rule.status == "sent" and rule.result["order_id"] == "T-9" and rule.result["dry_run"] is False
    assert len(sent) == 1 and sent[0]["clientOrderId"] == rule.result["draft_id"]
    assert (sent[0]["side"], sent[0]["quantity"], sent[0]["price"]) == ("BUY", "1", "4.25")


@pytest.mark.anyio
async def test_blocked_at_fire_time_does_not_send_or_retry(tmp_path: Path) -> None:
    svc, eng, t = await make(tmp_path, held="20")
    rule, _, _ = await eng.create(sell_rule(), "user")
    await eng.arm(rule.id)
    await eng.run_once()
    svc.account.state.holdings = Holdings(None, None, None, None, ())      # shares sold elsewhere meanwhile
    await play(svc, eng, t, ["4.31", "4.26"])
    assert rule.status == "blocked" and rule.result["issues"][0]["code"] == "not_enough_shares"
    await play(svc, eng, t, ["4.31", "4.26"])
    assert rule.status == "blocked" and svc.sender.simulated == [] and ORDER_WRITES == []


@pytest.mark.anyio
async def test_regular_only_rule_is_blocked_outside_the_regular_session(tmp_path: Path) -> None:
    svc, eng, t = await make(tmp_path)
    rule, _, _ = await eng.create(sell_rule(sessions="regular", expires=kst_iso(NOW + 20 * 3600)), "user")
    await eng.arm(rule.id)
    await eng.run_once()
    reg_end = next(s.end for s in svc.sessions() if s.name == "regular" and s.start <= t[0] < s.end)
    t[0] = reg_end + 60                                          # after-hours now
    svc.account.state.updated = t[0]
    await eng.run_once()                                         # the jump in time counts as a gap
    await play(svc, eng, t, ["4.31", "4.26"])
    assert rule.status == "blocked" and rule.result["issues"][0]["code"] == "rule_session"
    assert svc.sender.simulated == []


@pytest.mark.anyio
async def test_stale_feed_pauses_and_forgets_the_touch(tmp_path: Path) -> None:
    svc, eng, t = await make(tmp_path)
    rule, _, _ = await eng.create(sell_rule(), "user")
    await eng.arm(rule.id)
    await eng.run_once()
    await play(svc, eng, t, ["4.31"])
    assert rule.status == "touched"
    svc.market.connected = False                                 # stream down and no fresh REST read
    await play(svc, eng, t, ["4.26"])
    assert rule.status == "armed" and rule.paused == "stale_data" and svc.sender.simulated == []
    svc.market.connected = True
    await play(svc, eng, t, ["4.25"])                            # feed back: the old touch is gone
    assert rule.status == "armed" and rule.paused is None and svc.sender.simulated == []
    await play(svc, eng, t, ["4.31", "4.26"])
    assert rule.status == "sent"


@pytest.mark.anyio
async def test_old_trades_and_a_sleeping_computer_never_fire(tmp_path: Path) -> None:
    svc, eng, t = await make(tmp_path)
    rule, _, _ = await eng.create(sell_rule(), "user")
    await eng.arm(rule.id)
    await eng.run_once()
    t[0] += 1
    svc.market.trade("SOXS", "4.31", at=t[0] - 60)               # a trade from a minute ago, delivered late
    await eng.run_once()
    assert rule.status == "armed"
    await play(svc, eng, t, ["4.31"])
    assert rule.status == "touched"
    t[0] += 600                                                  # the laptop slept for 10 minutes
    svc.market.trade("SOXS", "4.26")
    await eng.run_once()
    assert rule.status == "armed" and svc.sender.simulated == []  # the half-done touch was dropped


@pytest.mark.anyio
async def test_rule_expires(tmp_path: Path) -> None:
    svc, eng, t = await make(tmp_path)
    rule, _, _ = await eng.create(sell_rule(expires=kst_iso(NOW + 5)), "user")
    await eng.arm(rule.id)
    await eng.run_once()
    await play(svc, eng, t, ["4.31"], step=3)
    await play(svc, eng, t, ["4.26"], step=3)                    # 6 s after NOW: past the expiry
    assert rule.status == "expired" and svc.sender.simulated == []
    with pytest.raises(RuleError):
        await eng.arm(rule.id)


@pytest.mark.anyio
async def test_candle_confirmed_rule_through_the_engine(tmp_path: Path) -> None:
    svc, eng, t = await make(tmp_path)
    end = (int(NOW) // 300 + 1) * 300                            # the 5m candle that is forming at NOW
    view: dict[str, Any] = {"meta": {"status": "ok", "updated": kst_iso(end - 10)}, "bars": []}

    async def candles_view(symbol: str, tf: str) -> dict[str, Any]:
        assert (symbol, tf) == ("SOXS", "5m")
        return view

    svc.candles_view = candles_view
    rule, errors, _ = await eng.create(sell_rule(trigger={"dir": "down", "price": "4.26", "confirm": "close_5m"}), "user")
    assert rule is not None, errors
    await eng.arm(rule.id)
    await eng.run_once()
    await play(svc, eng, t, ["4.31", "4.20"])                    # touch, then a wick under Y: no fire
    assert rule.status == "touched" and svc.sender.simulated == []
    while t[0] < end + 6:                                        # the loop runs every few seconds meanwhile
        await play(svc, eng, t, ["4.28"], step=5)
        svc.account.state.updated = t[0]
    view["bars"] = [{"end": end, "high": D("4.31"), "low": D("4.20"), "close": D("4.24")}]
    await play(svc, eng, t, ["4.24"])                            # candle data is from BEFORE the candle ended
    assert rule.status == "touched"
    view["meta"]["updated"] = kst_iso(end + 4)
    await play(svc, eng, t, ["4.24"], step=6)
    assert rule.status == "sent" and rule.fire_price == D("4.24") and len(svc.sender.simulated) == 1


@pytest.mark.anyio
async def test_restart_keeps_armed_rules_and_never_resends(tmp_path: Path) -> None:
    svc, eng, t = await make(tmp_path)
    a, _, _ = await eng.create(sell_rule(), "user")
    b, _, _ = await eng.create(sell_rule(), "user")
    c, _, _ = await eng.create(sell_rule(), "ai")
    await eng.arm(a.id)
    await eng.arm(b.id)
    await eng.run_once()
    await play(svc, eng, t, ["4.31"])
    assert a.status == b.status == "touched"
    b.status = "firing"                                          # as if the program died in the middle of a send
    eng.save()
    eng2 = RuleEngine(svc, tmp_path / "rules.json")
    assert eng2.rules[a.id].status == "armed" and eng2.rules[a.id].touched_at is None   # armed, touch forgotten
    assert eng2.rules[b.id].status == "unknown" and eng2.rules[b.id].result["error"] == "stopped_while_sending"
    assert eng2.rules[c.id].status == "proposed" and eng2.rules[c.id].source == "ai"
    await eng2.run_once()
    await play(svc, eng2, t, ["4.26"])                           # b is never sent again; a needs a new touch
    assert svc.sender.simulated == [] and ORDER_WRITES == []


@pytest.mark.anyio
async def test_broken_rules_file_arms_nothing_and_is_not_overwritten(tmp_path: Path) -> None:
    svc, _eng, _t = await make(tmp_path)
    path = tmp_path / "rules.json"
    path.write_text("{ not json", encoding="utf-8")
    eng = RuleEngine(svc, path)
    assert eng.rules == {} and eng.load_error is not None
    rule, errors, _ = await eng.create(sell_rule(), "user")
    assert rule is not None
    with pytest.raises(RuleError) as e:
        await eng.arm(rule.id)
    assert "rules_file_broken" in [i["code"] for i in e.value.issues]
    assert path.read_text(encoding="utf-8") == "{ not json"


@pytest.mark.anyio
async def test_arm_limits_kill_switch_disarm_and_delete(tmp_path: Path) -> None:
    svc, eng, t = await make(tmp_path)
    svc.config.rules.max_armed = 2
    rules = [(await eng.create(sell_rule(), "user"))[0] for _ in range(3)]
    await eng.arm(rules[0].id)
    await eng.arm(rules[1].id)
    with pytest.raises(RuleError) as e:
        await eng.arm(rules[2].id)
    assert [i["code"] for i in e.value.issues] == ["rule_too_many_armed"]
    with pytest.raises(RuleError) as e2:
        await eng.arm(rules[0].id)                               # already armed
    assert e2.value.code == "rule_not_proposed"
    assert eng.disarm_all() == 2 and all(r.status == "proposed" for r in rules)
    await eng.run_once()
    await play(svc, eng, t, ["4.31", "4.26"])
    assert svc.sender.simulated == []                            # disarmed rules do nothing
    await eng.arm(rules[0].id)
    eng.delete(rules[0].id)
    assert rules[0].id not in eng.rules
    svc.config.rules.enabled = False
    with pytest.raises(RuleError) as e3:
        await eng.arm(rules[1].id)
    assert "rules_off" in [i["code"] for i in e3.value.issues]
    assert "rule_disarmed" in events(tmp_path) and "rule_deleted" in events(tmp_path)


@pytest.mark.anyio
async def test_rules_routes(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_api import LOCAL

    svc, eng, _t = await make(tmp_path)
    svc.rules = eng
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        bad = c.post("/api/rules", json=sell_rule(touch=None))
        assert bad.status_code == 422 and bad.json()["errors"][0]["code"] == "rule_use_reserved_order"
        r = c.post("/api/rules", json={**sell_rule(), "source": "ai"})      # the screen cannot claim "ai"
        assert r.status_code == 201 and r.json()["rule"]["status"] == "proposed"
        rule = r.json()["rule"]
        assert rule["source"] == "user" and rule["trigger"]["price"] == "4.26" and rule["order"]["quantity"] == "14"
        rid = rule["id"]
        chk = c.get(f"/api/rules/{rid}/check").json()
        assert chk["errors"] == [] and chk["dry_run"] is True
        assert c.post(f"/api/rules/{rid}/arm", json={}).status_code == 400            # needs approved: true
        assert c.post(f"/api/rules/{rid}/arm", json={"approved": True},
                      headers={"Origin": "https://evil.example"}).status_code == 403  # other websites refused
        a = c.post(f"/api/rules/{rid}/arm", json={"approved": True})
        assert a.status_code == 200 and a.json()["rule"]["status"] == "armed"
        again = c.post(f"/api/rules/{rid}/arm", json={"approved": True})
        assert again.status_code == 409 and again.json()["error"] == "rule_not_proposed"
        listed = c.get("/api/rules").json()
        assert listed["armed"] == 1 and listed["rules"][0]["id"] == rid and listed["dry_run"] is True
        assert c.post("/api/rules/disarm-all", json={}).json()["disarmed"] == 1
        assert c.get("/api/rules").json()["armed"] == 0
        assert c.delete(f"/api/rules/{rid}").status_code == 200
        assert c.delete(f"/api/rules/{rid}").status_code == 404
        assert c.get("/api/rules/nope/check").status_code == 404


@pytest.mark.anyio
async def test_buy_cap_blocks_arming_and_firing(tmp_path: Path) -> None:
    svc, eng, t = await make(tmp_path)
    rule, _, warnings = await eng.create(buy_rule(), "user")      # buy 10 x 4.25 = 42.50 USD of 1000 USD
    assert rule is not None and warnings == []
    svc.config.orders.rule_max_buy_pct = D("4")                  # cap 40 USD
    with pytest.raises(RuleError) as e:
        await eng.arm(rule.id)
    assert [i["code"] for i in e.value.issues] == ["rule_over_buy_cap"]
    svc.config.orders.rule_max_buy_pct = None
    await eng.arm(rule.id)
    await eng.run_once()
    svc.config.orders.rule_max_buy_pct = D("4")                  # cap set after arming: checked again at fire time
    await play(svc, eng, t, ["4.19", "4.25"])
    assert rule.status == "blocked" and rule.result["issues"][0]["code"] == "rule_over_buy_cap"
    assert svc.sender.simulated == []


@pytest.mark.anyio
async def test_workshop_copy_cannot_arm_and_never_fires(tmp_path: Path) -> None:
    """Cloud plan: deploy.role workshop forces DRY_RUN and switches rules off (config check)."""
    from app.settings import AppConfig

    ws = AppConfig.model_validate({"deploy": {"role": "workshop"}, "orders": {"dry_run": False},
                                   "rules": {"enabled": True}})
    assert ws.orders.dry_run is True and ws.rules.enabled is False
    svc, eng, t = await make(tmp_path)
    old, _, _ = await eng.create(sell_rule(), "user")
    await eng.arm(old.id)                                       # armed while the copy was still "live"
    await eng.run_once()
    svc.config.rules = ws.rules                                 # now the copy is a workshop
    new, _, _ = await eng.create(sell_rule(), "user")
    with pytest.raises(RuleError):
        await eng.arm(new.id)
    await play(svc, eng, t, ["4.26", "4.31", "4.27", "4.26"])   # would have fired when live
    assert old.status == "armed" and svc.sender.simulated == [] and ORDER_WRITES == []
