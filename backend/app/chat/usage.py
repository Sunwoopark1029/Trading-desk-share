"""logs/usage.jsonl: one line per Claude API call, totals by period/model, budget check.

Periods are Korea time (KST): "today" = KST date, "month" = KST calendar month.
No keys or message text are ever written here, only token counts and costs.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from app.chat.costs import CallCost, cache_hit_rate, TokenUsage, to_krw
from app.settings import REPO_ROOT, BudgetConfig
from app.timeutil import KST

USAGE_PATH = REPO_ROOT / "logs" / "usage.jsonl"
ZERO = Decimal(0)

BudgetState = Literal["ok", "warn", "over"]
Decision = Literal["allow", "confirm", "block"]


def _s(d: Decimal | None) -> str | None:
    return None if d is None else format(d, "f")


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v)) if v is not None else ZERO
    except (InvalidOperation, ValueError):
        return ZERO


def build_record(
    *,
    now: datetime,
    purpose: str,
    tier: str,
    response_model: str,
    costs: list[CallCost],
    web_search_requests: int = 0,
    web_search_usd: Decimal = ZERO,
    fx_rate: Decimal | None = None,
    fx_time: datetime | None = None,
    conversation_id: str | None = None,
    turn_id: str | None = None,
    stop_reason: str | None = None,
    symbol: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """One usage line. Totals sum every attempt (a refusal fallback can have several)."""
    tot = TokenUsage(
        model=response_model,
        input_tokens=sum(c.usage.input_tokens for c in costs),
        output_tokens=sum(c.usage.output_tokens for c in costs),
        cache_write_5m=sum(c.usage.cache_write_5m for c in costs),
        cache_write_1h=sum(c.usage.cache_write_1h for c in costs),
        cache_read=sum(c.usage.cache_read for c in costs),
    )
    usd = sum((c.total_usd for c in costs), ZERO) + web_search_usd
    hit = cache_hit_rate(tot)
    return {
        "ts": now.astimezone(KST).isoformat(),
        "purpose": purpose,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "symbol": symbol,             # ticker of the chat (phase 6 part C); None for background work
        "kind": kind,                 # desk | research; None for background work
        "tier": tier,
        "model": response_model,
        "input_tokens": tot.input_tokens,
        "output_tokens": tot.output_tokens,
        "cache_write_5m_tokens": tot.cache_write_5m,
        "cache_write_1h_tokens": tot.cache_write_1h,
        "cache_read_tokens": tot.cache_read,
        "cache_hit_rate": None if hit is None else round(float(hit), 4),
        "web_search_requests": web_search_requests,
        "web_search_usd": _s(web_search_usd),
        "cost_usd": _s(usd),
        "fx_rate": _s(fx_rate),
        "fx_time": fx_time.astimezone(KST).isoformat() if fx_time else None,
        "cost_krw": _s(to_krw(usd, fx_rate)),
        "price_known": all(c.price_known for c in costs),
        "stop_reason": stop_reason,
        "attempts": [
            {"model": c.usage.model, "input_tokens": c.usage.input_tokens,
             "output_tokens": c.usage.output_tokens, "cache_write_5m_tokens": c.usage.cache_write_5m,
             "cache_write_1h_tokens": c.usage.cache_write_1h, "cache_read_tokens": c.usage.cache_read,
             "cost_usd": _s(c.total_usd), "price_known": c.price_known, "long_prompt": c.long_prompt}
            for c in costs
        ],
    }


@dataclass
class Totals:
    usd: Decimal = ZERO
    krw: Decimal = ZERO
    krw_missing_usd: Decimal = ZERO  # USD of lines logged without an FX rate
    calls: int = 0
    by_model: dict[str, Decimal] = field(default_factory=dict)

    def add(self, rec: dict[str, Any]) -> None:
        usd = _dec(rec.get("cost_usd"))
        self.usd += usd
        if rec.get("cost_krw") is not None:
            self.krw += _dec(rec.get("cost_krw"))
        else:
            self.krw_missing_usd += usd
        self.calls += 1
        m = str(rec.get("model") or "?")
        self.by_model[m] = self.by_model.get(m, ZERO) + usd

    def krw_total(self, current_rate: Decimal | None) -> tuple[Decimal | None, bool]:
        """(KRW, estimated?) Lines without a logged rate use the current rate (estimate)."""
        if self.krw_missing_usd == 0:
            return self.krw, False
        if current_rate is None:
            return None, True
        return self.krw + self.krw_missing_usd * current_rate, True


class UsageLog:
    def __init__(self, path: Path = USAGE_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def records(self) -> list[dict[str, Any]]:
        """All valid lines (broken lines are skipped, never fatal)."""
        if not self.path.exists():
            return []
        out = []
        with self._lock, self.path.open("r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("ts"), str):
                    out.append(rec)
        return out

    def totals(self, now: datetime) -> tuple[Totals, Totals, Totals]:
        """(today, this month, all time) in KST."""
        today, month, total = Totals(), Totals(), Totals()
        k = now.astimezone(KST)
        for rec in self.records():
            try:
                ts = datetime.fromisoformat(rec["ts"]).astimezone(KST)
            except ValueError:
                continue
            total.add(rec)
            if (ts.year, ts.month) == (k.year, k.month):
                month.add(rec)
                if ts.date() == k.date():
                    today.add(rec)
        return today, month, total

    def since(self, start: datetime) -> Totals:
        """AI spend logged at or after ``start``."""
        out = Totals()
        for rec in self.records():
            try:
                ts = datetime.fromisoformat(rec["ts"])
            except (KeyError, ValueError):
                continue
            if ts.tzinfo is not None and ts >= start:
                out.add(rec)
        return out


@dataclass(frozen=True)
class BudgetStatus:
    state: BudgetState
    day_usd: Decimal
    month_usd: Decimal
    daily_limit: Decimal
    monthly_limit: Decimal
    day_ratio: Decimal
    month_ratio: Decimal

    @property
    def ratio(self) -> Decimal:
        return max(self.day_ratio, self.month_ratio)


def budget_status(day_usd: Decimal, month_usd: Decimal, cfg: BudgetConfig) -> BudgetStatus:
    dr, mr = day_usd / cfg.daily_usd, month_usd / cfg.monthly_usd
    worst = max(dr, mr)
    state: BudgetState = "over" if worst >= 1 else "warn" if worst >= cfg.warn_at else "ok"
    return BudgetStatus(state, day_usd, month_usd, cfg.daily_usd, cfg.monthly_usd, dr, mr)


def budget_decision(status: BudgetStatus, tier: str) -> Decision:
    """At 100%: Deep / Very deep blocked, Normal asks first, background (summaries) allowed."""
    if status.state != "over":
        return "allow"
    if tier == "normal":
        return "confirm"
    if tier == "background":
        return "allow"
    return "block"
