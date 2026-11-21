"""Candle input A/B test (phase 2).

A = code-calculated level summary only.  B = the same + raw candles (5m bars since the session
open, last N 1m bars). Same questions, Normal tier, no tools. For every answer, code checks each
price-like number against the values the model was given, and level ids against the real ids.

Run with ab_test.cmd (close the dashboard first: this opens its own Toss connection, and a new
Toss token cancels the dashboard's). It prints a FREE estimate (token counting) and asks before
spending. Every call is logged to logs/usage.jsonl (purpose "ab_test") and counts toward the budget.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from app.chat.engine import PRICE_LIKE, ChatEngine, dump_block
from app.chat.levels import compute_levels, price_matches
from app.chat.prompt import candles_block, context_block, live_block
from app.chat.store import ChatStore
from app.chat.tools import NUM_RE
from app.chat.usage import UsageLog, budget_status
from app.services import Services
from app.settings import REPO_ROOT, get_config, get_secrets
from app.timeutil import KST

def say(msg: str) -> None:
    print(msg, flush=True)


QUESTIONS = Path(__file__).resolve().parents[2] / "ab" / "questions.yaml"
OUT_DIR = REPO_ROOT / "logs" / "ab"
EST_OUTPUT_TOKENS = 1500  # per answer incl. thinking (conservative)
LEVEL_ID_RE = re.compile(r"\b(?:ema\d+_(?:1m|5m|1d)|vwap_(?:1m|5m)|fib_[0-9.]+|swing_(?:high|low)_\d+|"
                         r"session_(?:high|low)|prev_(?:close|high|low)|avg_price)\b")


def nums(text: str) -> list[Decimal]:
    out = []
    for m in NUM_RE.findall(text or ""):
        try:
            out.append(Decimal(m.replace(",", "")))
        except Exception:  # noqa: BLE001
            pass
    return out


async def build_cases(svc: Services, questions: list[dict[str, Any]], last_1m: int) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    cases = []
    for i, q in enumerate(questions, start=1):
        sym, tf = q["symbol"].upper(), q.get("timeframe", "5m")
        say(f"  preparing question {i}/{len(questions)} ({sym} {tf})")
        levels = await compute_levels(svc, sym, tf)
        ctx = context_block(svc, levels)
        live = live_block(levels, now, {"symbol": sym, "timeframe": tf})
        v5 = await svc.candles_view(sym, "5m")
        v1 = await svc.candles_view(sym, "1m")
        candles, _ = candles_block(v5, v1, {}, last_1m)
        ids = {lv.id for lv in levels.all}
        allowed_a = levels.prices() + nums(ctx) + nums(live) + nums(q["text"])
        allowed_b = allowed_a + nums(candles or "")
        for variant in ("A", "B"):
            static = [{"type": "text", "text": ctx}]
            if variant == "B" and candles:
                static.append({"type": "text", "text": candles})
            static[-1] = {**static[-1], "cache_control": {"type": "ephemeral"}}  # shared across questions
            content = [*static, {"type": "text", "text": q["text"]}, {"type": "text", "text": live}]
            cases.append({"question": q["text"], "symbol": sym, "variant": variant, "ids": ids,
                          "allowed": allowed_b if variant == "B" else allowed_a,
                          "messages": [{"role": "user", "content": content}]})
    return cases


def check_answer(text: str, allowed: list[Decimal], ids: set[str]) -> dict[str, Any]:
    cited = [m for m in PRICE_LIKE.findall(text or "")]
    wrong = [m for m in cited if not price_matches(Decimal(m.replace(",", "")), allowed, Decimal("0.001"))]
    cited_ids = sorted(set(LEVEL_ID_RE.findall(text or "")))
    bad_ids = [i for i in cited_ids if i not in ids]
    return {"prices_cited": len(cited), "prices_wrong": wrong, "ids_cited": len(cited_ids), "ids_unknown": bad_ids}


async def main() -> int:
    say("Candle A/B test. Nothing is spent until you type YES.")
    cfg, secrets = get_config(), get_secrets()
    if cfg.llm is None:
        print("config.yaml has no llm section")
        return 1
    key = secrets.ANTHROPIC_API_KEY.get_secret_value().strip()
    if not key:
        print("ANTHROPIC_API_KEY: key not set")
        return 1
    usage = UsageLog()
    today, month, _ = usage.totals(datetime.now(UTC))
    if budget_status(today.usd, month.usd, cfg.budget).state == "over":
        print("Budget limit reached; the A/B test is not run.")
        return 1
    questions = (YAML(typ="safe").load(QUESTIONS.read_text(encoding="utf-8")) or {}).get("questions", [])
    svc = Services(cfg, secrets)
    try:
        say("1/3 Connecting to Toss: market calendar, stock list, FX, quotes (about 10-20 s)...")
        await svc.refresh_calendar()
        await svc.refresh_stocks()
        await svc.refresh_fx()
        await svc.refresh_quotes()
        problems = [f"{k}: {e.error_message}" for k in ("toss.calendar", "toss.stocks", "toss.quotes")
                    if (e := svc.cache.get(k)) is not None and e.status == "error"]
        if problems:
            say("Toss problem: " + "; ".join(problems))
            say("Fix this first (is the dashboard really closed? is the IP registered?). Nothing was spent.")
            return 1
        say("2/3 Loading candles and levels for each question...")
        cases = await build_cases(svc, questions, cfg.llm.candles.last_1m_bars)
        empty = sorted({c["symbol"] for c in cases if "<candles" not in json.dumps(c["messages"])})
        if empty:
            say(f"Note: no candles since today's session open for {', '.join(empty)}; "
                "B has little extra data. Best run during or after a US session.")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        eng = ChatEngine(svc, cfg.llm, api_key=key, store=ChatStore(OUT_DIR / "ab.sqlite"), usage=usage)
        client = eng.client()
        tier = cfg.llm.tiers["normal"]
        price = cfg.llm.prices_usd_per_mtok[tier.model]

        say("3/3 Counting tokens for the estimate (free)...")
        # free estimate: count tokens of the first case of each variant
        est = Decimal(0)
        per_variant = {}
        for v in ("A", "B"):
            c = next(c for c in cases if c["variant"] == v)
            p = eng.params("normal", c["messages"], tools=False)
            n = await client.messages.count_tokens(model=p["model"], system=p["system"], messages=p["messages"])
            per_variant[v] = n.input_tokens
            k = sum(1 for x in cases if x["variant"] == v)
            est += (Decimal(n.input_tokens) * price.input + EST_OUTPUT_TOKENS * price.output) * k / 1_000_000
        mid, _, _ = svc.fx_rate()
        krw = f" (about KRW {est * mid:,.0f})" if mid else ""
        print(f"{len(questions)} questions x 2 variants on {tier.model}.")
        print(f"Input tokens per question: A {per_variant['A']:,} / B {per_variant['B']:,}.")
        print(f"Estimated cost without cache savings: USD {est:.2f}{krw}. Budget left this month: "
              f"USD {cfg.budget.monthly_usd - month.usd:.2f}.")
        if input("Type YES to run it: ").strip() != "YES":
            print("Not run. Nothing was spent (token counting is free).")
            return 0

        say("Running... (one line per answer)")
        rows = []
        for i, c in enumerate(cases, start=1):
            p = eng.params("normal", c["messages"], tools=False)
            async with client.beta.messages.stream(**p) as stream:
                msg = await stream.get_final_message()
            rec = eng.record_usage(msg, tier="normal", purpose="ab_test", cid=None, turn_id=f"ab{i}")
            text = "".join(dump_block(b).get("text", "") for b in msg.content if dump_block(b).get("type") == "text")
            chk = check_answer(text, c["allowed"], c["ids"])
            rows.append({"n": (i + 1) // 2, "variant": c["variant"], "symbol": c["symbol"], "question": c["question"],
                         "answer": text, **chk, "input_tokens": rec["input_tokens"] + rec["cache_read_tokens"]
                         + rec["cache_write_5m_tokens"] + rec["cache_write_1h_tokens"],
                         "output_tokens": rec["output_tokens"], "cache_hit_rate": rec["cache_hit_rate"],
                         "cost_usd": rec["cost_usd"]})
            print(f"{i}/{len(cases)} {c['variant']} cost USD {rec['cost_usd']} wrong prices: {chk['prices_wrong'] or 'none'}")
    finally:
        await svc.stop()

    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M")
    (OUT_DIR / f"ab_{stamp}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["# Candle A/B test " + stamp + " KST", "",
             "| Variant | Prices cited | Wrong prices | Unknown level ids | Avg input tokens | Avg output tokens | Avg cache hit | Total USD |",
             "|---|---|---|---|---|---|---|---|"]
    for v in ("A", "B"):
        rs = [r for r in rows if r["variant"] == v]
        n = len(rs) or 1
        hits = [r["cache_hit_rate"] for r in rs if r["cache_hit_rate"] is not None]
        lines.append(f"| {v} | {sum(r['prices_cited'] for r in rs)} | {sum(len(r['prices_wrong']) for r in rs)} | "
                     f"{sum(len(r['ids_unknown']) for r in rs)} | {sum(r['input_tokens'] for r in rs) // n:,} | "
                     f"{sum(r['output_tokens'] for r in rs) // n:,} | "
                     f"{(sum(hits) / len(hits) * 100 if hits else 0):.0f}% | "
                     f"{sum(Decimal(r['cost_usd']) for r in rs):.4f} |")
    lines += ["", "| # | Question | A wrong prices | B wrong prices | A USD | B USD |", "|---|---|---|---|---|---|"]
    for k in sorted({r["n"] for r in rows}):
        a = next((r for r in rows if r["n"] == k and r["variant"] == "A"), None)
        b = next((r for r in rows if r["n"] == k and r["variant"] == "B"), None)
        if a and b:
            lines.append(f"| {k} | {a['question'][:60]} | {', '.join(a['prices_wrong']) or '0'} | "
                         f"{', '.join(b['prices_wrong']) or '0'} | {a['cost_usd']} | {b['cost_usd']} |")
    report = OUT_DIR / f"ab_{stamp}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:5]))
    print(f"\nFull report: {report}  (answers in the .json next to it)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
