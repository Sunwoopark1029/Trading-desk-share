"""Toss Securities Open API client (read-only market data).

Only the paths in ALLOWED_PATHS can be called. There is no order, account or
holdings code here, and the X-Tossinvest-Account header is never sent.
Source of truth: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
(pinned copy: docs/specs/toss_openapi.json, spec version 1.2.28, read 2026-11-20)
and https://openapi.tossinvest.com/openapi-docs/overview.md (rate-limit table)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TOKEN_PATH = "/oauth2/token"
WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"  # official AsyncAPI; same bearer token as REST
MAX_TRADES = 50

# (method, path) -> rate-limit group, per the official spec.
ALLOWED_PATHS: dict[tuple[str, str], str] = {
    ("POST", TOKEN_PATH): "AUTH",
    ("GET", "/api/v1/candles"): "MARKET_DATA_CHART",
    ("GET", "/api/v1/prices"): "MARKET_DATA",
    ("GET", "/api/v1/orderbook"): "MARKET_DATA",   # phase 3 (read-only)
    ("GET", "/api/v1/trades"): "MARKET_DATA",      # phase 3 (read-only), today only, max 50
    ("GET", "/api/v1/market-calendar/US"): "MARKET_INFO",
    ("GET", "/api/v1/exchange-rate"): "MARKET_INFO",
    ("GET", "/api/v1/stocks/all"): "STOCK_ALL",
    # phase 6 part A (read-only market info; token only, no account header)
    ("GET", "/api/v1/stocks"): "STOCK",                    # basic info, up to 200 symbols
    ("GET", "/api/v1/rankings"): "RANKING",
    ("GET", "/api/v1/sectors"): "SECTOR",
    ("GET", "/api/v1/sectors/rankings"): "SECTOR_RANKING",  # literal path: matched before the {sectorId} patterns
    # phase 4 part 1: account data, READ-ONLY (GET). These send the X-Tossinvest-Account header.
    ("GET", "/api/v1/accounts"): "ACCOUNT",
    ("GET", "/api/v1/buying-power"): "ORDER_INFO",
    ("GET", "/api/v1/holdings"): "ASSET",
    ("GET", "/api/v1/orders"): "ORDER_HISTORY",
    ("GET", "/api/v1/conditional-orders"): "CONDITIONAL_ORDER_HISTORY",
    ("GET", "/api/v1/transactions"): "TRANSACTION",
}

# Read-only paths with an id segment (opaque server ids, URL-quoted before matching).
ID_SEGMENT = r"[A-Za-z0-9\-_.~%]{1,300}"
ALLOWED_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("GET", re.compile(rf"^/api/v1/orders/{ID_SEGMENT}$"), "ORDER_HISTORY"),
    ("GET", re.compile(rf"^/api/v1/conditional-orders/{ID_SEGMENT}$"), "CONDITIONAL_ORDER_HISTORY"),
    # phase 6 part A: sectorId uses the spec's own pattern (no dots or %, so no path tricks).
    # \Z, not $: Python's $ also matches before a trailing newline.
    ("GET", re.compile(r"^/api/v1/sectors/[A-Za-z0-9_-]{1,40}\Z"), "SECTOR"),
    ("GET", re.compile(r"^/api/v1/sectors/[A-Za-z0-9_-]{1,40}/stocks\Z"), "SECTOR"),
    ("GET", re.compile(r"^/api/v1/sectors/[A-Za-z0-9_-]{1,40}/etfs\Z"), "SECTOR"),
)
ACCOUNT_TYPES_OK = ("BROKERAGE",)

# Order WRITES (phase 4). Separate from the read allowlist and OFF unless enable_order_writes() was
# called, which the app does only at startup when config orders.dry_run is false. In DRY RUN these
# paths can never be reached: order_write() raises before any network call.
ORDER_WRITE_ROUTES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"^/api/v1/orders$"), "ORDER"),
    ("POST", re.compile(rf"^/api/v1/orders/{ID_SEGMENT}/modify$"), "ORDER"),
    ("POST", re.compile(rf"^/api/v1/orders/{ID_SEGMENT}/cancel$"), "ORDER"),
    ("POST", re.compile(r"^/api/v1/conditional-orders$"), "CONDITIONAL_ORDER"),
    ("POST", re.compile(rf"^/api/v1/conditional-orders/{ID_SEGMENT}/modify$"), "CONDITIONAL_ORDER"),
    ("DELETE", re.compile(rf"^/api/v1/conditional-orders/{ID_SEGMENT}$"), "CONDITIONAL_ORDER"),
)


def write_group(method: str, path: str) -> str | None:
    for m, pattern, g in ORDER_WRITE_ROUTES:
        if m == method and pattern.match(path):
            return g
    return None


def route_group(method: str, path: str) -> str | None:
    """Rate-limit group if (method, path) is allowed, else None. Nothing that writes is allowed."""
    group = ALLOWED_PATHS.get((method, path))
    if group is not None:
        return group
    for m, pattern, g in ALLOWED_PATTERNS:
        if m == method and pattern.match(path):
            return g
    return None

# Requests per second, from the official overview (rate limits section).
DOCUMENTED_LIMITS: dict[str, float] = {
    "AUTH": 5,
    "MARKET_DATA": 15,
    "MARKET_DATA_CHART": 20,
    "MARKET_INFO": 3,
    "STOCK_ALL": 1,
    "ACCOUNT": 1,                       # accounts list
    "ORDER_INFO": 6,                    # buying power (3/s at 09:00-09:10 KST; we poll far slower)
    "ASSET": 5,                         # holdings
    "ORDER_HISTORY": 5,                 # orders list / detail
    "CONDITIONAL_ORDER_HISTORY": 10,    # conditional orders list / detail
    "TRANSACTION": 5,                   # transactions
    "ORDER": 10,                        # order create / modify / cancel (writes)
    "CONDITIONAL_ORDER": 5,             # conditional create / modify / cancel (writes)
    "STOCK": 5,                         # phase 6: stock basic info
    "RANKING": 5,                       # phase 6: stock rankings
    "SECTOR": 5,                        # phase 6: sector list / detail / stocks / ETFs
    "SECTOR_RANKING": 5,                # phase 6: sector rankings
}

IP_NOT_ALLOWED_MESSAGE = "Register this PC's IP in Toss WTS (Settings > Open API)"
TOKEN_REVOKED_MESSAGE = "token replaced elsewhere (second copy running?)"

SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
_CODE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
INTERVALS = ("1m", "1d")
MARKETS = ("KOSPI", "KOSDAQ", "NYSE", "NASDAQ", "AMEX", "KR_ETC", "US_ETC")
MAX_COUNT = 200
MAX_SYMBOLS = 200
# phase 6 part A request enums, exactly as in the spec (1.2.28). Response enums are NOT checked: the spec
# says clients must accept unknown values, so parsed fields keep whatever string arrives.
MARKET_COUNTRIES = ("KR", "US")
RANKING_TYPES = ("MARKET_TRADING_AMOUNT", "MARKET_TRADING_VOLUME", "TOP_GAINERS", "TOP_LOSERS",
                 "TOSS_SECURITIES_TRADING_AMOUNT", "TOSS_SECURITIES_TRADING_VOLUME")
RANKING_DURATIONS = ("realtime", "1d", "1w", "1mo", "3mo", "6mo", "1y")
NO_REALTIME_TYPES = ("TOP_GAINERS", "TOP_LOSERS")   # spec: 400 unsupported-ranking-duration
MAX_RANKING_COUNT = 100
SECTOR_RANKING_TYPES = ("TOP_GAINERS", "MARKET_TRADING_AMOUNT")
SECTOR_DURATIONS = ("1d", "1w", "1mo", "3mo", "6mo", "1y")
SECTOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}\Z")          # spec pattern; \Z = true end of string
STOCKS_SYMBOLS_RE = re.compile(r"^[A-Za-z0-9.\-]+\Z")   # spec: letters, digits, '.', '-' (comma-joined)


# ---------- errors (status + short code only; never URLs or str(exc)) ----------

class TossError(Exception):
    """Toss call failed. Carries only an HTTP status and a short code."""

    def __init__(self, status: int | None, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(f"toss error status={status} code={code}")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status!r}, code={self.code!r})"


class TossPathNotAllowed(TossError):
    """Path/method is not on the read-only allowlist."""


class TossTokenRevoked(TossError):
    """A newer token was issued elsewhere; no auto reissue until reset_revoked()."""

    user_message = TOKEN_REVOKED_MESSAGE


class TossIpNotAllowed(TossError):
    """This PC's IP is not registered in Toss WTS."""

    user_message = IP_NOT_ALLOWED_MESSAGE


class TossAuthError(TossError):
    """Token could not be issued or was rejected for another reason."""


class TossRateLimited(TossError):
    """Still 429 after one Retry-After retry."""


# ---------- data ----------

@dataclass(frozen=True)
class Candle:
    timestamp: datetime  # 1m: bar END time; 1d: trading day at local midnight
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    currency: str


@dataclass(frozen=True)
class CandlePage:
    candles: list[Candle]  # newest first, as returned
    next_before: datetime | None


@dataclass(frozen=True)
class Price:
    symbol: str
    timestamp: datetime | None
    last_price: Decimal
    currency: str


@dataclass(frozen=True)
class SessionWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class CalendarDay:
    date: date
    day_market: SessionWindow | None
    pre_market: SessionWindow | None
    regular_market: SessionWindow | None
    after_market: SessionWindow | None


@dataclass(frozen=True)
class MarketCalendar:
    today: CalendarDay
    previous_business_day: CalendarDay
    next_business_day: CalendarDay


@dataclass(frozen=True)
class ExchangeRate:
    base_currency: str
    quote_currency: str
    rate: Decimal
    mid_rate: Decimal
    basis_point: Decimal  # meaning for FX cost is UNVERIFIED
    rate_change_type: str | None
    valid_from: datetime
    valid_until: datetime | None


@dataclass(frozen=True)
class ListedStock:
    symbol: str
    name: str
    security_type: str
    is_common_share: bool
    isin_code: str


# ---------- parsing helpers ----------

def _dec(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TossError(200, "parse-error")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise TossError(200, "parse-error") from None


def _ts(value: Any) -> datetime:
    """Parse an ISO 8601 timestamp; the offset must be present (never assumed)."""
    if not isinstance(value, str):
        raise TossError(200, "parse-error")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise TossError(200, "parse-error") from None
    if parsed.tzinfo is None:
        raise TossError(200, "parse-error")
    return parsed


def _ts_opt(value: Any) -> datetime | None:
    return None if value is None else _ts(value)


def _session(raw: Any) -> SessionWindow | None:
    if raw is None:
        return None
    return SessionWindow(start=_ts(raw["startTime"]), end=_ts(raw["endTime"]))


def _day(raw: dict[str, Any]) -> CalendarDay:
    try:
        day = date.fromisoformat(raw["date"])
    except (TypeError, ValueError):
        raise TossError(200, "parse-error") from None
    return CalendarDay(
        date=day,
        day_market=_session(raw.get("dayMarket")),
        pre_market=_session(raw.get("preMarket")),
        regular_market=_session(raw.get("regularMarket")),
        after_market=_session(raw.get("afterMarket")),
    )


def parse_candle_page(result: dict[str, Any]) -> CandlePage:
    candles = [
        Candle(
            timestamp=_ts(c["timestamp"]),
            open=_dec(c["openPrice"]),
            high=_dec(c["highPrice"]),
            low=_dec(c["lowPrice"]),
            close=_dec(c["closePrice"]),
            volume=_dec(c["volume"]),
            currency=str(c["currency"]),
        )
        for c in result["candles"]
    ]
    return CandlePage(candles=candles, next_before=_ts_opt(result.get("nextBefore")))


def parse_prices(result: list[dict[str, Any]]) -> list[Price]:
    return [
        Price(
            symbol=str(p["symbol"]),
            timestamp=_ts_opt(p.get("timestamp")),
            last_price=_dec(p["lastPrice"]),
            currency=str(p["currency"]),
        )
        for p in result
    ]


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    volume: Decimal


@dataclass(frozen=True)
class Orderbook:
    """asks: cheapest first; bids: highest first (per spec). Level count is not documented."""

    timestamp: datetime | None
    currency: str
    asks: tuple[BookLevel, ...]
    bids: tuple[BookLevel, ...]


@dataclass(frozen=True)
class TradeTick:
    price: Decimal
    volume: Decimal
    timestamp: datetime
    currency: str


def _levels(raw: Any) -> tuple[BookLevel, ...]:
    out = []
    for lv in raw if isinstance(raw, list) else []:
        try:
            out.append(BookLevel(_dec(lv["price"]), _dec(lv["volume"])))
        except (KeyError, TypeError, ValueError, TossError):
            continue  # one bad level never breaks the book
    return tuple(out)


def parse_orderbook(result: dict[str, Any]) -> Orderbook:
    """Same shape for REST `result` and WebSocket `data`."""
    asks = tuple(sorted(_levels(result.get("asks")), key=lambda x: x.price))
    bids = tuple(sorted(_levels(result.get("bids")), key=lambda x: x.price, reverse=True))
    return Orderbook(_ts_opt(result.get("timestamp")), str(result.get("currency") or ""), asks, bids)


def parse_trade(raw: dict[str, Any]) -> TradeTick:
    return TradeTick(_dec(raw["price"]), _dec(raw["volume"]), _ts(raw["timestamp"]), str(raw.get("currency") or ""))


def parse_trades(result: list[dict[str, Any]]) -> list[TradeTick]:
    out = []
    for raw in result if isinstance(result, list) else []:
        try:
            out.append(parse_trade(raw))
        except (KeyError, TypeError, ValueError, TossError):
            continue
    return sorted(out, key=lambda t: t.timestamp)


# ---------- account data (phase 4, read-only) ----------

def _dec_opt(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return _dec(v)


@dataclass(frozen=True)
class Account:
    account_no: str        # personal: never logged in full (mask with masked())
    account_seq: int       # goes into the X-Tossinvest-Account header
    account_type: str

    def masked(self) -> str:
        return ("*" * max(0, len(self.account_no) - 4)) + self.account_no[-4:]


def parse_accounts(result: list[dict[str, Any]]) -> list[Account]:
    return [Account(str(a["accountNo"]), int(a["accountSeq"]), str(a.get("accountType") or ""))
            for a in result]


@dataclass(frozen=True)
class Execution:
    filled_quantity: Decimal | None
    average_filled_price: Decimal | None
    filled_amount: Decimal | None
    commission: Decimal | None
    tax: Decimal | None
    filled_at: datetime | None
    settlement_date: str | None


@dataclass(frozen=True)
class Order:
    order_id: str
    symbol: str
    side: str
    order_type: str
    time_in_force: str | None
    status: str                  # unknown values are kept as-is (docs: accept unknown codes)
    price: Decimal | None        # None for MARKET
    quantity: Decimal | None
    order_amount: Decimal | None
    currency: str
    ordered_at: datetime | None
    canceled_at: datetime | None
    execution: Execution | None


def parse_order(o: dict[str, Any]) -> Order:
    ex = o.get("execution")
    execution = None if not isinstance(ex, dict) else Execution(
        _dec_opt(ex.get("filledQuantity")), _dec_opt(ex.get("averageFilledPrice")), _dec_opt(ex.get("filledAmount")),
        _dec_opt(ex.get("commission")), _dec_opt(ex.get("tax")), _ts_opt(ex.get("filledAt")),
        str(ex["settlementDate"]) if ex.get("settlementDate") else None)
    return Order(str(o["orderId"]), str(o["symbol"]), str(o["side"]), str(o["orderType"]),
                 str(o["timeInForce"]) if o.get("timeInForce") else None, str(o["status"]),
                 _dec_opt(o.get("price")), _dec_opt(o.get("quantity")), _dec_opt(o.get("orderAmount")),
                 str(o.get("currency") or ""), _ts_opt(o.get("orderedAt")), _ts_opt(o.get("canceledAt")), execution)


def parse_order_page(result: Any) -> tuple[list[Order], str | None]:
    """PaginatedOrderResponse {orders, nextCursor, hasNext}; tolerate a bare list."""
    if isinstance(result, list):
        return [parse_order(o) for o in result], None
    orders = [parse_order(o) for o in result["orders"]]
    return orders, (result.get("nextCursor") if result.get("hasNext") else None)


@dataclass(frozen=True)
class ConditionLeg:
    type: str                    # STOP | PROFIT_RATE
    status: str
    order_side: str
    trigger_price: Decimal | None
    target_profit_rate: Decimal | None   # percent
    order_price: Decimal | None
    triggered_order_id: str | None


@dataclass(frozen=True)
class ConditionalOrder:
    conditional_order_id: str
    type: str                    # SINGLE | OCO | OTO
    status: str                  # WATCHING | PAUSED | ORDERING | ORDERED | COMPLETED | EXPIRED (+unknown)
    symbol: str
    market: str
    quantity: Decimal
    order_type: str
    expire_date: str | None
    first: ConditionLeg
    second: ConditionLeg | None
    created_at: datetime | None


def _leg(c: dict[str, Any]) -> ConditionLeg:
    return ConditionLeg(str(c["type"]), str(c["status"]), str(c["orderSide"]), _dec_opt(c.get("triggerPrice")),
                        _dec_opt(c.get("targetProfitRate")), _dec_opt(c.get("orderPrice")),
                        str(c["triggeredOrderId"]) if c.get("triggeredOrderId") else None)


def parse_conditional(o: dict[str, Any]) -> ConditionalOrder:
    second = o.get("second")
    return ConditionalOrder(str(o["conditionalOrderId"]), str(o["type"]), str(o["status"]), str(o["symbol"]),
                            str(o["market"]), _dec(o["quantity"]), str(o["orderType"]),
                            str(o["expireDate"]) if o.get("expireDate") else None, _leg(o["first"]),
                            _leg(second) if isinstance(second, dict) else None, _ts_opt(o.get("createdAt")))


def parse_conditional_page(result: Any) -> tuple[list[ConditionalOrder], str | None]:
    items = [parse_conditional(o) for o in result["conditionalOrders"]]
    return items, (result.get("nextCursor") if result.get("hasNext") else None)


@dataclass(frozen=True)
class HoldingItem:
    symbol: str
    name: str
    market_country: str
    currency: str
    quantity: Decimal
    last_price: Decimal | None
    average_purchase_price: Decimal | None
    market_value: dict[str, Any]
    profit_loss: dict[str, Any]
    daily_profit_loss: dict[str, Any]
    cost: dict[str, Any]


@dataclass(frozen=True)
class Holdings:
    total_purchase_amount: Any
    market_value: Any
    profit_loss: Any
    daily_profit_loss: Any
    items: tuple[HoldingItem, ...]


def parse_holdings(r: dict[str, Any]) -> Holdings:
    items = []
    for it in r.get("items") or []:
        items.append(HoldingItem(str(it["symbol"]), str(it.get("name") or ""), str(it.get("marketCountry") or ""),
                                 str(it.get("currency") or ""), _dec(it["quantity"]), _dec_opt(it.get("lastPrice")),
                                 _dec_opt(it.get("averagePurchasePrice")), it.get("marketValue") or {},
                                 it.get("profitLoss") or {}, it.get("dailyProfitLoss") or {}, it.get("cost") or {}))
    return Holdings(r.get("totalPurchaseAmount"), r.get("marketValue"), r.get("profitLoss"),
                    r.get("dailyProfitLoss"), tuple(items))


def parse_calendar(result: dict[str, Any]) -> MarketCalendar:
    return MarketCalendar(
        today=_day(result["today"]),
        previous_business_day=_day(result["previousBusinessDay"]),
        next_business_day=_day(result["nextBusinessDay"]),
    )


def parse_exchange_rate(result: dict[str, Any]) -> ExchangeRate:
    return ExchangeRate(
        base_currency=str(result["baseCurrency"]),
        quote_currency=str(result["quoteCurrency"]),
        rate=_dec(result["rate"]),
        mid_rate=_dec(result["midRate"]),
        basis_point=_dec(result["basisPoint"]),
        rate_change_type=result.get("rateChangeType"),
        valid_from=_ts(result["validFrom"]),
        valid_until=_ts_opt(result.get("validUntil")),
    )


def parse_stocks(result: list[dict[str, Any]]) -> list[ListedStock]:
    return [
        ListedStock(
            symbol=str(s["symbol"]),
            name=str(s["name"]),
            security_type=str(s["securityType"]),
            is_common_share=bool(s["isCommonShare"]),
            isin_code=str(s["isinCode"]),
        )
        for s in result
    ]


# ---------- phase 6 part A: rankings, sectors, stock basics (read-only) ----------

@dataclass(frozen=True)
class RankingRow:
    rank: int
    symbol: str
    currency: str                  # per row (US rows: USD in the spec example)
    last_price: Decimal
    base_price: Decimal            # TOP_GAINERS / TOP_LOSERS: price at duration start; others: previous close
    change_rate: Decimal | None    # fraction (0.0125 = 1.25%); null when basePrice is 0
    trading_volume: Decimal        # cumulative over duration; Toss-executed only for TOSS_SECURITIES_* types
    trading_amount: Decimal


@dataclass(frozen=True)
class RankingPage:
    ranked_at: datetime | None     # null when rankings is empty
    rows: tuple[RankingRow, ...]


@dataclass(frozen=True)
class Sector:
    sector_id: str
    name: str                      # Korean in the spec examples (no language option)
    depth: int                     # 0 = top level, 1 = middle, 2 = sub
    parent_sector_id: str | None


@dataclass(frozen=True)
class SectorRankingRow:
    rank: int
    sector_id: str
    name: str
    change_rate: Decimal | None    # fraction; spec text: "always has a value, 0 when no data" (schema allows null)
    trading_amount: Decimal | None  # KRW-converted even for US (see currency)
    market_cap: Decimal | None      # sum of this sector's stocks, sub-sectors excluded; KRW-converted
    currency: str


@dataclass(frozen=True)
class SectorDetail:
    sector_id: str
    name: str
    depth: int
    parent_sector_id: str | None
    summary: str | None
    change_rates: tuple[tuple[str, Decimal | None], ...]   # (duration, fraction or None), 1d -> 1y


@dataclass(frozen=True)
class SectorStock:
    symbol: str
    name: str
    volume: Decimal | None          # today, cumulative; null without quotes
    trading_amount: Decimal | None  # today; KRW-converted even for US stocks
    market_cap: Decimal | None      # updated daily; KRW-converted even for US stocks
    currency: str


@dataclass(frozen=True)
class SectorEtf:
    symbol: str
    name: str                       # the spec gives symbol and name only (prices via /prices)


@dataclass(frozen=True)
class StockInfo:
    symbol: str
    name: str                       # Korean name
    english_name: str | None        # schema says required, but the spec's own ETF example omits it
    isin_code: str
    market: str
    security_type: str
    is_common_share: bool
    status: str                     # SCHEDULED / ACTIVE / DELISTED (unknown values kept)
    currency: str
    list_date: date | None          # KST date
    delist_date: date | None
    shares_outstanding: Decimal     # meaning for ADRs UNVERIFIED
    leverage_factor: Decimal | None  # ETF / ETN only (1, 2, -1 ...)


def _str_opt(v: Any) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise TossError(200, "parse-error")
    return v


def _int(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise TossError(200, "parse-error")
    return v


def _date_opt(v: Any) -> date | None:
    if v is None:
        return None
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        raise TossError(200, "parse-error") from None


def parse_rankings(result: dict[str, Any]) -> RankingPage:
    rows = tuple(
        RankingRow(rank=_int(r["rank"]), symbol=str(r["symbol"]), currency=str(r["currency"]),
                   last_price=_dec(r["price"]["lastPrice"]), base_price=_dec(r["price"]["basePrice"]),
                   change_rate=_dec_opt(r["price"].get("changeRate")),
                   trading_volume=_dec(r["tradingVolume"]), trading_amount=_dec(r["tradingAmount"]))
        for r in result["rankings"]
    )
    return RankingPage(ranked_at=_ts_opt(result.get("rankedAt")), rows=rows)


def parse_sectors(result: dict[str, Any]) -> list[Sector]:
    return [Sector(sector_id=str(x["sectorId"]), name=str(x["name"]), depth=_int(x["depth"]),
                   parent_sector_id=_str_opt(x.get("parentSectorId")))
            for x in result["sectors"]]


def parse_sector_rankings(result: dict[str, Any]) -> list[SectorRankingRow]:
    return [SectorRankingRow(rank=_int(x["rank"]), sector_id=str(x["sectorId"]), name=str(x["name"]),
                             change_rate=_dec_opt(x.get("changeRate")), trading_amount=_dec_opt(x.get("tradingAmount")),
                             market_cap=_dec_opt(x.get("marketCapitalization")), currency=str(x["currency"]))
            for x in result["rankings"]]


def parse_sector_detail(result: dict[str, Any]) -> SectorDetail:
    rates = tuple((str(c["duration"]), _dec_opt(c.get("changeRate")))
                  for c in result["performance"]["changeRates"])
    return SectorDetail(sector_id=str(result["sectorId"]), name=str(result["name"]), depth=_int(result["depth"]),
                        parent_sector_id=_str_opt(result.get("parentSectorId")), summary=_str_opt(result.get("summary")),
                        change_rates=rates)


def parse_sector_stocks(result: dict[str, Any]) -> list[SectorStock]:
    return [SectorStock(symbol=str(x["symbol"]), name=str(x["name"]), volume=_dec_opt(x.get("volume")),
                        trading_amount=_dec_opt(x.get("tradingAmount")),
                        market_cap=_dec_opt(x.get("marketCapitalization")), currency=str(x["currency"]))
            for x in result["stocks"]]


def parse_sector_etfs(result: dict[str, Any]) -> list[SectorEtf]:
    return [SectorEtf(symbol=str(x["symbol"]), name=str(x["name"])) for x in result["etfs"]]


def parse_stock_infos(result: list[dict[str, Any]]) -> list[StockInfo]:
    if not isinstance(result, list):
        raise TossError(200, "parse-error")
    out = []
    for x in result:
        common = x["isCommonShare"]
        if not isinstance(common, bool):
            raise TossError(200, "parse-error")
        out.append(StockInfo(
            symbol=str(x["symbol"]), name=str(x["name"]), english_name=_str_opt(x.get("englishName")),
            isin_code=str(x["isinCode"]), market=str(x["market"]), security_type=str(x["securityType"]),
            is_common_share=common, status=str(x["status"]), currency=str(x["currency"]),
            list_date=_date_opt(x.get("listDate")), delist_date=_date_opt(x.get("delistDate")),
            shares_outstanding=_dec(x["sharesOutstanding"]), leverage_factor=_dec_opt(x.get("leverageFactor"))))
    return out


def _query_string(params: dict[str, Any]) -> str:
    """Percent-encode query values; '+' always becomes %2B (spec requirement)."""
    def text(v: Any) -> str:
        return ("true" if v else "false") if isinstance(v, bool) else str(v)   # JSON-style booleans

    return "&".join(
        f"{quote(k, safe='')}={quote(text(v), safe=',')}" for k, v in params.items()
    )


def _error_code(body: Any, status: int) -> str:
    """Short error code from the Toss envelope or the OAuth2 error format."""
    code: Any = None
    if isinstance(body, dict):
        err = body.get("error")
        code = err.get("code") if isinstance(err, dict) else err
    if isinstance(code, str) and _CODE_RE.match(code):
        return code
    return f"http-{status}"


# ---------- rate limiter ----------

class RateLimiter:
    """Per-group spacing limiter: at most limit*safety requests per second."""

    def __init__(
        self,
        limits: dict[str, float],
        safety: float = 0.8,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not 0 < safety <= 1:
            raise ValueError("safety must be in (0, 1]")
        self._interval = {g: 1.0 / (rps * safety) for g, rps in limits.items() if rps > 0}
        self._next: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._clock = clock
        self._sleep = sleep

    def interval(self, group: str) -> float:
        return self._interval[group]

    async def acquire(self, group: str) -> None:
        if group not in self._interval:
            raise TossError(None, "unknown-rate-group")
        async with self._lock:
            now = self._clock()
            slot = max(now, self._next.get(group, now))
            self._next[group] = slot + self._interval[group]
        wait = slot - now
        if wait > 0:
            await self._sleep(wait)


# ---------- client ----------

class TossClient:
    """Read-only Toss market-data client with lazy token and rate limiting."""

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        *,
        http: httpx.AsyncClient | None = None,
        group_limits: dict[str, float] | None = None,
        safety: float = 0.8,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        token_margin_s: float = 300.0,
        max_retry_after_s: float = 30.0,
        timeout_s: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.AsyncClient(timeout=timeout_s)
        self._owns_http = http is None
        self._limiter = RateLimiter({**DOCUMENTED_LIMITS, **(group_limits or {})}, safety, clock, sleep)
        self._clock = clock
        self._sleep = sleep
        self._token_margin_s = token_margin_s
        self._max_retry_after_s = max_retry_after_s
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._revoked = False
        self.token_serial = 0  # +1 on every issue: the stream can tell our own renewal from a takeover
        self._order_writes = False  # see ORDER_WRITE_ROUTES; only enable_order_writes() turns this on

    @property
    def revoked(self) -> bool:
        return self._revoked

    def reset_revoked(self) -> None:
        """Allow token issuing again after a token-revoked stop."""
        self._revoked = False
        self._token = None
        self._token_expires_at = 0.0

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ----- low level -----

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        token: str | None = None,
        account: int | None = None,
    ) -> httpx.Response:
        """One HTTP attempt with allowlist, rate limit and one 429 retry."""
        group = route_group(method, path)
        if group is None:
            raise TossPathNotAllowed(None, "path-not-allowed")
        url = self._base_url + path
        if params:
            url += "?" + _query_string(params)
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if account is not None:  # account endpoints only (phase 4)
            headers["X-Tossinvest-Account"] = str(int(account))
        for attempt in range(2):
            await self._limiter.acquire(group)
            failed = False
            try:
                resp = await self._http.request(method, url, headers=headers, data=form)
            except httpx.HTTPError:
                failed = True
            if failed:  # raised outside the except block: no chained httpx error
                raise TossError(None, "transport")
            if resp.status_code != 429 or attempt == 1:
                break
            await self._sleep(self._retry_after(resp))
        if resp.status_code == 429:
            raise TossRateLimited(429, "rate-limit-exceeded")
        return resp

    def _retry_after(self, resp: httpx.Response) -> float:
        try:
            wait = float(resp.headers.get("Retry-After", "1"))
        except ValueError:
            wait = 1.0
        return min(max(wait, 0.0), self._max_retry_after_s)

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return json.loads(resp.content, parse_float=Decimal)
        except ValueError:
            return None

    async def _issue_token(self) -> None:
        resp = await self._send(
            "POST",
            TOKEN_PATH,
            form={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        body = self._json(resp)
        if resp.status_code != 200:
            code = _error_code(body, resp.status_code)
            if resp.status_code == 403:
                raise TossIpNotAllowed(403, code)
            raise TossAuthError(resp.status_code, code)
        try:
            token = body["access_token"]
            expires_in = float(body["expires_in"])
        except (TypeError, KeyError, ValueError):
            raise TossAuthError(200, "bad-token-response") from None
        if not isinstance(token, str) or not token:
            raise TossAuthError(200, "bad-token-response")
        self._token = token
        self._token_expires_at = self._clock() + expires_in
        self.token_serial += 1

    async def _get_token(self) -> str:
        async with self._token_lock:
            if self._revoked:
                raise TossTokenRevoked(401, "token-revoked")
            near_expiry = self._clock() >= self._token_expires_at - self._token_margin_s
            if self._token is None or near_expiry:
                self._token = None
                await self._issue_token()
            assert self._token is not None
            return self._token

    async def _drop_token(self, used: str) -> None:
        async with self._token_lock:
            if self._token == used:
                self._token = None

    @property
    def order_writes_enabled(self) -> bool:
        return self._order_writes

    def enable_order_writes(self) -> None:
        """Called ONLY at startup when config orders.dry_run is false (real orders)."""
        self._order_writes = True

    async def order_write(self, method: str, path: str, body: dict[str, Any] | None,
                          account: int) -> tuple[int, Any]:
        """One order write (no automatic retry here). Returns (HTTP status, `result` or None).

        Raises TossPathNotAllowed while writes are off (DRY RUN), TossError(status, code) on errors.
        """
        if not self._order_writes:
            raise TossPathNotAllowed(None, "order-writes-disabled")
        group = write_group(method, path)
        if group is None:
            raise TossPathNotAllowed(None, "path-not-allowed")
        token = await self._get_token()
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}",
                   "X-Tossinvest-Account": str(int(account))}
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body, separators=(",", ":"))
        await self._limiter.acquire(group)
        failed = False
        try:
            resp = await self._http.request(method, self._base_url + path, headers=headers, content=content)
        except httpx.HTTPError:
            failed = True
        if failed:
            raise TossError(None, "transport")
        data = self._json(resp)
        if resp.status_code in (200, 201, 204):
            return resp.status_code, (data.get("result") if isinstance(data, dict) else None)
        code = _error_code(data, resp.status_code)
        if resp.status_code == 401 and code == "token-revoked":
            self._revoked = True
            self._token = None
            raise TossTokenRevoked(401, code)
        if resp.status_code == 403 and code in ("ip-not-allowed", "access_denied"):
            raise TossIpNotAllowed(403, code)
        raise TossError(resp.status_code, code)

    async def stream_token(self) -> tuple[str, int]:
        """(token, serial) for the WebSocket handshake. Same token as REST; never logged."""
        token = await self._get_token()
        return token, self.token_serial

    async def drop_stream_token(self, token: str) -> None:
        """The WebSocket handshake got 401 with this token: issue a fresh one next time."""
        await self._drop_token(token)

    async def _api_get(self, path: str, params: dict[str, Any] | None = None,
                       account: int | None = None) -> Any:
        """GET an allowlisted API path and return the `result` of the envelope."""
        if route_group("GET", path) is None:
            raise TossPathNotAllowed(None, "path-not-allowed")
        for attempt in range(2):
            token = await self._get_token()
            resp = await self._send("GET", path, params=params, token=token, account=account)
            body = self._json(resp)
            if resp.status_code == 200:
                if not isinstance(body, dict) or "result" not in body:
                    raise TossError(200, "bad-envelope")
                return body["result"]
            code = _error_code(body, resp.status_code)
            if resp.status_code == 401:
                if code == "token-revoked":
                    self._revoked = True
                    self._token = None
                    raise TossTokenRevoked(401, code)
                if code in ("expired-token", "invalid-token") and attempt == 0:
                    await self._drop_token(token)
                    continue
                raise TossAuthError(401, code)
            if resp.status_code == 403 and code in ("ip-not-allowed", "access_denied"):
                raise TossIpNotAllowed(403, code)
            raise TossError(resp.status_code, code)
        raise TossAuthError(401, "reissue-failed")  # pragma: no cover

    # ----- market calls -----

    async def candles(
        self,
        symbol: str,
        interval: str,
        count: int = MAX_COUNT,
        before: datetime | str | None = None,
    ) -> CandlePage:
        """One page of candles (newest first). `before` is inclusive per spec."""
        _check_symbol(symbol)
        if interval not in INTERVALS:
            raise TossError(None, "bad-interval")
        if not 1 <= count <= MAX_COUNT:
            raise TossError(None, "bad-count")
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "count": count}
        if before is not None:
            if isinstance(before, datetime):
                if before.tzinfo is None:
                    raise TossError(None, "naive-before")
                before = before.isoformat()
            params["before"] = before
        result = await self._api_get("/api/v1/candles", params)
        return _safe(parse_candle_page, result)

    async def candles_history(
        self,
        symbol: str,
        interval: str,
        total: int,
        before: datetime | str | None = None,
        max_pages: int = 50,
    ) -> list[Candle]:
        """Page back via nextBefore; returns up to `total` unique bars, oldest first."""
        by_ts: dict[datetime, Candle] = {}
        cursor = before
        for _ in range(max_pages):
            page = await self.candles(symbol, interval, MAX_COUNT, cursor)
            before_count = len(by_ts)
            for c in page.candles:
                by_ts.setdefault(c.timestamp, c)
            if (
                len(by_ts) >= total
                or page.next_before is None
                or len(by_ts) == before_count
            ):
                break
            cursor = page.next_before
        ordered = sorted(by_ts.values(), key=lambda c: c.timestamp)
        return ordered[-total:] if total > 0 else []

    async def orderbook(self, symbol: str) -> Orderbook:
        _check_symbol(symbol)
        result = await self._api_get("/api/v1/orderbook", {"symbol": symbol})
        return _safe(parse_orderbook, result)

    async def trades(self, symbol: str, count: int = MAX_TRADES) -> list[TradeTick]:
        """Today's most recent trades (max 50), oldest first."""
        _check_symbol(symbol)
        if not 1 <= count <= MAX_TRADES:
            raise TossError(None, "bad-count")
        result = await self._api_get("/api/v1/trades", {"symbol": symbol, "count": count})
        return _safe(parse_trades, result)

    # ----- account data (phase 4, read-only) -----

    async def accounts(self) -> list[Account]:
        return _safe(parse_accounts, await self._api_get("/api/v1/accounts"))

    async def buying_power(self, account: int, currency: str = "USD") -> Decimal:
        if currency not in ("USD", "KRW"):
            raise TossError(None, "bad-currency")
        result = await self._api_get("/api/v1/buying-power", {"currency": currency}, account=account)
        return _safe(lambda r: _dec(r["cashBuyingPower"]), result)

    async def holdings(self, account: int) -> Holdings:
        return _safe(parse_holdings, await self._api_get("/api/v1/holdings", None, account=account))

    async def open_orders(self, account: int) -> list[Order]:
        """All open orders (the docs say limit/cursor are ignored for OPEN)."""
        result = await self._api_get("/api/v1/orders", {"status": "OPEN"}, account=account)
        return _safe(lambda r: parse_order_page(r)[0], result)

    async def closed_orders(self, account: int, *, date_from: date | None = None, date_to: date | None = None,
                            cursor: str | None = None, limit: int = 100) -> tuple[list[Order], str | None]:
        params: dict[str, Any] = {"status": "CLOSED", "limit": max(1, min(100, limit))}
        if date_from:
            params["from"] = date_from.isoformat()
        if date_to:
            params["to"] = date_to.isoformat()
        if cursor:
            params["cursor"] = cursor
        return _safe(parse_order_page, await self._api_get("/api/v1/orders", params, account=account))

    async def order(self, account: int, order_id: str) -> Order:
        path = "/api/v1/orders/" + quote(order_id, safe="-_.~")
        return _safe(parse_order, await self._api_get(path, None, account=account))

    async def conditional_orders(self, account: int, status: str = "OPEN", cursor: str | None = None,
                                 limit: int = 100) -> tuple[list[ConditionalOrder], str | None]:
        if status not in ("OPEN", "CLOSED"):
            raise TossError(None, "bad-status")
        params: dict[str, Any] = {"status": status, "limit": max(1, min(100, limit))}
        if cursor:
            params["cursor"] = cursor
        result = await self._api_get("/api/v1/conditional-orders", params, account=account)
        return _safe(parse_conditional_page, result)

    async def transactions(self, account: int, *, date_from: date, date_to: date, types: str = "BUY,SELL",
                           cursor: str | None = None, limit: int = 100) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {"transactionTypes": types, "from": date_from.isoformat(),
                                  "to": date_to.isoformat(), "limit": max(1, min(100, limit))}
        if cursor:
            params["cursor"] = cursor
        result = await self._api_get("/api/v1/transactions", params, account=account)
        return _safe(lambda r: (list(r["transactions"]), r.get("nextCursor") if r.get("hasNext") else None), result)

    async def prices(self, symbols: list[str]) -> list[Price]:
        if not 1 <= len(symbols) <= MAX_SYMBOLS:
            raise TossError(None, "bad-symbol-count")
        for s in symbols:
            _check_symbol(s)
        result = await self._api_get("/api/v1/prices", {"symbols": ",".join(symbols)})
        return _safe(parse_prices, result)

    async def market_calendar(self, day: date | None = None) -> MarketCalendar:
        """US market calendar; `day` is a US local date (old dates UNVERIFIED)."""
        params = {"date": day.isoformat()} if day is not None else None
        result = await self._api_get("/api/v1/market-calendar/US", params)
        return _safe(parse_calendar, result)

    async def exchange_rate(self, base: str = "USD", quote_ccy: str = "KRW") -> ExchangeRate:
        result = await self._api_get(
            "/api/v1/exchange-rate", {"baseCurrency": base, "quoteCurrency": quote_ccy}
        )
        return _safe(parse_exchange_rate, result)

    async def stocks_all(self, market: str) -> list[ListedStock]:
        if market not in MARKETS:
            raise TossError(None, "bad-market")
        result = await self._api_get("/api/v1/stocks/all", {"market": market})
        return _safe(parse_stocks, result)

    # ----- phase 6 part A: rankings, sectors, stock basics (read-only, token only) -----

    async def rankings(self, ranking_type: str, duration: str, market_country: str = "US",
                       count: int = MAX_RANKING_COUNT, exclude_investment_caution: bool = False) -> RankingPage:
        """Stock ranking (top `count`, max 100). Rows have no name: use stock basics / the stock list."""
        if ranking_type not in RANKING_TYPES:
            raise TossError(None, "bad-ranking-type")
        if duration not in RANKING_DURATIONS:
            raise TossError(None, "bad-duration")
        if duration == "realtime" and ranking_type in NO_REALTIME_TYPES:
            raise TossError(None, "unsupported-ranking-duration")   # same code the server would return
        _check_market_country(market_country)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_RANKING_COUNT:
            raise TossError(None, "bad-count")
        params: dict[str, Any] = {"type": ranking_type, "marketCountry": market_country, "duration": duration,
                                  "count": count}
        if exclude_investment_caution:
            params["excludeInvestmentCaution"] = True
        return _safe(parse_rankings, await self._api_get("/api/v1/rankings", params))

    async def sectors(self) -> list[Sector]:
        """Whole Toss sector tree (the spec recommends fetching it once a day)."""
        return _safe(parse_sectors, await self._api_get("/api/v1/sectors"))

    async def sector_rankings(self, ranking_type: str, duration: str,
                              market_country: str = "US") -> list[SectorRankingRow]:
        """Top 100 sectors (depth 0 and 1 only). Amounts are KRW-converted, also for US."""
        if ranking_type not in SECTOR_RANKING_TYPES:
            raise TossError(None, "bad-ranking-type")
        if duration not in SECTOR_DURATIONS:
            raise TossError(None, "bad-duration")
        _check_market_country(market_country)
        params = {"type": ranking_type, "marketCountry": market_country, "duration": duration}
        return _safe(parse_sector_rankings, await self._api_get("/api/v1/sectors/rankings", params))

    async def sector(self, sector_id: str, market_country: str = "US") -> SectorDetail:
        _check_market_country(market_country)
        path = _sector_path(sector_id)
        return _safe(parse_sector_detail, await self._api_get(path, {"marketCountry": market_country}))

    async def sector_stocks(self, sector_id: str, market_country: str = "US") -> list[SectorStock]:
        """Common stocks of the sector incl. sub-sectors, by market cap (no prices in this response)."""
        _check_market_country(market_country)
        path = _sector_path(sector_id) + "/stocks"
        return _safe(parse_sector_stocks, await self._api_get(path, {"marketCountry": market_country}))

    async def sector_etfs(self, sector_id: str, market_country: str = "US") -> list[SectorEtf]:
        """ETFs classified in this sector (sub-sectors not included): symbol and name only."""
        _check_market_country(market_country)
        path = _sector_path(sector_id) + "/etfs"
        return _safe(parse_sector_etfs, await self._api_get(path, {"marketCountry": market_country}))

    async def stock_infos(self, symbols: list[str]) -> list[StockInfo]:
        """Basic info for up to 200 symbols (names, market, type, shares outstanding, leverage)."""
        if not isinstance(symbols, (list, tuple)) or not 1 <= len(symbols) <= MAX_SYMBOLS:
            raise TossError(None, "bad-symbol-count")
        for sym in symbols:
            if not isinstance(sym, str) or not STOCKS_SYMBOLS_RE.match(sym):
                raise TossError(None, "bad-symbol")
        return _safe(parse_stock_infos, await self._api_get("/api/v1/stocks", {"symbols": ",".join(symbols)}))


def _safe(parser: Callable[[Any], Any], result: Any) -> Any:
    """Run a parser; malformed payloads become TossError(200, 'parse-error')."""
    try:
        return parser(result)
    except (KeyError, TypeError, AttributeError, ValueError):
        raise TossError(200, "parse-error") from None


def _check_market_country(mc: str) -> None:
    if mc not in MARKET_COUNTRIES:
        raise TossError(None, "bad-market-country")


def _sector_path(sector_id: str) -> str:
    """Sector path for an id matching the spec pattern. "rankings" is a different endpoint, never an id."""
    if not isinstance(sector_id, str) or not SECTOR_ID_RE.match(sector_id) or sector_id == "rankings":
        raise TossError(None, "bad-sector-id")
    return "/api/v1/sectors/" + sector_id


def _check_symbol(symbol: str) -> None:
    if not isinstance(symbol, str) or not SYMBOL_RE.match(symbol):
        raise TossError(None, "bad-symbol")
