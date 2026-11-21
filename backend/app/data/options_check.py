"""One-off live check of yfinance option chains (read-only; no keys involved).

Run from backend/:  .venv/Scripts/python -m app.data.options_check SOXX SOXS

Prints what the options plan needs confirmed before any calculation uses it:
- expirations list and its format
- per chain: strike count, contract size, currency, how many quotes have bid/ask = 0
- impliedVolatility near the money (raw value: is it a decimal like 0.45 or a percent like 45?)
- last trade time range (how stale the chain is)
- underlying quote fields (price, market state, exchange time zone)
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import yfinance as yf


def check(symbol: str, chains: int = 2) -> None:
    tk = yf.Ticker(symbol)
    exps = tk.options
    print(f"\n=== {symbol}: {len(exps)} expirations; first 8: {list(exps[:8])}")
    for exp in exps[:chains]:
        oc = tk.option_chain(exp)
        u = oc.underlying or {}
        price = u.get("regularMarketPrice")
        print(f"-- expiry {exp}")
        print("   underlying:", {k: u.get(k) for k in (
            "regularMarketPrice", "regularMarketTime", "marketState", "currency", "exchangeTimezoneName",
            "postMarketPrice", "preMarketPrice") if k in u})
        for name, df in (("calls", oc.calls), ("puts", oc.puts)):
            if df is None or df.empty:
                print(f"   {name}: empty")
                continue
            zero_bid = int((df["bid"].fillna(0) <= 0).sum())
            zero_ask = int((df["ask"].fillna(0) <= 0).sum())
            print(f"   {name}: {len(df)} strikes {df['strike'].min()}..{df['strike'].max()}; "
                  f"contractSize={set(df['contractSize'])}; currency={set(df['currency'])}; "
                  f"bid=0: {zero_bid}; ask=0: {zero_ask}; "
                  f"lastTrade {df['lastTradeDate'].min()} .. {df['lastTradeDate'].max()}")
            if price:
                near = df.iloc[(df["strike"] - float(price)).abs().argsort()[:3]]
                for _, r in near.iterrows():
                    print(f"     strike {r['strike']}: bid {r['bid']} ask {r['ask']} last {r['lastPrice']} "
                          f"vol {r['volume']} OI {r['openInterest']} IV(raw) {r['impliedVolatility']}")


if __name__ == "__main__":
    print("checked at", datetime.now(UTC).isoformat())
    for s in sys.argv[1:] or ["SOXX", "SOXS"]:
        try:
            check(s.upper())
        except Exception as e:  # noqa: BLE001 - a check script: report and go on
            print(f"{s}: failed: {type(e).__name__}: {e}")
