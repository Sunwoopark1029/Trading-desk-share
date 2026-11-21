"""One chat turn: build the prompt, stream Claude's answer, run tools, log usage.

History is append-only (each request = the previous request + new messages) until the chat is longer
than the memory setting (Settings -> AI, default 5 earlier turns; user choice 2026-11-21). From then on the
oldest turns are dropped before each message (kept on disk, no longer sent): no summary, but the history
part of the prompt is re-cached on each such message (tools + system stay cached).

The API key is passed to the SDK only; it is never logged or put in an error message.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.chat import costs as cost_mod
from app.chat.levels import compute_levels, price_matches
from app.chat.prompt import (
    candles_block, cash_snapshot, context_block, context_key, language_line, live_block, orders_snapshot, rules_snapshot,
    LANGUAGES, system_blocks, upcoming_events,
)
from app.chat.scenarios import CostModel
from app.chat.store import ChatStore
from app.chat.tools import (
    NUM_RE, TOOL_DEFS, ToolContext, ToolError, result_block, run_tool, user_numbers, web_search_tool,
)
from app.chat.usage import UsageLog, build_record
from app.chat import research as rmode
from app.options_view import compact as options_compact, options_view
from app.services import Services
from app.settings import LlmConfig, TierConfig

log = logging.getLogger("desk.chat")

MAX_CALLS_PER_TURN = 8
BETA_BINDING = "thinking-binding-controls-2026-08-01"
BETA_FALLBACK = "server-side-fallback-2026-07-01"
KEY_NOT_SET = "key not set: add ANTHROPIC_API_KEY to .env and restart"
PRICE_LIKE = re.compile(r"(?<![\w.])\$?(\d{1,6}(?:,\d{3})*\.\d{2,6})(?![\d%])(?!\s*(?:%|R\b|x\b))")
THINKING_TYPES = ("thinking", "redacted_thinking")
# blocks that describe the chart at that time; dropped from kept turns when old turns leave the memory
STALE_BLOCK_PREFIXES = ("<context>", "<candles", "<summary of earlier conversation>", "<fundamentals>")

@dataclass
class TurnRequest:
    text: str
    tier: str
    screen: dict[str, Any]
    web_search: bool = False
    lang: str = "en"   # answer language: "en" or "ko"


@dataclass
class TurnResult:
    texts: list[str] = field(default_factory=list)
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)   # usage records
    tools_used: list[str] = field(default_factory=list)
    valuations: list[dict[str, Any]] = field(default_factory=list)   # research chat cards
    candidates: list[dict[str, Any]] = field(default_factory=list)   # research chat ticker lists


def dump_block(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    return block.model_dump(mode="json", by_alias=True, exclude_none=True)


def echo_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Content as it must be sent back. After a mid-output refusal fallback, model-internal
    blocks before the last `fallback` block are omitted (the fallback block stays in place)."""
    last_fb = max((i for i, b in enumerate(content) if b.get("type") == "fallback"), default=-1)
    if last_fb < 0:
        return content
    result_ids = {b.get("tool_use_id") for b in content if str(b.get("type", "")).endswith("_tool_result")}
    out = []
    for i, b in enumerate(content):
        t = b.get("type")
        if i < last_fb and (t in (*THINKING_TYPES, "tool_use")
                            or (t == "server_tool_use" and b.get("id") not in result_ids)
                            or t not in ("text", "fallback", "server_tool_use")
                            and not str(t).endswith("_tool_result")):
            continue
        out.append(b)
    return out


def strip_thinking(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in content if b.get("type") not in THINKING_TYPES]


def unverified_numbers(text: str, sources: list[str], known: list[Decimal]) -> list[str]:
    """Price-like numbers in the answer that appear in no code data the model was given."""
    allowed = list(known)
    for s in sources:
        for m in NUM_RE.findall(s or ""):
            try:
                allowed.append(Decimal(m.replace(",", "")))
            except Exception:  # noqa: BLE001
                pass
    bad = []
    for m in PRICE_LIKE.findall(text or ""):
        d = Decimal(m.replace(",", ""))
        if d > 0 and not price_matches(d, allowed, Decimal("0.001")) and m not in bad:
            bad.append(m)
    return bad


class ChatEngine:
    def __init__(self, svc: Services, llm: LlmConfig, *, api_key: str, store: ChatStore,
                 usage: UsageLog, client_factory: Callable[[str], Any] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.svc = svc
        self.llm = llm
        self._key = api_key
        self.store = store
        self.usage = usage
        self._client_factory = client_factory
        self._client: Any = None
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def key_set(self) -> bool:
        return bool(self._key)

    def client(self) -> Any:
        if not self._key:
            raise RuntimeError(KEY_NOT_SET)
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory(self._key)
            else:
                import anthropic
                self._client = anthropic.AsyncAnthropic(api_key=self._key, max_retries=2, timeout=600.0)
        return self._client

    def tier(self, name: str) -> TierConfig:
        return self.llm.tiers[name]  # type: ignore[index]

    def cost_model(self, symbol: str | None = None) -> CostModel:
        _mid, _t, bp = self.svc.fx_rate()
        fx_bp = self.svc.config.pnl.fx_spread_bp
        if fx_bp is None and bp is not None:
            fx_bp = abs(bp)
        market = getattr(self.svc, "market", None)
        spread_pct = market.spread_pct(symbol) if market is not None and symbol else None
        return CostModel(self.svc.config.pnl.fee_rate, fx_bp, spread_pct)

    # ── request ──────────────────────────────────────────────────────────

    def params(self, tier_name: str, messages: list[dict[str, Any]], *, tools: bool = True,
               web_search: bool = False, system: Any = None, kind: str = "desk") -> dict[str, Any]:
        tier = self.tier(tier_name)
        research = kind == "research"
        if system is None:
            system = (rmode.research_system_blocks(self.llm.cache_ttl_static) if research
                      else system_blocks(self.llm.cache_ttl_static, getattr(self.llm, "risk_profile", "standard")))
        p: dict[str, Any] = {
            "model": tier.model,
            "max_tokens": tier.max_tokens,
            "system": system,
            "messages": messages,
            "cache_control": {"type": "ephemeral"},   # automatic cache point 2 (5m) at the end
            "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
            "output_config": {"effort": tier.effort},
            "betas": [BETA_BINDING],
        }
        if tools:
            t = rmode.research_tool_defs() if research else list(TOOL_DEFS)
            if web_search:
                ws = self.llm.web_search
                t.append(web_search_tool(ws.max_uses_per_message, ws.allowed_domains, ws.blocked_domains))
            p["tools"] = t
        if self.llm.refusal_fallback and tier_name != "background":
            p["betas"].append(BETA_FALLBACK)
            p["fallbacks"] = "default"
        return p

    def record_usage(self, msg: Any, *, tier: str, purpose: str, cid: str | None,
                     turn_id: str | None, symbol: str | None = None, kind: str | None = None) -> dict[str, Any]:
        tier_model = self.tier(tier).model
        prices = self.llm.prices_usd_per_mtok
        att = cost_mod.attempts(msg.usage, str(msg.model))
        call_costs = [cost_mod.call_cost(a, prices, tier_model) for a in att]
        n_ws = cost_mod.web_search_count(msg.usage)
        mid, fx_time, _ = self.svc.fx_rate()
        rec = build_record(
            now=self.clock(), purpose=purpose, tier=tier, response_model=str(msg.model), costs=call_costs,
            web_search_requests=n_ws, web_search_usd=cost_mod.web_search_cost(n_ws, self.llm.web_search.usd_per_1000),
            fx_rate=mid, fx_time=fx_time, conversation_id=cid, turn_id=turn_id,
            stop_reason=str(msg.stop_reason) if msg.stop_reason else None, symbol=symbol, kind=kind)
        self.usage.append(rec)
        dropped = [t for t in (getattr(msg, "input_transformations", None) or [])
                   if getattr(t, "type", None) == "thinking_dropped"
                   and getattr(t, "reason", None) != "model_binding_mismatch"]
        if dropped:
            log.warning("chat %s: %d earlier reasoning block(s) dropped (history changed)", cid, len(dropped))
        return rec

    # ── memory: last N turns ─────────────────────────────────────────────

    def trim_history(self, cid: str, state: dict[str, Any]) -> int:
        """Keep only the last `memory_turns` earlier turns. Older rows become inactive (still on disk and on
        screen, no longer sent). Kept turns lose their <context> / <candles> blocks (they may be partial now) and
        their reasoning blocks; the new message then re-sends context and candles in full. Returns turns dropped."""
        keep = self.llm.memory_turns
        rows = self.store.rows(cid, active_only=True)
        turns: list[list[dict[str, Any]]] = []
        for r in rows:
            if not turns or turns[-1][0]["turn_id"] != r["turn_id"]:
                turns.append([])
            turns[-1].append(r)
        if len(turns) <= keep:
            return 0
        old, kept = turns[:-keep], turns[-keep:]
        rewrites: dict[int, list[dict[str, Any]]] = {}
        for turn in kept:
            for r in turn:
                if r["role"] == "assistant":
                    content = strip_thinking(r["content"])
                else:
                    content = [b for b in r["content"] if not (
                        b.get("type") == "text" and str(b.get("text", "")).startswith(STALE_BLOCK_PREFIXES))]
                if content != r["content"]:
                    rewrites[r["id"]] = content
        self.store.compact(cid, None, kept[0][0]["id"], rewrites)
        state.pop("context_key", None)       # re-send ticker context and the full candle set with this message
        state.pop("candles", None)
        state.pop("fundamentals_key", None)  # research chat: re-send the fundamentals pack
        return len(old)

    # ── one turn ─────────────────────────────────────────────────────────

    async def run_turn(self, cid: str, req: TurnRequest) -> AsyncIterator[dict[str, Any]]:
        lock = self._locks.setdefault(cid, asyncio.Lock())
        if lock.locked():
            yield {"type": "error", "message": "this chat is still answering; wait for it to finish"}
            return
        async with lock:
            async for ev in self._turn(cid, req):
                yield ev

    async def _turn(self, cid: str, req: TurnRequest) -> AsyncIterator[dict[str, Any]]:
        conv = self.store.get(cid)
        if conv is None:
            yield {"type": "error", "message": "chat not found"}
            return
        state = dict(conv["state"])
        tier = self.tier(req.tier)
        turn_id = uuid.uuid4().hex[:12]
        res = TurnResult()
        symbol = self.svc.validate_symbol(str(req.screen.get("symbol") or conv.get("symbol")
                                              or self.svc.config.tickers.default))
        tf = req.screen.get("timeframe") if req.screen.get("timeframe") in ("1m", "5m", "1d") else "5m"
        kind = conv.get("kind") or "desk"
        research = kind == "research"
        yield {"type": "start", "turn_id": turn_id, "tier": req.tier, "model": tier.model}

        dropped = self.trim_history(cid, state)
        if dropped:
            log.info("chat %s: %d oldest turn(s) left out (memory %d turns)", cid, dropped, self.llm.memory_turns)

        # user message: [context if ticker/day changed] [question] [live] [candles in mode B]
        texts = [r["display"].get("text", "") for r in self.store.rows(cid)
                 if r.get("display") and r["display"].get("kind") == "question"] + [req.text]
        ctx = ToolContext(self.svc, symbol, req.screen, user_numbers(texts), self.cost_model(symbol),
                          user_text=req.text)
        blocks: list[dict[str, Any]] = []
        data_texts: list[str] = []
        events = upcoming_events(self.svc)
        if research:
            # [context if ticker / day / events changed] [fundamentals if its data changed] [question] [live]
            rdata = await rmode.research_data(self.svc, symbol)
            ctx.research.update(pack=rmode.split_pack(rdata)[0], price=rmode.price_of(rdata), as_of=rmode.as_of(rdata),
                                conversation_id=cid, last_card=rmode.last_card(self.store.rows(cid)))
            from app.research.thesis import memory_for_chat, memory_numbers_text
            memory = memory_for_chat(self.svc, symbol)
            c, ckey = rmode.context_block(self.svc, symbol, rdata, events, memory)
            if state.get("context_key") != ckey:
                blocks.append({"type": "text", "text": c})
                state["context_key"] = ckey
            # the number check reads the context without the thesis words (they may be the AI's own)
            data_texts.append(rmode.context_block(self.svc, symbol, rdata, events)[0])
            data_texts.append(memory_numbers_text(memory))
            ctx.research["sources"] = data_texts      # same list: save_thesis checks against everything so far
            f, fkey = rmode.fundamentals_block(symbol, rdata)
            if state.get("fundamentals_key") != fkey:
                blocks.append({"type": "text", "text": f})
                state["fundamentals_key"] = fkey
                log.info("research chat %s: fundamentals block sent (%d chars)", symbol, len(f))
            data_texts.append(f)                 # its numbers stay allowed on later turns, when it is not re-sent
            data_texts.extend(rmode.earlier_tool_texts(self.store.history(cid)))   # results the AI may quote again
            blocks.append({"type": "text", "text": req.text})
            lv = rmode.live_block(symbol, rdata, self.clock(), LANGUAGES.get(req.lang, "English"))
            blocks.append({"type": "text", "text": lv})
            data_texts.append(lv)
            blocks.append({"type": "text", "text": language_line(req.lang)})
        else:
            ctx.options[symbol] = await options_view(self.svc, symbol)   # cached 5 / 30 min; bands join the levels
            levels = await ctx.levels(symbol, tf)
            from app.research.thesis import desk_thesis
            thesis = desk_thesis(self.svc, symbol)
            if state.get("context_key") != context_key(levels, events, thesis):
                c = context_block(self.svc, levels, events, thesis)
                blocks.append({"type": "text", "text": c})
                data_texts.append(context_block(self.svc, levels, events))   # thesis words never vouch for numbers
                state["context_key"] = context_key(levels, events, thesis)
            blocks.append({"type": "text", "text": req.text})
            lv = live_block(levels, self.clock(), req.screen, req.lang, cash_snapshot(self.svc),
                            orders_snapshot(self.svc), rules_snapshot(self.svc), options_compact(ctx.options[symbol]))
            blocks.append({"type": "text", "text": lv})
            data_texts.append(lv)
            if self.llm.candles.mode == "B" and tf != "1d":
                v5 = await self.svc.candles_view(symbol, "5m")
                v1 = await self.svc.candles_view(symbol, "1m") if self.llm.candles.last_1m_bars else None
                cb, state = candles_block(v5, v1, state, self.llm.candles.last_1m_bars)
                if cb:
                    blocks.append({"type": "text", "text": cb})
                    data_texts.append(cb)
            blocks.append({"type": "text", "text": language_line(req.lang)})
        if not conv["title"]:
            self.store.set_title(cid, req.text.strip().splitlines()[0][:80] if req.text.strip() else "Chat")
        self.store.add(cid, turn_id, "user", blocks,
                       {"kind": "question", "text": req.text, "tier": req.tier, "symbol": symbol,
                        "timeframe": tf, "web_search": req.web_search})

        last_row = None
        for call_no in range(MAX_CALLS_PER_TURN):
            history = self.store.history(cid)
            p = self.params(req.tier, history, web_search=req.web_search, kind=kind)
            async with self.client().beta.messages.stream(**p) as stream:
                async for event in stream:
                    et = getattr(event, "type", None)
                    if et == "text":
                        yield {"type": "text", "text": event.text}
                    elif et == "content_block_start":
                        b = getattr(event, "content_block", None)
                        bt = getattr(b, "type", None)
                        if bt in ("tool_use", "server_tool_use"):
                            yield {"type": "tool", "name": getattr(b, "name", "?"), "status": "running"}
                        elif bt == "thinking":
                            yield {"type": "thinking"}
                msg = await stream.get_final_message()
            rec = self.record_usage(msg, tier=req.tier, purpose="chat", cid=cid, turn_id=turn_id,
                                    symbol=symbol, kind=conv.get("kind") or "desk")
            res.calls.append(rec)
            state["last_prompt_tokens"] = (rec["input_tokens"] + rec["cache_read_tokens"]
                                           + rec["cache_write_5m_tokens"] + rec["cache_write_1h_tokens"])
            yield {"type": "usage", "record": rec}

            content = echo_content([dump_block(b) for b in msg.content])
            stop = msg.stop_reason
            if stop == "refusal":
                cat = getattr(getattr(msg, "stop_details", None), "category", None)
                res.warnings.append("The model declined this request (safety filter"
                                    + (f": {cat}" if cat else "") + "). Try rephrasing.")
                break
            if stop == "max_tokens":
                content = [b for b in content if b.get("type") != "tool_use"]  # never run a cut-off call
                res.warnings.append("Answer was cut off at the length limit.")
            res.texts.extend(b.get("text", "") for b in content if b.get("type") == "text")
            if content:
                last_row = self.store.add(cid, turn_id, "assistant", content)
            if stop == "pause_turn":
                continue
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                res.tools_used.append(tu["name"])
                try:
                    runner = rmode.run_research_tool if research else run_tool
                    out = await runner(tu["name"], tu.get("input"), ctx, self.clock())
                    results.append(result_block(tu["id"], out))
                    yield {"type": "tool", "name": tu["name"], "status": "done"}
                except ToolError as exc:
                    results.append(result_block(tu["id"], f"Error: {exc}", is_error=True))
                    yield {"type": "tool", "name": tu["name"], "status": "error", "message": str(exc)}
                except Exception as exc:  # noqa: BLE001 - a tool bug must not break the chat
                    log.warning("tool %s failed: %s", tu["name"], type(exc).__name__)
                    results.append(result_block(tu["id"], "Error: tool failed inside the dashboard", is_error=True))
                    yield {"type": "tool", "name": tu["name"], "status": "error", "message": "tool failed"}
                for ev in ctx.events:
                    if ev["type"] == "scenarios":
                        res.scenarios.append(ev["table"])
                    elif ev["type"] == "valuation":
                        res.valuations.append(ev["card"])
                    elif ev["type"] == "candidates":
                        res.candidates.append(ev["list"])
                    yield ev
                ctx.events.clear()
                if tu["name"] not in rmode.NOT_SOURCES:   # echoes AI / user words (cards, theses): never a source
                    data_texts.append(results[-1]["content"])
            self.store.add(cid, turn_id, "user", results)
            if call_no == MAX_CALLS_PER_TURN - 1:
                res.warnings.append("Stopped after too many tool steps.")

        answer = "".join(res.texts).strip()
        if research:                    # extended check: percents, multiples, amounts, prices (+ card text)
            checked = "\n".join([answer, *(rmode.card_texts(c) for c in res.valuations),
                                 *(rmode.candidates_text(c) for c in res.candidates)])
            bad = rmode.unverified_research_numbers(checked, data_texts + texts, ctx.known_prices())
        else:
            bad = unverified_numbers(answer, data_texts + texts, ctx.known_prices())
        if bad:
            res.warnings.append("Unverified number(s) not found in dashboard data: " + ", ".join(bad))
        self.store.set_state(cid, state)
        display = self.turn_display(req, symbol, tf, answer, res)
        if last_row is not None:
            self.store.set_display(last_row, display)
        yield {"type": "done", "display": display}

    def turn_display(self, req: TurnRequest, symbol: str, tf: str, answer: str,
                     res: TurnResult) -> dict[str, Any]:
        def tot(key: str) -> int:
            return sum(int(c.get(key) or 0) for c in res.calls)

        usd = sum((Decimal(c["cost_usd"]) for c in res.calls if c.get("cost_usd")), Decimal(0))
        krw_vals = [Decimal(c["cost_krw"]) for c in res.calls if c.get("cost_krw")]
        prompt = tot("input_tokens") + tot("cache_read_tokens") + tot("cache_write_5m_tokens") + tot("cache_write_1h_tokens")
        last = res.calls[-1] if res.calls else {}
        return {
            "kind": "answer", "text": answer, "scenarios": res.scenarios, "warnings": res.warnings,
            **({"valuations": res.valuations} if res.valuations else {}),
            **({"candidates": res.candidates} if res.candidates else {}),
            "tier": req.tier, "model": last.get("model"), "symbol": symbol, "timeframe": tf,
            "tools": res.tools_used,
            "cost": {
                "calls": len(res.calls),
                "models": sorted({c.get("model") for c in res.calls if c.get("model")}),
                "input_tokens": tot("input_tokens"), "output_tokens": tot("output_tokens"),
                "cache_write_tokens": tot("cache_write_5m_tokens") + tot("cache_write_1h_tokens"),
                "cache_read_tokens": tot("cache_read_tokens"),
                "cache_hit_rate": None if prompt == 0 else round(tot("cache_read_tokens") / prompt, 4),
                "web_search_requests": tot("web_search_requests"),
                "usd": format(usd, "f"),
                "krw": format(sum(krw_vals, Decimal(0)), "f") if len(krw_vals) == len(res.calls) and res.calls else None,
                "fx_rate": last.get("fx_rate"), "fx_time": last.get("fx_time"),
                "price_known": all(c.get("price_known", True) for c in res.calls),
            },
        }
