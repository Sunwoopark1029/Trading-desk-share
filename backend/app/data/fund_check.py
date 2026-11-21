r"""One live, read-only check of the phase 6 step 7 sources (Finnhub fundamentals, yfinance targets).

    backend\.venv\Scripts\python.exe -m app.data.fund_check
About 4 Finnhub calls (spaced 1.1 s) and 2 yfinance lookups. No Toss call, so the dashboard may stay open.
Keys are read from .env and never printed.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta

from app.data.finnhub_fund import FinnhubError, FinnhubFund, FinnhubGate
from app.data.yf_fund import YFFund, YFFundError
from app.settings import get_config, get_secrets


async def run() -> int:
    key = get_secrets().FINNHUB_API_KEY.get_secret_value().strip()
    fh = FinnhubFund(key, FinnhubGate(get_config().news.finnhub_min_interval_seconds))
    today = date.today()
    try:
        p = await fh.profile("AAPL")
        print(f"Finnhub profile2 AAPL: {p.name if p else None} · {p.industry if p else None} · ipo {p.ipo if p else None}"
              f" · filing currency {p.currency if p else None}")
        e = await fh.earnings("AAPL")
        print(f"Finnhub earnings AAPL: {len(e)} quarters; newest {e[0].period if e else None} actual {e[0].actual if e else None}"
              f" vs estimate {e[0].estimate if e else None}")
        cal = await fh.earnings_calendar("AAPL", today - timedelta(days=30), today + timedelta(days=120))
        print(f"Finnhub calendar AAPL (-30 d .. +120 d): {[(c.date, c.hour) for c in cal]}")
        r = await fh.recommendations("AAPL")
        print(f"Finnhub recommendation AAPL: {len(r)} months; newest {r[0] if r else None}")
    except FinnhubError as exc:
        print(f"Finnhub error {exc.status or ''} {exc.code}")
    finally:
        await fh.aclose()
    yf = YFFund()
    for sym in ("AAPL", "NVDA"):
        try:
            t = await yf.price_targets(sym)
            print(f"yfinance targets {sym}: {t}")
        except YFFundError as exc:
            print(f"yfinance {sym}: {exc.code}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
