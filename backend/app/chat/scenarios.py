"""Scenario table: the AI proposes direction/entry/invalidation/target and the levels behind them;
this code checks the prices and calculates risk/reward and the expected move after costs.

Rows never send orders: the screen's buttons only pre-fill the order ticket.
Quantity (user choice 2026-11-20): only when the user asks for one, the AI may suggest whole shares;
this code calculates the money figures and warns when it exceeds buying power / shares held.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from app.chat.levels import price_matches

BP = Decimal(10000)


class ScenarioError(ValueError):
    """Bad scenario input from the AI (message goes back to the AI as a tool error)."""


@dataclass(frozen=True)
class CostModel:
    fee_rate: Decimal                 # per side (0.001 = 0.1%), est. (qty unknown: <$10 exemption ignored)
    fx_spread_bp: Decimal | None      # per conversion, est.; None = not known
    spread_pct: Decimal | None = None  # current bid/ask spread % (phase 3); crossing it in + out ~ 1 spread


@dataclass(frozen=True)
class ScenarioRow:
    direction: Literal["long", "short"]
    entry: Decimal
    invalidation: Decimal
    target: Decimal
    entry_basis: str
    invalidation_basis: str
    target_basis: str
    reasoning: str
    plan: Literal["entry", "entry_exits", "exits"] = "entry"   # which order buttons the screen offers
    quantity: int | None = None       # AI-suggested whole shares, only when the user asked for a quantity


@dataclass(frozen=True)
class AccountLimits:
    """What the quantity is checked against (from the Toss account data; None = not known)."""

    buying_power_usd: Decimal | None = None
    held_quantity: Decimal | None = None


PLANS = ("entry", "entry_exits", "exits")
MAX_QTY = 1_000_000


def _dec(v: Any, name: str) -> Decimal:
    if isinstance(v, bool) or v is None:
        raise ScenarioError(f"{name} must be a number")
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        raise ScenarioError(f"{name} must be a number") from None
    if not d.is_finite() or d <= 0:
        raise ScenarioError(f"{name} must be a positive number")
    return d


def _qty(v: Any) -> int | None:
    """Whole shares (>= 1) or None. Fractional / zero / negative / huge -> error back to the AI."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise ScenarioError("quantity must be a whole number of shares or null")
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        raise ScenarioError("quantity must be a whole number of shares or null") from None
    if not d.is_finite() or d != d.to_integral_value():
        raise ScenarioError("quantity must be a whole number of shares (no fractions)")
    if not 1 <= d <= MAX_QTY:
        raise ScenarioError(f"quantity must be between 1 and {MAX_QTY}")
    return int(d)


def parse_row(raw: Any) -> ScenarioRow:
    if not isinstance(raw, dict):
        raise ScenarioError("each row must be an object")
    direction = raw.get("direction")
    if direction not in ("long", "short"):
        raise ScenarioError("direction must be 'long' or 'short'")
    texts = {}
    for k in ("entry_basis", "invalidation_basis", "target_basis", "reasoning"):
        v = raw.get(k)
        if not isinstance(v, str) or not v.strip():
            raise ScenarioError(f"{k} is required (name the level ids it is based on)")
        texts[k] = v.strip()[:1000]
    plan = raw.get("plan") if raw.get("plan") in PLANS else "entry"   # missing / unknown: entry buttons only
    return ScenarioRow(direction, _dec(raw.get("entry"), "entry"), _dec(raw.get("invalidation"), "invalidation"),
                       _dec(raw.get("target"), "target"), plan=plan, quantity=_qty(raw.get("quantity")), **texts)


def _q(d: Decimal, places: str) -> Decimal:
    return d.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def sells_shares(row: ScenarioRow) -> bool:
    """The row's first order sells shares (short entry, or protecting held shares)."""
    return row.direction == "short" or row.plan == "exits"


def quantity_view(row: ScenarioRow, costs: CostModel, account: AccountLimits | None) -> dict[str, Any]:
    """Money figures for an AI-suggested quantity, all calculated here. Warnings never block the row:
    the draft checks run again when the user creates it."""
    q = row.quantity
    if q is None:
        return {"quantity": None, "quantity_source": None, "position_usd": None,
                "loss_at_stop_usd_est": None, "net_at_target_usd_est": None, "quantity_warnings": []}
    e, inv, tgt = row.entry, row.invalidation, row.target
    sign = Decimal(1) if row.direction == "long" else Decimal(-1)
    position = q * e
    loss = q * (abs(e - inv) + costs.fee_rate * (e + inv))        # price move to the stop + fees both sides
    net = q * (sign * (tgt - e) - costs.fee_rate * (e + tgt))
    warnings: list[str] = []
    acct = account or AccountLimits()
    if sells_shares(row):
        if acct.held_quantity is None:
            warnings.append("shares held unknown (Toss account data not loaded)")
        elif q > acct.held_quantity:
            warnings.append(f"quantity {q} is more than the {format(acct.held_quantity, 'f')} shares held")
    else:
        if acct.buying_power_usd is None:
            warnings.append("buying power unknown (Toss account data not loaded)")
        elif position > acct.buying_power_usd:
            warnings.append(f"cost {format(_q(position, '0.01'), 'f')} USD is more than buying power "
                            f"{format(_q(acct.buying_power_usd, '0.01'), 'f')} USD")
    return {"quantity": q, "quantity_source": "ai_suggested",
            "position_usd": format(_q(position, "0.01"), "f"),
            "loss_at_stop_usd_est": format(_q(loss, "0.01"), "f"),
            "net_at_target_usd_est": format(_q(net, "0.01"), "f"),
            "quantity_warnings": warnings}


def evaluate(row: ScenarioRow, known_prices: list[Decimal], costs: CostModel,
             account: AccountLimits | None = None) -> dict[str, Any]:
    """Risk/reward and the after-cost move, plus checks. All numbers from code."""
    problems: list[str] = []
    e, inv, tgt = row.entry, row.invalidation, row.target
    if row.direction == "long" and not (inv < e < tgt):
        problems.append("long needs invalidation < entry < target")
    if row.direction == "short" and not (tgt < e < inv):
        problems.append("short needs target < entry < invalidation")
    # user choice 2026-11-20: a price that is not a code level or the user's number is allowed (the user
    # checks it), shown as a warning; it no longer switches the order buttons off
    price_warnings = [f"{name} {p} is AI-proposed: not a code-calculated level or a price you gave (check it)"
                      for name, p in (("entry", e), ("invalidation", inv), ("target", tgt))
                      if not price_matches(p, known_prices)]

    risk = abs(e - inv)
    reward = abs(tgt - e)
    rr = None if risk == 0 else reward / risk
    sign = Decimal(1) if row.direction == "long" else Decimal(-1)
    gross_pct = sign * (tgt - e) / e * 100
    fee_pct = costs.fee_rate * 2 * 100                       # buy + sell
    fx_pct = None if costs.fx_spread_bp is None else costs.fx_spread_bp * 2 / BP * 100  # KRW->USD->KRW
    sp_pct = costs.spread_pct if costs.spread_pct is not None and costs.spread_pct >= 0 else None
    cost_pct = fee_pct + (fx_pct or 0) + (sp_pct or 0)
    net_pct = gross_pct - cost_pct
    net_per_share = sign * (tgt - e) - costs.fee_rate * (e + tgt)
    return {
        "direction": row.direction,
        "entry": format(e, "f"), "invalidation": format(inv, "f"), "target": format(tgt, "f"),
        "entry_basis": row.entry_basis, "invalidation_basis": row.invalidation_basis,
        "target_basis": row.target_basis, "reasoning": row.reasoning, "plan": row.plan,
        "risk_per_share": format(risk, "f"), "reward_per_share": format(reward, "f"),
        "risk_reward": None if rr is None else format(_q(rr, "0.01"), "f"),
        "gross_move_pct": format(_q(gross_pct, "0.01"), "f"),
        "costs_pct_est": format(_q(cost_pct, "0.001"), "f"),
        "fx_cost_known": fx_pct is not None,
        "spread_pct_est": None if sp_pct is None else format(_q(sp_pct, "0.001"), "f"),
        "net_move_pct_est": format(_q(net_pct, "0.01"), "f"),
        "net_per_share_usd_est": format(_q(net_per_share, "0.0001"), "f"),
        **quantity_view(row, costs, account),
        "problems": problems,
        "price_warnings": price_warnings,
        "valid": not problems,
    }


def evaluate_table(rows: Any, known_prices: list[Decimal], costs: CostModel,
                   account: AccountLimits | None = None) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        raise ScenarioError("rows must be a non-empty list")
    if len(rows) > 6:
        raise ScenarioError("at most 6 scenarios")
    return [evaluate(parse_row(r), known_prices, costs, account) for r in rows]
