"""yfinance fundamentals (phase 6 part B): analyst price targets, and statements only as a last fallback.

yfinance is UNOFFICIAL (scrapes Yahoo, can break without notice): every value from here is labelled
"yfinance · unofficial". Facts from the installed yfinance 1.8.0 source (read 2026-11-21):
- Ticker.analyst_price_targets: a dict built from Yahoo's quoteSummary "financialData" module; keys current, low,
  high, mean, median, only those present. No currency field: the app only lists US tickers, whose Yahoo quote
  currency is USD, so values are shown as USD with that assumption stated.
- Failures are mostly SILENT (config hide_exceptions=True): {} / None / an empty table instead of an exception.
  Empty is treated as "unavailable", never as zero. YFRateLimitError can escape and is caught.
- Results are cached on the Ticker object and HTTP GETs are cached in-process, so a fresh Ticker is made per call.
- income_stmt / quarterly_income_stmt / cashflow / balance_sheet: DataFrames, line items as rows, period-end
  dates as columns (newest first), floats with NaN for missing.
Calls run in a worker thread (yfinance is blocking) behind a backoff after failures.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.data.yf_source import Backoff

log = logging.getLogger("desk.yf_fund")
TARGET_KEYS = ("current", "low", "high", "mean", "median")
STATEMENT_ROWS = {   # yfinance row name -> our line id (only when SEC has nothing for a company)
    "income": {"Total Revenue": "revenue", "Gross Profit": "gross_profit", "Operating Income": "operating_income",
               "Net Income": "net_income", "Diluted EPS": "eps_diluted", "Diluted Average Shares": "shares_diluted"},
    "cashflow": {"Operating Cash Flow": "operating_cash_flow", "Capital Expenditure": "capex"},
    "balance": {"Cash And Cash Equivalents": "cash", "Total Assets": "total_assets",
                "Stockholders Equity": "equity", "Long Term Debt": "long_term_debt"},
}


class YFFundError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"yfinance error code={code}")

    def __repr__(self) -> str:
        return f"YFFundError(code={self.code!r})"


@dataclass(frozen=True)
class PriceTargets:
    current: Decimal | None
    low: Decimal | None
    high: Decimal | None
    mean: Decimal | None
    median: Decimal | None
    currency: str = "USD"                 # assumed (US listing); yfinance returns no currency
    source: str = "yfinance · unofficial"


@dataclass(frozen=True)
class YFStatements:
    lines: dict[str, dict[date, Decimal]]    # line id -> {period end: value}; empty = unavailable
    currency: str | None                     # Yahoo's financialCurrency; None = unknown (shown as such)
    source: str = "yfinance · unofficial"


def _dec(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return Decimal(repr(f))


def parse_targets(raw: Any) -> PriceTargets | None:
    """yfinance dict -> targets; None when nothing usable came back (empty dict / no target keys)."""
    if not isinstance(raw, dict):
        return None
    vals = {k: _dec(raw.get(k)) for k in TARGET_KEYS}
    if all(vals[k] is None for k in ("low", "high", "mean", "median")):
        return None                                  # only the current price, or nothing: no targets
    return PriceTargets(**vals)


def parse_statement(df: Any, rows: dict[str, str]) -> dict[str, dict[date, Decimal]]:
    """yfinance statement DataFrame -> {line id: {period end: value}}; NaN / missing rows skipped."""
    out: dict[str, dict[date, Decimal]] = {}
    if df is None or getattr(df, "empty", True):
        return out
    for name, line in rows.items():
        if name not in df.index:
            continue
        series = df.loc[name]
        vals: dict[date, Decimal] = {}
        for col, v in series.items():
            d = _dec(v)
            when = col.date() if hasattr(col, "date") else None
            if d is not None and isinstance(when, date):
                vals[when] = d
        if vals:
            out[line] = vals
    return out


_SESSION: Any = None


def os_trust_session() -> Any:
    """curl_cffi session that trusts the standard public CA list PLUS the certificates Windows trusts.

    A company security proxy (Zscaler) re-signs Yahoo's traffic with its own certificate, which Windows trusts but
    yfinance's bundled list does not: Yahoo's login-cookie request then fails and targets come back empty (seen
    2026-11-21). Verification stays on; only the trusted list is extended. yfinance shares one session app-wide.
    """
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    import ssl
    import sys

    import certifi
    from curl_cffi import requests as creq

    from app.settings import CACHE_DIR

    pems = [Path(certifi.where()).read_text(encoding="utf-8")]
    if sys.platform == "win32":
        for store in ("ROOT", "CA"):
            for cert, enc, _trust in ssl.enum_certificates(store):   # type: ignore[attr-defined]
                if enc == "x509_asn":
                    pems.append(ssl.DER_cert_to_PEM_cert(cert))
    bundle = CACHE_DIR / "os_trust.pem"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text("\n".join(pems), encoding="utf-8")
    _SESSION = creq.Session(impersonate="chrome", verify=str(bundle))
    return _SESSION


class YFFund:
    def __init__(self, ticker_factory: Callable[[str], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if ticker_factory is None:
            import yfinance

            def ticker_factory(sym: str) -> Any:
                return yfinance.Ticker(sym, session=os_trust_session())
        self._ticker = ticker_factory
        self._clock = clock
        self._backoff = Backoff(initial=60.0, maximum=3600.0)
        self._retry_at = 0.0

    def _ok(self) -> None:
        self._backoff.reset()
        self._retry_at = 0.0

    def _fail(self, code: str) -> YFFundError:
        self._retry_at = self._clock() + self._backoff.next_delay()
        log.info("yfinance fundamentals failed: %s", code)
        return YFFundError(code)

    def _check(self) -> None:
        if self._clock() < self._retry_at:
            raise YFFundError("backing-off")

    async def price_targets(self, symbol: str) -> PriceTargets | None:
        self._check()

        def run() -> Any:
            return self._ticker(symbol).analyst_price_targets      # a fresh Ticker: no stale in-object cache

        try:
            raw = await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001 - YFRateLimitError and friends; never the library message
            raise self._fail("rate-limited" if "RateLimit" in type(exc).__name__ else f"error:{type(exc).__name__}") from None
        self._ok()
        return parse_targets(raw)

    async def statements(self, symbol: str, quarterly: bool = False) -> YFStatements:
        """Fallback statements when SEC has nothing for a company. Empty `lines` = unavailable."""
        self._check()

        def run() -> YFStatements:
            t = self._ticker(symbol)
            out: dict[str, dict[date, Decimal]] = {}
            frames = {"income": t.quarterly_income_stmt if quarterly else t.income_stmt,
                      "cashflow": t.quarterly_cashflow if quarterly else t.cashflow,
                      "balance": t.quarterly_balance_sheet if quarterly else t.balance_sheet}
            for kind, df in frames.items():
                out.update(parse_statement(df, STATEMENT_ROWS[kind]))
            try:                                         # Yahoo's reporting currency; unknown stays None (never
                cur = (t.info or {}).get("financialCurrency")    # an assumed USD)
            except Exception:  # noqa: BLE001
                cur = None
            return YFStatements(out, cur if isinstance(cur, str) and len(cur) == 3 and cur.isalpha() else None)

        try:
            data = await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            raise self._fail(f"error:{type(exc).__name__}") from None
        self._ok()
        return data
