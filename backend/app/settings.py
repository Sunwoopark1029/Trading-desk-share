"""Settings: config.yaml (non-secret) and .env (keys, as SecretStr). Never print key values."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from app.timeutil import parse_iso_with_offset

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"        # app data: positions, overlays, settings, watchlist (gitignored)
CACHE_DIR = DATA_DIR / "cache"       # disposable downloads (EDGAR, sector index, ...)
CONFIG_PATH = REPO_ROOT / "config.yaml"
LOCAL_CONFIG_PATH = DATA_DIR / "config.local.yaml"   # optional, gitignored: this machine's overrides
TOSS_HOST = "https://openapi.tossinvest.com"  # the only documented host
ENV_PATH = REPO_ROOT / ".env"
SECRET_NAMES = ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "FRED_API_KEY", "ANTHROPIC_API_KEY",
                "FINNHUB_API_KEY", "MASSIVE_API_KEY", "SEC_USER_AGENT")
OPTIONAL_SECRETS = ("MASSIVE_API_KEY", "SEC_USER_AGENT")  # not listed as a problem when empty


class ConfigError(Exception):
    """config.yaml is missing or invalid (message is safe to show)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TickersConfig(_Strict):
    default: str = "SOXS"
    compare: list[str] = ["SOXX"]
    strip: list[str] = ["SOXS", "SOXX"]


class PollingConfig(_Strict):
    quotes_seconds: float = Field(5, gt=0)
    candles_seconds: float = Field(20, gt=0)
    chart_seconds: float = Field(2, gt=0)   # how often the screen redraws the chart
    fx_seconds: float = Field(60, gt=0)
    calendar_seconds: float = Field(3600, gt=0)
    stock_list_seconds: float = Field(86400, gt=0)
    yfinance_seconds: float = Field(300, gt=0)
    fred_seconds: float = Field(21600, gt=0)
    stale_after_factor: float = Field(3, gt=0)


class TossConfig(_Strict):
    base_url: str = TOSS_HOST
    rate_limit_safety: float = Field(0.8, gt=0, le=1)
    rate_limits_per_second: dict[str, float] = {
        "AUTH": 5, "MARKET_DATA": 15, "MARKET_DATA_CHART": 20, "MARKET_INFO": 3, "STOCK_ALL": 1,
    }
    timeout_seconds: float = Field(10, gt=0)
    token_refresh_margin_seconds: float = Field(300, ge=0)

    @field_validator("base_url")
    @classmethod
    def _documented_host_only(cls, v: str) -> str:
        """Keys are sent to this host, so only the documented HTTPS host is allowed."""
        if v.rstrip("/") != TOSS_HOST:
            raise ValueError(f"toss.base_url must be {TOSS_HOST}")
        return TOSS_HOST


class YFinanceConfig(_Strict):
    symbols: list[str] = ["NQ=F", "ES=F", "^ICESEMI", "^VIX", "^TNX", "KRW=X"]
    delay_minutes: dict[str, int] = {}
    backoff_start_seconds: float = Field(2, gt=0)
    backoff_max_seconds: float = Field(900, gt=0)
    tnx_vs_fred_warn_points: Decimal = Decimal("1")


class ResearchConfig(_Strict):
    """Phase 6 part B: Company view (fundamentals joined per ticker). Fetched when a company is opened."""

    years: int = Field(5, ge=1, le=10)                    # fiscal years shown (D3); one more is read for 5y growth
    quarters: int = Field(8, ge=0, le=16)                 # quarters shown (D3)
    peers: int = Field(5, ge=1, le=10)                    # top N by market cap in the company's Toss sector (D7)
    sector_index_hours: float = Field(24, ge=1)           # stock -> Toss sector map, rebuilt once a day
    profile_ttl_seconds: float = Field(86400, ge=60)      # Finnhub profile
    earnings_ttl_seconds: float = Field(21600, ge=60)     # Finnhub earnings / calendar / ratings, yfinance targets
    history_ttl_seconds: float = Field(86400, ge=60)      # Toss daily candles for fiscal-year-end prices
    peers_ttl_seconds: float = Field(3600, ge=60)         # peer metrics (prices move; statements are cached anyway)
    news_ttl_seconds: float = Field(900, ge=60)
    news_days: int = Field(30, ge=1, le=90)


class EdgarConfig(_Strict):
    """Phase 6 part B: SEC EDGAR (official, free). SEC allows 10 requests/second per user; we use half."""

    requests_per_second: float = Field(5, gt=0, le=10)
    facts_ttl_hours: float = Field(24, gt=0)       # companyfacts JSON cached on disk (data/cache/edgar/)
    tickers_ttl_hours: float = Field(24, gt=0)     # SEC ticker -> CIK map
    cooldown_minutes: float = Field(10, ge=1)      # after a 403 / 429: SEC says access resumes after 10 min below the limit
    timeout_seconds: float = Field(30, gt=0)


class FredConfig(_Strict):
    series_id: str = "DGS10"
    limit: int = Field(5, gt=0)


class PnlConfig(_Strict):
    fee_rate: Decimal = Field(Decimal("0.001"), ge=0)
    fee_free_below_usd: Decimal = Field(Decimal("10"), ge=0)
    fx_spread_bp: Decimal | None = None


class EventConfig(_Strict):
    name: str
    time: datetime
    note: str | None = None

    @field_validator("time", mode="before")
    @classmethod
    def _explicit_offset(cls, v: Any) -> datetime:
        if isinstance(v, datetime):
            if v.tzinfo is None or v.utcoffset() is None:
                raise ValueError("event time needs a UTC offset, e.g. 2026-12-10T08:30:00-05:00")
            return v
        if isinstance(v, str):
            return parse_iso_with_offset(v)
        raise ValueError("event time must be an ISO 8601 string with a UTC offset")


class ModelPrice(_Strict):
    """USD per million tokens. A long-prompt tier (e.g. Haiku 5.5 over 100k) is optional."""

    input: Decimal = Field(..., ge=0)
    output: Decimal = Field(..., ge=0)
    cache_write_5m: Decimal = Field(..., ge=0)
    cache_write_1h: Decimal = Field(..., ge=0)
    cache_read: Decimal = Field(..., ge=0)
    long_prompt_over_tokens: int | None = Field(None, gt=0)
    long_prompt: ModelPrice | None = None

    @model_validator(mode="after")
    def _tier_pair(self) -> ModelPrice:
        if (self.long_prompt_over_tokens is None) != (self.long_prompt is None):
            raise ValueError("long_prompt_over_tokens and long_prompt must be set together")
        return self


TierName = Literal["normal", "deep", "very_deep", "background"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class TierConfig(_Strict):
    model: str = Field(..., pattern=r"^[a-z0-9][a-z0-9.\-]{2,63}$")
    label: str
    effort: Effort = "medium"
    max_tokens: int = Field(16000, gt=0, le=128000)


class WebSearchConfig(_Strict):
    default_on: bool = False
    max_uses_per_message: int = Field(3, ge=1, le=20)
    usd_per_1000: Decimal = Field(Decimal("10"), ge=0)
    allowed_domains: list[str] = []
    blocked_domains: list[str] = []

    @model_validator(mode="after")
    def _not_both(self) -> WebSearchConfig:
        if self.allowed_domains and self.blocked_domains:
            raise ValueError("set allowed_domains or blocked_domains, not both")
        return self


class CandlesConfig(_Strict):
    mode: Literal["A", "B"] = "A"   # A = levels summary only, B = + raw candles
    last_1m_bars: int = Field(60, ge=0, le=400)


class LlmConfig(_Strict):
    tiers: dict[TierName, TierConfig]
    prices_usd_per_mtok: dict[str, ModelPrice]
    cache_ttl_static: Literal["5m", "1h"] = "1h"
    risk_profile: Literal["standard", "aggressive"] = "standard"   # chat tone; Settings -> AI (user_settings)
    refusal_fallback: bool = True
    summarize_after_tokens: int = Field(60000, gt=1000)   # no longer used (memory_turns replaced summaries)
    keep_recent_turns: int = Field(6, ge=1, le=50)        # no longer used
    memory_turns: int = Field(5, ge=1, le=20)             # earlier turns the AI sees (Settings page)
    candles: CandlesConfig = CandlesConfig()
    web_search: WebSearchConfig = WebSearchConfig()

    @model_validator(mode="after")
    def _complete(self) -> LlmConfig:
        missing = {"normal", "deep", "very_deep", "background"} - set(self.tiers)
        if missing:
            raise ValueError(f"llm.tiers is missing: {', '.join(sorted(missing))}")
        unpriced = sorted({t.model for t in self.tiers.values()} - set(self.prices_usd_per_mtok))
        if unpriced:
            raise ValueError(f"llm.prices_usd_per_mtok has no price for: {', '.join(unpriced)}")
        return self


class BudgetConfig(_Strict):
    daily_usd: Decimal = Field(Decimal("5"), gt=0)
    monthly_usd: Decimal = Field(Decimal("100"), gt=0)
    warn_at: Decimal = Field(Decimal("0.8"), gt=0, lt=1)


class MarketStreamConfig(_Strict):
    enabled: bool = True                     # Toss WebSocket for orderbook + trades
    ping_seconds: float = Field(60, gt=0, le=170)   # server closes after 180 s of silence
    backoff_start_seconds: float = Field(1, gt=0)
    backoff_max_seconds: float = Field(60, gt=0)
    rest_poll_seconds: float = Field(2, gt=0)       # REST fallback while the stream is down
    push_per_second: float = Field(4, gt=0, le=20)  # screen updates per second (max)


class SpreadConfig(_Strict):
    warn_pct: Decimal = Field(Decimal("0.20"), gt=0)   # spread wider than this % -> orange


class NewsConfig(_Strict):
    enabled: bool = True
    poll_seconds: float = Field(300, ge=60)
    max_age_hours: float = Field(48, gt=0)
    related: dict[str, list[str]] = {}       # optional extras per ticker, e.g. SOXS: [NVDA, AMD]
    extra: list[str] = []                    # always watched
    max_symbols: int = Field(20, ge=1, le=60)
    finnhub_min_interval_seconds: float = Field(1.1, gt=0)   # documented cap is 30/s; stay far below
    massive_calls_per_minute: float = Field(5, gt=0)         # UNVERIFIED (phase 0 note); backup only
    tag_batch_size: int = Field(20, ge=1, le=50)
    tag_enabled: bool = True


class OrdersConfig(_Strict):
    """Phase 4. dry_run stays true until the user changes it themselves."""

    dry_run: bool = True                     # true: order endpoints are never called
    account_seq: int | None = None           # only needed if Toss lists several brokerage accounts
    account_refresh_seconds: float = Field(10, ge=3)
    draft_ttl_seconds: int = Field(180, ge=30, le=3600)       # default; adjustable on screen
    price_band_pct: Decimal = Field(Decimal("3"), gt=0, le=50)  # default; adjustable on screen
    max_order_usd: Decimal | None = None     # optional limits: off by user choice 2026-11-17
    max_day_usd: Decimal | None = None
    max_orders_per_day: int | None = None
    allow_market_outside_regular: bool = True   # user choice; the draft shows a thin-book warning
    rule_max_buy_pct: Decimal | None = Field(None, gt=0, le=100)   # automatic rules: a BUY may use at most
                                             # this % of USD buying power (off by user choice 2026-11-20; Settings page)


class RulesConfig(_Strict):
    """Automatic rules (docs/plans/AUTO_RULES_PLAN.md). A rule sends its order only after the user armed it."""

    enabled: bool = True                     # false: no rule can be armed and nothing fires
    max_armed: int = Field(10, ge=1, le=50)
    max_proposed: int = Field(30, ge=1, le=200)
    stale_seconds: float = Field(15, ge=5, le=120)   # no live data for this long: rules pause, old trades ignored
    max_days: int = Field(30, ge=1, le=365)          # longest rule life


class EventCalendarConfig(_Strict):
    """Top strip "Next event" + event list (ROADMAP Data sources: event calendar, 2026-11-21)."""

    enabled: bool = True
    days: int = Field(14, ge=1, le=60)                     # list window
    earnings_days: int = Field(30, ge=7, le=60)            # earnings looked up this far ahead
    big_semis: list[str] = ["NVDA", "AVGO", "AMD", "TSM", "MU", "QCOM", "INTC", "ASML", "ARM", "AMAT", "LRCX",
                            "KLAC", "TXN", "MRVL", "ADI"]  # user choice 2026-11-21: big semiconductors
    top_market_cap: int = Field(30, ge=0, le=100)          # + the top N US stocks by market cap (user choice)
    max_symbols: int = Field(80, ge=1, le=200)             # earnings lookups per refresh (Finnhub, shared gate)
    macro_ttl_hours: float = Field(24, ge=1)
    market_ttl_hours: float = Field(12, ge=1)
    earnings_ttl_hours: float = Field(6, ge=1)


class OptionsConfig(_Strict):
    """Options-implied context (yfinance option chains, ~15 min; ROADMAP Data sources 2026-11-21)."""

    enabled: bool = True
    max_expiries: int = Field(6, ge=1, le=12)              # chains fetched per ticker (1 request each)
    ttl_live_seconds: float = Field(300, ge=60)            # refresh while the regular session is live
    ttl_closed_seconds: float = Field(1800, ge=60)         # refresh otherwise
    use_oi: bool = False                                   # open interest failed the live check: keep off


class RemoteConfig(_Strict):
    """Phase 5: login on every device; access from your phone through Tailscale (private, never public)."""

    allow_remote: bool = False              # false = this computer only: every request from another device is refused
    allowed_logins: list[str] = []          # optional: Tailscale accounts allowed (empty = any device in your tailnet)
    local_session_days: float = Field(30, gt=0, le=365)
    remote_session_hours: float = Field(24, gt=0, le=168)
    max_failed_attempts: int = Field(5, ge=1, le=20)
    lockout_minutes: int = Field(15, ge=1, le=1440)
    orders_enabled: bool = False            # sending orders from the phone: off until you switch it on
    block_market_orders: bool = True        # from the phone: limit orders only


class ExploreConfig(_Strict):
    """Phase 6 part A: Explore views (Toss rankings / sectors). Cached on demand, only while someone looks."""

    rankings_ttl_seconds: float = Field(10, ge=2)        # Toss rankings (RANKING group 5/s)
    sector_list_ttl_seconds: float = Field(86400, ge=60)  # spec: sector tree changes rarely, read once a day
    sector_ttl_seconds: float = Field(60, ge=5)           # detail / stocks / ETFs / their prices
    names_ttl_seconds: float = Field(86400, ge=60)        # stock basics (English names, type), per symbol
    sector_prices_top: int = Field(50, ge=0, le=100)      # last price for the top N stocks and top N ETFs
                                                          # (<= 100 each: one /prices call takes 200 symbols)


class DeployConfig(_Strict):
    """Cloud server plan (docs/plans/CLOUD_PLAN.md). "live" = the copy that trades (as before);
    "workshop" = a development copy: DRY_RUN is forced on and automatic rules are off."""

    role: Literal["live", "workshop"] = "live"


class AppConfig(_Strict):
    tickers: TickersConfig = TickersConfig()
    polling: PollingConfig = PollingConfig()
    toss: TossConfig = TossConfig()
    yfinance: YFinanceConfig = YFinanceConfig()
    fred: FredConfig = FredConfig()
    pnl: PnlConfig = PnlConfig()
    events: list[EventConfig] = []
    llm: LlmConfig | None = None   # None = chat disabled (no llm section in config.yaml)
    budget: BudgetConfig = BudgetConfig()
    market_stream: MarketStreamConfig = MarketStreamConfig()
    spread: SpreadConfig = SpreadConfig()
    news: NewsConfig = NewsConfig()
    orders: OrdersConfig = OrdersConfig()
    rules: RulesConfig = RulesConfig()
    options: OptionsConfig = OptionsConfig()
    event_calendar: EventCalendarConfig = EventCalendarConfig()
    remote: RemoteConfig = RemoteConfig()
    explore: ExploreConfig = ExploreConfig()
    edgar: EdgarConfig = EdgarConfig()
    research: ResearchConfig = ResearchConfig()
    deploy: DeployConfig = DeployConfig()

    @model_validator(mode="after")
    def _workshop_is_safe(self) -> "AppConfig":
        if self.deploy.role == "workshop":   # a development copy never sends and never fires
            self.orders.dry_run = True
            self.rules.enabled = False
        return self


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Keys in `over` replace the same keys in `base`; nested mappings merge, lists are replaced."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ConfigError(f"{path.name} not found") from exc
    try:
        data = YAML(typ="safe", pure=True).load(text) or {}
    except YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a mapping at the top level")
    return data


class Secrets(BaseSettings):
    """Keys from the repo-root .env. Values are SecretStr; never log them."""

    model_config = SettingsConfigDict(env_file=ENV_PATH, env_file_encoding="utf-8", extra="ignore")

    TOSS_CLIENT_ID: SecretStr = SecretStr("")
    TOSS_CLIENT_SECRET: SecretStr = SecretStr("")
    FRED_API_KEY: SecretStr = SecretStr("")
    ANTHROPIC_API_KEY: SecretStr = SecretStr("")
    FINNHUB_API_KEY: SecretStr = SecretStr("")
    MASSIVE_API_KEY: SecretStr = SecretStr("")
    SEC_USER_AGENT: SecretStr = SecretStr("")   # phase 6: "name email" for SEC's required User-Agent (personal)


def load_config(path: Path = CONFIG_PATH, local: Path | None = None) -> AppConfig:
    """Read config.yaml (safe loader), lay the optional local overrides file on top, and validate."""
    data = _read_mapping(path)
    name = path.name
    if local is not None and local.is_file():
        data = _merge(data, _read_mapping(local))
        name = f"{path.name} + {local.name}"
    try:
        return AppConfig.model_validate(data)
    except ValueError as exc:
        raise ConfigError(f"{name} is invalid: {exc}") from exc


def load_secrets(env_file: Path | None = ENV_PATH) -> Secrets:
    """Read keys from env_file (and process environment). Missing values stay empty."""
    return Secrets(_env_file=env_file)


def key_status(secrets: Secrets | None = None) -> dict[str, str]:
    """Name -> "set" | "empty". Never returns values."""
    s = secrets if secrets is not None else get_secrets()
    return {
        name: "set" if getattr(s, name).get_secret_value().strip() else "empty"
        for name in SECRET_NAMES
    }


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return load_config(CONFIG_PATH, LOCAL_CONFIG_PATH)


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return load_secrets()
