r"""One live, read-only check of SEC EDGAR (phase 6 step 5): ticker map + companyfacts structure.

Run (the dashboard may stay open: SEC has nothing to do with the Toss token):
    backend\.venv\Scripts\python.exe -m app.data.edgar_check
Needs SEC_USER_AGENT in .env ("name email"); its value is never printed. About 4 SEC requests, paced at
5/second. Prints structure only (taxonomies, concept counts, field names of one fact), so step 6 can rely
on what SEC actually sends; the files are cached under data/cache/edgar/.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter

from app.data.edgar import EdgarClient, EdgarError
from app.data_dir import migrate_to_data_dir
from app.settings import CACHE_DIR, DATA_DIR, REPO_ROOT, get_config, get_secrets

CHECK = ["AAPL", "NVDA", "TSM", "SOXS"]   # US filer, US filer (Jan fiscal year), foreign filer (20-F), ETF


async def run() -> int:
    ua = get_secrets().SEC_USER_AGENT.get_secret_value().strip()
    if not ua:
        print("SEC_USER_AGENT: empty in .env (add a line like: SEC_USER_AGENT=Trading Desk you@example.com)")
        return 1
    e = get_config().edgar
    migrate_to_data_dir(REPO_ROOT, DATA_DIR)   # may run before the app ever started
    client = EdgarClient(ua, CACHE_DIR / "edgar", requests_per_second=e.requests_per_second,
                         facts_ttl_s=e.facts_ttl_hours * 3600, tickers_ttl_s=e.tickers_ttl_hours * 3600,
                         cooldown_s=e.cooldown_minutes * 60, timeout_s=e.timeout_seconds)
    try:
        table = await client.tickers()
        print(f"Ticker map: {len(table)} tickers")
        for sym in CHECK:
            entry = await client.cik_for(sym)
            if entry is None:
                print(f"\n{sym}: not in SEC's ticker map")
                continue
            print(f"\n{sym}: CIK {entry.cik} · {entry.title}")
            try:
                res = await client.company_facts(entry.cik)
            except EdgarError as exc:
                print(f"  companyfacts: SEC error {exc.status or ''} {exc.code}")
                continue
            facts = res.data.get("facts", {})
            print(f"  top-level keys: {sorted(res.data)}")
            for tax, concepts in facts.items():
                units = Counter(u for c in concepts.values() for u in c.get("units", {}))
                print(f"  {tax}: {len(concepts)} concepts; units {dict(units.most_common(4))}")
            for tax in ("us-gaap", "ifrs-full"):
                rev = next((facts[tax][k] for k in ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                                                     "Revenue") if k in facts.get(tax, {})), None)
                if rev:
                    unit, rows = next(iter(rev["units"].items()))
                    print(f"  {tax} revenue concept keys: {sorted(rev)}; unit {unit}; {len(rows)} facts")
                    print(f"  one fact's fields: {sorted(rows[-1])}")
                    print(f"  forms seen: {dict(Counter(r.get('form') for r in rows).most_common(5))}")
                    print(f"  fp values: {dict(Counter(r.get('fp') for r in rows).most_common(6))}")
                    break
    except EdgarError as exc:
        print(f"SEC error {exc.status or ''} {exc.code}")
        if exc.code == "bad-user-agent":
            print("SEC_USER_AGENT may only use English letters, digits, spaces and the email address")
        return 1
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
