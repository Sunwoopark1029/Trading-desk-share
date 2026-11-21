# Phase 6 plan — Explore, Research, and the link to the Desk (2026-11-20; Parts A, B and C built 2026-11-21)

ROADMAP.md and CLAUDE.md rules apply; this file fixes the details. On approval, "Fundamental analysis tool" and "Multiple tickers at once" move out of ROADMAP "Later" into Phase 6.

**No order code changes in this phase.** `config.yaml` currently has `orders.dry_run: false` (real orders on), so every build step and test here runs without touching `orders/` and never calls the send route.

---

## 1. How the app works today (read 2026-11-20)

- **One screen, four views** switched by a `view` state in `App.tsx` (no router): Desk · Orders · Journal · Settings. Phone width uses tabs.
- **One global ticker** (`symbol` in `App.tsx`), starting at `config.tickers.default` (SOXS). Quick-switch buttons come from `tickers.compare` (SOXX). The top strip's stock slots come from `tickers.strip` (SOXS, SOXX). The ticker is not remembered between visits.
- **Desk flow**: chart (Toss 1m/5m/1d + EMA/VWAP/Fib/swings) → chat (`get_levels`, overlays, `show_scenarios`, news, account) → scenario row pre-fills the order ticket → draft → approve → send.
- **Chat memory**: `logs/chat/chat.sqlite`, one global list of conversations. A conversation has no ticker or page attached; the ticker travels with each message (`screen.symbol`), and a `<context>` block is re-sent when ticker or trading day changes. Long chats are summarized by Haiku.
- **Number rule in code**: every price in an answer must come from `get_levels`, a data block, or the user's text (`unverified_numbers` in `chat/engine.py`). The check only looks at price-shaped numbers (2+ decimals).
- **Data today**: Toss (prices, candles, orderbook, trades, calendar, FX, stock list, account, orders), yfinance (strip), FRED (10Y), Finnhub (company news only). Toss paths are on a code allowlist.
- **Not there yet**: any market-wide view (rankings, sectors), any company fundamentals, watchlist folders, per-ticker chat, any saved link between "why I like this stock" and the intraday screen.

## 2. What this phase adds (your six points)

| # | You asked for | What gets built |
|---|---|---|
| 1 | Pick a ticker, or find one by talking to the chatbot | **Explore rail** (rankings, sectors, watchlist, search) + **research chat** with discovery tools |
| 2 | Balance sheets, news, last n years of earnings, target price | **Fundamentals backend** (SEC EDGAR + Finnhub + yfinance) and a **Company view** |
| 3 | Chatbot judges cheap / expensive vs earnings and growth | Code-built **fundamentals pack** with ids → AI explains it and fills a **valuation card** |
| 4 | Carry it to the Desk for entry and intraday | Saved **thesis** per ticker → Desk card, chart reference lines, `get_levels` ids, `<thesis>` block in desk chat |
| 5 | Toss-like navigation, not SOXS/SOXX-centred; likes and folders | Rankings by volume / amount / movers, sector drill-down, **watchlist folders**, pinned tickers replace the fixed SOXS/SOXX slots |
| 6 | Chat memory per ticker; connect the two pages | Conversations get `symbol` + `kind`; a small **ticker memory** (thesis + notes) is shared by both chats |

Built in three parts. Each part is useful on its own and ends with a "done when".

```
            ┌────────────── Research view ──────────────┐            ┌────────── Desk view ──────────┐
 Explore ──▶│ Company view ──▶ research chat            │            │ thesis card · chart lines     │
 (rank /    │ (code: statements, metrics, pack)         │──thesis──▶ │ get_levels (+research ids)    │
  sector /  │        valuation card ──[Save thesis]     │   + notes  │ desk chat (<thesis> block)    │
  folders)  └───────────────────────────────────────────┘            │ scenarios → ticket → draft    │
      └──────────── one active ticker, remembered, shared by every view ───────────────┘
```

---

## 3. Data sources (docs read 2026-11-20; added to ROADMAP "Data sources" 2026-11-21)

Toss rows below were re-checked in step A1 against the raw spec, saved as `docs/specs/toss_openapi.json` (OpenAPI 3.1.0, spec version **1.2.28**), field by field. Per-second limits are not in the JSON; they come from the `overview.md` rate-limit table. All seven Toss paths need only the token (no account header).

| Item | Source | Status | Notes |
|---|---|---|---|
| Rankings (volume, amount, gainers, losers) | Toss `GET /api/v1/rankings` | **Verified in spec 1.2.28** | Required: `type` (`MARKET_TRADING_AMOUNT`, `MARKET_TRADING_VOLUME`, `TOP_GAINERS`, `TOP_LOSERS`, `TOSS_SECURITIES_TRADING_AMOUNT`, `TOSS_SECURITIES_TRADING_VOLUME`), `marketCountry` (**`KR` / `US`**), `duration` (`realtime`, `1d`, `1w`, `1mo`, `3mo`, `6mo`, `1y`; `TOP_GAINERS` / `TOP_LOSERS` with `realtime` → 400 `unsupported-ranking-duration`). Optional: `count` 1–100 (default 100), `excludeInvestmentCaution` (default false). Response: `rankedAt` (null when empty) + rows `rank`, `symbol`, `currency` (per row; USD for US in the example), `price.lastPrice` / `basePrice` / `changeRate` (fraction; null if base is 0), `tradingVolume`, `tradingAmount`. **Rows have no name**: names come from `/stocks` or the cached stock list. `basePrice` / `changeRate` = start of `duration` for gainers/losers, **previous close for all other types**. `TOSS_SECURITIES_*` count Toss executions only; others the whole market. Fewer rows than `count` possible; unranked combinations return an empty list, not an error. Group `RANKING` 5/s |
| Sectors | Toss `GET /api/v1/sectors`, `/sectors/rankings`, `/sectors/{id}`, `/sectors/{id}/stocks`, `/sectors/{id}/etfs` | **Verified in spec 1.2.28** | List: no parameters, whole tree in one call (`sectorId` string, `name`, `depth` 0–2, `parentSectorId`); spec says fetch once a day. A stock can be in several sectors. **Rankings**: `type` only `TOP_GAINERS` / `MARKET_TRADING_AMOUNT` (no losers, no volume), `duration` `1d`…`1y` (no `realtime`), **no `count`** (top 100, fewer possible), depth 0 and 1 only; rows `rank`, `sectorId`, `name`, `changeRate`, `tradingAmount`, `marketCapitalization`, `currency`. **Detail**: `summary` (may be null) and `performance.changeRates`: always 6 entries `1d`→`1y`, each null when no data; `marketCountry` required. **Stocks**: common shares incl. sub-sectors, by market cap, **no paging and no price or change field**, only `symbol`, `name`, `volume`, `tradingAmount`, `marketCapitalization`, `currency`; volume/amount are today's, null without quotes. **ETFs**: sub-sectors not included, **symbol and name only** (prices via `/prices`). **All sector amounts are converted to KRW, also for US**. `sectorId` pattern `^[A-Za-z0-9_-]{1,40}$`; unknown id → 404 `sector-not-found`. `marketCountry` `KR` / `US`. Groups `SECTOR` 5/s, `SECTOR_RANKING` 5/s |
| Stock basics | Toss `GET /api/v1/stocks?symbols=` (≤200) | **Verified in spec 1.2.28** | Rate group **`STOCK`, 5/s (confirmed)**. Fields: `symbol`, `name` (Korean), `englishName`, `isinCode`, `market` (KOSPI, KOSDAQ, NYSE, NASDAQ, AMEX, KR_ETC, US_ETC), `securityType` (STOCK, FOREIGN_STOCK, DEPOSITARY_RECEIPT, INFRASTRUCTURE_FUND, REIT, ETF, FOREIGN_ETF, ETN, STOCK_WARRANTS), `isCommonShare`, `status` (SCHEDULED, ACTIVE, DELISTED), `currency`, `listDate` / `delistDate` (KST dates, nullable), `sharesOutstanding`, `leverageFactor` (ETF/ETN only, else null), `koreanMarketDetail` (KR only). The schema marks `englishName` required, but **the spec's own ETF example omits it**, so the client treats it as optional. Symbols: letters, digits, `.`, `-` |

All amounts and rates are JSON strings parsed as exact decimals. Every enum in these responses carries the spec note "clients must accept unknown values", so the client keeps unknown strings instead of failing.
| Statements (balance sheet, income, cash flow), n years | **SEC EDGAR** `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` (10-digit CIK with leading zeros); ticker→CIK from `https://www.sec.gov/files/company_tickers.json` | **Verified 2026-11-20** on SEC's API, accessing-EDGAR-data and privacy pages; live-checked 2026-11-21 | No key. Declared `User-Agent` ("name email", ASCII) required; max **10 requests/s per user** (client uses 5); over the limit SEC limits the IP "for a brief period", access resumes after 10 min under the limit; no status code documented, so any 403 / 429 pauses SEC calls 10 min. XBRL data updates within a minute of a filing. Ticker map: SEC updates it "periodically", no accuracy guarantee; share classes use `-` (BRK-B); ETFs are not in it (SOXS absent). **Observed in live responses** (field meanings not documented on the API page): top keys `cik`, `entityName`, `facts`; `facts` → taxonomy (`us-gaap`, `ifrs-full`, `dei`, `srt` …) → concept → `label`, `description`, `units` → unit (`USD`, `shares`, `USD/shares`, `pure`, `TWD` …) → list of facts with `accn`, `end`, `filed`, `form`, `fp`, `frame`, `fy`, `start`, `val`. AAPL's old `Revenues` concept has only 11 facts while its current revenue concept is different, so **the concept with the latest data wins, not the first one listed**. Foreign filer TSM: `ifrs-full`, values mostly in **TWD**, forms 20-F / 6-K (6-K facts have no `fp`) → currency conversion + ADR ratio needed for valuation |
| Statements fallback | yfinance `income_stmt` / `cashflow` / `balance_sheet` | Unofficial; checked in the installed yfinance 1.8.0 source | Used only when EDGAR has nothing usable; labeled "yfinance · unofficial" with Yahoo's reporting currency (unknown stays unknown). **Changed 2026-11-21:** Finnhub `/stock/financials-reported` dropped, since its own spec says it is the SEC filings dataset, so it can't fill a gap EDGAR leaves |
| EPS actual vs estimate | Finnhub `/stock/earnings` | **Verified** in the pinned spec `docs/specs/finnhub_swagger.json`; free tier "Last 4 quarters" (live 2026-11-21: 4 rows) | Fields actual, estimate, surprise, surprisePercent, period, year, quarter. Older EPS (actuals only) comes from EDGAR |
| Next earnings date | Finnhub `/calendar/earnings` | **Verified**; free tier "1 month of historical earnings and new updates" (live: next AAPL date found) | `hour` = bmo / amc / dmh; the spec says its EPS and revenue are **non-GAAP** (labelled so). Also feeds the top strip "next event" for the active ticker |
| Analyst ratings | Finnhub `/stock/recommendation` | **Verified**, free | strongBuy / buy / hold / sell / strongSell per month |
| **Target price** | Finnhub `/stock/price-target` | **"Premium required." (verified)** | yfinance `Ticker.analyst_price_targets` (current / low / high / mean / median from Yahoo's financialData), labeled "yfinance · unofficial", USD assumed for US listings (no currency field). Behind the company Zscaler proxy it needs a session trusting the Windows certificate store (same fix as the Finnhub news). **Decision D2** |
| Profile | Finnhub `/stock/profile2` | **Verified**, free | Name, country, filing currency, exchange, IPO date, industry, logo, website. Its marketCapitalization / shareOutstanding have **no unit in the spec**: not used |
| News | Finnhub `/company-news` (existing feed + Haiku tags) | Already built | Research view asks for a longer window than the Desk's 48 h |
| ETFs (SOXS, SOXX …) | — | Finnhub ETF profile / holdings are premium | No company statements exist. Show Toss `leverageFactor`, sector, sector ETFs, and related tickers; say so plainly |

Standardized statements, EPS / revenue estimates and upgrade/downgrade history on Finnhub are premium: not used.

**UNVERIFIED until one read-only call each (after keys + allowlist):**
Items 1–3 were checked live on 2026-11-20 16:15 KST (US "day" session) with `backend/app/explore/live_check.py`, read-only:
1. ~~`marketCountry=US`~~ answered by the spec (`KR` / `US`). **`realtime` returns data during the day session** (20 of 20 rows, `rankedAt` = check time); regular / pre / after sessions and closed hours not checked yet. **ETFs appear in rankings**: 61 of the top 100 US 1d volume symbols were ETFs / ETNs (e.g. SNDU, BITX, TSLL); row currency USD.
2. **US sector names are Korean** (300 sectors, 39 top level: 운송, 방위산업물자 …; US sector ranking 95 rows), and US stock names in sector lists are Korean too (`릴렉스(ADR)`); summaries can be null. English names exist only in stock basics (`englishName`). Sector-id stability: first snapshot saved to `logs/explore/`; **still open** until a later run compares.
3. **`sharesOutstanding` for ADRs looks like the ADS-equivalent count, not ordinary shares**: TSM 5,186,474,000, BABA 2,487,744,939, ASML 385,417,665 (all `DEPOSITARY_RECEIPT`). TSM's number matches its ordinary-share count divided by its ADS ratio (5 shares per ADS), so price × this number gives the ADR market cap. **Still unverified by Toss docs**: market cap from it is labeled "est." for depositary receipts until confirmed.
4. How far back Toss `1d` candles go (needed for "own-history" multiples; 1m is documented from 2021-12-01, daily is not).
5. Finnhub free-plan call cap: still not in the spec (only "30 API calls/second on top of all plan's limit", 429 when over). Handled: one `FinnhubGate` for news + fundamentals (1.1 s spacing ≈ 54/min), a 429 pauses all Finnhub calls 60 s.
6. ~~`analyst_price_targets` works in yfinance 1.8.0~~: **yes** (live 2026-11-21), once the session trusts the Windows certificate store; without that, Yahoo's cookie request fails behind Zscaler and targets come back empty (shown as unavailable).
7. ~~Whether as-reported EPS history from EDGAR is split-adjusted~~: answered in step 6: values are as reported; a diluted-share jump (>= 1.5x) is flagged as a likely split and never adjusted; own-history P/E stops at the jump.

---

## 4. Part A — Explore + watchlist (Toss-like navigation) — ✅ built 2026-11-20 (commits bb8d608, adef327, a45a406, a4cedd8)

**Screen** (new "Research" view; left rail also usable as a drawer on the Desk):
```
┌ Top strip (stock slots = your pinned tickers, default SOXS/SOXX) ────────────────────┐
├───────────────┬───────────────────────────────────────┬───────────────────────────┤
│ Explore        │ NVDA · name · USD price · chg · mkt cap  │ Research chat (this ticker) │
│ [Watch][Rank]  │ [Overview][Financials][Earnings]         │  threads for this ticker    │
│ [Sectors]      │ [Analysts][News][Peers]                  │  valuation card             │
│ rows: symbol,  │ metrics grid · statements table          │  [Save thesis] [Open Desk]  │
│ price, chg, ★  │ saved thesis                             │                             │
└───────────────┴───────────────────────────────────────┴───────────────────────────┘
```
- **Rank tab**: Volume · Amount · Gainers · Losers · "Toss popular" (Toss-executed amount), with the duration switch. Polled every ~15 s only while visible.
- **Sectors tab**: sector ranking (gainers / amount) → sector → its stocks (by market cap) and ETFs.
- **Watch tab**: folders you make ("Semis", "Waiting for earnings" …). ★ on any row adds to a folder; a ticker can sit in several folders. One folder is **pinned**: it supplies the chart's quick-switch buttons and the top strip's stock slots.
- Clicking any row sets the **active ticker** for every view. The active ticker and last view are remembered (browser storage); `tickers.default` stays as the first-run fallback.

**Backend**
- `data/toss.py`: add the read-only GET paths above to `ALLOWED_PATHS` / `ALLOWED_PATTERNS`, groups `RANKING`, `SECTOR`, `SECTOR_RANKING`, `STOCK` to `DOCUMENTED_LIMITS`, parsers (Decimal), client methods. Allowlist test extended.
- `explore/` (new): cached views. Rankings TTL ~10 s, sector list 24 h, sector detail/stocks 60 s, stock basics 24 h. Stale / error handling as in `cache.py`.
- `watchlist.py` (new): `watchlist.json` in the repo root (gitignored), atomic write like `positions.json`. First run migrates `tickers.default` + `tickers.compare` + Settings watchlist into a pinned "Favorites" folder. Limits: 20 folders, 100 tickers per folder; every ticker validated against the Toss list.
- Watch-tab prices: one Toss `/prices` call (≤200 symbols) for the **visible** folder only.
- News stays capped (`news.max_symbols`): active ticker + holdings + pinned folder, in that order.

**API**
| Path | Returns |
|---|---|
| `GET /api/explore/rankings?type=&duration=&count=` | rows + `ranked_at`, source, status |
| `GET /api/explore/sectors` · `/sectors/rankings?type=&duration=` · `/sectors/{id}` | list · ranking · detail + stocks + ETFs |
| `GET /api/watchlist` · `PUT /api/watchlist` | folders (whole document replaced, validated) |
| `GET /api/watchlist/quotes?folder=` | price, change, change % per ticker |

**Done when**: with SOXS/SOXX nowhere on screen, you can open today's US volume ranking, drill into a sector, star three tickers into a folder, pin it, and see those tickers as the chart's quick buttons and strip slots after a restart.

## 5. Part B — Fundamentals data + Company view — ✅ built 2026-11-21 (commits 1e431db, 6194660, cda0969, 73cbe39, 17d5c81, ff7bf50)

**Rule (extends CLAUDE.md "Numbers")**: every fundamental figure, growth rate, margin, multiple and implied price is calculated in code with unit tests. The AI never computes one.

**Backend**
- `data/edgar.py`: client with the declared User-Agent (new `.env` name `SEC_USER_AGENT`: name added to `.env.example`, you fill in the value), limiter at 5/s, ticker→CIK map cached daily, `companyfacts` cached on disk under `cache/edgar/` for 24 h (files can be several MB; parsed in a worker thread; never sent to the model). Errors keep status + short code only.
- `data/finnhub_fund.py`: profile2, earnings, calendar/earnings, recommendation, company-news (~~financials-reported~~: dropped 2026-11-21, it is the SEC dataset). Key in header, OS-trust TLS context and **the same limiter instance** as the news feed.
- `data/yf_fund.py`: `analyst_price_targets` (and statement fallback) via `asyncio.to_thread`, short error codes, backoff.
- `fundamentals/` (new, all unit-tested on trimmed fixture JSON):
  - `concepts.py`: XBRL concept map with ordered fallbacks (e.g. revenue: `RevenueFromContractWithCustomerExcludingAssessedTax` → `Revenues` → `SalesRevenueNet`). A missing concept gives "not reported", never 0.
  - `statements.py`: facts → annual and quarterly tables. Picks one value per period (latest filing wins), labels fiscal years by end date ("FY2026, ended 2026-01-25"), builds TTM from the last four quarters, derives Q4 = FY − Q1..Q3 when only the annual is tagged.
  - `metrics.py`: YoY and 3y / 5y CAGR, gross / operating / net / FCF margins, ROE, net debt, share-count change; valuation at the live Toss price: market cap (price × Toss `sharesOutstanding`), P/E (TTM), P/S, P/B, EV/EBIT, FCF yield; own-history median multiples (if daily history allows: unverified item 4); **implied prices** (e.g. own-median P/E × TTM EPS).
  - `peers.py`: peer set = top N by market cap in the same Toss sector (default 5), same metrics, medians.
  - `pack.py`: the **fundamentals pack**: every value gets an id (`fy2025.revenue`, `rev_cagr_3y`, `pe_ttm`, `fv_pe_own_median`), a pre-formatted display string ("USD 35.1B", "23.4x", "+18.2%"), unit, period, source, and as-of time. Screen and chat both read this one object.
- Result cache: per ticker, statements refreshed daily and on a new filing; price-dependent metrics recomputed on read.

**Company view tabs**: Overview (key metrics grid, next earnings, sector, data-source status line) · Financials (annual / quarterly tables, n years, small trend bars) · Earnings (EPS actual vs estimate, surprise %) · Analysts (rating counts, target range vs current price) · News (existing feed, 30 days) · Peers (metric table with medians). Every delayed or unofficial value shows source and time; missing data says why (ETF, foreign filer, not reported).

**API**
| Path | Returns |
|---|---|
| `GET /api/research/{symbol}/overview` | profile, security type, market cap, sector, next earnings, per-source status |
| `GET /api/research/{symbol}/financials?freq=annual\|quarterly&years=` | statement tables |
| `GET /api/research/{symbol}/metrics` | the fundamentals pack |
| `GET /api/research/{symbol}/earnings` · `/analysts` · `/peers` · `/news?days=` | as named |

**Done when**: for a US filer (e.g. NVDA) the page shows 5 fiscal years of the three statements, growth and margin rows, multiples at the live price, the last 4 EPS surprises, rating counts and a labeled target range; for SOXS it shows the ETF card and says there are no statements; for a foreign filer it shows what exists and names the fallback source.

## 6. Part C — Research chat, thesis hand-off, per-ticker memory — ✅ built 2026-11-21 (commits c0e58d6, 1d7f001, 6b58e90, 34df298, a438581, 9a22f52)

### 6.1 Research chat (a second chat mode)
- Same engine, budget, tiers, usage log and append-only history. A separate static prefix (`RESEARCH_SYSTEM_PROMPT` + research tool list) so each mode keeps its own prompt cache.
- The pack goes into a `<fundamentals>` block inside the conversation, sent once per ticker and re-sent only when its data version changes (same idea as `<context>`).
- **Tools**
  - Find: `get_rankings`, `list_sectors`, `get_sector`, `search_tickers`, `show_candidates` (clickable ticker chips; every symbol checked against the Toss list in code, so the AI cannot invent a ticker). Screening in this phase uses Toss fields only (market cap, volume, amount, change); screening by fundamentals across a whole sector is left for later.
  - Read: `get_fundamentals(symbol, sections)`, `get_earnings`, `get_analysts`, `compare_peers`, `search_news` (existing), `get_levels` (existing: where price sits now), `web_search` (existing switch; context only, never a source of numbers).
  - Output: `show_valuation`: a card with verdict (`cheap` / `fair` / `expensive` / `unclear`) on three separate axes (vs own history, vs peers, vs growth), basis ids, reasoning, risks, what would change the view, and which code-computed reference prices matter. The card has **Save thesis** and **Open in Desk** buttons.
- **Number guard, extended**: the current check only sees price-shaped numbers. Research answers contain percentages, multiples and large amounts, so the guard for this mode matches the pack's display strings and raw values; anything else is listed under "unverified number(s)" as today.

### 6.2 Thesis: the object that travels to the Desk
Saved only when you press **Save thesis** (you may edit the text first). One current thesis per ticker; older ones kept as history. Stored in `logs/research/research.sqlite`.
```json
{
  "symbol": "NVDA", "saved_at": "ISO8601", "price_at_save": "…", "currency": "USD",
  "verdict": {"own_history": "cheap", "peers": "fair", "growth": "cheap"},
  "summary": "≤ 400 characters, yours to edit",
  "basis": ["pe_ttm", "pe_own_5y_median", "rev_cagr_3y"],
  "reference_levels": [
    {"id": "fv_pe_own_median", "price": "…", "label": "Price at own 5y median P/E", "formula": "pe_own_5y_median × eps_ttm", "show_on_chart": true},
    {"id": "target_mean", "price": "…", "label": "Analyst target (mean)", "source": "yfinance · unofficial", "show_on_chart": false}
  ],
  "risks": ["…"], "next_earnings": {"time": "ISO8601", "source": "Finnhub"},
  "data_as_of": {"statements": "…", "price": "…"}, "conversation_id": "…"
}
```
What the Desk does with it:
1. **Thesis card** in the side column for the active ticker: verdict chips, summary, age, "price moved x% since saved" (code), "new filing since saved" flag, earnings countdown, link back to Research.
2. **Chart**: ticked `reference_levels` drawn as `hline` overlays (`source: "indicator"`, group `research`, derived like plan / avg lines, not stored in `overlays.json`), with their own toggle.
3. **`get_levels`**: a `research` group with the same ids, plus `vs_last` / `diff_from_last` like every other level, so the desk AI may cite them and use them as scenario targets under the existing guard.
4. **Desk chat**: a short `<thesis>` block added to the `<context>` block; new read tool `get_thesis(symbol)` for detail. The thesis informs the answer; order rules do not change.

### 6.3 Chat memory per ticker
Two layers, because one shared thread per ticker would mix intraday candle blocks with fundamentals, break caching and cost more.
- **Threads**: `conversations` gets `symbol` and `kind` (`desk` / `research` / `discover`). Opening a ticker on a page loads that ticker's latest thread of that kind; the list shows only that ticker's threads; "New chat" starts another. Switching ticker switches thread (**decision D5**). `discover` threads have no ticker. Existing conversations are kept: `symbol` backfilled from their first question, `kind = desk`.
- **Ticker memory** (shared by both chats): the current thesis + **notes**. A note is a dated line you pin from an answer or type yourself; code also appends facts it knows for certain (thesis saved, order from this app filled). No LLM writes memory in this phase. Capped at ~1,500 tokens, oldest notes dropped first, editable on the Research page.
- **Injection**: the memory rides in the `<context>` block; `context_key` becomes `symbol | trading date | memory version`, so it is re-sent only when it changes and earlier history is never rewritten.
- **Usage log** gains `symbol` and `kind`, so AI cost per ticker and per page becomes visible (the journal's per-trade credits can use it later).

**API**
| Path | Returns |
|---|---|
| `GET /api/research/{symbol}/thesis` · `PUT` · `DELETE` | current thesis (+ history list) |
| `GET /api/research/{symbol}/notes` · `POST` · `DELETE /notes/{id}` | notes |
| `GET /api/chat/conversations?symbol=&kind=` · `POST {symbol, kind}` | filtered list · new thread |

**Done when**: on Research you ask "is NVDA cheap?", get a valuation card whose every number traces to a pack id, save the thesis, press Open in Desk, and see the thesis card, two reference lines on the chart, and a desk-chat scenario that cites a research level id. Reopening NVDA the next day brings back both threads and the notes.

---

## 7. Files

```
backend/app/
  data/toss.py            + rankings / sectors / stocks (allowlist, limits, parsers, methods)
  data/edgar.py           NEW   data/finnhub_fund.py  NEW   data/yf_fund.py  NEW
  explore/                NEW   views.py, routes.py
  watchlist.py            NEW
  fundamentals/           NEW   concepts.py statements.py metrics.py peers.py pack.py
  research/               NEW   store.py (thesis, notes) routes.py
  chat/store.py           schema: symbol, kind (+ migration)      chat/usage.py  + symbol, kind
  chat/prompt.py          research prompt, <fundamentals>, <thesis>, context_key
  chat/tools.py           research + discovery tools, get_thesis  chat/engine.py  mode, guard
  chat/levels.py          research level group                    chat/routes.py  filters
  settings.py             research / explore config, new rate groups
  user_settings.py        start view, pinned folder
  main.py                 routers
  tests/                  test_toss (new paths), test_edgar, test_statements, test_metrics, test_pack,
                          test_peers, test_watchlist, test_explore, test_research_store,
                          test_chat_modes, test_guard_research  (+ fixtures; no real network)
frontend/src/
  research/               NEW   ResearchPage, ExploreRail, WatchFolders, CompanyHeader, MetricsGrid,
                                StatementsTable, EarningsPanel, AnalystPanel, PeersTable, ValuationCard
  components/ThesisCard.tsx   NEW (Research + Desk)
  chat/ChatPanel.tsx      kind + symbol props, per-ticker thread list
  App.tsx                 "research" view + phone tab, remembered active ticker, thesis lines
  components/TopStrip.tsx, TickerSearch.tsx   pinned folder instead of fixed tickers
  api.ts  i18n.tsx (EN + KO)  styles.css
config.yaml (+ explore:, research:)   .env.example (+ SEC_USER_AGENT)   .gitignore (+ watchlist.json)
README.md / README.ko.md   ROADMAP.md (phase 6, data sources, decision log)   CLAUDE.md (numbers + chat role)
```

## 8. Build order (commit after each step; `git status` check before every commit)

**A — Explore** ✅ done 2026-11-20
1. Re-read the raw Toss spec for the seven paths; fill the unverified items it answers. Allowlist, limits, parsers, tests.
2. `explore/` views + routes + cache; one live read-only check of items 1–3.
3. `watchlist.py` + migration + routes + tests.
4. Frontend: Research view shell, Explore rail (Rank / Sectors / Watch), remembered active ticker, pinned folder → quick buttons and strip.

**B — Fundamentals** ✅ done 2026-11-21
5. EDGAR client + CIK map + disk cache + tests (403 without User-Agent, 429, bad JSON).
6. `concepts.py`, `statements.py` + tests (fiscal-year labels, restated values, missing concept, derived Q4, TTM).
7. Finnhub and yfinance fundamentals sources, shared limiter + tests.
8. `metrics.py`, `peers.py`, `pack.py` + tests (hand-calculated cases, negative earnings → P/E "n/a", zero revenue, split flag).
9. Research routes; Company view tabs; ETF and foreign-filer states.

**C — Chat and hand-off** ✅ done 2026-11-21 (built as C1-C7: threads, research chat backend, research chat screen, thesis + notes, Desk side, finding stocks, docs)
10. Chat store migration (`symbol`, `kind`), usage log fields, per-ticker thread list in `ChatPanel`.
11. Research mode: prompt, tools, `<fundamentals>` block, extended guard, `show_valuation` card + tests. Log the pack's token size and cache hit rate.
12. Thesis + notes store, routes, Save / edit UI.
13. Desk side: thesis card, chart lines + toggle, `get_levels` research group, `<thesis>` in context, `get_thesis` + tests.
14. Discovery tools + `show_candidates`.
15. README (EN + KO) with a check-list; ROADMAP phase 6 done + decision log; CLAUDE.md additions.

## 9. Tests required
Toss (new paths on the allowlist, unknown path still raises, Decimal parsing, `unsupported-ranking-duration`, `sector-not-found`); watchlist (BOM, invalid JSON never overwritten, limits, unknown ticker rejected, migration); EDGAR (User-Agent sent, key-free URL, limiter, cache hit, stale on error); statements (fiscal year ≠ calendar year, duplicate facts, restatement, missing concept ≠ 0, TTM, derived Q4); metrics (CAGR with negative start, margins, net debt, P/E with loss, implied price, own-median with short history → "not enough history"); pack (ids unique, display strings round-trip, source + as-of on every value); peers (fewer than N, self excluded); chat (mode prefixes differ, `<fundamentals>` sent once, context_key change on memory version, thread filter, migration backfill); guard (pack number passes, invented percent / multiple / amount flagged, invented ticker rejected); thesis (save, edit, history, reference levels appear in `get_levels` and as derived overlays, stale flags).

## 10. Decisions for you
| # | Question | My pick |
|---|---|---|
| D1 | Statements source | **Chosen:** SEC EDGAR first, yfinance as the only labeled fallback (Finnhub as-reported dropped 2026-11-21). Originally: SEC EDGAR first (official, 10+ years), Finnhub as-reported then yfinance as labeled fallbacks. Alternative: yfinance only (simpler, about 4 years, unofficial) |
| D2 | Target price | yfinance, labeled unofficial. Alternatives: pay for Finnhub estimates, or leave targets out |
| D3 | Default history | 5 fiscal years + 8 quarters |
| D4 | Who saves a thesis | **Chosen 2026-11-21:** your button, or the AI when your own latest message asks for it (checked in code, like arming rules). Originally: only you, by button |
| D5 | Switching ticker | **Chosen:** switches to that ticker's thread on both pages |
| D6 | SEC contact address | SEC requires a contact in the User-Agent; you put an email in `.env` |
| D7 | Peer set | Top 5 by market cap in the same Toss sector |
| D8 | Research chat default model | **Chosen:** Normal (Sonnet), switchable as today |
| D9 | Order of parts | A → B → C. A needs no new source and removes the SOXS/SOXX focus first |

## 11. Out of scope (stays in "Later")
Korean stocks (EDGAR and Finnhub fundamentals are US-only); fundamentals screening across a whole sector; ETF holdings look-through; earnings-call transcripts; alerts when price reaches a thesis level; an LLM that writes ticker memory by itself; any change to order drafting, approval or sending.
