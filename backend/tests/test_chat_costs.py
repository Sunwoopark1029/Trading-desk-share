"""Cost calculation (incl. cache tokens, Haiku long-prompt tier, fallback attempts) and budget rules."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.chat import costs
from app.chat.usage import UsageLog, budget_decision, budget_status, build_record
from app.settings import BudgetConfig, ModelPrice, load_config

D = Decimal
SONNET = ModelPrice(input=D("2"), output=D("10"), cache_write_5m=D("2.5"), cache_write_1h=D("4"), cache_read=D("0.1"))
HAIKU = ModelPrice(input=D("0.10"), output=D("0.50"), cache_write_5m=D("0.125"), cache_write_1h=D("0.20"),
                   cache_read=D("0.01"), long_prompt_over_tokens=100_000,
                   long_prompt=ModelPrice(input=D("0.50"), output=D("2.50"), cache_write_5m=D("0.625"),
                                          cache_write_1h=D("1"), cache_read=D("0.05")))
PRICES = {"claude-sonnet-5-5": SONNET, "claude-haiku-5-5": HAIKU}
KST = datetime.fromisoformat("2026-11-16T10:00:00+09:00").tzinfo


def test_cost_all_token_kinds_hand_calculated() -> None:
    usage = {"input_tokens": 1000, "output_tokens": 500, "cache_creation_input_tokens": 3000,
             "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 2000},
             "cache_read_input_tokens": 20000}
    u = costs.token_usage(usage, "claude-sonnet-5-5")
    c = costs.call_cost(u, PRICES, "claude-sonnet-5-5")
    # 1000*2 + 500*10 + 1000*2.5 + 2000*4 + 20000*0.1 = 2000+5000+2500+8000+2000 = 19500 / 1e6
    assert c.total_usd == D("0.0195")
    assert c.input_usd == D("0.002") and c.output_usd == D("0.005")
    assert c.cache_write_usd == D("0.0105") and c.cache_read_usd == D("0.002")
    assert c.price_known and not c.long_prompt
    assert costs.cache_hit_rate(u) == D(20000) / D(24000)


def test_cache_write_without_breakdown_counts_as_5m() -> None:
    u = costs.token_usage({"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1000},
                          "claude-sonnet-5-5")
    assert (u.cache_write_5m, u.cache_write_1h) == (1000, 0)
    assert costs.call_cost(u, PRICES, "claude-sonnet-5-5").total_usd == D("0.0025")


def test_haiku_long_prompt_tier_counts_cache_tokens() -> None:
    short = costs.token_usage({"input_tokens": 50_000, "output_tokens": 1000,
                               "cache_read_input_tokens": 50_000}, "claude-haiku-5-5")
    assert short.prompt_tokens == 100_000  # not over the threshold
    c1 = costs.call_cost(short, PRICES, "claude-haiku-5-5")
    assert not c1.long_prompt and c1.total_usd == (D(50_000) * D("0.10") + D(1000) * D("0.5") + D(50_000) * D("0.01")) / 1_000_000
    long = costs.token_usage({"input_tokens": 50_001, "output_tokens": 1000,
                              "cache_read_input_tokens": 50_000}, "claude-haiku-5-5")
    c2 = costs.call_cost(long, PRICES, "claude-haiku-5-5")
    assert c2.long_prompt
    assert c2.total_usd == (D(50_001) * D("0.5") + D(1000) * D("2.5") + D(50_000) * D("0.05")) / 1_000_000


def test_fallback_attempts_priced_per_model_unknown_flagged() -> None:
    usage = {"input_tokens": 10, "output_tokens": 10, "iterations": [
        {"type": "message", "model": "claude-sonnet-5-5", "input_tokens": 100, "output_tokens": 0},
        {"type": "fallback_message", "model": "claude-other-9", "input_tokens": 100, "output_tokens": 50},
        {"type": "compaction", "input_tokens": 999}]}
    att = costs.attempts(usage, "claude-other-9")
    assert [a.model for a in att] == ["claude-sonnet-5-5", "claude-other-9"]
    cc = [costs.call_cost(a, PRICES, "claude-sonnet-5-5") for a in att]
    assert cc[0].price_known and not cc[1].price_known
    assert cc[1].total_usd == (D(100) * 2 + D(50) * 10) / 1_000_000  # priced as the tier model (est.)
    assert costs.attempts({"input_tokens": 5}, "m")[0].input_tokens == 5  # no iterations: top level


def test_web_search_cost() -> None:
    assert costs.web_search_count({"server_tool_use": {"web_search_requests": 3}}) == 3
    assert costs.web_search_cost(3, D("10")) == D("0.03")


def test_config_prices_match_verified_table() -> None:
    cfg = load_config()
    assert cfg.llm is not None
    p = cfg.llm.prices_usd_per_mtok
    assert p["claude-sonnet-5-5"].cache_read == D("0.10") and p["claude-opus-5-5"].output == D("20.00")
    assert p["claude-fable-5-2"].cache_write_1h == D("16.00")
    assert p["claude-haiku-5-5"].long_prompt.input == D("0.50")
    assert cfg.llm.tiers["normal"].model == "claude-sonnet-5-5"
    assert cfg.budget.monthly_usd == D("100")


# ── usage log + budget ───────────────────────────────────────────────────

def _rec(ts: str, usd: str, krw: str | None, model: str = "claude-sonnet-5-5") -> dict:
    return {"ts": ts, "cost_usd": usd, "cost_krw": krw, "model": model}


def test_usage_record_and_totals_by_kst_period(tmp_path: Path) -> None:
    log = UsageLog(tmp_path / "usage.jsonl")
    u = costs.token_usage({"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 3000},
                          "claude-sonnet-5-5")
    rec = build_record(now=datetime(2026, 11, 16, 1, 0, tzinfo=KST), purpose="chat", tier="normal",
                       response_model="claude-sonnet-5-5",
                       costs=[costs.call_cost(u, PRICES, "claude-sonnet-5-5")],
                       fx_rate=D("1400"), fx_time=datetime(2026, 11, 16, 0, 59, tzinfo=KST))
    assert rec["cost_usd"] == "0.0033" and rec["cost_krw"] == "4.6200"
    assert rec["cache_hit_rate"] == 0.75
    assert "key" not in str(rec).lower()
    log.append(rec)
    log.append(_rec("2026-11-15T23:59:00+09:00", "1", None))          # yesterday KST
    log.append(_rec("2026-10-31T12:00:00+09:00", "5", "7000", "claude-opus-5-5"))  # last month
    (tmp_path / "usage.jsonl").open("a", encoding="utf-8").write("not json\n")
    today, month, total = log.totals(datetime(2026, 11, 16, 9, 0, tzinfo=KST))
    assert today.usd == D("0.0033") and today.calls == 1
    assert month.usd == D("1.0033") and month.calls == 2
    assert total.usd == D("6.0033")
    krw, est = month.krw_total(D("1500"))
    assert est and krw == D("4.62") + D("1500")  # line without a rate uses current (est.)
    assert total.by_model["claude-opus-5-5"] == D("5")


def test_budget_warn_and_blocking_rules() -> None:
    cfg = BudgetConfig(daily_usd=D("5"), monthly_usd=D("100"), warn_at=D("0.8"))
    ok = budget_status(D("1"), D("10"), cfg)
    assert ok.state == "ok" and budget_decision(ok, "very_deep") == "allow"
    warn = budget_status(D("4"), D("10"), cfg)          # 80% of daily
    assert warn.state == "warn" and budget_decision(warn, "deep") == "allow"
    over_day = budget_status(D("5"), D("10"), cfg)      # 100% daily
    assert over_day.state == "over"
    assert budget_decision(over_day, "deep") == "block"
    assert budget_decision(over_day, "very_deep") == "block"
    assert budget_decision(over_day, "normal") == "confirm"
    assert budget_decision(over_day, "background") == "allow"
    over_month = budget_status(D("0"), D("100"), cfg)   # monthly cap alone also blocks
    assert over_month.state == "over" and budget_decision(over_month, "deep") == "block"
