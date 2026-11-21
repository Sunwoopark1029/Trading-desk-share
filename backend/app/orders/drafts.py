"""Order drafts: created from the user's exact inputs, validated in code, expire after N seconds.

A draft is only a proposal shown on screen. Nothing here sends anything (sending is a separate,
later step that also needs approval and is blocked entirely while orders.dry_run is true).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.orders.validate import Context, Issue, Result, validate
from app.settings import OrdersConfig
from app.timeutil import KST

# ── on-screen settings (price band, draft expiry) ────────────────────────

@dataclass
class OrderSettings:
    price_band_pct: Decimal
    draft_ttl_seconds: int


class SettingsStore:
    """Price band and draft expiry as currently in effect (set on the Settings page)."""

    def __init__(self, cfg: OrdersConfig) -> None:
        self.cfg = cfg

    def get(self) -> OrderSettings:
        return OrderSettings(self.cfg.price_band_pct, self.cfg.draft_ttl_seconds)


# ── summaries ─────────────────────────────────────────────────────────────

def _p(d: Decimal | None) -> str:
    return "?" if d is None else f"{d} USD"


def _q(d: Decimal | None) -> str:
    return "?" if d is None else format(d.normalize(), "f")


def summarize(n: dict[str, Any]) -> tuple[str, str]:
    """Plain sentences (EN, KO) from the validated values. Never adds a number of its own."""
    k, sym, ot = n.get("kind"), n.get("symbol", "?"), n.get("order_type")
    qty, amount, price = n.get("quantity"), n.get("amount"), n.get("price")
    pct = n.get("percent")
    size_en = f"{_q(qty)} {sym}" if amount is None else f"{sym} for {amount} USD"
    size_ko = f"{sym} {_q(qty)}주" if amount is None else f"{sym} {amount} USD어치"
    if pct is not None:
        basis_en = "of buying power" if n.get("side") == "BUY" else "of shares held"
        basis_ko = "주문가능금액의" if n.get("side") == "BUY" else "보유 수량의"
        size_en = f"{pct}% {basis_en} = {size_en}"
        size_ko = f"{basis_ko} {pct}% = {size_ko}"
    tif = n.get("time_in_force")
    tif_en = {"DAY": "valid today", "CLS": "at the close"}.get(tif or "", "")
    tif_ko = {"DAY": "당일", "CLS": "종가"}.get(tif or "", "")
    if k in ("BUY", "SELL"):
        verb_en, verb_ko = ("Buy", "매수") if k == "BUY" else ("Sell", "매도")
        if ot == "LIMIT":
            return (f"{verb_en} {size_en} at {_p(price)}, limit, {tif_en}",
                    f"{size_ko} {_p(price)} 지정가 {verb_ko} ({tif_ko})")
        return f"{verb_en} {size_en} at market, {tif_en}", f"{size_ko} 시장가 {verb_ko} ({tif_ko})"
    legs = n.get("legs") or []
    exp = n.get("expire_date")

    def leg_en(l: dict[str, Any]) -> str:
        what = f"{l['side'].lower()} at {_p(l['order_price'])}" if ot == "LIMIT" else f"{l['side'].lower()} at market"
        return f"{what} when price reaches {_p(l['trigger_price'])}"

    def leg_ko(l: dict[str, Any]) -> str:
        v = "매수" if l["side"] == "BUY" else "매도"
        what = f"{_p(l['order_price'])} 지정가 {v}" if ot == "LIMIT" else f"시장가 {v}"
        return f"{_p(l['trigger_price'])} 도달 시 {what}"

    if k in ("COND_BUY", "COND_SELL") and legs:
        name_en = "Reserved buy" if k == "COND_BUY" else "Reserved sell"
        name_ko = "예약 매수" if k == "COND_BUY" else "예약 매도"
        return (f"{name_en} {size_en}: {leg_en(legs[0])}; expires {exp}",
                f"{name_ko} {size_ko}: {leg_ko(legs[0])}, 만료 {exp}")
    if k == "OCO" and len(legs) == 2:
        return (f"Take-profit / stop for {size_en}: {leg_en(legs[0])}, or {leg_en(legs[1])}; "
                f"the other is cancelled; expires {exp}",
                f"{size_ko} 익절/손절: {leg_ko(legs[0])}, 또는 {leg_ko(legs[1])} (하나가 실행되면 나머지 취소), 만료 {exp}")
    if k == "OTO" and len(legs) == 2:
        return (f"{size_en}: {leg_en(legs[0])}, then {leg_en(legs[1])}; expires {exp}",
                f"{size_ko}: {leg_ko(legs[0])}, 체결되면 {leg_ko(legs[1])}, 만료 {exp}")
    return "Incomplete order", "미완성 주문"


# ── drafts ────────────────────────────────────────────────────────────────

@dataclass
class Draft:
    id: str
    created: float
    expires: float
    source: str                       # panel | scenario | chat
    inputs: dict[str, Any]            # the user's raw values, exactly as typed
    result: Result
    summary_en: str
    summary_ko: str
    context: dict[str, Any]
    dry_run: bool
    status: str = "draft"             # draft | invalid | discarded | expired | sent
    meta: dict[str, Any] = field(default_factory=dict)

    def view(self, now: float) -> dict[str, Any]:
        def kst(e: float) -> str:
            return datetime.fromtimestamp(e, UTC).astimezone(KST).isoformat()

        def issue(i: Issue) -> dict[str, Any]:
            return {"code": i.code, "params": i.params}

        status = "expired" if self.status in ("draft", "invalid") and now >= self.expires else self.status
        return {
            "id": self.id, "status": status, "source": self.source, "dry_run": self.dry_run,
            "created": kst(self.created), "expires": kst(self.expires),
            "seconds_left": max(0, int(self.expires - now)),
            "inputs": self.inputs, "values": self.result.normalized,
            "errors": [issue(i) for i in self.result.errors],
            "warnings": [issue(i) for i in self.result.warnings],
            "estimate": self.result.estimate, "summary_en": self.summary_en, "summary_ko": self.summary_ko,
            "context": self.context,
        }


class DraftStore:
    def __init__(self, clock: Callable[[], float] = time.time, max_keep: int = 100) -> None:
        self.clock = clock
        self.max_keep = max_keep
        self._d: dict[str, Draft] = {}
        self._lock = threading.Lock()

    def create(self, inputs: dict[str, Any], ctx: Context, *, ttl: int, source: str, dry_run: bool) -> Draft:
        res = validate(inputs, ctx)
        en, ko = summarize(res.normalized)
        return self.add(inputs, res, en, ko, ctx, ttl=ttl, source=source, dry_run=dry_run)

    def add(self, inputs: dict[str, Any], res: Result, en: str, ko: str, ctx: Context, *, ttl: int, source: str,
            dry_run: bool) -> Draft:
        """Store an already validated draft (new order, or modify / cancel of an open order)."""
        now = self.clock()
        d = Draft(id=uuid.uuid4().hex, created=now, expires=now + ttl, source=source,
                  inputs={k: v for k, v in inputs.items() if k != "confirm_high_value"} |
                  {"confirm_high_value": inputs.get("confirm_high_value") is True},
                  result=res, summary_en=en, summary_ko=ko, dry_run=dry_run,
                  context={"current_price": None if ctx.current_price is None else str(ctx.current_price),
                           "price_source": ctx.price_source, "session": ctx.session,
                           "price_band_pct": str(ctx.price_band_pct)},
                  status="draft" if res.ok else "invalid")
        with self._lock:
            self._d[d.id] = d
            if len(self._d) > self.max_keep:
                for old in sorted(self._d.values(), key=lambda x: x.created)[: len(self._d) - self.max_keep]:
                    del self._d[old.id]
        return d

    def get(self, draft_id: str) -> Draft | None:
        return self._d.get(draft_id)

    def discard(self, draft_id: str) -> bool:
        d = self._d.get(draft_id)
        if d is None or d.status == "sent":
            return False
        d.status = "discarded"
        return True

    def done(self, symbol: str | None = None, keep_s: float = 1800) -> list[Draft]:
        """Recently sent / rejected drafts (last 30 min) with their result, newest first."""
        now = self.clock()
        out = [d for d in self._d.values() if d.status in ("sent", "rejected", "unknown") and now - d.created < keep_s
               and (symbol is None or d.result.normalized.get("symbol") == symbol)]
        return sorted(out, key=lambda d: d.created, reverse=True)

    def active(self, symbol: str | None = None) -> list[Draft]:
        now = self.clock()
        out = [d for d in self._d.values()
               if d.status in ("draft", "invalid") and now < d.expires
               and (symbol is None or d.result.normalized.get("symbol") == symbol)]
        return sorted(out, key=lambda d: d.created, reverse=True)


def draft_lines(drafts: list[Draft], now_iso: str) -> list[dict[str, Any]]:
    """Valid drafts as dashed chart lines (shared overlay format, not saved)."""
    lines = []
    for d in drafts:
        if d.status != "draft":
            continue
        n = d.result.normalized
        prices: list[tuple[str, Decimal]] = []
        if n.get("price") is not None and not n.get("conditional"):
            prices.append(("", n["price"]))
        for i, l in enumerate(n.get("legs") or []):
            if l.get("trigger_price") is not None:
                prices.append((f" trigger {i + 1}", l["trigger_price"]))
        for tag, p in prices:
            lines.append({"id": f"draft-{d.id[:8]}{tag.replace(' ', '-')}", "type": "hline",
                          "points": [{"time": now_iso, "price": format(p, "f")}],
                          "label": f"Draft {n.get('side', '').lower()} {_q(n.get('quantity'))}{tag}",
                          "color": "#26a69a" if n.get("side") == "BUY" else "#ef5350",
                          "source": "user", "visible": True, "note": d.summary_en, "symbol": n.get("symbol"),
                          "derived": True, "dashed": True})
    return lines
