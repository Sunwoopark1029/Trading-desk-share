"""The rules engine: keeps the rules, watches live prices for armed ones, fires through OrderSender.

Safety (CLAUDE.md "Order safety"):
- A rule does nothing until it is armed: by the user's arm route, or by the chat AI only when the user's own
  message asks for arming (user choice 2026-11-20; checked in code in chat/tools.py).
- Fires once: the rule is saved as "firing" BEFORE anything is sent, so a crash can never fire it twice.
- The order goes through the normal draft checks and the one existing send path (DRY RUN included).
- No firing on stale data: a broken feed pauses the rule and forgets a half-finished touch; old trades
  (delivered late, or seen after the computer slept) are ignored.
- Runs only while this program runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.orders.context import build_context
from app.orders.validate import Issue
from app.rules.model import (
    ACTIVE, BAR_SECONDS, FINISHED, OPEN, SOFT, Rule, build_rule, buy_cap_issue, order_check, reset_touch,
    step_bar, step_tick, window_over,
)
from app.settings import REPO_ROOT
from app.timeutil import KST, to_epoch

log = logging.getLogger("desk.rules")
RULES_PATH = REPO_ROOT / "logs" / "rules.json"
MAX_SYMBOLS = 5              # distinct tickers across armed rules (the trade stream follows 10 at most)
BAR_GRACE_S = 5              # wait this long after a candle ends before reading its close
BAR_SETTLE_S = 3             # the candle data must have been fetched at least this long after the candle ended
BAR_GIVE_UP_S = 90           # no candle by then (no trades in it): skip that candle
KEEP_FINISHED = 50


def _issues(items: list[Issue]) -> list[dict[str, Any]]:
    return [{"code": i.code, "params": i.params} for i in items]


class RuleError(Exception):
    """A request that cannot be done; `code` is shown on screen (English / Korean)."""

    def __init__(self, code: str, issues: list[Issue] | None = None, **params: Any) -> None:
        super().__init__(code)
        self.code, self.params, self.issues = code, params, _issues(issues or [])


class RuleEngine:
    def __init__(self, svc: Any, path: Path | None = None) -> None:
        self.svc = svc
        self.path = path if path is not None else RULES_PATH   # looked up at call time (tests redirect it)
        self.rules: dict[str, Rule] = {}
        self.load_error: str | None = None
        self._seen: dict[str, set[tuple[Any, ...]]] = {}       # trades already handled, per ticker
        self._bar_try: dict[tuple[str, str], float] = {}       # last candle fetch per (ticker, timeframe)
        self._last_run: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        from app.orders.routes import stores
        stores(svc)                          # the draft store + the one order sender (shared with manual orders)
        self._load()

    # ── settings ─────────────────────────────────────────────────────────

    @property
    def cfg(self) -> Any:
        return self.svc.config.rules

    @property
    def dry_run(self) -> bool:
        sender = getattr(self.svc, "sender", None)
        return bool(sender.dry_run) if sender is not None else bool(self.svc.config.orders.dry_run)

    def _kst(self, epoch: float | None = None) -> str:
        e = self.svc.clock() if epoch is None else epoch
        return datetime.fromtimestamp(e, UTC).astimezone(KST).replace(microsecond=0).isoformat()

    # ── saving ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rules = [Rule.from_json(d) for d in raw["rules"]]
        except Exception as exc:  # noqa: BLE001 - a broken file must not stop the dashboard
            self.load_error = f"rules file could not be read ({type(exc).__name__}); no rule is armed"
            log.warning("rules: %s", self.load_error)
            return
        for r in rules:
            if r.status == "touched":        # trades were missed while the program was closed
                reset_touch(r)
            elif r.status == "firing":       # stopped in the middle of a send: never send again
                r.status = "unknown"
                r.result = {"ok": False, "status": "unknown", "error": "stopped_while_sending"}
            self.rules[r.id] = r

    def save(self) -> None:
        if self.load_error is not None:      # never overwrite a file we could not read
            return
        done = sorted((r for r in self.rules.values() if r.status in FINISHED), key=lambda r: r.created)
        for old in done[:-KEEP_FINISHED]:
            del self.rules[old.id]
        data = {"version": 1, "rules": [r.to_json() for r in self.rules.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _log(self, event: str, r: Rule, **extra: Any) -> None:
        sender = getattr(self.svc, "sender", None)
        if sender is not None:
            sender.log.append({"ts": self._kst(), "event": event, "rule_id": r.id, "symbol": r.symbol,
                               "source": r.source, "summary": r.summary_en, "dry_run": self.dry_run, **extra})

    # ── create / arm / disarm (called by the routes and the chat tool) ───

    async def create(self, inp: dict[str, Any], source: str) -> tuple[Rule | None, list[Issue], list[Issue]]:
        """Check and store a new rule as "proposed" (NOT armed)."""
        symbol = str(inp.get("symbol") or "").strip().upper()
        ctx = await build_context(self.svc, symbol, self.svc.config.orders.price_band_pct)
        rule, errors, warnings = build_rule(inp, ctx, now=self.svc.clock(), source=source,
                                            max_days=self.cfg.max_days,
                                            max_buy_pct=self.svc.config.orders.rule_max_buy_pct)
        if rule is None:
            return None, errors, warnings
        if sum(1 for r in self.rules.values() if r.status == "proposed") >= self.cfg.max_proposed:
            return None, [Issue("rule_too_many_proposed", {"max": self.cfg.max_proposed})], warnings
        self.rules[rule.id] = rule
        self.save()
        self._log("rule_created", rule)
        return rule, errors, warnings

    async def check_now(self, r: Rule) -> tuple[list[Issue], list[Issue]]:
        """(blockers for arming, warnings) as of now."""
        now = self.svc.clock()
        errors: list[Issue] = []
        if not self.cfg.enabled:
            errors.append(Issue("rules_off"))
        if self.load_error is not None:
            errors.append(Issue("rules_file_broken"))
        if r.expires <= now:
            errors.append(Issue("rule_expiry_past"))
        ctx = await build_context(self.svc, r.symbol, self.svc.config.orders.price_band_pct)
        res, o_err, warnings = order_check(r.order, ctx, r.trigger["price"])
        for i in o_err:
            (warnings if i.code in SOFT else errors).append(i)
        cap = buy_cap_issue(res, ctx, self.svc.config.orders.rule_max_buy_pct)
        if cap is not None:
            errors.append(cap)
        if r.side == "SELL" and self._reserved_sell(r.symbol):
            warnings.append(Issue("rule_reserved_sell_exists"))
        return errors, warnings

    def _reserved_sell(self, symbol: str) -> bool:
        acct = getattr(self.svc, "account", None)
        conds = acct.state.open_conditional if acct is not None else []
        return any(c.symbol == symbol and "SELL" in (c.first.order_side, c.second.order_side if c.second else "")
                   for c in conds)

    async def arm(self, rule_id: str, by: str = "user") -> Rule:
        """Arm one rule. Called by the user's arm route, or by the chat tool when the user asked (by="ai")."""
        async with self._lock:
            r = self.rules.get(rule_id)
            if r is None:
                raise RuleError("rule_not_found")
            if r.status != "proposed":
                raise RuleError("rule_not_proposed", status=r.status)
            errors, _w = await self.check_now(r)
            armed = [x for x in self.rules.values() if x.status in ACTIVE]
            if len(armed) >= self.cfg.max_armed:
                errors.append(Issue("rule_too_many_armed", {"max": self.cfg.max_armed}))
            if len({x.symbol for x in armed} | {r.symbol}) > MAX_SYMBOLS:
                errors.append(Issue("rule_too_many_symbols", {"max": MAX_SYMBOLS}))
            if errors:
                raise RuleError("rule_cannot_arm", errors)
            r.status, r.armed_at, r.armed_by = "armed", self.svc.clock(), by
            r.touched_at = r.touch_price = None
            r.last_bar_end = 0.0
            self.save()
            self._log("rule_armed", r, armed_by=by)
            return r

    def disarm(self, rule_id: str, why: str = "user") -> Rule:
        r = self.rules.get(rule_id)
        if r is None:
            raise RuleError("rule_not_found")
        if r.status in ACTIVE:
            r.status, r.armed_at, r.touched_at, r.touch_price, r.paused = "proposed", None, None, None, None
            r.armed_by = None
            self.save()
            self._log("rule_disarmed", r, why=why)
        return r

    def disarm_all(self) -> int:
        """Kill switch."""
        ids = [r.id for r in self.rules.values() if r.status in ACTIVE]
        for i in ids:
            self.disarm(i, why="kill_switch")
        return len(ids)

    def delete(self, rule_id: str) -> None:
        r = self.rules.get(rule_id)
        if r is None:
            raise RuleError("rule_not_found")
        if r.status == "firing":
            raise RuleError("rule_firing")
        if r.status in ACTIVE:
            self.disarm(rule_id, why="deleted")
        del self.rules[rule_id]
        self.save()
        self._log("rule_deleted", r)

    # ── watching ─────────────────────────────────────────────────────────

    def feed_ok(self, symbol: str, now: float) -> bool:
        """Is live data for this ticker arriving? (stream connected, or a recent good REST read)"""
        market = getattr(self.svc, "market", None)
        if market is None:
            return False
        if market.stream_connected():
            return True
        st = market.states.get(symbol)
        return st is not None and st.error is None and now - st.last_rest <= self.cfg.stale_seconds

    def _new_ticks(self, symbol: str, now: float) -> list[tuple[float, Decimal]]:
        """Trades not handled before, oldest first; ones older than stale_seconds are dropped."""
        market = self.svc.market
        st = market.states.get(symbol)
        tape = list(st.tape) if st is not None else []
        keys = {(t.timestamp, t.price, t.volume) for t in tape}
        seen = self._seen.get(symbol)
        self._seen[symbol] = keys
        if seen is None:                      # first look at this ticker: nothing before now counts
            return []
        out = [(t.timestamp.timestamp(), t.price) for t in tape if (t.timestamp, t.price, t.volume) not in seen]
        return sorted(x for x in out if now - x[0] <= self.cfg.stale_seconds)

    async def _finished_bar(self, symbol: str, confirm: str, now: float, after: float) -> dict[str, Any] | None:
        """The newest finished candle (end > after), read only after it has had time to settle."""
        interval = BAR_SECONDS[confirm]
        end = int(now // interval) * interval
        if end <= after or now < end + BAR_GRACE_S:
            return None
        key = (symbol, confirm)
        if now - self._bar_try.get(key, 0.0) < BAR_GRACE_S:
            return None
        self._bar_try[key] = now
        try:
            view = await self.svc.candles_view(symbol, "1m" if interval == 60 else "5m")
        except Exception as exc:  # noqa: BLE001 - try again on the next round
            log.warning("rules: candles for %s failed (%s)", symbol, type(exc).__name__)
            return None
        updated = view.get("meta", {}).get("updated")
        if view.get("meta", {}).get("status") != "ok" or not updated or to_epoch(updated) < end + BAR_SETTLE_S:
            return None                        # the candle data is from before this candle finished
        bar = next((b for b in reversed(view.get("bars") or []) if int(b["end"]) == end), None)
        if bar is None and now > end + BAR_GIVE_UP_S:
            return {"end": end, "missing": True}
        return bar

    async def run_once(self) -> None:
        """One round: expiry, feed health, new trades, finished candles. Never raises."""
        now = self.svc.clock()
        gap = self._last_run is not None and now - self._last_run > self.cfg.stale_seconds
        self._last_run = now
        changed = False
        active = [r for r in self.rules.values() if r.status in ACTIVE]
        if not self.cfg.enabled or not active:
            self._seen.clear()
            return
        market = getattr(self.svc, "market", None)
        for sym in {r.symbol for r in active}:
            if market is not None:
                market.interest(sym)           # keep its trade stream subscribed
            ok = self.feed_ok(sym, now) and not gap
            ticks = self._new_ticks(sym, now) if market is not None else []
            for r in [x for x in active if x.symbol == sym]:
                if r.status not in ACTIVE:
                    continue
                if now >= r.expires:
                    r.status, r.paused = "expired", None
                    self._log("rule_expired", r)
                    changed = True
                    continue
                if not ok:                     # stale feed / the computer slept: forget a half-done touch
                    if r.status == "touched":
                        reset_touch(r)
                        changed = True
                    r.paused = "stale_data"
                    continue
                r.paused = None
                before = r.status
                if window_over(r, now):
                    reset_touch(r)
                fire_price: Decimal | None = None
                for ts, price in ticks:
                    if step_tick(r, price, ts):
                        fire_price = price
                        break
                if fire_price is None and r.trigger["confirm"] in BAR_SECONDS:
                    bar = await self._finished_bar(sym, r.trigger["confirm"], now, max(r.last_bar_end, r.armed_at or 0))
                    if bar is not None and bar.get("missing"):
                        r.last_bar_end = float(bar["end"])
                    elif bar is not None and step_bar(r, bar, BAR_SECONDS[r.trigger["confirm"]]):
                        fire_price = Decimal(str(bar["close"]))
                    changed = changed or bar is not None
                if fire_price is not None:
                    await self._fire(r, fire_price)
                    changed = True
                elif r.status != before:
                    self._log("rule_touched" if r.status == "touched" else "rule_touch_reset", r,
                              touch_price=None if r.touch_price is None else format(r.touch_price, "f"))
                    changed = True
        if changed:
            self.save()

    # ── firing ───────────────────────────────────────────────────────────

    async def _fire(self, r: Rule, price: Decimal) -> None:
        """Condition met: final checks, then ONE send through the normal send path."""
        svc = self.svc
        r.status, r.fired_at, r.fire_price = "firing", svc.clock(), price
        self.save()                                             # on disk before anything is sent
        self._log("rule_fired", r, price=format(price, "f"))
        try:
            issues: list[Issue] = []
            ctx = await build_context(svc, r.symbol, svc.config.orders.price_band_pct)
            if ctx.session is None:
                issues.append(Issue("rule_market_closed"))
            elif r.sessions == "regular" and ctx.session != "regular":
                issues.append(Issue("rule_session", {"session": ctx.session}))
            res, o_err, _w = order_check(r.order, ctx, None)    # against the real current price
            issues.extend(o_err)
            cap = buy_cap_issue(res, ctx, svc.config.orders.rule_max_buy_pct)
            if cap is not None:
                issues.append(cap)
            if issues:
                r.status, r.result = "blocked", {"ok": False, "status": "blocked", "issues": _issues(issues)}
            else:
                d = svc.drafts.create(r.order, ctx, ttl=60, source="rule", dry_run=self.dry_run)
                d.meta["rule_id"] = r.id
                result = await svc.sender.submit(d.id)
                r.result = {**result, "draft_id": d.id}
                r.status = {"simulated": "sent", "accepted": "sent", "rejected": "rejected",
                            "error": "blocked"}.get(str(result.get("status")), "unknown")
        except Exception as exc:  # noqa: BLE001 - never fire twice; the user checks at Toss
            log.warning("rules: firing failed (%s)", type(exc).__name__)
            r.status, r.result = "unknown", {"ok": False, "status": "unknown", "error": type(exc).__name__}
        self.save()
        self._log("rule_result", r, status=r.status, result_status=(r.result or {}).get("status"),
                  error=(r.result or {}).get("error"), issues=(r.result or {}).get("issues"),
                  order_id=(r.result or {}).get("order_id"))

    # ── loop ─────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        seen = 0
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never die
                log.warning("rules engine: %s", type(exc).__name__)
            market = getattr(self.svc, "market", None)
            if market is not None and any(r.status in ACTIVE for r in self.rules.values()):
                seen = await market.wait_change(seen, 1.0)      # wake on every new trade, at least each second
                await asyncio.sleep(0.05)
            else:
                await asyncio.sleep(1.0)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="rules")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # ── views ────────────────────────────────────────────────────────────

    def view(self, r: Rule) -> dict[str, Any]:
        def lvl(x: dict[str, Any] | None) -> dict[str, Any] | None:
            return None if x is None else {**x, "price": format(x["price"], "f")}

        return {
            "id": r.id, "status": r.status, "source": r.source, "symbol": r.symbol,
            "touch": lvl(r.touch), "trigger": lvl(r.trigger), "window_min": r.window_min,
            "order": {"side": r.side, "order_type": r.order.get("order_type"), "price": r.order.get("price"),
                      "quantity": r.order.get("quantity"), "time_in_force": r.order.get("time_in_force"),
                      "currency": r.order.get("currency")},
            "sessions": r.sessions, "expires": self._kst(r.expires), "created": self._kst(r.created),
            "armed_at": None if r.armed_at is None else self._kst(r.armed_at), "armed_by": r.armed_by,
            "touched_at": None if r.touched_at is None else self._kst(r.touched_at),
            "touch_price": None if r.touch_price is None else format(r.touch_price, "f"),
            "touch_until": None if r.touched_at is None or r.window_min is None
            else self._kst(r.touched_at + r.window_min * 60),
            "fired_at": None if r.fired_at is None else self._kst(r.fired_at),
            "fire_price": None if r.fire_price is None else format(r.fire_price, "f"),
            "paused": r.paused, "ai_fields": r.ai_fields, "basis": r.basis, "note": r.note,
            "result": r.result, "summary_en": r.summary_en, "summary_ko": r.summary_ko,
        }

    def list_view(self) -> dict[str, Any]:
        rules = sorted(self.rules.values(), key=lambda r: (r.status not in OPEN, -r.created))
        return {"enabled": self.cfg.enabled, "dry_run": self.dry_run, "error": self.load_error,
                "armed": sum(1 for r in rules if r.status in ACTIVE), "max_armed": self.cfg.max_armed,
                "max_buy_pct": self.svc.config.orders.rule_max_buy_pct,
                "rules": [self.view(r) for r in rules]}

    def chart_lines(self, now_iso: str) -> list[dict[str, Any]]:
        """Armed rules as dashed chart lines (shared overlay format, not saved)."""
        lines = []
        for r in self.rules.values():
            if r.status not in ACTIVE:
                continue
            for tag, lv in (("touch", r.touch), ("trigger", r.trigger)):
                if lv is None:
                    continue
                lines.append({"id": f"rule-{r.id[:8]}-{tag}", "type": "hline",
                              "points": [{"time": now_iso, "price": format(lv["price"], "f")}],
                              "label": f"Rule {tag} ({r.side.lower()} {r.order.get('quantity')})",
                              "color": "#ffb300", "source": "user", "visible": True, "note": r.summary_en,
                              "symbol": r.symbol, "derived": True, "dashed": True})
        return lines

    def find(self, ref: str) -> Rule | None:
        """A rule by its full id or a unique id prefix (the chat sees 8-character ids)."""
        ref = str(ref or "").strip().lower()
        if len(ref) < 6:
            return None
        hits = [r for r in self.rules.values() if r.id.startswith(ref)]
        return hits[0] if len(hits) == 1 else None

    def chat_view(self) -> dict[str, Any]:
        """Compact list of unfinished rules for the chat's <live> block."""
        return {"note": "automatic rules; the AI may arm them only when the user's message asks for it",
                "rules": [{"id": r.id[:8], "status": r.status, "source": r.source, "armed_by": r.armed_by,
                           "summary": r.summary_en}
                          for r in self.rules.values() if r.status in OPEN]}
