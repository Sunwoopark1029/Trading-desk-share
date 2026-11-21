"""Options-implied context for the screen and the chat (plan step 3, 2026-11-21).

One place that turns the cached option chain (app/data/yf_options.py) into what the API and the AI see:
- `options_view`: the full summary (app/indicators/options.py) + source, KST times, staleness, explanations
- `compact`: the short version for the chat's <live> block
- `option_levels`: expected-move bands as levels with ids (em_day_up, ...) the AI cites and scenarios match

Every number is calculated in code; nothing here is estimated by the AI.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from decimal import Decimal
from typing import Any

from app.chat.levels import Level
from app.data import yf_options
from app.data.yf_options import SOURCE, OptionsService
from app.indicators.options import summarize, to_dict
from app.timeutil import epoch_to_kst_iso

log = logging.getLogger(__name__)

US_TICKER = re.compile(r"[A-Z][A-Z.\-]{0,9}")
FETCH_TIMEOUT_S = 10.0
NOTE = ("Options-implied context from yfinance option chains (~15 min delayed; after the close: the last session's "
        "quotes). IV comes from the at-the-money straddle (code); expected move = 1-sigma SIZE (about 68 % of outcomes "
        "inside), never a direction. Open interest is not used (unverified source).")
WARNING_TEXT = {
    "last_session": "market not in the regular session: quotes are from the last session",
    "thin_chain": "option quotes too wide (thin chain): no expected move from this ticker's options",
    "no_chain": "no usable option chain",
    "oi_unverified": "open interest not used (Yahoo's numbers failed a live check)",
    "oi_unavailable": "open interest not reported",
}


def service(svc: Any) -> OptionsService | None:
    """The shared options cache (created on first use). None when switched off."""
    cfg = svc.config.options
    if not cfg.enabled:
        return None
    if getattr(svc, "options", None) is None:
        svc.options = OptionsService(lambda s, n: yf_options.fetch_snapshot(s, n),   # looked up at call time (tests)
                                     max_expiries=cfg.max_expiries, ttl_live=cfg.ttl_live_seconds,
                                     ttl_closed=cfg.ttl_closed_seconds)
    return svc.options


async def _daily_closes(svc: Any, symbol: str) -> list[float]:
    try:
        view = await svc.candles_view(symbol, "1d")
    except Exception:  # noqa: BLE001 - realized vol is optional
        return []
    return [float(b["close"]) for b in view.get("bars", []) if b.get("close") is not None]


async def options_view(svc: Any, symbol: str, *, timeout: float = FETCH_TIMEOUT_S) -> dict[str, Any]:
    """Full options summary for `symbol`, or {"available": False, "reason": ...}."""
    sym = symbol.strip().upper()
    base = {"symbol": sym, "source": SOURCE}
    opts = service(svc)
    if opts is None:
        return base | {"available": False, "reason": "options switched off (config options.enabled)"}
    if not US_TICKER.fullmatch(sym):
        return base | {"available": False, "reason": "options data only for US tickers"}
    try:
        res = await asyncio.wait_for(opts.get(sym.replace(".", "-")), timeout)
    except TimeoutError:
        return base | {"available": False, "reason": "option chain source did not answer in time"}
    if res.snapshot is None:
        why = "no listed options" if res.error == "no_options" else f"option chain not available ({res.error})"
        return base | {"available": False, "reason": why}
    closes = await _daily_closes(svc, sym)
    s = summarize(res.snapshot, time.time(), daily_closes=closes, use_oi=svc.config.options.use_oi)
    d = to_dict(s)
    d["symbol"] = sym
    d["price_time_kst"] = None if s.price_time is None else epoch_to_kst_iso(s.price_time)
    d["fetched_kst"] = epoch_to_kst_iso(s.fetched_at)
    d.pop("price_time", None)
    d.pop("fetched_at", None)
    return base | d | {
        "available": True, "stale": res.stale, "fetch_error": res.error, "currency": "USD", "note": NOTE,
        "warnings_text": [WARNING_TEXT.get(w, w) for w in s.warnings],
        "expiries_listed": len(res.snapshot.expiries_all), "post_market_price": res.snapshot.post_price,
    }


def _pct(iv: float | None) -> str | None:
    return None if iv is None else f"{iv * 100:.0f}%"


def compact(v: dict[str, Any]) -> dict[str, Any]:
    """Short version for the chat's <live> block (details: get_options tool)."""
    if not v.get("available"):
        return {"available": False, "reason": v.get("reason"), "source": v.get("source")}
    out: dict[str, Any] = {
        "source": f"{v['source']}" + (" · STALE" if v.get("stale") else ""),
        "quotes_as_of_kst": v.get("price_time_kst"),
        "session": "live" if v.get("session_live") else "last session",
        "underlying_price_used": v.get("price"),
    }
    for key in ("day", "week", "month"):
        b = v.get(key)
        if b:
            out[f"expected_move_{key}"] = {
                "move_usd": round(b["move"], 2), "pct": b["move_pct"], "iv": _pct(b["iv"]), "quality": b["quality"],
                "to_expiry": b.get("expiry") if key != "day" else None,
                "levels": [f"em_{key}_up", f"em_{key}_down"]}
    if v.get("iv_vs_realized") is not None:
        out["iv_vs_realized_20d"] = v["iv_vs_realized"]
    if v.get("term"):
        out["term_structure"] = v["term"]
    warn = [WARNING_TEXT.get(w, w) for w in v.get("warnings", []) if w != "oi_unverified"]  # OI rule is in the prompt
    if warn:
        out["warnings"] = warn
    out["more"] = "get_options: per-expiry IV, skew, put/call volume, most traded strikes"
    return out


LABELS = {"day": "1 trading day", "week": "to {exp}", "month": "to {exp}"}


def option_levels(v: dict[str, Any]) -> list[Level]:
    """Expected-move bands as levels (kind 'options'). Centered on the chain's underlying price."""
    if not v.get("available"):
        return []
    out = []
    for key in ("day", "week", "month"):
        b = v.get(key)
        if not b:
            continue
        span = LABELS[key].format(exp=b.get("expiry"))
        tail = f"options expected move {span}, IV {_pct(b['iv'])}, from {v['price']} USD, {v['source']}"
        out.append(Level(f"em_{key}_up", f"Upper 1-sigma band ({tail})", Decimal(str(round(b["up"], 2))), "options"))
        out.append(Level(f"em_{key}_down", f"Lower 1-sigma band ({tail})", Decimal(str(round(b["down"], 2))), "options"))
    return out
