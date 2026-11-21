"""Rule data, validation and plain-language summaries. Pure (no network, no clock): unit tested.

A rule = [optional touch] + trigger + one order:
  touch    price reaches X ("up": X or higher, "down": X or lower)
  trigger  price then reaches Y the other way; confirmed by a trade ("tick") or by the CLOSE of a
           finished 1m / 5m candle ("close_1m" / "close_5m": a quick wick does not count)
  window   minutes the trigger may take after the touch (then the rule waits for a new touch)
  order    BUY / SELL, LIMIT / MARKET, whole shares, DAY / CLS: sent exactly as armed

Without a touch the trigger must be candle-confirmed ("candle closes beyond Y"); a plain "price reaches
Y" is a Toss reserved order (it also works while this program is closed), so it is refused here.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.orders.drafts import summarize
from app.orders.validate import Context, Issue, decimals, parse_number, validate
from app.timeutil import KST

CONFIRMS = ("tick", "close_1m", "close_5m")
BAR_SECONDS = {"close_1m": 60, "close_5m": 300}
ACTIVE = ("armed", "touched")                       # being watched
OPEN = ("proposed", "armed", "touched")             # not finished yet
FINISHED = ("sent", "rejected", "blocked", "expired", "unknown")
# order problems that depend on the moment (account / session): warnings when a rule is written,
# real blockers only when it fires
SOFT = ("not_enough_shares", "not_enough_buying_power", "no_buying_power", "market_regular_only")
MAX_WINDOW_MIN = 1440
LEVEL_MAX_DP = 4


@dataclass
class Rule:
    id: str
    created: float
    source: str                                  # user | ai
    symbol: str
    touch: dict[str, Any] | None                 # {"dir": up|down, "price": Decimal}
    trigger: dict[str, Any]                      # {"dir": up|down, "price": Decimal, "confirm": ...}
    window_min: int | None
    order: dict[str, Any]                        # raw order inputs (strings), as written
    sessions: str                                # any | regular
    expires: float
    status: str = "proposed"
    ai_fields: list[str] = field(default_factory=list)   # which numbers the AI proposed
    basis: str | None = None                     # level ids the AI used
    note: str | None = None
    armed_at: float | None = None
    armed_by: str | None = None                  # user | ai (the AI only when the user asked in chat)
    touched_at: float | None = None
    touch_price: Decimal | None = None
    last_bar_end: float = 0.0                    # newest candle already checked
    fired_at: float | None = None
    fire_price: Decimal | None = None
    paused: str | None = None                    # e.g. "stale_data" (not saved as a state)
    result: dict[str, Any] | None = None         # send result / block reasons
    summary_en: str = ""
    summary_ko: str = ""

    @property
    def side(self) -> str:
        return str(self.order.get("kind"))

    def to_json(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("paused")

        def conv(v: Any) -> Any:
            if isinstance(v, Decimal):
                return format(v, "f")
            if isinstance(v, dict):
                return {k: conv(x) for k, x in v.items()}
            if isinstance(v, list):
                return [conv(x) for x in v]
            return v

        return conv(d)

    @staticmethod
    def from_json(d: dict[str, Any]) -> Rule:
        def level(x: Any) -> dict[str, Any] | None:
            return None if x is None else {**x, "price": Decimal(str(x["price"]))}

        known = {f.name for f in dataclasses.fields(Rule)}
        r = Rule(**{k: v for k, v in d.items() if k in known})
        r.touch, r.trigger = level(r.touch), level(r.trigger) or {}
        r.touch_price = None if r.touch_price is None else Decimal(str(r.touch_price))
        r.fire_price = None if r.fire_price is None else Decimal(str(r.fire_price))
        return r


# ── validation ────────────────────────────────────────────────────────────

def _level_price(raw: Any, name: str, errors: list[Issue]) -> Decimal | None:
    p = parse_number(raw)
    if p is None or p <= 0:
        errors.append(Issue("price_required" if p is None else "price_positive", {"field": name}))
        return None
    if decimals(p) > LEVEL_MAX_DP:
        errors.append(Issue("price_decimals", {"field": name, "value": str(raw).strip(), "max_dp": LEVEL_MAX_DP}))
    return p


def parse_expiry(raw: Any) -> float | None:
    """'YYYY-MM-DDTHH:MM' (KST when no offset is given) -> epoch seconds. None if unreadable."""
    try:
        dt = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.timestamp()


def order_check(order: dict[str, Any], ctx: Context, at_price: Decimal | None) -> tuple[Any, list[Issue], list[Issue]]:
    """Check the order part with the normal draft rules. `at_price`: pretend the current price is the
    trigger level (how the order will look when the rule fires); None = use the real current price."""
    c = ctx if at_price is None else dataclasses.replace(ctx, current_price=at_price, price_source="rule trigger")
    res = validate(order, c)
    errors = list(res.errors)
    qty = res.normalized.get("quantity")
    if qty is not None and qty != qty.to_integral_value():
        errors.append(Issue("quantity_whole"))
    return res, errors, list(res.warnings)


def buy_cap_issue(res: Any, ctx: Context, max_buy_pct: Decimal | None) -> Issue | None:
    """Optional cap (Settings): a rule's BUY may use at most N % of USD buying power."""
    if max_buy_pct is None or res.normalized.get("side") != "BUY" or ctx.buying_power_usd is None:
        return None
    notional = res.estimate.get("notional_usd")
    if notional is None:
        return None
    limit = ctx.buying_power_usd * max_buy_pct / 100
    if Decimal(notional) > limit:
        return Issue("rule_over_buy_cap", {"pct": str(max_buy_pct), "needed": str(notional),
                                           "limit": str(limit.quantize(Decimal("0.01")))})
    return None


def build_rule(inp: dict[str, Any], ctx: Context, *, now: float, source: str, max_days: int,
               max_buy_pct: Decimal | None = None) -> tuple[Rule | None, list[Issue], list[Issue]]:
    """Check a new rule. Returns (rule or None, errors, warnings). The rule is NOT armed."""
    errors: list[Issue] = []
    warnings: list[Issue] = []
    symbol = str(inp.get("symbol") or "").strip().upper()
    if not symbol:
        errors.append(Issue("symbol_required"))

    trig_in = inp.get("trigger") if isinstance(inp.get("trigger"), dict) else {}
    tdir, confirm = trig_in.get("dir"), trig_in.get("confirm") or "tick"
    if tdir not in ("up", "down"):
        errors.append(Issue("rule_direction", {"field": "trigger"}))
    if confirm not in CONFIRMS:
        errors.append(Issue("rule_confirm"))
    y = _level_price(trig_in.get("price"), "trigger", errors)

    touch: dict[str, Any] | None = None
    window: int | None = None
    if inp.get("touch") is not None:
        t_in = inp["touch"] if isinstance(inp["touch"], dict) else {}
        x = _level_price(t_in.get("price"), "touch", errors)
        xdir = t_in.get("dir")
        if xdir not in ("up", "down"):
            errors.append(Issue("rule_direction", {"field": "touch"}))
        elif tdir in ("up", "down") and x is not None and y is not None:
            # touch up -> fall back down to a LOWER level; touch down -> bounce up to a HIGHER level
            if xdir == tdir or (xdir == "up" and not y < x) or (xdir == "down" and not y > x):
                errors.append(Issue("rule_levels_order"))
        touch = {"dir": xdir, "price": x}
        try:
            window = int(str(inp.get("window_min")))
        except (TypeError, ValueError):
            window = None
        if window is None or not 1 <= window <= MAX_WINDOW_MIN:
            errors.append(Issue("rule_window", {"max": MAX_WINDOW_MIN}))
        elif window * 60 < BAR_SECONDS.get(confirm, 0):      # a candle must be able to finish in the window
            errors.append(Issue("rule_window_short", {"min": BAR_SECONDS[confirm] // 60}))
    elif confirm == "tick":
        errors.append(Issue("rule_use_reserved_order"))   # plain "price reaches Y": Toss does this itself

    sessions = inp.get("sessions") or "any"
    if sessions not in ("any", "regular"):
        errors.append(Issue("rule_sessions"))
    expires = parse_expiry(inp.get("expires"))
    if expires is None:
        errors.append(Issue("rule_expiry_required"))
    elif expires <= now:
        errors.append(Issue("rule_expiry_past"))
    elif expires > now + max_days * 86400:
        errors.append(Issue("rule_expiry_too_far", {"days": max_days}))

    o_in = inp.get("order") if isinstance(inp.get("order"), dict) else {}
    side = str(o_in.get("side") or o_in.get("kind") or "").upper()
    if side not in ("BUY", "SELL"):
        errors.append(Issue("rule_side"))
        side = "SELL"
    order = {"kind": side, "symbol": symbol, "currency": str(o_in.get("currency") or "USD").upper(),
             "order_type": str(o_in.get("order_type") or "").upper(), "size_mode": "shares",
             "quantity": None if o_in.get("quantity") is None else str(o_in.get("quantity")).strip(),
             "time_in_force": str(o_in.get("time_in_force") or "").upper()}
    if o_in.get("price") not in (None, ""):
        order["price"] = str(o_in.get("price")).strip()
    res, o_err, o_warn = order_check(order, ctx, y)
    for i in o_err:
        (warnings if i.code in SOFT else errors).append(i)
    warnings.extend(o_warn)
    cap = buy_cap_issue(res, ctx, max_buy_pct)
    if cap is not None:
        warnings.append(cap)       # blocks arming and firing; a proposed rule only shows it

    if errors or y is None or expires is None:
        return None, errors, warnings
    ai_fields = [str(f) for f in inp.get("ai_fields") or []] if source == "ai" else []
    rule = Rule(id=uuid.uuid4().hex, created=now, source=source, symbol=symbol, touch=touch,
                trigger={"dir": tdir, "price": y, "confirm": confirm}, window_min=window, order=order,
                sessions=sessions, expires=expires, ai_fields=ai_fields,
                basis=str(inp["basis"])[:300] if inp.get("basis") else None,
                note=str(inp["note"])[:500] if inp.get("note") else None)
    rule.summary_en, rule.summary_ko = summarize_rule(rule, res.normalized)
    return rule, errors, warnings


# ── summaries (code-written; never adds a number of its own) ──────────────

def _usd(p: Decimal) -> str:
    return f"{format(p, 'f')} USD"


def kst_text(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def summarize_rule(r: Rule, order_norm: dict[str, Any]) -> tuple[str, str]:
    order_en, order_ko = summarize(order_norm)
    y, down = r.trigger["price"], r.trigger["dir"] == "down"
    confirm = r.trigger["confirm"]
    tf = {"close_1m": "1m", "close_5m": "5m"}.get(confirm)
    tf_ko = {"close_1m": "1분", "close_5m": "5분"}.get(confirm)
    if tf:
        trig_en = f"a {tf} candle closes at {_usd(y)} or {'lower' if down else 'higher'}"
        trig_ko = f"{tf_ko}봉이 {_usd(y)} {'이하' if down else '이상'}로 마감"
    else:
        trig_en = f"{'falls' if down else 'rises'} to {_usd(y)} or {'lower' if down else 'higher'}"
        trig_ko = f"{_usd(y)} {'이하로 하락' if down else '이상으로 상승'}"
    if r.touch is not None:
        x, up = r.touch["price"], r.touch["dir"] == "up"
        cond_en = (f"if the price touches {_usd(x)} or {'higher' if up else 'lower'} and then, within "
                   f"{r.window_min} min, {trig_en if tf else 'it ' + trig_en}")
        cond_ko = f"가격이 {_usd(x)} {'이상' if up else '이하'}에 닿은 뒤 {r.window_min}분 안에 {trig_ko}하면"
    else:
        cond_en, cond_ko = f"if {trig_en}", f"{trig_ko}하면"
    sess_en = "" if r.sessions == "any" else " Regular session only."
    sess_ko = "" if r.sessions == "any" else " 정규장에서만."
    return (f"{r.symbol}: {cond_en}: {order_en}. Rule ends {kst_text(r.expires)}.{sess_en}",
            f"{r.symbol}: {cond_ko} → {order_ko}. 규칙 종료 {kst_text(r.expires)}.{sess_ko}")


# ── state machine (pure): the engine feeds trades and finished candles ────

def _crossed(direction: str, price: Decimal, level: Decimal) -> bool:
    return price >= level if direction == "up" else price <= level


def window_over(r: Rule, at: float) -> bool:
    return r.status == "touched" and r.touched_at is not None and r.window_min is not None \
        and at - r.touched_at > r.window_min * 60


def reset_touch(r: Rule) -> None:
    """Back to waiting for a (new) touch. The rule stays armed."""
    if r.status == "touched":
        r.status, r.touched_at, r.touch_price = "armed", None, None


def step_tick(r: Rule, price: Decimal, ts: float) -> bool:
    """One trade. Returns True when the rule must fire now."""
    if r.status not in ACTIVE or r.armed_at is None or ts < r.armed_at:
        return False
    if window_over(r, ts):
        reset_touch(r)
    if r.status == "armed":
        if r.touch is not None and _crossed(r.touch["dir"], price, r.touch["price"]):
            r.status, r.touched_at, r.touch_price = "touched", ts, price
        return False
    return r.trigger["confirm"] == "tick" and _crossed(r.trigger["dir"], price, r.trigger["price"])


def step_bar(r: Rule, bar: dict[str, Any], interval: int) -> bool:
    """One FINISHED candle ({"end", "high", "low", "close"}). Returns True when the rule must fire.
    Only for candle-confirmed rules of that interval. The candle's high / low also counts as the touch
    (the trade stream can miss ticks), so "wick up to X, close back under Y" fires on that same candle."""
    if r.status not in ACTIVE or r.armed_at is None or BAR_SECONDS.get(r.trigger["confirm"]) != interval:
        return False
    end = float(bar["end"])
    if end <= r.last_bar_end or end <= r.armed_at:
        return False
    r.last_bar_end = end
    if window_over(r, end):
        reset_touch(r)
    if r.touch is not None and r.status == "armed":
        extreme = Decimal(str(bar["high"] if r.touch["dir"] == "up" else bar["low"]))
        if _crossed(r.touch["dir"], extreme, r.touch["price"]):
            r.status, r.touched_at, r.touch_price = "touched", end, extreme
    if r.touch is not None and r.status != "touched":
        return False
    return _crossed(r.trigger["dir"], Decimal(str(bar["close"])), r.trigger["price"])
