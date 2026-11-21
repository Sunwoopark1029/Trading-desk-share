"""One-off live check of the event sources (read-only). Keys are never printed (only "set" / "empty").

Run from backend/ with the dashboard CLOSED (a new Toss token logs the dashboard out):
    .venv/Scripts/python -m app.events.live_check

Checks:
1. BLS release calendar (bls.ics): reachable, event count, last date, upcoming CPI / PPI / jobs / JOLTS with ET times
2. FRED release dates: does include_release_dates_with_no_data=true return FUTURE dates? which ids for GDP / PCE / FOMC?
3. Finnhub /calendar/earnings per symbol through the app client (plain httpx cannot connect on this machine)
4. Toss sectors: can the top 30 US stocks by market cap be built from the top-level sector stock lists?
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta

import httpx

from app.explore.live_check import dashboard_running
from app.settings import get_config, get_secrets
from app.timeutil import KST

UA = {"User-Agent": "personal-trading-desk/1.0 (event calendar check)"}
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
FRED_DATES = "https://api.stlouisfed.org/fred/releases/dates"


async def check_bls(http: httpx.AsyncClient, today: date) -> None:
    print("\n[1] BLS bls.ics")
    r = await http.get(BLS_ICS, headers=UA)
    print(f"  status {r.status_code}, type {r.headers.get('content-type')}, {len(r.text)} chars")
    if r.status_code != 200:
        return
    events = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", r.text, re.S)
    rows = []
    for ev in events:
        m = re.search(r"DTSTART;TZID=([^:]+):(\d{8}T\d{6})", ev)
        s = re.search(r"SUMMARY:(.*)", ev)
        if m and s:
            rows.append((m.group(2), m.group(1), s.group(1).strip()))
    rows.sort()
    print(f"  {len(events)} events, {len(rows)} with TZID start; first {rows[0][0] if rows else '-'} last {rows[-1][0] if rows else '-'}")
    print(f"  TZIDs: {sorted({x[1] for x in rows})}")
    future = [x for x in rows if x[0][:8] >= today.strftime("%Y%m%d")]
    keys = ("Consumer Price Index", "Producer Price Index", "Employment Situation", "Job Openings")
    for x in future:
        if any(k in x[2] for k in keys):
            print(f"   {x[0]} {x[1]}  {x[2]}")
    print(f"  all upcoming summaries (first 15): {[x[2] for x in future[:15]]}")


async def check_fred(http: httpx.AsyncClient, key: str, today: date) -> None:
    print("\n[2] FRED releases/dates (future)")
    params = {"api_key": key, "file_type": "json", "realtime_start": today.isoformat(),
              "realtime_end": (today + timedelta(days=60)).isoformat(), "include_release_dates_with_no_data": "true",
              "order_by": "release_date", "sort_order": "asc", "limit": 1000}
    r = await http.get(FRED_DATES, params=params)
    print(f"  status {r.status_code}")
    if r.status_code != 200:
        print("  body (first 200 chars, key removed):", r.text[:200].replace(key, "***"))
        return
    rows = r.json().get("release_dates", [])
    fut = [x for x in rows if x.get("date", "") > today.isoformat()]
    print(f"  {len(rows)} rows, {len(fut)} after today; last date {rows[-1]['date'] if rows else '-'}")
    pat = re.compile(r"Gross Domestic Product|Personal Income|FOMC|Consumer Price|Employment Situation|Producer Price", re.I)
    for x in fut:
        if pat.search(x.get("release_name", "")):
            print(f"   {x['date']}  id {x['release_id']}  {x['release_name']}")


async def check_finnhub(http: httpx.AsyncClient, key: str, today: date) -> None:
    """Per-symbol earnings through the app's own client (it needs the app's TLS setup; plain httpx fails here)."""
    from app.data.finnhub_fund import FinnhubFund, FinnhubGate

    print("\n[3] Finnhub /calendar/earnings per symbol (app client)")
    fh = FinnhubFund(key, FinnhubGate(1.1))
    try:
        for sym in ("NVDA", "DELL", "AAPL", "SOXS"):
            rows = await fh.earnings_calendar(sym, today, today + timedelta(days=30))
            print(f"  {sym}: {[(r.date.isoformat() if r.date else None, r.hour) for r in rows]}")
    finally:
        await fh.aclose()


async def check_toss_top30() -> None:
    print("\n[4] Toss top 30 by market cap from top-level sector stock lists")
    if dashboard_running():
        print("  skipped: the dashboard is running on port 8000 (a new Toss token would log it out)")
        return
    from app.data.toss import TossClient

    sec, cfg = get_secrets(), get_config()
    cid, csec = sec.TOSS_CLIENT_ID.get_secret_value().strip(), sec.TOSS_CLIENT_SECRET.get_secret_value().strip()
    if not cid or not csec:
        print("  TOSS keys: empty")
        return
    client = TossClient(cfg.toss.base_url, cid, csec, group_limits=cfg.toss.rate_limits_per_second,
                        safety=cfg.toss.rate_limit_safety)
    try:
        tree = await client.sectors()
        tops = [s for s in tree if s.depth == 0]
        print(f"  {len(tree)} sectors, {len(tops)} top level")
        caps: dict[str, tuple[float, str, str]] = {}
        sizes = []
        for s in tops:
            stocks = await client.sector_stocks(s.sector_id, "US")
            sizes.append(len(stocks))
            for st in stocks:
                if st.market_cap is not None:
                    caps[st.symbol] = (float(st.market_cap), st.name, st.currency)
        print(f"  stocks per top-level sector: {sizes}; {len(caps)} US symbols with a market cap")
        top = sorted(caps.items(), key=lambda kv: -kv[1][0])[:30]
        print("  top 30:", [k for k, _ in top])
        print("  currency of caps:", sorted({v[2] for _, v in top}))
        for sym in ("NVDA", "AAPL", "MSFT", "TSM", "AVGO", "BRK.B", "MU"):
            print(f"   {sym}: {'in list' if sym in caps else 'NOT in any top-level list'}")
    finally:
        await client.aclose() if hasattr(client, "aclose") else None


async def run() -> None:
    sec = get_secrets()
    fred = sec.FRED_API_KEY.get_secret_value().strip()
    fin = sec.FINNHUB_API_KEY.get_secret_value().strip()
    today = datetime.now(KST).date()
    print(f"checked {datetime.now(KST):%Y-%m-%d %H:%M} KST; FRED key {'set' if fred else 'empty'}, "
          f"Finnhub key {'set' if fin else 'empty'}")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        for name, coro in (("bls", check_bls(http, today)),
                           ("fred", check_fred(http, fred, today) if fred else None),
                           ("finnhub", check_finnhub(http, fin, today) if fin else None)):
            if coro is None:
                print(f"\n[{name}] skipped: key empty")
                continue
            try:
                await coro
            except Exception as e:  # noqa: BLE001 - check script
                print(f"  {name} failed: {type(e).__name__}")
    try:
        await check_toss_top30()
    except Exception as e:  # noqa: BLE001
        print(f"  toss failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(run())
