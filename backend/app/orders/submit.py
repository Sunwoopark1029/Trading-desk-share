"""Sending an APPROVED draft. The only place that can place an order.

Order safety (CLAUDE.md): a draft must exist, be valid, unexpired and never sent before; it is
re-checked against the latest price right before sending; the user's numbers are never changed
(a %-sized draft whose share count would now differ is refused); one draft = one send
(Toss clientOrderId = draft id, so even a retry can't double-send).
DRY RUN (config orders.dry_run: true): the response is simulated here; Toss order endpoints are not
called (and the Toss client refuses them anyway while writes are off).
Every request and response is appended to logs/trades.jsonl (no keys, no account number).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.data.toss import TossError
from app.orders.context import build_context
from app.orders.drafts import Draft, DraftStore
from app.orders.validate import validate
from app.settings import REPO_ROOT
from app.timeutil import KST

log = logging.getLogger("desk.orders")
TRADES_PATH = REPO_ROOT / "logs" / "trades.jsonl"


# ── trade log ─────────────────────────────────────────────────────────────

class TradeLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else TRADES_PATH   # looked up at call time (tests redirect it)
        self._lock = threading.Lock()

    def append(self, rec: dict[str, Any]) -> None:
        line = json.dumps(rec, ensure_ascii=False, default=str, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with self._lock, self.path.open(encoding="utf-8-sig") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    out.append(r)
        return out

    def sent_today(self, now: float) -> tuple[int, Decimal]:
        """(orders sent today KST, their USD value) for the optional daily limits."""
        today = datetime.fromtimestamp(now, UTC).astimezone(KST).date().isoformat()
        n, usd = 0, Decimal(0)
        for r in self.records():
            if r.get("event") == "result" and r.get("status") in ("accepted", "simulated") \
                    and str(r.get("ts", ""))[:10] == today:
                n += 1
                try:
                    usd += Decimal(str(r.get("notional_usd") or 0))
                except InvalidOperation:
                    pass
        return n, usd


# ── Toss request bodies (field names from the official OpenAPI) ──────────

def _s(d: Decimal) -> str:
    return format(d, "f")   # the user's exact number, no rounding


def toss_request(n: dict[str, Any], client_order_id: str) -> tuple[str, str, dict[str, Any]]:
    """Validated draft values -> (method, path, JSON body) for Toss."""
    hv = bool(n.get("confirm_high_value"))
    if not n.get("conditional"):
        body: dict[str, Any] = {"symbol": n["symbol"], "side": n["side"], "orderType": n["order_type"],
                                "clientOrderId": client_order_id, "confirmHighValueOrder": hv}
        if n.get("amount") is not None:
            body["orderAmount"] = _s(n["amount"])           # amount order: MARKET only, no timeInForce
        else:
            body["quantity"] = _s(n["quantity"])
            body["timeInForce"] = n["time_in_force"]
            if n["order_type"] == "LIMIT":
                body["price"] = _s(n["price"])
        return "POST", "/api/v1/orders", body
    kind_type = {"COND_BUY": "SINGLE", "COND_SELL": "SINGLE", "OCO": "OCO", "OTO": "OTO"}[n["kind"]]

    def leg(l: dict[str, Any]) -> dict[str, Any]:
        c: dict[str, Any] = {"orderSide": l["side"], "triggerPrice": _s(l["trigger_price"])}
        if n["order_type"] == "LIMIT":
            c["orderPrice"] = _s(l["order_price"])
        return c

    legs = n["legs"]
    return "POST", "/api/v1/conditional-orders", {
        "symbol": n["symbol"], "type": kind_type, "quantity": _s(n["quantity"]), "orderType": n["order_type"],
        "expireDate": n["expire_date"].isoformat(), "first": leg(legs[0]),
        "second": leg(legs[1]) if len(legs) > 1 else None,
        "clientOrderId": client_order_id, "confirmHighValueOrder": hv}


SIZE_KEYS = ("symbol", "side", "kind", "order_type", "quantity", "amount", "price", "time_in_force", "expire_date")


def same_order(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True if the re-checked draft would send exactly what the user approved."""
    if any(a.get(k) != b.get(k) for k in SIZE_KEYS):
        return False
    return [(l["trigger_price"], l["order_price"]) for l in a.get("legs") or []] == \
        [(l["trigger_price"], l["order_price"]) for l in b.get("legs") or []]


# ── sender ────────────────────────────────────────────────────────────────

class OrderSender:
    def __init__(self, svc: Any, drafts: DraftStore, *, dry_run: bool, log_: TradeLog | None = None) -> None:
        self.svc = svc
        self.drafts = drafts
        self.dry_run = dry_run
        self.log = log_ or TradeLog()
        self.simulated: list[dict[str, Any]] = []        # DRY RUN "orders" for the screen (part 5: modify/cancel)
        self._lock = asyncio.Lock()

    def _kst(self) -> str:
        return datetime.fromtimestamp(self.svc.clock(), UTC).astimezone(KST).isoformat()

    async def submit(self, draft_id: str) -> dict[str, Any]:
        async with self._lock:                               # one send at a time: no double click races
            d = self.drafts.get(draft_id)
            if d is None:
                return {"ok": False, "status": "error", "error": "draft_not_found"}
            if d.status in ("sent", "rejected", "unknown"):
                return d.meta["result"]                      # already used: same answer, nothing new sent
            now = self.svc.clock()
            if now >= d.expires:
                return {"ok": False, "status": "error", "error": "draft_expired"}
            if d.status != "draft":
                return {"ok": False, "status": "error", "error": "draft_invalid"}
            return await self._send(d)

    async def _send(self, d: Draft) -> dict[str, Any]:
        if d.inputs.get("action") in ("modify", "cancel"):
            return await self._send_action(d)
        svc = self.svc
        n0 = d.result.normalized
        ctx = await build_context(svc, n0["symbol"], svc.config.orders.price_band_pct)
        fresh = validate(d.inputs, ctx)                       # final check against the latest price
        if not fresh.ok:
            d.result, d.status = fresh, "invalid"
            return {"ok": False, "status": "error", "error": "recheck_failed",
                    "issues": [{"code": i.code, "params": i.params} for i in fresh.errors]}
        if not same_order(n0, fresh.normalized):
            d.status = "invalid"
            return {"ok": False, "status": "error", "error": "size_changed"}
        method, path, body = toss_request(fresh.normalized, d.id)
        base = {"ts": self._kst(), "draft_id": d.id, "client_order_id": d.id, "dry_run": self.dry_run,
                "source": d.source, "summary": d.summary_en, "method": method, "path": path}
        self.log.append({**base, "event": "request", "body": body})
        result: dict[str, Any]
        if self.dry_run:
            sim_id = "DRYRUN-" + d.id[:12]
            result = {"ok": True, "status": "simulated", "dry_run": True,
                      "order_id": sim_id if not fresh.normalized.get("conditional") else None,
                      "conditional_order_id": sim_id if fresh.normalized.get("conditional") else None,
                      "error": None}
            self.simulated.insert(0, {"id": sim_id, "draft_id": d.id, "created": base["ts"], "status": "OPEN",
                                      "summary_en": d.summary_en, "summary_ko": d.summary_ko,
                                      "values": fresh.normalized, "body": body})
        else:
            result = await self._toss(method, path, body)
        result.update({"sent_at": base["ts"], "request": body, "dry_run": self.dry_run})
        self.log.append({**base, "event": "result", "status": result["status"], "error": result.get("error"),
                         "order_id": result.get("order_id"), "conditional_order_id": result.get("conditional_order_id"),
                         "notional_usd": fresh.estimate.get("notional_usd")})
        d.result = fresh
        d.status = {"simulated": "sent", "accepted": "sent", "rejected": "rejected"}.get(result["status"], "unknown")
        d.meta["result"] = result
        acct = getattr(svc, "account", None)
        if acct is not None and not self.dry_run:
            asyncio.create_task(acct.refresh_now())           # show the new order right away
        return result

    async def _send_action(self, d: Draft) -> dict[str, Any]:
        """Modify / cancel. Re-checked against the latest order state; sent ONCE (no idempotency key in
        the Toss docs for these, so never retried automatically)."""
        from app.orders.manage import action_request, find_target, validate_action

        svc = self.svc
        n0 = d.result.normalized
        if n0["target_type"] != "simulated" and getattr(svc, "account", None) is not None:
            await svc.account.refresh_now()
        target = find_target(svc, n0["target_type"], n0["target_id"])
        ctx = await build_context(svc, n0["symbol"], svc.config.orders.price_band_pct)
        fresh = validate_action(d.inputs, target, ctx)
        if not fresh.ok:
            d.result, d.status = fresh, "invalid"
            return {"ok": False, "status": "error", "error": "recheck_failed",
                    "issues": [{"code": i.code, "params": i.params} for i in fresh.errors]}
        n = fresh.normalized
        method, path, body = action_request(n)
        base = {"ts": self._kst(), "draft_id": d.id, "dry_run": self.dry_run, "source": d.source,
                "summary": d.summary_en, "method": method, "path": path, "action": n["action"]}
        self.log.append({**base, "event": "request", "body": body})
        if self.dry_run:
            new_id = "DRYRUN-" + d.id[:12] if n["action"] == "modify" else None
            if n["target_type"] == "simulated":
                for s in self.simulated:
                    if s["id"] == n["target_id"]:
                        s["status"] = "CANCELED" if n["action"] == "cancel" else "REPLACED"
                if n["action"] == "modify":
                    old = next(s for s in self.simulated if s["id"] == n["target_id"])
                    vals = dict(old["values"])
                    for k in ("price", "order_type", "legs", "expire_date"):
                        if n.get(k) is not None:
                            vals[k] = n[k]
                    self.simulated.insert(0, {**old, "id": new_id, "draft_id": d.id, "created": base["ts"],
                                              "status": "OPEN", "values": vals,
                                              "summary_en": d.summary_en, "summary_ko": d.summary_ko})
            result = {"ok": True, "status": "simulated", "error": None, "order_id": new_id}
        else:
            acct = getattr(svc, "account", None)
            seq = acct.state.account.account_seq if acct is not None and acct.state.account else None
            if seq is None or svc.toss is None:
                result = {"ok": False, "status": "rejected", "error": "no_account", "order_id": None}
            else:
                try:
                    _st, res = await svc.toss.order_write(method, path, body, seq)
                    res = res if isinstance(res, dict) else {}
                    result = {"ok": True, "status": "accepted", "error": None,
                              "order_id": res.get("orderId") or res.get("conditionalOrderId")}
                except TossError as exc:
                    unknown = exc.code in ("transport", "request-in-progress")
                    result = {"ok": False, "status": "unknown" if unknown else "rejected", "error": exc.code,
                              "order_id": None}
                asyncio.create_task(acct.refresh_now())
        result.update({"sent_at": base["ts"], "request": body, "dry_run": self.dry_run, "action": n["action"]})
        self.log.append({**base, "event": "result", "status": result["status"], "error": result.get("error"),
                         "order_id": result.get("order_id")})
        d.result = fresh
        d.status = {"simulated": "sent", "accepted": "sent", "rejected": "rejected"}.get(result["status"], "unknown")
        d.meta["result"] = result
        return result

    async def _toss(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Real send. A lost response is retried ONCE with the same clientOrderId (Toss then returns
        the original result instead of placing a second order)."""
        acct = getattr(self.svc, "account", None)
        seq = acct.state.account.account_seq if acct is not None and acct.state.account else None
        if seq is None or self.svc.toss is None:
            return {"ok": False, "status": "rejected", "error": "no_account", "order_id": None}
        for attempt in range(2):
            try:
                _status, res = await self.svc.toss.order_write(method, path, body, seq)
                res = res if isinstance(res, dict) else {}
                return {"ok": True, "status": "accepted", "error": None, "order_id": res.get("orderId"),
                        "conditional_order_id": res.get("conditionalOrderId")}
            except TossError as exc:
                if exc.code in ("transport", "request-in-progress") and attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                status = "unknown" if exc.code in ("transport", "request-in-progress") else "rejected"
                return {"ok": False, "status": status, "error": exc.code, "order_id": None}
        return {"ok": False, "status": "unknown", "error": "transport", "order_id": None}  # pragma: no cover
