"""AI news tagging in the background (Claude Haiku, structured JSON output).

For each new item: priority (high/medium/low), direction for the user's holdings
(helps/hurts/unclear/no_position), and a one-line reason in English and Korean.
Batches keep cost low; usage goes to logs/usage.jsonl (purpose "news_tag"). Paused when the
budget is used up. The model never changes the news itself; it only labels it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.chat.usage import budget_status
from app.news.feed import NewsItem

log = logging.getLogger("desk.news")

PRIORITIES = ("high", "medium", "low")
DIRECTIONS = ("helps", "hurts", "unclear", "no_position")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "priority": {"type": "string", "enum": list(PRIORITIES)},
                    "direction": {"type": "string", "enum": list(DIRECTIONS)},
                    "reason_en": {"type": "string"},
                    "reason_ko": {"type": "string"},
                },
                "required": ["id", "priority", "direction", "reason_en", "reason_ko"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM = (
    "You label market news for one retail trader. For each item give: priority (high = likely to move the "
    "tickers it names today or reflects a major event: earnings, guidance, export rules, M&A, big analyst moves; "
    "medium = relevant context; low = generic or promotional), direction for the trader's holdings (helps / hurts / "
    "unclear; no_position if the trader holds nothing the item affects), and a one-line reason (max 20 words) in "
    "English and in Korean. Inverse or bear ETFs (e.g. names with 'Bear', 'Inverse', '-3X') move OPPOSITE to their "
    "underlying sector: good semiconductor news HURTS a semiconductor bear ETF. Use only the given headline and "
    "summary; do not invent facts or numbers. If the date is marked unverified, say nothing about timing."
)


def validate_tags(data: Any, batch_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Keep only well-formed tags for ids in this batch."""
    out: dict[str, dict[str, Any]] = {}
    items = data.get("items") if isinstance(data, dict) else None
    for t in items if isinstance(items, list) else []:
        if not isinstance(t, dict) or t.get("id") not in batch_ids:
            continue
        if t.get("priority") not in PRIORITIES or t.get("direction") not in DIRECTIONS:
            continue
        out[t["id"]] = {"priority": t["priority"], "direction": t["direction"],
                        "reason_en": str(t.get("reason_en") or "")[:300],
                        "reason_ko": str(t.get("reason_ko") or "")[:300]}
    return out


def batch_prompt(items: list[NewsItem], holdings: list[dict[str, str]]) -> str:
    rows = [{"id": i.id, "tickers": i.tickers, "headline": i.headline, "summary": i.summary[:400],
             "source": i.source,
             "published_utc": (datetime.fromtimestamp(i.published, UTC).isoformat() if i.published else "unverified")}
            for i in items]
    return json.dumps({"holdings": holdings, "news": rows}, ensure_ascii=False)


class NewsTagger:
    def __init__(self, engine: Any, store: Any, batch_size: int, max_age_hours: float,
                 holdings: Callable[[], list[dict[str, str]]], clock: Callable[[], float],
                 enabled: Callable[[], bool] = lambda: True) -> None:
        self.enabled = enabled          # Settings page switch (read on every round)
        self.engine = engine            # ChatEngine (client, usage log, budget, prices)
        self.store = store
        self.batch_size = batch_size
        self.max_age_hours = max_age_hours
        self.holdings = holdings
        self.clock = clock
        self.status = "idle"
        self.error: str | None = None
        self.tagged = 0

    def budget_over(self) -> bool:
        today, month, _ = self.engine.usage.totals(datetime.now(UTC))
        return budget_status(today.usd, month.usd, self.engine.svc.config.budget).state == "over"

    async def tag_once(self) -> int:
        if not self.enabled():
            self.status = "off"
            return 0
        if self.engine is None or not self.engine.key_set:
            self.status = "off: key not set"
            return 0
        if self.budget_over():
            self.status = "paused: budget"
            return 0
        batch = self.store.untagged(self.clock() - self.max_age_hours * 3600, self.batch_size)
        if not batch:
            self.status = "idle"
            return 0
        self.status = "tagging"
        tier = self.engine.tier("background")
        try:
            msg = await self.engine.client().messages.create(
                model=tier.model, max_tokens=tier.max_tokens, system=SYSTEM,
                messages=[{"role": "user", "content": batch_prompt(batch, self.holdings())}],
                output_config={"effort": tier.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            )
        except Exception as exc:  # noqa: BLE001
            self.status, self.error = "error", type(exc).__name__
            log.warning("news tagging failed: %s", type(exc).__name__)
            return 0
        self.engine.record_usage(msg, tier="background", purpose="news_tag", cid=None, turn_id=None)
        if msg.stop_reason == "refusal":
            # mark as unclear so the same items aren't retried forever
            tags = {i.id: {"priority": "low", "direction": "unclear", "reason_en": "not labelled (model declined)",
                           "reason_ko": "분류되지 않음 (모델이 거절함)"} for i in batch}
        else:
            text = next((getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text"), "")
            try:
                tags = validate_tags(json.loads(text), {i.id for i in batch})
            except ValueError:
                tags = {}
            for i in batch:  # skipped/garbled items are not retried (each retry costs money)
                tags.setdefault(i.id, {"priority": "low", "direction": "unclear",
                                       "reason_en": "not labelled", "reason_ko": "분류되지 않음"})
        self.store.set_tags(tags)
        self.tagged += len(tags)
        self.status, self.error = "idle", None
        return len(tags)
