r"""One live, read-only check of the phase 6 open questions 1-3 (Toss rankings / sectors / stock basics).

Run with the dashboard CLOSED (a new Toss token revokes the dashboard's):
    backend\.venv\Scripts\python.exe -m app.explore.live_check
Only GET market-info paths are called (no account header, nothing written to Toss). Prints facts only; keys
are read from .env by the settings module and never printed. A sector snapshot is saved under
logs/explore/ so a later run can tell whether sector ids stay the same.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from datetime import datetime
from pathlib import Path

from app.data.toss import TossClient, TossError
from app.settings import REPO_ROOT, get_config, get_secrets
from app.timeutil import KST

SNAP_DIR = REPO_ROOT / "logs" / "explore"
ADRS = ["TSM", "ASML", "BABA"]          # depositary receipts / foreign listings, for open question 3
ETF_TYPES = ("ETF", "FOREIGN_ETF", "ETN")


def dashboard_running(port: int = 8000) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def session_now(cal: object, now: datetime) -> str:
    day = getattr(cal, "today", None)
    for name in ("day_market", "pre_market", "regular_market", "after_market"):
        w = getattr(day, name, None)
        if w is not None and w.start <= now < w.end:
            return name.replace("_market", "")
    return "closed / between sessions"


async def run() -> int:
    if dashboard_running():
        print("The dashboard is running on port 8000. Close it first: a new Toss token would log it out.")
        return 2
    sec, cfg = get_secrets(), get_config()
    cid, csec = sec.TOSS_CLIENT_ID.get_secret_value().strip(), sec.TOSS_CLIENT_SECRET.get_secret_value().strip()
    if not cid or not csec:
        print("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET: empty in .env")
        return 1
    client = TossClient(cfg.toss.base_url, cid, csec, group_limits=cfg.toss.rate_limits_per_second,
                        safety=cfg.toss.rate_limit_safety)
    now = datetime.now(KST)
    try:
        cal = await client.market_calendar()
        print(f"Checked at {now:%Y-%m-%d %H:%M} KST, US session now: {session_now(cal, now)}")

        print("\n[1] Rankings, marketCountry=US")
        rt = await client.rankings("MARKET_TRADING_VOLUME", "realtime", "US", 20)
        print(f"  realtime volume ranking: {len(rt.rows)} rows, rankedAt {rt.ranked_at}")
        day = await client.rankings("MARKET_TRADING_VOLUME", "1d", "US", 100)
        print(f"  1d volume ranking: {len(day.rows)} rows, rankedAt {day.ranked_at}")
        syms = list(dict.fromkeys([r.symbol for r in rt.rows] + [r.symbol for r in day.rows]))
        infos = await client.stock_infos(syms[:200]) if syms else []
        etfs = [i.symbol for i in infos if i.security_type in ETF_TYPES]
        print(f"  ETFs / ETNs among ranked symbols: {len(etfs)} of {len(infos)}  e.g. {etfs[:8]}")
        print(f"  row currencies: {sorted({r.currency for r in day.rows})}")

        print("\n[2] Sectors (language, ids)")
        tree = await client.sectors()
        tops = [s for s in tree if s.depth == 0]
        print(f"  {len(tree)} sectors ({len(tops)} top level). Top-level names: {[s.name for s in tops[:8]]}")
        sr = await client.sector_rankings("TOP_GAINERS", "1d", "US")
        print(f"  US sector ranking: {len(sr)} rows. First names: {[r.name for r in sr[:5]]}")
        if sr:
            d = await client.sector(sr[0].sector_id, "US")
            print(f"  detail of {d.sector_id}: summary = {d.summary!r}")
            stocks = await client.sector_stocks(sr[0].sector_id, "US")
            print(f"  its US stocks: {len(stocks)}, first: {[(s.symbol, s.name, s.currency) for s in stocks[:3]]}")
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        snaps = sorted(SNAP_DIR.glob("sectors_*.json"))
        current = {s.sector_id: s.name for s in tree}
        if snaps:
            old = json.loads(snaps[-1].read_text(encoding="utf-8"))
            gone, new = sorted(set(old) - set(current)), sorted(set(current) - set(old))
            renamed = sorted(k for k in set(old) & set(current) if old[k] != current[k])
            print(f"  vs {snaps[-1].name}: {len(gone)} ids gone, {len(new)} new, {len(renamed)} renamed")
        else:
            print("  no earlier snapshot: run this again on another day to compare sector ids")
        (SNAP_DIR / f"sectors_{now:%Y%m%d}.json").write_text(json.dumps(current, ensure_ascii=False, indent=1),
                                                              encoding="utf-8")

        print("\n[3] sharesOutstanding for ADRs / foreign listings")
        for i in await client.stock_infos(ADRS):
            print(f"  {i.symbol}: securityType {i.security_type}, market {i.market}, "
                  f"sharesOutstanding {i.shares_outstanding}")
        print("  (compare with the company's own ADS / share count to tell which one Toss reports)")
    except TossError as exc:
        print(f"Toss error {exc.status or ''} {exc.code}")
        return 1
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
