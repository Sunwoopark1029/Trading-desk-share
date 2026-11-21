"""User settings from the Settings page: saved in data/user_settings.json (not in git).

config.yaml holds the defaults; values saved here override them and apply immediately (they are
written onto the live config objects that the rest of the backend reads).
Deliberately NOT here: orders.dry_run (real orders stay a hand edit of config.yaml), API keys
(.env only), refresh intervals (need a restart).
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.settings import DATA_DIR, REPO_ROOT, AppConfig
from app.timeutil import parse_iso_with_offset

SETTINGS_PATH = DATA_DIR / "user_settings.json"
OLD_ORDER_SETTINGS = REPO_ROOT / "logs" / "orders" / "settings.json"   # phase 4 part 2 file (migrated once)
TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,20}$")
TOGGLES = ("ema", "vwap", "rsi", "fib", "swings", "lines", "aiLines", "sessions", "volume")


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _ticker(v: str) -> str:
    v = v.strip().upper()
    if not TICKER_RE.match(v):
        raise ValueError(f"'{v}' is not a ticker (letters, digits, '.' or '-')")
    return v


class General(_S):
    language: Literal["en", "ko"] = "en"


class Chart(_S):
    timeframe: Literal["1m", "5m", "1d"] = "5m"
    toggles: dict[str, bool] = Field(default_factory=lambda: {
        "ema": True, "vwap": True, "rsi": False, "fib": False, "swings": False, "lines": True, "aiLines": True,
        "sessions": False, "volume": True})
    height_pct: int = Field(40, ge=20, le=80)          # chart share of the window (wide screens)
    # look of ALL dotted/dashed lines: VWAP, Fibonacci, zones, drafts, last price (library presets;
    # spacing can't be set in pixels)
    dotted_style: Literal["solid", "dotted", "sparse_dotted", "dashed", "large_dashed"] = "sparse_dotted"
    vwap_width: int = Field(2, ge=1, le=4)
    color_scheme: Literal["kr", "us"] = "kr"     # kr: up = red, down = blue (as in Toss); us: green / red
    dotted_opacity: int = Field(45, ge=10, le=100)   # % strength of dotted/dashed lines (lower = fainter)
    solid_width: int = Field(1, ge=1, le=3)          # px: plan/avg/AI lines and trendlines
    zone_fill_pct: int = Field(8, ge=0, le=30)       # % tint between a zone's two lines (0 = off)
    grid: bool = False

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, v: Any) -> Any:
        if isinstance(v, dict) and "vwap_style" in v:      # setting saved before it applied to all lines
            v = dict(v)
            v["dotted_style"] = v.pop("vwap_style")         # the saved choice wins over the new default
        return v

    @field_validator("toggles")
    @classmethod
    def _known(cls, v: dict[str, bool]) -> dict[str, bool]:
        unknown = set(v) - set(TOGGLES)
        if unknown:
            raise ValueError(f"unknown chart toggles: {', '.join(sorted(unknown))}")
        return {k: bool(v.get(k, False)) for k in TOGGLES}


class Tickers(_S):
    default: str = "SOXS"
    watchlist: list[str] = Field(default_factory=list, max_length=30)
    related: dict[str, list[str]] = Field(default_factory=dict)

    _d = field_validator("default")(classmethod(lambda cls, v: _ticker(v)))

    @field_validator("watchlist")
    @classmethod
    def _wl(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(_ticker(x) for x in v))

    @field_validator("related")
    @classmethod
    def _rel(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        if len(v) > 50:
            raise ValueError("at most 50 tickers with related lists")
        out = {}
        for k, lst in v.items():
            if len(lst) > 20:
                raise ValueError(f"at most 20 related tickers for {k}")
            out[_ticker(k)] = list(dict.fromkeys(_ticker(x) for x in lst))
        return out


class Orderbook(_S):
    spread_warn_pct: Decimal = Field(Decimal("0.20"), gt=0, le=10)


class Orders(_S):
    price_band_pct: Decimal = Field(Decimal("3"), ge=Decimal("0.1"), le=50)
    draft_ttl_seconds: int = Field(180, ge=30, le=3600)
    max_order_usd: Decimal | None = Field(None, gt=0)
    max_day_usd: Decimal | None = Field(None, gt=0)
    max_orders_per_day: int | None = Field(None, gt=0, le=1000)
    allow_market_outside_regular: bool = True
    rule_max_buy_pct: Decimal | None = Field(None, gt=0, le=100)   # automatic rules: max % of buying power per buy


class Ai(_S):
    default_tier: Literal["normal", "deep", "very_deep"] = "normal"
    web_search_default: bool = False
    daily_usd: Decimal = Field(Decimal("5"), gt=0, le=10000)
    monthly_usd: Decimal = Field(Decimal("100"), gt=0, le=100000)
    raw_candles: bool = True
    memory_turns: int = Field(5, ge=1, le=20)    # earlier chat turns the AI sees; older ones are left out
    quick_prompt: str = Field("", max_length=4000)  # chat quick-analysis button; empty = built-in text (UI only)
    risk_profile: Literal["standard", "aggressive"] = "standard"   # how the Desk chat frames risk


class News(_S):
    max_age_hours: int = Field(48, ge=1, le=168)
    tag_enabled: bool = True


class UserEvent(_S):
    """One of your own events (Settings -> My events): elections, meetings, anything without a feed."""

    name: str = Field(..., min_length=1, max_length=80)
    time: str = Field(..., description="YYYY-MM-DD (date only) or ISO 8601 with a UTC offset")
    note: str | None = Field(None, max_length=200)

    @field_validator("time")
    @classmethod
    def _when(cls, v: str) -> str:
        v = v.strip()
        if len(v) == 10:
            date.fromisoformat(v)                 # ValueError -> validation error
            return v
        parse_iso_with_offset(v)                  # needs an offset: no timezone guessing
        return v


class MyEvents(_S):
    items: list[UserEvent] = Field(default_factory=list, max_length=100)


class UserSettings(_S):
    general: General = General()
    chart: Chart = Chart()
    tickers: Tickers = Tickers()
    orderbook: Orderbook = Orderbook()
    orders: Orders = Orders()
    ai: Ai = Ai()
    news: News = News()
    events: MyEvents = MyEvents()


class SettingsError(ValueError):
    """Invalid settings (message is safe to show)."""


def defaults_from(cfg: AppConfig) -> UserSettings:
    """Today's effective values from config.yaml (what the page shows before anything is saved)."""
    llm = cfg.llm
    return UserSettings(
        tickers=Tickers(default=cfg.tickers.default, watchlist=[s for s in cfg.tickers.compare],
                        related=cfg.news.related),
        orderbook=Orderbook(spread_warn_pct=cfg.spread.warn_pct),
        orders=Orders(price_band_pct=cfg.orders.price_band_pct, draft_ttl_seconds=cfg.orders.draft_ttl_seconds,
                      max_order_usd=cfg.orders.max_order_usd, max_day_usd=cfg.orders.max_day_usd,
                      max_orders_per_day=cfg.orders.max_orders_per_day,
                      allow_market_outside_regular=cfg.orders.allow_market_outside_regular,
                      rule_max_buy_pct=cfg.orders.rule_max_buy_pct),
        ai=Ai(web_search_default=llm.web_search.default_on if llm else False, daily_usd=cfg.budget.daily_usd,
              monthly_usd=cfg.budget.monthly_usd, raw_candles=bool(llm and llm.candles.mode == "B"),
              memory_turns=llm.memory_turns if llm else 5,
              risk_profile=llm.risk_profile if llm else "standard"),
        news=News(max_age_hours=int(cfg.news.max_age_hours), tag_enabled=cfg.news.tag_enabled),
    )


def _errors(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())


def merge(base: UserSettings, patch: Any) -> UserSettings:
    """base + a (partial) dict from the page -> validated settings, or SettingsError."""
    if not isinstance(patch, dict):
        raise SettingsError("settings must be an object")
    data = base.model_dump(mode="json")
    for section, values in patch.items():
        if section not in data:
            raise SettingsError(f"unknown settings section: {section}")
        if not isinstance(values, dict):
            raise SettingsError(f"{section} must be an object")
        data[section] = {**data[section], **values}
    try:
        return UserSettings.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(_errors(exc)) from None


class SettingsFile:
    def __init__(self, path: Path = SETTINGS_PATH, old_orders: Path = OLD_ORDER_SETTINGS) -> None:
        self.path = path
        self.old_orders = old_orders

    def saved(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            pass
        try:  # one-time migration of the part-2 order settings
            old = json.loads(self.old_orders.read_text(encoding="utf-8"))
            return {"orders": {k: old[k] for k in ("price_band_pct", "draft_ttl_seconds") if k in old}}
        except (OSError, ValueError, TypeError):
            return {}

    def load(self, defaults: UserSettings) -> UserSettings:
        try:
            return merge(defaults, self.saved())
        except SettingsError:
            return defaults  # a broken file never stops the app; the page shows defaults

    def save(self, s: UserSettings) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(s.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def apply(s: UserSettings, cfg: AppConfig) -> None:
    """Write the settings onto the live config objects (shared by the rest of the backend)."""
    cfg.tickers.default = s.tickers.default
    cfg.tickers.compare = [t for t in s.tickers.watchlist if t != s.tickers.default]
    cfg.news.related = dict(s.tickers.related)
    cfg.news.extra = list(s.tickers.watchlist)
    cfg.news.max_age_hours = float(s.news.max_age_hours)
    cfg.news.tag_enabled = s.news.tag_enabled
    cfg.spread.warn_pct = s.orderbook.spread_warn_pct
    o = cfg.orders
    o.price_band_pct = s.orders.price_band_pct
    o.draft_ttl_seconds = s.orders.draft_ttl_seconds
    o.max_order_usd = s.orders.max_order_usd
    o.max_day_usd = s.orders.max_day_usd
    o.max_orders_per_day = s.orders.max_orders_per_day
    o.allow_market_outside_regular = s.orders.allow_market_outside_regular
    o.rule_max_buy_pct = s.orders.rule_max_buy_pct
    cfg.budget.daily_usd = s.ai.daily_usd
    cfg.budget.monthly_usd = s.ai.monthly_usd
    if cfg.llm is not None:
        cfg.llm.web_search.default_on = s.ai.web_search_default
        cfg.llm.candles.mode = "B" if s.ai.raw_candles else "A"
        cfg.llm.memory_turns = s.ai.memory_turns
        cfg.llm.risk_profile = s.ai.risk_profile
