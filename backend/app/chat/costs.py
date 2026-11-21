"""Cost of one Claude API call from its usage block and the price table in config.yaml.

All money is Decimal USD. Prices are per million tokens. Nothing here calls the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.settings import ModelPrice

MTOK = Decimal(1_000_000)
ZERO = Decimal(0)


@dataclass(frozen=True)
class TokenUsage:
    """Tokens for one attempt by one model (a fallback turn has several attempts)."""

    model: str
    input_tokens: int = 0          # uncached input, full price
    output_tokens: int = 0         # includes thinking tokens
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0

    @property
    def prompt_tokens(self) -> int:
        """Everything sent: uncached + cache writes + cache reads (decides Haiku's long tier)."""
        return self.input_tokens + self.cache_write_5m + self.cache_write_1h + self.cache_read


@dataclass(frozen=True)
class CallCost:
    usage: TokenUsage
    input_usd: Decimal
    output_usd: Decimal
    cache_write_usd: Decimal
    cache_read_usd: Decimal
    total_usd: Decimal
    price_known: bool       # False: model not in config -> priced at the fallback price, estimate
    long_prompt: bool       # True: the long-prompt price tier was used


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _int(v: Any) -> int:
    return int(v) if isinstance(v, int) and v > 0 else 0


def token_usage(usage: Any, model: str) -> TokenUsage:
    """One usage object (SDK model or dict) -> TokenUsage.

    Cache writes are split into 5m / 1h when the API gives the breakdown; otherwise
    the whole `cache_creation_input_tokens` is counted as 5m.
    """
    total_write = _int(_get(usage, "cache_creation_input_tokens"))
    cc = _get(usage, "cache_creation")
    w5 = _int(_get(cc, "ephemeral_5m_input_tokens")) if cc is not None else 0
    w1 = _int(_get(cc, "ephemeral_1h_input_tokens")) if cc is not None else 0
    if w5 + w1 != total_write:
        w5, w1 = total_write, 0
    return TokenUsage(
        model=model,
        input_tokens=_int(_get(usage, "input_tokens")),
        output_tokens=_int(_get(usage, "output_tokens")),
        cache_write_5m=w5,
        cache_write_1h=w1,
        cache_read=_int(_get(usage, "cache_read_input_tokens")),
    )


def attempts(usage: Any, response_model: str) -> list[TokenUsage]:
    """Per-attempt usage. With a refusal fallback, `usage.iterations` lists each attempt
    (with its own model) and is the billing source of truth; otherwise top-level usage."""
    its = _get(usage, "iterations") or []
    out = []
    for it in its:
        if _get(it, "type") in ("message", "fallback_message"):
            out.append(token_usage(it, str(_get(it, "model") or response_model)))
    return out or [token_usage(usage, response_model)]


def web_search_count(usage: Any) -> int:
    stu = _get(usage, "server_tool_use")
    return _int(_get(stu, "web_search_requests")) if stu is not None else 0


def price_for(u: TokenUsage, price: ModelPrice) -> tuple[ModelPrice, bool]:
    if price.long_prompt is not None and price.long_prompt_over_tokens is not None \
            and u.prompt_tokens > price.long_prompt_over_tokens:
        return price.long_prompt, True
    return price, False


def call_cost(u: TokenUsage, prices: dict[str, ModelPrice], fallback_model: str) -> CallCost:
    """Cost of one attempt. A model missing from the table is priced as `fallback_model`
    (the tier's model) and flagged price_known=False."""
    known = u.model in prices
    base = prices[u.model] if known else prices[fallback_model]
    p, long_tier = price_for(u, base)
    inp = Decimal(u.input_tokens) * p.input / MTOK
    out = Decimal(u.output_tokens) * p.output / MTOK
    cw = (Decimal(u.cache_write_5m) * p.cache_write_5m + Decimal(u.cache_write_1h) * p.cache_write_1h) / MTOK
    cr = Decimal(u.cache_read) * p.cache_read / MTOK
    return CallCost(u, inp, out, cw, cr, inp + out + cw + cr, known, long_tier)


def web_search_cost(count: int, usd_per_1000: Decimal) -> Decimal:
    return Decimal(count) * usd_per_1000 / Decimal(1000)


def cache_hit_rate(u: TokenUsage) -> Decimal | None:
    """Share of the prompt served from cache (0..1); None when nothing was sent."""
    total = u.prompt_tokens
    return None if total == 0 else Decimal(u.cache_read) / Decimal(total)


def to_krw(usd: Decimal, rate: Decimal | None) -> Decimal | None:
    return None if rate is None else usd * rate
