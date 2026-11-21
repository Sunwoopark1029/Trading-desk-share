"""Tools the AI can call. Definitions are fixed (same bytes every request = cache hits).

Rules enforced in code, not just in the prompt:
- every price the AI draws must be a code-calculated level or a number the user typed
- trendline / Fibonacci points must be swing points from get_levels (price and bar time)
- the AI can delete only its own lines (source "ai")
- a scenario quantity is accepted only when the user's message asks for one (user choice 2026-11-20)
- drafts and rules (user choice 2026-11-20): numbers the user gave are used exactly; numbers the AI picks are
  allowed but flagged "AI-proposed" (prices must be code-calculated levels)
- the AI arms rules only when the user's latest message asks for arming (user choice 2026-11-20)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app import overlays as overlay_mod
from app.chat.levels import Level, LevelSet, compute_levels, match_price, price_matches, shown_price
from app.chat.scenarios import AccountLimits, CostModel, ScenarioError, evaluate_table
from app.services import Services
from app.timeutil import KST, parse_iso_with_offset, to_epoch

AI_COLOR = "#29b6f6"
HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{6})$")
NUM_RE = re.compile(r"(?<![\w.])\d{1,7}(?:,\d{3})*(?:\.\d+)?(?![\w])")

_POINT = {
    "type": "object",
    "properties": {
        "time": {"type": ["string", "null"],
                 "description": "Bar time as ISO 8601 with offset, copied from a get_levels swing point. null for hline/zone."},
        "price": {"type": "number", "description": "Copied exactly from a get_levels level or from the user's message."},
    },
    "required": ["time", "price"],
    "additionalProperties": False,
}

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "get_levels",
        "description": ("Code-calculated price levels for a ticker and timeframe: last price, EMA 9/20/60, session "
                        "VWAP, today's session high/low, Fibonacci levels, recent swing highs/lows (with bar times), "
                        "previous day close/high/low, the user's average price and planned buy/sell prices, saved "
                        "Fibonacci and AI lines. Each level has an id. These are the ONLY prices you may state."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker, e.g. SOXS"},
                "timeframe": {"type": "string", "enum": ["1m", "5m", "1d"]},
            },
            "required": ["symbol", "timeframe"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_options",
        "description": ("Options-implied context for a US ticker, calculated in code from yfinance option chains "
                        "(~15 min delayed; after the close: last session): expected 1-sigma move for one trading day / "
                        "to the weekly and monthly expiry with bands (level ids em_day_up ...), IV per expiry (from "
                        "the at-the-money straddle) with a quality grade, term structure, IV vs 20-day realized "
                        "volatility, put vs call price skew, put/call volume, most traded strikes. Expected move is "
                        "SIZE, never direction. Thin quality = do not rely on it."),
        # not strict (grammar size limit, see propose_rule)
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Ticker; default: the screen's"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_screen_state",
        "description": "What the user is looking at: ticker, timeframe, which indicators are on, which lines are visible.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "add_overlay",
        "description": ("Draw a line on the user's chart (current ticker). Types and points: hline = 1 point (price), "
                        "zone = 2 points (two prices), trendline = 2 swing points, fib = 2 swing points (from, to). "
                        "Prices must be copied from get_levels or from the user's message; trendline/fib points must "
                        "be swing points from get_levels with their time. Lines appear right away."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["hline", "trendline", "fib", "zone"]},
                "points": {"type": "array", "items": _POINT},
                "label": {"type": "string", "description": "Short label shown on the chart"},
                "color": {"type": ["string", "null"], "description": "#rrggbb, or null for the default AI color"},
                "note": {"type": "string", "description": "Why this line: which levels it is based on (level ids)"},
            },
            "required": ["type", "points", "label", "color", "note"],
            "additionalProperties": False,
        },
    },
    {
        "name": "remove_overlay",
        "description": "Delete one of YOUR (AI) lines by id. You cannot delete the user's lines.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_news",
        "description": ("Search the dashboard's news feed (Finnhub, fetched by code, last ~48 h) by tickers and/or "
                        "keywords, e.g. 'NVDA' or 'NVDA export'. Returns headline, source, published time (KST, or "
                        "'date unverified'), link, and the AI tag (priority, effect on holdings, reason). Cite the "
                        "link and date for every item you mention."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "show_scenarios",
        "description": ("Show a scenario table to the user. Use whenever the user asks for scenarios or entries. "
                        "Give prices copied from get_levels (or the user's message) and name the level ids in each "
                        "basis. The dashboard calculates risk/reward and the move after fees and FX costs and returns "
                        "them; explain the table using those returned numbers. Display only: nothing is ordered."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "direction": {"type": "string", "enum": ["long", "short"]},
                            "entry": {"type": "number"},
                            "invalidation": {"type": "number"},
                            "target": {"type": "number"},
                            "entry_basis": {"type": "string", "description": "Level ids, e.g. 'vwap_5m + fib_0.618'"},
                            "invalidation_basis": {"type": "string"},
                            "target_basis": {"type": "string"},
                            "reasoning": {"type": "string", "description": "Which levels overlap and why"},
                            "plan": {"type": "string", "enum": ["entry", "entry_exits", "exits"],
                                     "description": ("Which order buttons the screen offers. 'entry' (default): only "
                                                     "a buy/sell and a reserved buy/sell at the entry. 'entry_exits': "
                                                     "ONLY when the user asked for a plan with both the entry and the "
                                                     "exits (take-profit / stop) or you explicitly recommend them "
                                                     "together; adds OTO and OCO. 'exits': ONLY for protecting shares "
                                                     "the user already holds with take-profit / stop (long rows)")},
                            "quantity": {"type": ["integer", "null"],
                                         "description": ("Whole shares. null unless the user asked you for a "
                                                         "quantity (수량 / how many shares). Then call get_account "
                                                         "first and size it from buying power (buys) or shares "
                                                         "held (sells); say why in reasoning")},
                        },
                        "required": ["direction", "entry", "invalidation", "target", "entry_basis",
                                     "invalidation_basis", "target_basis", "reasoning", "plan", "quantity"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["rows"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_account",
        "description": ("Read-only: USD buying power and the shares held of one ticker (from Toss, with its time). "
                        "Call it only when the user asks you for a quantity, before show_scenarios."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_order_draft",
        "description": ("Create an order DRAFT (it is NOT sent: the user reviews and approves it on the Orders tab; "
                        "you cannot send orders). Use it when the user asks to buy or sell, or asks you to prepare an "
                        "order. Numbers the user gave (price, shares, %, USD amount, trigger prices, validity, date) "
                        "are copied EXACTLY. Numbers the user didn't give you may propose: prices must be code-"
                        "calculated levels from get_levels (a limit may be that level rounded to the price tick); "
                        "shares are whole numbers sized with get_account; validity DAY or CLS. Everything you picked "
                        "is labeled AI-proposed on the approval screen; say in your answer what you picked and why. "
                        "COND_BUY / COND_SELL / OCO / OTO ALWAYS need expire_date (the user's, else pick one and say "
                        "so). kind: BUY, SELL, COND_BUY / COND_SELL (reserved: trigger + expiry), OCO, OTO."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["BUY", "SELL", "COND_BUY", "COND_SELL", "OCO", "OTO"]},
                "symbol": {"type": "string"},
                "order_type": {"type": "string", "enum": ["LIMIT", "MARKET"]},
                "price": {"type": ["string", "null"], "description": "Limit price in USD, exactly as typed; null for market"},
                "size_mode": {"type": "string", "enum": ["shares", "percent", "amount"]},
                "quantity": {"type": ["string", "null"]},
                "percent": {"type": ["string", "null"], "description": "% of buying power (buy) or of shares held (sell)"},
                "amount": {"type": ["string", "null"], "description": "USD amount (market orders only)"},
                "time_in_force": {"type": ["string", "null"], "description": "DAY (today) or CLS (until the close)"},
                "legs": {"type": ["array", "null"], "items": {
                    "type": "object",
                    "properties": {"trigger_price": {"type": "string"}, "order_price": {"type": ["string", "null"]}},
                    "required": ["trigger_price", "order_price"], "additionalProperties": False}},
                "expire_date": {"type": ["string", "null"], "description": "YYYY-MM-DD for reserved orders"},
            },
            "required": ["kind", "symbol", "order_type", "price", "size_mode", "quantity", "percent", "amount",
                         "time_in_force", "legs", "expire_date"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_rule",
        "description": ("Propose an AUTOMATIC RULE: a price condition + one order that code sends by itself once the "
                        "rule is ARMED. Types: "
                        "touch up to X then fall back down to Y within N minutes (Y < X; e.g. sell when a level is "
                        "rejected); touch down to X then bounce up to Y within N minutes (Y > X; e.g. buy when "
                        "support holds); or no touch and a candle CLOSE beyond Y (breakout / breakdown). confirm: "
                        "'tick' = any trade at / beyond Y; 'close_1m' / 'close_5m' = a finished candle must CLOSE "
                        "at / beyond Y (filters quick wicks; use it when the user wants to see if a move holds). "
                        "A plain 'price reaches Y' is a Toss reserved order (create_order_draft COND_*), not a rule. "
                        "Prices: code levels from get_levels or the user's numbers. Quantity: the user's, else whole "
                        "shares you size with get_account. It is saved as 'proposed'; arm it with arm_rules only "
                        "when the user's message asks you to arm, otherwise tell the user to press Arm in the "
                        "Automatic rules panel. Say which numbers you picked."),
        # not strict: with it, all strict tools together exceed the API's grammar size limit (400 "compiled
        # grammar is too large", 2026-11-20). Every value is checked in code (chat_propose_rule, build_rule).
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "touch": {"type": ["object", "null"], "description": "null for a candle-close rule without a touch",
                          "properties": {"dir": {"type": "string", "enum": ["up", "down"]},
                                         "price": {"type": "string", "description": "X in USD"}},
                          "required": ["dir", "price"], "additionalProperties": False},
                "trigger": {"type": "object",
                            "properties": {"dir": {"type": "string", "enum": ["up", "down"]},
                                           "price": {"type": "string", "description": "Y in USD"},
                                           "confirm": {"type": "string", "enum": ["tick", "close_1m", "close_5m"]}},
                            "required": ["dir", "price", "confirm"], "additionalProperties": False},
                "window_min": {"type": ["integer", "null"],
                               "description": "Minutes Y may take after the touch; null without a touch"},
                "order": {"type": "object",
                          "properties": {"side": {"type": "string", "enum": ["BUY", "SELL"]},
                                         "order_type": {"type": "string", "enum": ["LIMIT", "MARKET"]},
                                         "price": {"type": ["string", "null"], "description": "Limit USD; null for market"},
                                         "quantity": {"type": "string", "description": "Whole shares"},
                                         "time_in_force": {"type": "string", "enum": ["DAY", "CLS"]}},
                          "required": ["side", "order_type", "price", "quantity", "time_in_force"],
                          "additionalProperties": False},
                "sessions": {"type": "string", "enum": ["any", "regular"]},
                "expires": {"type": "string",
                            "description": "When the rule ends, KST 'YYYY-MM-DDTHH:MM' (today is in <live> as_of_kst)"},
                "basis": {"type": "string", "description": "Level ids the prices came from, e.g. 'swing_high_1 / vwap_5m'"},
                "note": {"type": "string", "description": "One short sentence: why this rule"},
            },
            "required": ["symbol", "touch", "trigger", "window_min", "order", "sessions", "expires", "basis", "note"],
            "additionalProperties": False,
        },
    },
    {
        "name": "arm_rules",
        "description": ("Arm, disarm or delete automatic rules by id (8-character ids from propose_rule / list_rules / "
                        "<live> rules). ARM only when the user's latest message asks you to arm / activate / turn on "
                        "rules (e.g. 'arm them', '걸어줘', '활성화해줘'); the code refuses otherwise. Once armed, code "
                        "sends the rule's order by itself when the condition is met (real orders unless DRY RUN). "
                        "Arm only the rules the user means; disarm or delete rules you replace so the same shares are "
                        "never covered twice. delete: only unarmed rules. Report each result to the user."),
        # not strict (grammar size limit, see propose_rule); ids are checked in code
        "input_schema": {
            "type": "object",
            "properties": {
                "arm": {"type": "array", "items": {"type": "string"}},
                "disarm": {"type": "array", "items": {"type": "string"}},
                "delete": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["arm", "disarm", "delete"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_rules",
        "description": "Read-only: the user's automatic rules that are not finished (proposed / armed / touched).",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "get_thesis",
        "description": ("Read-only: the saved research thesis of a ticker (valuation verdicts, summary, reference "
                        "prices with ids thesis_...), what code knows now (price moved since saved, newer SEC filing, "
                        "days to earnings), its history and the user's notes."),
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string", "description": "Ticker"}},
                         "required": [], "additionalProperties": False},
    },
]


def web_search_tool(max_uses: int, allowed: list[str], blocked: list[str]) -> dict[str, Any]:
    t: dict[str, Any] = {"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}
    if allowed:
        t["allowed_domains"] = allowed
    if blocked:
        t["blocked_domains"] = blocked
    return t


def user_numbers(texts: list[str]) -> list[Decimal]:
    """Numbers the user typed (prices they gave count as allowed levels)."""
    out = []
    for t in texts:
        for m in NUM_RE.findall(t or ""):
            try:
                d = Decimal(m.replace(",", ""))
            except InvalidOperation:
                continue
            if d > 0:
                out.append(d)
    return out


class ToolError(Exception):
    """Goes back to the AI as an error tool_result (it can fix and retry)."""


@dataclass
class ToolContext:
    svc: Services
    symbol: str
    screen: dict[str, Any]
    user_prices: list[Decimal]
    cost_model: CostModel
    level_sets: list[LevelSet] = field(default_factory=list)   # every set computed this turn
    user_text: str = ""                                        # the user's latest message (order numbers come from here)
    events: list[dict[str, Any]] = field(default_factory=list)  # for the screen (overlay_changed, scenarios)
    options: dict[str, dict[str, Any]] = field(default_factory=dict)  # options_view per symbol, fetched this turn
    research: dict[str, Any] = field(default_factory=dict)   # research chat: pack / price / as_of of the chat ticker

    def known_prices(self) -> list[Decimal]:
        return [p for ls in self.level_sets for p in ls.prices()] + self.user_prices

    def swings(self) -> list[Level]:
        return [lv for ls in self.level_sets for lv in ls.all if lv.kind == "swing" and lv.time]

    async def levels(self, symbol: str, tf: str) -> LevelSet:
        ls = await compute_levels(self.svc, symbol, tf)
        from app.options_view import option_levels
        ls.live.extend(option_levels(self.options.get(symbol, {})))   # expected-move bands (em_day_up ...)
        self.level_sets.append(ls)
        return ls


def _dec(v: Any, what: str) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ToolError(f"{what} must be a number")
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        raise ToolError(f"{what} must be a number") from None
    if not d.is_finite() or d <= 0:
        raise ToolError(f"{what} must be greater than 0")
    return d


def _match_swing(time_s: Any, price: Decimal, swings: list[Level]) -> Level:
    if not isinstance(time_s, str):
        raise ToolError("trendline/fib points need the swing point's time from get_levels")
    try:
        t = to_epoch(parse_iso_with_offset(time_s))
    except ValueError:
        raise ToolError(f"time '{time_s}' must be ISO 8601 with an offset") from None
    for s in swings:
        if s.time and to_epoch(parse_iso_with_offset(s.time)) == t and price_matches(price, [s.price]):
            return s
    raise ToolError(f"point ({time_s}, {price}) is not a swing point from get_levels; call get_levels and copy one")


def level_time(price: Decimal, ctx: ToolContext) -> str | None:
    """Bar time of a code-calculated level at exactly this price (the earliest, if several), else None."""
    times = sorted((lv.time for ls in ctx.level_sets for lv in ls.all if lv.time and lv.price == price),
                   key=lambda s: to_epoch(parse_iso_with_offset(s)))
    return times[0] if times else None


def overlay_timeframe(otype: str, points: list[dict[str, Any]], ctx: ToolContext) -> str | None:
    """Timeframe of the candles an AI line was built from: the level set holding its swing points (trendline / fib)
    or its price levels (hline / zone); the chart on screen breaks ties and covers typed prices."""
    screen = ctx.screen.get("timeframe") if isinstance(ctx.screen, dict) else None
    screen = screen if screen in ("1m", "5m", "1d") else None
    found: list[str] = []
    for ls in ctx.level_sets:
        for p in points:
            for lv in ls.all:
                same_price = shown_price(lv.price) == p["price"]
                if lv.kind in ("ai_line", "user_fib") or not same_price:
                    continue
                if otype in ("trendline", "fib") and not (lv.kind == "swing" and lv.time == p["time"]):
                    continue
                found.append(ls.timeframe)
    if not found:
        return screen
    return screen if screen in found else found[-1]   # latest get_levels call wins otherwise


def validate_ai_overlay(args: dict[str, Any], ctx: ToolContext, now: datetime) -> dict[str, Any]:
    """AI tool input -> overlay dict in the shared format (source "ai"). Raises ToolError."""
    otype = args.get("type")
    if otype not in overlay_mod.POINTS_REQUIRED:
        raise ToolError("type must be hline, trendline, fib or zone")
    pts = args.get("points")
    need = overlay_mod.POINTS_REQUIRED[otype]
    if not isinstance(pts, list) or len(pts) != need:
        raise ToolError(f"{otype} needs exactly {need} point(s)")
    if not ctx.level_sets:
        raise ToolError("call get_levels first; lines may only use code-calculated prices")
    known = ctx.known_prices()
    now_iso = now.astimezone(KST).replace(microsecond=0).isoformat()
    points = []
    for i, p in enumerate(pts):
        if not isinstance(p, dict):
            raise ToolError("each point must be an object")
        price = _dec(p.get("price"), f"point {i + 1} price")
        if otype in ("trendline", "fib"):
            s = _match_swing(p.get("time"), price, ctx.swings())
            points.append({"time": s.time, "price": shown_price(s.price)})
        else:
            hit = match_price(price, known)
            if hit is None:
                raise ToolError(f"price {price} is not a code-calculated level or a price the user gave; "
                                "use a value from get_levels")
            # saved at the code value; time = the bar the level comes from (swing / session), so the chart line
            # starts there (Toss style). Levels without a bar (EMA, VWAP, typed prices) keep the drawing time.
            points.append({"time": level_time(hit, ctx) or now_iso, "price": shown_price(hit)})
    color = args.get("color")
    label = str(args.get("label") or "").strip()[:80] or otype
    tf = overlay_timeframe(otype, points, ctx)
    note = str(args.get("note") or "").strip()[:2000] or None
    return {
        "type": otype, "points": points, "label": label,
        "color": color if isinstance(color, str) and HEX_RE.match(color) else AI_COLOR,
        "source": "ai", "visible": True, "note": note, "symbol": ctx.symbol, "timeframe": tf,
    }


# words that mean the user asked for a quantity ("수량도 정해줘", "how many shares", "10주")
QTY_ASK_RE = re.compile(
    r"수량|몇\s*주|주\s*수|\d\s*주(?!\s*(?:일|간|차|신고|신저|최고|최저|고점|저점|이동|평균))|사이즈"
    r"|\bquantity\b|\bqty\b|\bshares?\b|how many|position size",
    re.IGNORECASE)


def asked_for_quantity(user_text: str) -> bool:
    return bool(QTY_ASK_RE.search(user_text or ""))


def account_limits(svc: Services, symbol: str) -> tuple[AccountLimits, dict[str, Any]]:
    """Buying power + shares held of one ticker from the account service (read-only, no new Toss call)."""
    acct = getattr(svc, "account", None)
    st = getattr(acct, "state", None)
    if st is None or st.updated is None:
        err = getattr(st, "error", None) or "Toss account data not loaded yet"
        return AccountLimits(), {"available": False, "symbol": symbol, "message": err}
    held = acct.held_quantity(symbol)
    item = next((i for i in (st.holdings.items if st.holdings else ()) if i.symbol == symbol), None)
    bp = st.buying_power_usd
    as_of = datetime.fromtimestamp(st.updated, KST).isoformat() if st.updated else None
    return AccountLimits(bp, held), {
        "available": True, "symbol": symbol,
        "buying_power_usd": None if bp is None else format(bp, "f"),
        "held_quantity": format(held, "f"),
        "average_price_usd": None if item is None or item.average_purchase_price is None
        else format(item.average_purchase_price, "f"),
        "as_of_kst": as_of, "error": st.error,
        "note": "Read-only. Use only to size a quantity the user asked for."}


async def run_tool(name: str, args: Any, ctx: ToolContext, now: datetime) -> dict[str, Any]:
    """Run one tool. Returns the JSON result; raises ToolError for a fixable mistake."""
    if not isinstance(args, dict):
        raise ToolError("tool input must be an object")
    if name == "get_levels":
        sym = str(args.get("symbol") or ctx.symbol).strip().upper()
        tf = args.get("timeframe") if args.get("timeframe") in ("1m", "5m", "1d") else "5m"
        try:
            sym = ctx.svc.validate_symbol(sym)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return (await ctx.levels(sym, tf)).as_dict()
    if name == "get_options":
        from app.options_view import options_view
        try:
            sym = ctx.svc.validate_symbol(str(args.get("symbol") or ctx.symbol).strip().upper())
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        if sym not in ctx.options:
            ctx.options[sym] = await options_view(ctx.svc, sym)
        return ctx.options[sym]
    if name == "get_screen_state":
        return ctx.screen
    if name == "add_overlay":
        ov = validate_ai_overlay(args, ctx, now)
        try:
            saved = ctx.svc.save_overlay(ov)
        except overlay_mod.OverlayError as exc:
            raise ToolError(str(exc)) from None
        ctx.events.append({"type": "overlay_changed", "action": "added", "id": saved["id"]})
        out = {"ok": True, "id": saved["id"], "type": saved["type"], "symbol": ctx.symbol}
        if saved.get("levels"):
            out["levels"] = saved["levels"]
        return out
    if name == "remove_overlay":
        oid = str(args.get("id") or "")
        existing = overlay_mod.get_overlay(oid, ctx.svc.overlays_path) if oid else None
        if existing is None:
            raise ToolError(f"no line with id '{oid}'")
        if existing.source != "ai":
            raise ToolError("that line belongs to the user; only AI lines can be removed")
        ctx.svc.delete_overlay(oid)
        ctx.events.append({"type": "overlay_changed", "action": "removed", "id": oid})
        return {"ok": True, "removed": oid}
    if name == "search_news":
        return search_news(ctx.svc, str(args.get("query") or ""))
    if name == "create_order_draft":
        return await chat_order_draft(args, ctx)
    if name == "propose_rule":
        return await chat_propose_rule(args, ctx)
    if name == "list_rules":
        from app.rules.routes import engine
        return engine(ctx.svc).chat_view()
    if name == "get_thesis":
        from app.research.thesis import thesis_view
        try:
            sym = ctx.svc.validate_symbol(str(args.get("symbol") or ctx.symbol).strip().upper())
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return await thesis_view(ctx.svc, sym)
    if name == "arm_rules":
        return await chat_arm_rules(args, ctx)
    if name == "get_account":
        try:
            sym = ctx.svc.validate_symbol(str(args.get("symbol") or ctx.symbol).strip().upper())
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return account_limits(ctx.svc, sym)[1]
    if name == "show_scenarios":
        if not ctx.level_sets:
            raise ToolError("call get_levels first; scenario prices must be code-calculated levels")
        raw_rows = args.get("rows")
        has_qty = isinstance(raw_rows, list) and any(isinstance(r, dict) and r.get("quantity") is not None
                                                     for r in raw_rows)
        if has_qty and not asked_for_quantity(ctx.user_text):
            raise ToolError("the user did not ask for a quantity: set quantity to null in every row")
        try:
            limits = account_limits(ctx.svc, ctx.symbol)[0] if has_qty else None
            rows = evaluate_table(raw_rows, ctx.known_prices(), ctx.cost_model, limits)
        except ScenarioError as exc:
            raise ToolError(str(exc)) from None
        table = {"symbol": ctx.symbol, "currency": "USD", "rows": rows,
                 "note": "Display only. Costs are estimates: 0.1% fee each side; FX spread from the Toss reference."}
        ctx.events.append({"type": "scenarios", "table": table})
        return table
    raise ToolError(f"unknown tool '{name}'")


# numbers glued to a unit count too ("10주", "30%", "$500"), unlike NUM_RE
ORDER_NUM_RE = re.compile(r"(?<![\d.,])\d[\d,]*(?:\.\d+)?(?!\d|\.\d)")
TIF_WORDS = {"DAY": ("today", "day", "오늘", "당일", "금일"),
             "CLS": ("close", "closing", "종가", "장마감", "마감")}


def numbers_in(text: str) -> set[str]:
    """Every number the user typed, normalized (commas removed, trailing zeros kept as typed)."""
    return {m.replace(",", "") for m in ORDER_NUM_RE.findall(text or "")}


def whole_shares(v: Any) -> bool:
    """True for a whole number of shares >= 1 ("10", "1,000", "10.0" -> True; "2.5", "0" -> False)."""
    try:
        d = Decimal(str(v).replace(",", "").strip())
    except InvalidOperation:
        return False
    return d.is_finite() and d >= 1 and d == d.to_integral_value()


def expiry_typed(expire_date: Any, user_text: str) -> bool:
    """The expiry date's month and day both appear in the user's message (so it is the user's date)."""
    typed = numbers_in(user_text)
    _y, m, d = (str(expire_date or "") + "--").split("-")[:3]
    return bool({m.lstrip("0"), m} & typed and {d.lstrip("0"), d} & typed)


SHARES_RE = re.compile(r"(\d[\d,]*)\s*(?:주|shares?\b)", re.IGNORECASE)


def user_share_counts(user_text: str) -> set[str]:
    """Share counts the user wrote ("10주", "10 shares")."""
    return {m.replace(",", "") for m in SHARES_RE.findall(user_text or "")}


def _typed(v: Any, typed: set[str]) -> bool:
    return str(v).replace(",", "").strip() in typed


def _price_from(v: Any, typed: set[str], known: list[Decimal]) -> str:
    """'user' | 'level' | 'unknown' for a price the AI put in a draft or rule."""
    if _typed(v, typed):
        return "user"
    try:
        p = Decimal(str(v).replace(",", "").strip())
    except InvalidOperation:
        return "unknown"
    return "level" if p.is_finite() and p > 0 and price_matches(p, known) else "unknown"


def check_verbatim(args: dict[str, Any], user_text: str, known: list[Decimal] | None = None) -> list[str]:
    """Problems with a draft's numbers. The user's numbers must be used exactly; numbers the AI picks are allowed
    (flagged, see ai_fields): prices only from code levels, whole shares, never another count than the user's."""
    typed = numbers_in(user_text)
    known = known or []
    probs = []
    prices = [("price", args.get("price"))]
    for i, l in enumerate(args.get("legs") or []):
        prices += [(f"leg{i + 1} trigger", l.get("trigger_price")), (f"leg{i + 1} order price", l.get("order_price"))]
    for name, v in prices:
        if v not in (None, "") and _price_from(v, typed, known) == "unknown":
            probs.append(f"{name} {v} is neither in the user's message nor a code-calculated level: call get_levels "
                         "and use a level (or ask the user)")
    for name in ("percent", "amount"):
        v = args.get(name)
        if v not in (None, "") and not _typed(v, typed):
            probs.append(f"{name} {v} is not in the user's message: use a whole-share quantity instead, or ask")
    q = args.get("quantity")
    counts = user_share_counts(user_text)
    if q not in (None, "") and counts and not _typed(q, counts):
        probs.append(f"the user gave {', '.join(sorted(counts))} shares: use exactly that quantity")
    tif = args.get("time_in_force")
    if args.get("kind") in ("BUY", "SELL"):
        if tif not in TIF_WORDS:
            probs.append("validity missing: DAY (today) or CLS (until the close): the user's choice, or pick one")
    elif not args.get("expire_date"):
        # the user's date, or (user choice 2026-11-20) one the AI picks; validate.py checks today..1 year
        probs.append("expiry date missing: use the user's date, or pick one yourself (YYYY-MM-DD, from today up "
                     "to 1 year ahead) and tell the user you chose it")
    size = args.get("size_mode")
    if size == "shares" and not args.get("quantity") or size == "percent" and not args.get("percent") \
            or size == "amount" and not args.get("amount"):
        probs.append("order size missing: the user's size, or a whole-share quantity you size with get_account")
    if size == "shares" and q not in (None, "") and not whole_shares(q):
        probs.append(f"quantity {q} is not a whole number of shares: use a whole number")
    if args.get("order_type") == "LIMIT" and args.get("kind") in ("BUY", "SELL") and not args.get("price"):
        probs.append("limit price missing: the user's price, or a code-calculated level")
    return probs


def ai_fields(args: dict[str, Any], user_text: str) -> list[str]:
    """Which of a draft's values the AI picked (not in the user's message). Shown as AI-proposed."""
    typed = numbers_in(user_text)
    vals = [("price", args.get("price")), ("quantity", args.get("quantity"))]
    for i, l in enumerate(args.get("legs") or []):
        vals += [(f"leg{i + 1}.trigger_price", l.get("trigger_price")),
                 (f"leg{i + 1}.order_price", l.get("order_price"))]
    out = [n for n, v in vals if v not in (None, "") and not _typed(v, typed)]
    tif = args.get("time_in_force")
    if args.get("kind") in ("BUY", "SELL") and tif in TIF_WORDS and \
            not any(w in (user_text or "").lower() for w in TIF_WORDS[tif]):
        out.append("time_in_force")
    return out


async def chat_order_draft(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """AI tool: a DRAFT (user's numbers exact, AI-picked ones flagged). Never sends (no send tool exists for the AI)."""
    probs = check_verbatim(args, ctx.user_text, ctx.known_prices())
    if probs:
        raise ToolError("; ".join(probs))
    from app.orders.context import build_context
    from app.orders.routes import stores

    svc = ctx.svc
    try:
        sym = svc.validate_symbol(str(args.get("symbol") or ""))
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    drafts, st = stores(svc)
    inputs = {k: v for k, v in args.items() if v is not None}
    inputs.update(symbol=sym, currency="USD")
    settings = st.get()
    c = await build_context(svc, sym, settings.price_band_pct)
    d = drafts.create(inputs, c, ttl=settings.draft_ttl_seconds, source="chat", dry_run=svc.config.orders.dry_run)
    exp = args.get("expire_date")
    exp_source = None
    if args.get("kind") not in ("BUY", "SELL") and exp:
        exp_source = "user" if expiry_typed(exp, ctx.user_text) else "ai_chosen"
        d.meta["expire_date_source"] = exp_source
        if exp_source == "ai_chosen":   # shown on the draft / approval screen so the user checks it
            from app.orders.validate import Issue
            d.result.warnings.append(Issue("expire_date_ai", {"date": str(exp)}))
    picked = ai_fields(args, ctx.user_text)
    if picked:                          # user choice 2026-11-20: AI numbers allowed, always labeled
        from app.orders.validate import Issue
        d.meta["ai_fields"] = picked
        d.result.warnings.append(Issue("ai_proposed", {"fields": ", ".join(picked)}))
    ctx.events.append({"type": "draft_created", "id": d.id})
    v = d.view(svc.clock())
    return {"draft_id": d.id, "status": v["status"], "summary_en": d.summary_en, "summary_ko": d.summary_ko,
            "problems": [i["code"] for i in v["errors"]], "warnings": [i["code"] for i in v["warnings"]],
            "expire_date_source": exp_source, "ai_proposed": picked,
            "expires_in_seconds": v["seconds_left"], "dry_run": d.dry_run,
            "note": "Draft only. Tell the user to review and approve it on the Orders tab (Drafts). You cannot send "
                    "orders. If problems are listed, explain them; the user fixes them and creates a new draft."}


async def chat_propose_rule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """AI tool: save an automatic rule as PROPOSED. It does nothing until the user arms it on screen."""
    from app.rules.routes import engine

    typed = numbers_in(ctx.user_text)
    known = ctx.known_prices()
    order = args.get("order") if isinstance(args.get("order"), dict) else {}
    touch = args.get("touch") if isinstance(args.get("touch"), dict) else None
    trigger = args.get("trigger") if isinstance(args.get("trigger"), dict) else {}
    probs: list[str] = []
    picked: list[str] = []
    for name, v in (("touch.price", touch.get("price") if touch else None), ("trigger.price", trigger.get("price")),
                    ("order.price", order.get("price"))):
        if v in (None, ""):
            continue
        src = _price_from(v, typed, known)
        if src == "unknown":
            probs.append(f"{name} {v} is neither in the user's message nor a code-calculated level: call get_levels "
                         "and use a level")
        elif src == "level":
            picked.append(name)
    q = order.get("quantity")
    counts = user_share_counts(ctx.user_text)
    if counts and not _typed(q, counts):
        probs.append(f"the user gave {', '.join(sorted(counts))} shares: use exactly that quantity")
    if not whole_shares(q):
        probs.append("quantity must be a whole number of shares")
    elif not _typed(q, typed):
        picked.append("order.quantity")
    if args.get("window_min") is not None and not _typed(args.get("window_min"), typed):
        picked.append("window_min")
    if not expiry_typed(str(args.get("expires") or "")[:10], ctx.user_text):
        picked.append("expires")
    if probs:
        raise ToolError("; ".join(probs))
    try:
        sym = ctx.svc.validate_symbol(str(args.get("symbol") or ""))
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    inp = {**args, "symbol": sym, "ai_fields": picked, "order": {**order, "currency": "USD"}}
    eng = engine(ctx.svc)
    rule, errors, warnings = await eng.create(inp, "ai")
    if rule is None:
        raise ToolError("rule not saved: " + "; ".join(f"{i.code} {i.params or ''}".strip() for i in errors))
    ctx.events.append({"type": "rule_created", "id": rule.id})
    return {"rule_id": rule.id[:8], "status": rule.status, "summary_en": rule.summary_en, "summary_ko": rule.summary_ko,
            "ai_proposed": picked, "warnings": [i.code for i in warnings], "dry_run": eng.dry_run,
            "note": "Saved as PROPOSED: it does nothing yet. If the user's message asked you to arm, call arm_rules "
                    "with this id; otherwise tell them to press Arm in the Automatic rules panel (or ask you to arm "
                    "it). Once armed, code sends the order when the condition is met, with no further approval, only "
                    "while the app is running."}


ARM_RE = re.compile(r"(?<![a-z])(?:arm|activate|enable)(?![a-z])|활성화|걸어|켜\s*(?:줘|주|라|놔|둬)|아밍",
                    re.IGNORECASE)
NO_ARM_RE = re.compile(r"(?:don'?t|do not|never|not)\s+(?:arm|activate|enable)|활성화\s*하지|걸지\s*마|켜지\s*마|"
                       r"하지\s*마", re.IGNORECASE)


def asked_to_arm(user_text: str) -> bool:
    """The user's own message asks for rules to be armed (user choice 2026-11-20: then the AI may arm)."""
    text = user_text or ""
    return bool(ARM_RE.search(text)) and not NO_ARM_RE.search(text)


async def chat_arm_rules(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """AI tool: arm (only when the user's message asks), disarm, delete (unarmed only) rules."""
    from app.rules.engine import RuleError
    from app.rules.routes import engine

    eng = engine(ctx.svc)

    def ids(key: str) -> list[str]:
        v = args.get(key)
        return [str(x) for x in v] if isinstance(v, list) else []

    want_arm, want_disarm, want_delete = ids("arm"), ids("disarm"), ids("delete")
    if want_arm and not asked_to_arm(ctx.user_text):
        raise ToolError("the user's message does not ask to arm rules: don't arm; tell them to say so "
                        "(e.g. 'arm them' / '걸어줘') or press Arm in the Automatic rules panel")
    out: list[dict[str, Any]] = []
    for ref in want_disarm:                      # first: free the shares of rules being replaced
        r = eng.find(ref)
        if r is None:
            out.append({"id": ref, "action": "disarm", "ok": False, "error": "rule_not_found"})
            continue
        eng.disarm(r.id, why="chat")
        out.append({"id": r.id[:8], "action": "disarm", "ok": True, "status": r.status})
    for ref in want_delete:
        r = eng.find(ref)
        if r is None:
            out.append({"id": ref, "action": "delete", "ok": False, "error": "rule_not_found"})
        elif r.status != "proposed":
            out.append({"id": r.id[:8], "action": "delete", "ok": False, "error": "only unarmed rules can be deleted"})
        else:
            eng.delete(r.id)
            out.append({"id": r.id[:8], "action": "delete", "ok": True})
    for ref in want_arm:
        r = eng.find(ref)
        if r is None:
            out.append({"id": ref, "action": "arm", "ok": False, "error": "rule_not_found"})
            continue
        try:
            await eng.arm(r.id, by="ai")
            out.append({"id": r.id[:8], "action": "arm", "ok": True, "status": r.status, "summary": r.summary_en})
        except RuleError as e:
            out.append({"id": r.id[:8], "action": "arm", "ok": False, "error": e.code,
                        "issues": [i["code"] for i in e.issues]})
    if any(o["ok"] for o in out):
        ctx.events.append({"type": "rules_changed"})
    return {"results": out, "dry_run": eng.dry_run,
            "note": "Armed rules send their order by themselves when the condition is met, only while the app runs "
                    "(real orders unless DRY RUN). Tell the user exactly which rules are armed, which failed and why, "
                    "and that Disarm / Disarm all in the panel stop them."}


PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def search_news(svc: Services, query: str, limit: int = 10) -> dict[str, Any]:
    """Feed items matching tickers (upper-case words) and/or keywords; important + recent first."""
    feed = getattr(svc, "news", None)
    if feed is None:
        return {"available": False, "message": "The news feed is off."}
    words = [w for w in re.findall(r"[A-Za-z0-9.\-]+", query)]
    tickers = {w.upper() for w in words if w.isupper() or w.upper() in feed.watch_list()}
    keywords = [w.lower() for w in words if w.upper() not in tickers]
    items = feed.items(tickers or None)
    if keywords:
        items = [i for i in items if any(k in (i.headline + " " + i.summary).lower() for k in keywords)] or items
    items.sort(key=lambda i: (PRIORITY_ORDER.get((i.tag or {}).get("priority", "low"), 3),
                              -(i.published or i.first_seen)))
    out = []
    for i in items[:limit]:
        v = i.view()
        out.append({"headline": i.headline, "source": i.source, "url": i.url, "tickers": i.tickers,
                    "published_kst": v["published_kst"] or "date unverified",
                    "priority": (i.tag or {}).get("priority", "not tagged yet"),
                    "effect_on_holdings": (i.tag or {}).get("direction", "not tagged yet"),
                    "reason": (i.tag or {}).get("reason_en", "")})
    return {"available": True, "query": query, "count": len(out), "items": out,
            "note": "Feed covers the tickers on screen, held, related and extra (config.yaml), last "
                    f"{feed.cfg.max_age_hours:g} h. No items does not mean there is no news."}


def result_block(tool_use_id: str, payload: Any, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, sort_keys=True)
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if is_error:
        block["is_error"] = True
    return block
