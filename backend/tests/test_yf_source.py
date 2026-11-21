"""yfinance strip tests with fixture DataFrames (yfinance.download is monkeypatched)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from app.data import yf_source
from app.data.yf_source import Backoff, YFinanceError, fetch_strip, fetch_strip_async

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def ticker_frame(data: dict[str, list[float]]) -> pd.DataFrame:
    """MultiIndex columns (ticker, field) like group_by='ticker'."""
    idx = pd.DatetimeIndex(["2026-11-11", "2026-11-12", "2026-11-13"], name="Date")
    frames = {}
    for sym, closes in data.items():
        frames[sym] = pd.DataFrame({"Open": closes, "Close": closes}, index=idx)
    return pd.concat(frames, axis=1)


def patch_download(monkeypatch: pytest.MonkeyPatch, df: pd.DataFrame | None, calls: list | None = None) -> None:
    def fake(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return df

    monkeypatch.setattr(yf_source.yfinance, "download", fake)


def test_batched_download_and_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    df = ticker_frame({"NQ=F": [100.0, 101.5, 103.0], "^VIX": [15.0, 16.0, 12.0]})
    patch_download(monkeypatch, df, calls)
    quotes = fetch_strip(["NQ=F", "^VIX"], now=lambda: NOW)
    assert len(calls) == 1
    assert calls[0]["tickers"] == ["NQ=F", "^VIX"]
    assert calls[0]["interval"] == "1d" and calls[0]["period"] == "10d"
    nq, vix = quotes
    assert nq.status == "ok"
    assert nq.last_close == Decimal("103.0") and nq.prev_close == Decimal("101.5")
    assert nq.change == Decimal("1.5")
    assert nq.change_pct == Decimal("1.5") / Decimal("101.5") * 100
    assert nq.bar_date == date(2026, 11, 13)
    assert nq.fetched_at == NOW
    assert vix.change == Decimal("-4.0")


def test_per_symbol_dropna(monkeypatch: pytest.MonkeyPatch) -> None:
    # KRW=X has a value on the last day; ^ICESEMI does not (NaN) -> use its last two real closes.
    df = ticker_frame({"KRW=X": [1400.0, 1410.0, 1420.0], "^ICESEMI": [5000.0, 5100.0, np.nan]})
    patch_download(monkeypatch, df)
    krw, semi = fetch_strip(["KRW=X", "^ICESEMI"], now=lambda: NOW)
    assert krw.last_close == Decimal("1420.0")
    assert semi.last_close == Decimal("5100.0") and semi.prev_close == Decimal("5000.0")
    assert semi.bar_date == date(2026, 11, 12)


def test_missing_symbol_and_too_few_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    df = ticker_frame({"ES=F": [np.nan, np.nan, 6000.0]})
    patch_download(monkeypatch, df)
    es, tnx = fetch_strip(["ES=F", "^TNX"], now=lambda: NOW)
    assert es.status == "missing" and es.last_close is None
    assert tnx.status == "missing"


def test_column_grouped_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    idx = pd.DatetimeIndex(["2026-11-12", "2026-11-13"])
    cols = pd.MultiIndex.from_tuples([("Close", "NQ=F"), ("Close", "ES=F")])
    df = pd.DataFrame([[1.0, 2.0], [1.5, 2.5]], index=idx, columns=cols)
    patch_download(monkeypatch, df)
    nq, es = fetch_strip(["NQ=F", "ES=F"], now=lambda: NOW)
    assert nq.change == Decimal("0.5") and es.last_close == Decimal("2.5")


def test_download_error_hides_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs):
        raise RuntimeError("secret https://example/?key=abc")

    monkeypatch.setattr(yf_source.yfinance, "download", boom)
    with pytest.raises(YFinanceError) as ei:
        fetch_strip(["NQ=F"])
    assert "abc" not in str(ei.value) and "abc" not in repr(ei.value)
    assert ei.value.__context__ is None


def test_empty_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_download(monkeypatch, pd.DataFrame())
    with pytest.raises(YFinanceError) as ei:
        fetch_strip(["NQ=F"])
    assert ei.value.code == "empty"


@pytest.mark.anyio
async def test_async_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_download(monkeypatch, ticker_frame({"^VIX": [1.0, 2.0, 3.0]}))
    quotes = await fetch_strip_async(["^VIX"])
    assert quotes[0].last_close == Decimal("3.0")


def test_backoff_sequence_and_reset() -> None:
    b = Backoff()
    seq = [b.next_delay() for _ in range(11)]
    assert seq == [2, 4, 8, 16, 32, 64, 128, 256, 512, 900, 900]
    b.reset()
    assert b.next_delay() == 2
