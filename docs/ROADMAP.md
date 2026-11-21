# ROADMAP — Personal Trading Desk

## One-line summary
A personal tool that shows chart, orderbook, indicators, and news on one screen; lets the user ask an AI chat on the same screen for analysis; shows AI-drawn lines and a scenario table; and, once the user decides on an entry, sends only the orders they approve through the Toss Securities Open API.

## Principles
1. People decide, the API executes. Orders go draft → approve → send, or (from the automatic-rules phase) rule → user arms it → code sends when the condition fires.
2. Code calculates the numbers; the AI interprets them.
3. localhost only; keys only in the backend `.env`.
4. One phase at a time. Each phase must be useful on its own.

## Screen layout (target)
```
┌ Top strip: USD/KRW · Nasdaq/S&P futures · ICESEMI · VIX · US 10Y · next event ──────┐
├──────────────────────────────────┬──────────────────┤
│ Main chart (1m / 5m / daily)       │ Orderbook · trades │
│  Indicators: EMA 9/20/60, VWAP, Fib ├──────────────────┤
│  Lines: user / indicator / AI (toggle)│ News feed (priority)│
│  My info: avg price, open orders, fills│                  │
├──────────────────────────────────┴──────────────────┤
│ AI chat (multi-turn) → scenario table → pick row → order draft → approve → send → result │
└────────────────────────────────────────────────────┘
```

## Structure
- **Backend**: Python 3.15 (installed), FastAPI. If a needed library doesn't support 3.15 yet, install 3.13 alongside and use that
  - `backend/app/data/` external API connections (Toss, yfinance, FRED, Finnhub, SEC EDGAR). Caching and rate-limit handling
  - `indicators/` EMA, VWAP, RSI, Fibonacci, swings, options-implied moves, risk/reward (unit tests required)
  - `overlays.py` save / list / delete lines on the chart
  - `chat/` (phase 2) Claude API calls, prompts and tool definitions (Desk chat; Research chat from phase 6 C)
  - `orders/` (phase 4) draft, validate, send, cancel, log; `rules/` automatic rules (engine, routes)
  - `news/` (phase 3) news feed + AI tags; `events/` event calendar ("Next event")
  - `explore/` (phase 6) Toss rankings / sectors; `research/` + `fundamentals/` Company view data and metrics; `watchlist.py` folders
- **Frontend**: Vite + React + TypeScript, TradingView Lightweight Charts. Views: Desk, Research, Orders, Journal, Settings
- **Config**: `.env` (keys; names in `env.example`), `config.yaml` (tickers, refresh intervals, order limits, DRY_RUN, model IDs and prices)
- **Folders in the repo root** (tidy-up 2026-11-21): `data/` app data (`positions.json` with manually entered trades and plans, `overlays.json`, `user_settings.json`, `watchlist.json`, `cache/`; gitignored), `logs/` (trades, usage, chat and news databases; gitignored), `docs/` (this roadmap, `plans/`, `prompts/`, pinned API `specs/`), `scripts/` (double-click `.cmd` files), `secrets/` (login hash; gitignored)

## P&L panel (phase 1 basic → phase 2 adds credits → phase 4 automated)
Always visible at the top right of the screen.

| Item | Contents |
|---|---|
| Unrealized P&L | Held qty × (current price − avg price), KRW and USD |
| Realized P&L | Based on sell fills; per trade, daily, monthly |
| Trading costs | Fees + FX spread (estimate) + fill spread cost (estimate) |
| AI credits | Spend over the same period from `logs/usage.jsonl` (in KRW) |
| **Net P&L** | Realized + unrealized − trading costs − AI credits |
| Breakeven | Breakeven price of the current holding, including credits and fees |
| Efficiency | Credits as a share of net P&L, average cost per trade |

- Period selector: today / this week / this month / all
- Per-trade view: entry/exit price, holding time, P&L, credits spent during that trade (chat cost in that window)
- Show estimates (FX spread, fill spread) separately from confirmed values

## Shared overlay format
```json
{
  "id": "string",
  "type": "hline | trendline | fib | zone",
  "points": [{"time": "ISO8601", "price": 0.0}],
  "label": "string",
  "color": "#hex",
  "source": "user | indicator | ai",
  "visible": true,
  "note": "reasoning (optional)",
  "symbol": "SOXS (optional; ticker the line belongs to, omitted = shown on every ticker)",
  "timeframe": "1m | 5m | 1d (optional; chart timeframe the line belongs to, omitted = shown on every timeframe)"
}
```
Use this format from phase 1. In phase 2 the AI adds lines in the same format.

## Data sources (decided in phase 0, 2026-11-16 — free plan)
| Item | Source | Delay | Notes |
|---|---|---|---|
| US stock quotes and candles | Toss Open API | Real-time | Intervals are **1m and 1d only**: 5m is built from 1m bars in code (unit tested). Max 200 bars per request, paged with `before`/`nextBefore`. 1m bars are stamped with the bar **end** time. 1m history from 2021-12-01. Daily OHLC = regular session only. No session field on bars: sessions come from `GET /api/v1/market-calendar/US` |
| US sessions | Toss market calendar | — | 4 sessions (KST, shift with US DST). Verified winter (2026-11-16): day 10:00–18:00, pre 18:00–23:30, regular 23:30–06:00, after 06:00–09:50. Calendar works for past dates. Verified 2026-11-16: **1m bars include day, pre, regular and after-hours trades**; a few bars fall between sessions (~09:50–10:00) and are left untagged; today's daily bar already updates during pre-market |
| Orderbook and trades | Toss Open API (REST + WebSocket) | Real-time | Level count **unverified** (examples show 2–3 per side): test with one read-only call. US book is from some US exchanges, not NBBO. Trades: today only, max 50 per request. WebSocket price updates can be dropped (no sequence numbers) |
| FX rate (USD/KRW) | Toss `GET /api/v1/exchange-rate` | Refreshes every 1 min | **Reference rate**: the applied rate comes from transaction records. Backup: yfinance `KRW=X` |
| Nasdaq / S&P futures | yfinance `NQ=F`, `ES=F` | ~10 min | Unofficial, personal use only, can break: cache + backoff + "stale" marker |
| Semiconductor index | yfinance `^ICESEMI` (NYSE Semiconductor Index) | Delayed | SOXX and SOXS track **ICESEMI, not PHLX SOX**. Live proxy: SOXX price from Toss |
| VIX | yfinance `^VIX` | ~15 min | Backup: Cboe delayed quotes (unofficial), FRED `VIXCLS` (daily close) |
| US 10-year yield | FRED `DGS10` (official, daily) + yfinance `^TNX` (intraday) | Daily / ~15 min | Shown as two separately labeled values |
| News | Finnhub `/company-news` (free key) | — | Backup: Massive free Stocks Basic news (5 calls/min). Avoid NewsAPI |
| Orders | Toss Open API | — | See phase 4. **No sandbox / paper trading** |
| Rankings (phase 6) | Toss `GET /api/v1/rankings` (group `RANKING` 5/s) | Real-time | Spec 1.2.28, pinned at `docs/specs/toss_openapi.json`. Volume / amount / gainers / losers / Toss-executed; `realtime` … `1y` (no `realtime` for gainers / losers); top 100; **rows have no name** (names from `/stocks`); change vs previous close, or vs period start for gainers / losers. Live check 2026-11-20: works in the US day session; ETFs make up most of the volume ranking |
| Sectors (phase 6) | Toss `/api/v1/sectors`, `/sectors/rankings`, `/sectors/{id}`, `/stocks`, `/etfs` (groups `SECTOR` / `SECTOR_RANKING` 5/s) | Real-time (market cap daily) | Toss's own classification, names in Korean also for US; sector stock lists have **no price or change %** (prices from `/prices` for the top 50); **amounts are KRW-converted, also for US**; tree cached a day. Sector-id stability unverified (snapshot in `logs/explore/`) |
| Stock basics (phase 6) | Toss `GET /api/v1/stocks?symbols=` (group `STOCK` 5/s) | Daily | English name, type, shares outstanding, leverage. For ADRs `sharesOutstanding` looks like the ADS count (TSM matches shares ÷ 5); unverified by the docs, so ADR market cap is "est." |
| Company statements (phase 6) | SEC EDGAR `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` + ticker map `www.sec.gov/files/company_tickers.json` (official, no key) | Within a minute of a filing; cached 24 h | Declared `User-Agent` required (`SEC_USER_AGENT`, ASCII); limit 10/s per user (client 5/s); any 403 / 429 pauses SEC calls 10 min. Field structure observed live 2026-11-21 (not documented on the API page): taxonomy -> concept -> unit -> facts (`accn`, `end`, `filed`, `form`, `fp`, `fy`, `start`, `val`). The concept with the latest data wins. ETFs are not in SEC's data. Foreign filers (e.g. TSM: `ifrs-full`, TWD) get statements in their own currency and no multiples. Splits are flagged, never adjusted |
| Statements fallback (phase 6) | yfinance `income_stmt` / `cashflow` / `balance_sheet` (unofficial) | Daily | Only when SEC has no data for the company (never when SEC errors: then the error is shown). Annual only; "TTM" = latest fiscal year, labeled. Finnhub `/stock/financials-reported` not used (it is the SEC dataset itself) |
| EPS surprises, ratings, profile (phase 6) | Finnhub `/stock/earnings` (free: last 4 quarters), `/stock/recommendation`, `/stock/profile2`, `/calendar/earnings` | Daily | Spec pinned at `docs/specs/finnhub_swagger.json`. No currency field: USD assumed for US listings. Calendar EPS / revenue are non-GAAP (spec); GAAP vs non-GAAP of the surprise figures unverified. One app-wide gate (1.1 s spacing; a 429 pauses all Finnhub calls 60 s) shared with the news feed. `/stock/price-target` is premium (not used) |
| Analyst price targets (phase 6) | yfinance `Ticker.analyst_price_targets` (unofficial) | Daily | Low / median / mean / high / current; USD assumed; behind the company proxy it needs a session trusting the Windows certificate store (`cache/os_trust.pem`) |
| Peers (phase 6) | Toss `/sectors` + `/sectors/{id}/stocks` for every leaf sector (US), built once a day into `cache/sector_index.json` | Daily | Toss has no "sector of a stock" endpoint, so the app builds the reverse map on the first Company view of the day (about one call per leaf sector, SECTOR group 5/s). Peers = top 5 by market cap in the stock's deepest leaf sector; other share classes of the same company (same CIK) skipped |
| Event calendar: US macro (Next event box) | BLS `https://www.bls.gov/schedule/news_release/bls.ics` (official, no key) + FRED `fred/release/dates` (release 53 GDP, 54 Personal Income and Outlays = PCE) + FOMC dates from federalreserve.gov (no feed: kept in `backend/app/events/fomc.yaml`) | Daily | Live check 2026-11-21 (`app/events/live_check.py`): bls.ics 313 events 2025-01 .. 2026-12-30, every start `DTSTART;TZID=US-Eastern` with time (CPI / jobs / PPI 08:30 ET, JOLTS 10:00 ET); 2027 not published yet. FRED `include_release_dates_with_no_data=true` returns future dates (GDP + PCE 2026-11-25, 12-23) but **date only**; the all-releases query hits its 1000-row cap within 60 days, so single releases are asked; FRED's "FOMC Press Release" (id 101) is a daily data update, not the meeting dates. FOMC page (last update 2026-11-18): 2026 Jan 27-28, Mar 17-18*, Apr 28-29, Jun 16-17*, Jul 28-29, Sep 15-16*, Oct 27-28, Dec 8-9* (* = projections); 2027 Jan 26-27, Mar 16-17, Apr 27-28, Jun 8-9, Jul 27-28, Sep 14-15, Oct 26-27, Nov 16-17; "tentative until confirmed"; no statement time on the page (date only). Finnhub economic calendar = premium (not usable) |
| Event calendar: earnings | Finnhub `/calendar/earnings?symbol=` (free; client `app/data/finnhub_fund.py`) | Daily | Per symbol (verified by the phase 6 fixtures); `hour` bmo / amc / dmh, no exact time. The no-symbol form (everyone in one call) is **unverified** (this shell can't reach finnhub.io) and not used. Watched: screen ticker, holdings, watchlist, big semiconductors, top 30 US by market cap |
| Event calendar: top 30 by market cap | Toss `/sectors` + `/sectors/{id}/stocks` (marketCapitalization, KRW-converted, daily) | Daily | Built from the top-level sector stock lists; whether those lists hold every stock is **unverified** (check needs the dashboard closed: `python -m app.events.live_check`) |
| Option chains (options idea) | yfinance `Ticker.options` + `option_chain(date)` (Yahoo `v7/finance/options`; unofficial, personal use) | ~15 min | Toss and Finnhub (free) have no options market data. Live check 2026-11-21 (`app/data/options_check.py`, SOXX / SOXS / SPY, after the Friday close): expirations as `YYYY-MM-DD` (SOXX / SOXS weekly, SPY daily); per contract strike, bid, ask, last, volume, openInterest, impliedVolatility, lastTradeDate (UTC), contractSize, currency; underlying `regularMarketPrice`, `postMarketPrice`, `marketState`, `America/New_York`. **IV is a decimal** (0.40 = 40 %) but **unreliable**: after the close the call and put IV at the same SPY strike differ (0.41 vs 0.27), so IV is taken from the ATM call + put mid (straddle) in code, Yahoo's IV only as a cross-check. **openInterest is not trustworthy** (SPY at-the-money OI 24–34 contracts, really tens of thousands; many 0 or missing): OI-based numbers (walls, max pain, OI put/call, volume > OI) are **unverified and off**; volume looks plausible ("most traded strikes"). **SOXX chain is thin** (ATM bid 3.0 / ask 10.0); SOXS wider than SPY but usable (~15–20 % of mid). Adjusted strikes after a split (SOXX 121.67 / 123.33 / 126.67) come with the **normal root**, so they are dropped by strike grid (not a multiple of 0.50 USD); with them gone every SOXX expiry grades "thin" (no expected move shown). Options expire 16:00 ET on the expiry date |

Toss API facts that shape the backend:
- Auth: OAuth2 client credentials (`POST /oauth2/token`), no refresh token, and **issuing a new token revokes the old one** → one shared token in the backend, reissued only when expired.
- Allowed IP must be registered in Toss WTS (Settings > Open API > allowed IPs), otherwise `403 ip-not-allowed`.
- Rate limits are per second per API group: market data 15/s, charts 20/s, **account 1/s**, orders 10/s. HTTP 429 on excess.
- WebSocket: 2 connections per account, 100 subscriptions each, text `PING` every ~60 s.
- Terms: data only for your own trading; no sharing with third parties, even for free.
- Every value from a delayed source shows its source and delay on screen (e.g. "yfinance · ~15 min").

Paid upgrade path if real-time futures are ever needed: Databento Standard or Massive Futures Advanced (~$199/month). The $29–49 tiers have the same delay as free yfinance.

---

## Phase 0 — Design check (no code) — ✅ done 2026-11-16
- [x] Read the official docs and fill in the items to verify in the "Data sources" table above
- [x] Propose fixes where this file or CLAUDE.md doesn't match the docs
- [x] Build a comparison table of 2–3 alternative sources for indices/futures/news (free limits, delay, key required or not)
- **Done when**: the user has picked data sources and approved the corrected docs

## Phase 1 — Read-only dashboard — ✅ done 2026-11-16
- [x] Basic backend and frontend structure; one script runs both
- [x] Top strip: USD/KRW (Toss reference rate), NQ/ES futures, ICESEMI, VIX, US 10Y (FRED daily + intraday), manual event list. Each with last-updated time, source, and delay label
- [x] Main chart: ticker search/switch (default SOXS, compare SOXX), 1m/5m/daily (5m built from 1m in code), volume, session shading for day/pre/regular/after from the Toss market calendar (KST). Ticker list from `GET /api/v1/stocks/all`, cached daily (no free-text search endpoint)
- [x] Indicators: EMA 9/20/60, VWAP anchored at the pre-market open (17:00 KST, shifts with US DST; includes pre-market, excludes the day market), Fibonacci (default: today's low→high; user can pick two points), swing highs/lows
- [x] My-info lines: avg price, scheduled buy/sell prices entered in KRW or USD → horizontal lines, saved to `positions.json`
- [x] P&L panel (basic): calculated from buy/sell records entered manually in `positions.json` (time, price, qty, currency, fee)
  - Average price held, unrealized P&L (at current price), realized P&L, total fees
  - Show both KRW and USD, with the FX rate and time used
  - Fee rate and FX spread set in `config.yaml` (estimates labeled "est.")
- [x] `/overlays` API (GET/POST/DELETE)
- [x] Empty placeholder panels for orderbook, news, and chat
- [x] Responsive layout: full layout on wide screens; tabs on phone width (≤ ~430px): Chart / P&L / Chat / Orderbook·News. Build both layouts from the start (phone access in phase 5)
- [x] Polling (quotes ~5s, candles 15–30s; yfinance/FRED slower), backend cache, error display, "stale" marker when a source stops updating
- [x] One shared Toss token in the backend; respect per-group rate limits (429 → back off)
- [x] README (English, from install to run) and a check-list
- **Done when**: the SOXS 5m chart shows EMA, VWAP, Fibonacci, and the avg-price line, and the top strip updates

## Phase 2 — AI chat and drawing — ✅ done 2026-11-17
- [x] Chat panel (multi-turn, conversation history saved)
- [x] Claude API connection. Called only from the backend; key in `.env`
- [x] AI tools: read indicators/levels (code-calculated values), add/remove lines (overlay format), search/summarize news (placeholder "not available yet" until phase 3), read current screen state (ticker, timeframe, visible lines)
- [x] "Draw a trendline", "redo the Fibonacci" → shows on the chart right away; AI lines can be toggled separately
- [x] Web search in chat: Claude's built-in web search tool (~$10 per 1,000 searches + tokens read). On/off switch in the chat box, max searches per message in `config.yaml`, optional allowed/blocked site lists. Answers show source links and dates. Web results add context only; prices and levels always come from the dashboard's code-calculated data. Search cost goes into the credit UI and budget
- [x] Scenario table: direction, entry, invalidation, target, risk/reward, reasoning (overlapping levels), expected move after costs
- [x] Model selector (3-way switch in the chat box, default "normal")
  - Normal: Claude Sonnet — everyday questions, drawing lines, scenarios
  - Deep: Claude Opus — important entry/exit decisions
  - Very deep: Claude Fable — big calls, e.g. around earnings
  - Simple work like news tagging/summaries: Claude Haiku (background, not shown to the user)
  - Model IDs and prices live in `config.yaml`, not in code (prices change)
- [x] Prompt caching (use heavily)
  - Cache (rarely changes): system prompt, rules, tool definitions, ticker basics, fixed levels for the day (previous close, Fibonacci anchors, etc.)
  - Not cacheable (changes every time): current price, latest bar, orderbook, new news → put at the end of the prompt
  - For raw candles, anchor the window start at the session open and append new bars at the end, so the front stays identical and the cache keeps hitting
- [x] Candle input A/B test: script and 15 questions ready (`scripts\ab_test.cmd`); the user chose B (levels + raw candles) without running it
  - A: only the code-calculated indicator/level summary
  - B: summary + raw candles (last N 1m bars, full session of 5m bars)
  - Compare answer accuracy (are cited levels correct?), token count, and cost on the same 10–20 questions, then pick the default
  - Either way, code-calculated values are the source of truth for numbers; raw candles only help with pattern reading
- [x] Credit usage UI
  - Calculate cost from the API response usage (input, output, cache read, cache write tokens) and the price table in `config.yaml`
  - Per message: model name, tokens, cost (USD and KRW), cache hit rate
  - At the top or above the chat: today's spend, this month's spend, share by model
  - Budget: daily and monthly limits; warn at 80%; at 100% block "deep/very deep" (normal asks for confirmation)
  - Save usage to `logs/usage.jsonl`
  - Feed credit cost into the P&L panel (see "Net P&L" above)
- [x] Conversation length: when it gets long, summarize the older part and carry the summary forward (summaries by Haiku)
- **Done when**: asking in chat returns an answer with its supporting levels and a scenario table, and lines appear on the chart. The 3-way model switch works, and per-message and daily cost are visible (no order features)

## Phase 3 — Orderbook and news feed — ✅ done 2026-11-17
- [x] Orderbook (호가창) with multiple levels: show every level Toss returns with size bars; the panel shows the level count (docs give no number; their US example has 2). If too few, discuss a paid depth source
- [x] Trade tape via Toss WebSocket (`trade:us`, `orderbook:us`); REST snapshot on (re)connect since the stream sends no initial snapshot and can drop updates. Stay within 2 connections per account
- [x] Spread display (bid/ask gap and %), orange above `spread.warn_pct`; fed into scenario "after costs"
- [x] News feed (Finnhub; Massive backup OFF: API base URL unverified): news on held and watched tickers, with date and source, priority tag (AI-tagged, one-line reason)
- [x] Handle disconnects and reconnects (PING every ~60 s)
- **Done when**: the orderbook and spread update, and important news appears in the side panel

## Phase 4 — Orders (draft → approve → send) — ✅ built 2026-11-17 (waiting for your DRY RUN run-through: README section 8)
- [x] Read: balance, buying power, open orders
- [x] Order drafts using Toss's real order types:
  - Normal: LIMIT or MARKET, prices in USD; expiry `DAY` (today) or `CLS` (at the close, US limit only). No good-till-cancelled
  - Conditional: SINGLE, OCO (two sells: above and below), OTO (buy, then sell), with `expireDate`
  - Fractional / amount orders only from regular open to 1 hour before close
  - Validation (currency, price range, limits, allowed tickers, held quantity, expiry). Summary sentence, draft ID, draft expiry time
- [x] Confirm on the approval screen → send. Duplicate-send protection with Toss `clientOrderId`
- [x] Modify and cancel use the same two steps (US modify = price only; conditional modify issues a new ID)
- [x] DRY_RUN: Toss has no sandbox, so in DRY_RUN the order endpoints are **never called**; sends are simulated locally
- [x] Pick a scenario-table row → pre-filled draft → confirm quantity and expiry → approve
- [x] Show orders and fills on the chart; log to `logs/trades.jsonl` (trade journal)
- [x] Automate the P&L panel: calculate from the API's fills and balances (replaces manual entry; warn on mismatch)
- [ ] Demo the full flow with `DRY_RUN=true` → the user switches to real orders themselves — check-list in README section 8 (yours to run); how to switch on / back in README 7.5
- [x] User request (2026-11-17): separate **Orders tab** with buy / sell / reserved buy (매수 예약) / reserved sell (매도 예약) and the live spread from the phase 3 orderbook; order type picker (market 시장가, limit, "current price" 현재가 = fills the live price into the draft for the user to see before approving); the latest chat scenario table updates the suggested entry/stop shown in the tab; pressing a row creates a pre-filled draft, the user confirms qty/expiry, then one more press approves and sends (never one-press send). Which Toss order type "예약" maps to (conditional SINGLE / OCO / OTO or another feature) is UNVERIFIED: check the docs first
- **Done when**: in DRY_RUN, draft → approve → send → cancel works end to end, and real orders are possible only after the user changes the setting

## Phase 5 — Phone access — ✅ built 2026-11-17 (waiting for your Tailscale setup and check-list: README sections 8 and 9)
- [x] PWA setup (add icon to the phone home screen, full-screen like an app)
- [x] Private-network access: reachable only through a private VPN like Tailscale. No router port forwarding or public internet exposure
  - The server responds only on localhost and the private-network address
  - Step-by-step install and connection guide in the README (both computer and phone)
- [x] Login passcode (with session expiry). Required even inside the private network
- [x] Guide for keeping the laptop awake during market hours; show a notice on disconnect
- [x] Toss allowed IP: API calls always go out from the home computer, so the phone's location doesn't matter. Detect and warn if the home IP changes
- [x] Phone read-only features first: chart, P&L, chat, orderbook/news
- [x] Phone orders open last: only after phase 4 has been used enough on the computer. PIN re-entry on send, large-text summary screen, market orders blocked on the phone (default)
  - Built and tested, but OFF by default (`remote.orders_enabled: false`); the user switches it on
- **Done when**: away from home, the dashboard opens on the phone through the private network, it can't be reached without the passcode, and phone orders send only after PIN confirmation

## Phase 6 — Explore, Research, link to the Desk — ✅ built 2026-11-21 (Part A 2026-11-20; Parts B and C 2026-11-21; waiting for your check-list: README 9)
Plan and details: `docs/plans/PHASE6_PLAN.md`.
- [x] **Part A — Explore + watchlist**
  - [x] Toss rankings, sectors and stock basics in the read-only client (spec 1.2.28 checked field by field; allowlist, 5/s groups, exact decimals)
  - [x] `explore/` views and routes, fetched only when the screen asks, short caches; one live read-only check of the open questions
  - [x] Watchlist folders in `watchlist.json` (gitignored; broken file never overwritten; 20 folders × 100 tickers; first run = pinned "Favorites")
  - [x] Research view: Explore rail (Rank / Sectors / Watch), ★ to folders, remembered active ticker and view; the pinned folder feeds the chart quick buttons, the top strip stock slots (first 8) and news
  - **Done when** (yours to check): with SOXS / SOXX nowhere on screen, open today's US volume ranking, drill into a sector, star three tickers into a folder, pin it, and see them as the chart's quick buttons and strip slots after a restart
- [x] **Part B — Fundamentals data + Company view** (SEC EDGAR, Finnhub, yfinance; fundamentals calculated in code)
  - [x] SEC EDGAR client: ticker -> CIK map, companyfacts with a 24 h disk cache (`data/cache/edgar/`) and stale fallback, 5 requests/s, 10 min pause after a 403 / 429, declared User-Agent (`SEC_USER_AGENT` in `.env`)
  - [x] Statements from SEC facts: 5 fiscal years + 8 quarters + TTM for income / balance / cash flow; latest filing wins; derived Q4 and year-to-date quarters marked "derived"; split flagged, never adjusted
  - [x] Finnhub fundamentals (profile, last 4 EPS surprises, earnings calendar, rating trends) behind one Finnhub gate shared with the news feed; yfinance targets and fallback statements labeled unofficial
  - [x] Metrics in code with tests: growth / CAGR, margins, ROE, net debt, FCF, valuation at the live Toss price, own-history and peer P/E medians with implied prices; every n/a has a reason; the fundamentals pack (ids + display strings) for the screen and Part C's chat
  - [x] Research routes `/api/research/{symbol}/...` and the Company view (Overview · Financials · Earnings · Analysts · News · Peers); ETF, depositary-receipt, foreign-filer and no-SEC states
  - **Done when** (yours to check): NVDA shows 5 fiscal years of the three statements, growth and margin tiles, multiples at the live price, the last 4 EPS surprises, rating counts and a labeled (unofficial) target range; SOXS shows the ETF card and says there are no statements; TSM shows its statements in TWD with multiples refused and the reason
- [x] **Part C — Research chat, thesis hand-off to the Desk, per-ticker chat memory**
  - [x] C1 chats per ticker (D5): conversations get symbol + kind (desk / research), one-time database upgrade (old chats filed under their first question's ticker), usage log records symbol + kind
  - [x] C2 research chat: own system prompt + tools (no order / rule / drawing tools), <fundamentals> block re-sent only when values change, valuation card checked against the pack, extended number check (percents, multiples, scaled amounts, prices)
  - [x] C3 research chat on the Research page (desktop third column, phone Explore / Company / Chat), valuation card
  - [x] C4 thesis + notes per ticker (`logs/research/research.sqlite`): built only from a code-made card; Save thesis button; AI saves only when the user's message asks (D4 changed); notes; ticker memory in the research chat
  - [x] C5 Desk: thesis card, "Thesis lines" on the chart, research levels `thesis_*`, short thesis in the Desk context, get_thesis tool
  - [x] C6 finding stocks from the research chat: rankings, sectors, ticker search, clickable candidates (checked against the Toss list)
  - **Done when** (yours to check, README "Phase 6 check-list"): on Research ask "is NVDA cheap?", get a card whose every number is on the Company view, save the thesis, open the Desk and see the thesis card and a thesis line on the chart; reopen NVDA the next day and find both chats and the notes
  - Not built: "order filled" notes written by code (needs the order code, left untouched), AI-written memory, screening a whole sector by fundamentals

## Automatic rules — ✅ built 2026-11-20 (waiting for your DRY RUN run-through: README 7.8); plan in `docs/plans/AUTO_RULES_PLAN.md`
- Rule = price condition (e.g. "touches X, then falls back to Y within N minutes") + one order (side, type, price, quantity, expiry)
- Written by the user, or proposed by the AI chat (AI numbers labeled "AI-proposed")
- User arms a rule with one tap (PIN on the phone), or asks the chat to arm it ("arm them" / "걸어줘": `arm_rules`, only when the user's latest message asks; decision log 2026-11-20); code then sends its order when the condition fires, with no further approval
- Fires once, has an expiry, can be disarmed; one kill switch disarms all; same code checks as drafts at fire time; no fire on stale data; DRY_RUN simulates
- Fires only while the backend runs (shown on every armed rule); armed / fired / disarmed logged to `logs/trades.jsonl`
- AI chat may also create buy/sell drafts with its own numbers (labeled "AI-proposed"); approval unchanged
- Rule types: touch → fall back, touch → bounce (window N min), candle closes beyond; confirm by a trade or by a finished 1m / 5m candle close (filters quick wicks)
- **Done when**: in DRY RUN, a rule written on screen and one proposed by the AI can be armed, fire once on real market data, show up as a simulated order and in `logs/trades.jsonl`, and the kill switch stops armed rules

## Options-implied context — ✅ built 2026-11-21 (from "Later"; README 6.7)
- [x] Step 1: live check of the source (yfinance option chains; Data sources row)
- [x] Step 2: fetcher (cache 5 / 30 min, backoff, stale) + calculations with tests: straddle-implied IV per expiry with a quality grade, expected move today / weekly / monthly with bands, term structure, IV vs 20-day realized, put vs call price skew, put/call volume, most traded strikes, earnings move (when a date is passed in); adjusted strikes filtered; open interest off (unverified)
- [x] Step 3: chat: short summary in `<live>` "options", bands as levels `em_*`, `get_options` tool, prompt rules; `GET /api/options`
- [x] Step 4: Options panel on the Desk (phone: Book tab), chart toggle "Options bands" (day + week), README 6.7
- Not built: earnings date wiring for the implied earnings move and the Research earnings tab (now possible: phase 6 part B has the Finnhub calendar per ticker), SOXX-based stand-in for thin chains

## Event calendar ("Next event" box) — ✅ built 2026-11-21 (README 5, "Next event box")
- [x] Step 1: live check (BLS bls.ics, FRED release dates, FOMC page; Finnhub economic calendar is premium)
- [x] Step 2: backend: per-source cache + background refresh, merge, `GET /api/events`, strip next event; tests
- [x] Step 3: popup list from the box, Settings -> My events, chat `<context>` "upcoming_events_7d", README
- To check with the dashboard closed: `python -m app.events.live_check` (Toss holidays for future dates, top 30 from sector lists)

## Chat feature ideas — not started (ideas only; list and details in `docs/plans/CHAT_FEATURE_IDEAS.md`)
Added 2026-11-21 by the user. Before building any item: check it against the current code (the plan's "Check first" lines), then write a short plan for that one item. Every number the AI uses still comes from code.
- [ ] **Do first (fixes, not features):**
  - [ ] Set real numbers for `max_order_usd` and `rule_max_buy_pct` in the config
  - [ ] Tighten the chat arm check (`ARM_RE` in `chat/tools.py`): require an explicit phrase with the rule id (today "걸어" anywhere in the message passes)
  - [ ] Update `docs/plans/AUTO_RULES_PLAN.md` (still says "nothing built" and "the AI has no tool that arms")
- [ ] 1. Journal access for the chat: read-only `get_journal` (closed round trips, code-computed summaries), setup tag + source scenario / rule id per trade
- [ ] 2. Risk and sizing calculator: entry + stop + % of account at risk → whole shares, loss at the stop (USD / KRW), share of buying power; today's realized P&L and trade count in `<live>`; optional daily loss line (warns, doesn't block)
- [ ] 3. Base rates computed by code: `get_base_rates` (VWAP first-touch holds, opening-range break, gap sizes, distance from VWAP) with the sample size shown; "not enough data" under the minimum
- [ ] 4. More level types in `get_levels`: VWAP ±1σ / ±2σ, anchored VWAP, opening range, premarket high / low, volume profile (POC / VAH / VAL), unfilled gaps, ATR
- [ ] 5. Underlying context for leveraged ETFs: underlying levels mapped to the ETF price (labeled "estimated"), main drivers' moves; needs a config map ETF → underlying
- [ ] 6. Orderbook and volume state for the chat: spread, depth near the price, relative volume
- [ ] 7. Plan tracking: save the plan when a scenario becomes an order; compare plan vs result when the round trip closes (depends on 1)
- [ ] 8. Phone alerts: push on a level, a rule touched / fired / blocked, an event 10 min away (only while the program runs; new external service: docs check + Data sources row first)
- Order: build 1 and 2 first (both use data the program already stores)

## Cloud server — approved 2026-11-21, building (plan: `docs/plans/CLOUD_PLAN.md`)
AWS Lightsail, Seoul (`ap-northeast-2`), Ubuntu LTS, 2 GB, static IP; reachable only through Tailscale.
- [x] Workshop mode (`deploy.role: workshop` forces DRY_RUN, rules off, banner) + `data/config.local.yaml` overrides; tests
- [x] Trusted devices (PIN once to trust a browser; trusted = home-PC order rules; list / remove in Settings; others keep the phone rules); tests
- [x] Linux scripts: setup, systemd service, update with rollback, pause / resume, daily backup (`scripts/linux/`)
- [x] README "Cloud server" section (EN / KO): section 8b
- [ ] User: AWS account (2FA, billing alert), Lightsail server + static IP, Toss IP registration, Tailscale, `.env` on the server
- [ ] DRY_RUN days on the server, then the PC switches to workshop
- Open: Toss (several IPs? second key? cloud use in the terms?), Lightsail disk encryption; Yahoo blocking accepted for now
- **Done when**: the server runs the live copy through reboots and updates, the PC in workshop mode can't send or fire anything, a trusted desktop orders without a PIN while the phone still needs it, and a backup can be restored

## Later (out of scope, ideas only)
- Move to a cloud server: moved out of Later 2026-11-21, see "Cloud server" above
- Trade journal stats (win rate, risk/reward, cost share): now chat ideas 1 and 7 above
- Alerts (when a price or level is hit): now chat idea 8 above
- Multiple tickers at once (watchlist folders and the Explore rail: phase 6 part A)
- Korean stocks (user goal 2026-11-17): KRX calendar and sessions (incl. NXT), KRW prices, VWAP anchor, Korean news source (Finnhub covers North America only; e.g. DART disclosures, unverified). Toss stream already has `trade:kr` / `orderbook:kr`
- Fundamental analysis tool (user idea 2026-11-17; listed US companies now planned as phase 6 parts B and C): company data from TheVC, Crunchbase, etc. Needs a docs check first: TheVC official API unverified (no scraping); Crunchbase API is paid (plans unverified); these cover private startups, so for listed US stocks SEC EDGAR (official, free) or Finnhub fundamentals fit better; ETFs like SOXS/SOXX have no company fundamentals
- Options-implied expected move (user idea 2026-11-20): moved out of Later and built 2026-11-21, see "Options-implied context" above. Still later: the Research earnings tab view

---

## Decision log
| Date | Decision | Reason |
|---|---|---|
| 2026-11-16 | Use Claude only for the LLM (no Gemini) | Small-model prices are similar, so little reason to manage two vendors' APIs |
| 2026-11-16 | Model tiers: default Sonnet, deep Opus, very deep Fable, background Haiku | Keep everyday cost low; use large models only for important calls |
| 2026-11-16 | Heavy prompt caching; changing data goes at the end of the prompt | Cut the cost of repeated input |
| 2026-11-16 | Whether to include raw candles is decided by an A/B test | Decide by actually comparing accuracy and cost |
| 2026-11-16 | Show credit usage in the UI and set budget limits | Spend while seeing the cost |
| 2026-11-16 | P&L panel shows net P&L including AI credits, fees, and FX cost | See whether it actually makes money after tool costs |
| 2026-11-16 | Responsive web app (PWA) + private VPN instead of a phone app | One codebase; keys stay on my computer |
| 2026-11-16 | Phone orders only after phase 4 is stable, PIN required | Prevent mis-taps on a small screen |
| 2026-11-16 | Project docs switched to English | User works in English |
| 2026-11-16 | Free data plan: Toss (stocks, orderbook, FX), yfinance (futures, ICESEMI, VIX, intraday 10Y), FRED (official 10Y), Finnhub + Massive (news) | Paid $29–49 tiers have the same delay as free; real-time costs ~$199/month. Adapters per source so switching later is easy |
| 2026-11-16 | Top strip uses ICESEMI instead of SOX | SOXX and SOXS track the NYSE Semiconductor Index (ICESEMI), not PHLX SOX |
| 2026-11-16 | 5m candles built from Toss 1m bars in code | Toss provides only 1m and 1d intervals |
| 2026-11-16 | DRY_RUN never calls Toss order endpoints | Toss has no sandbox or paper trading |
| 2026-11-16 | Web search in chat via Claude's built-in tool, with switch and per-message cap | Context for analysis; cost visible in the credit UI |
| 2026-11-16 | Python 3.15 (already installed); fall back to 3.13 only if a library fails | Avoid an extra install unless needed |
| 2026-11-16 | VWAP includes pre-market (anchored at pre-market open) | User preference |
| 2026-11-16 | Shared overlay format gets an optional `symbol` field; `GET /api/overlays?symbol=` returns that ticker's lines plus ticker-less ones | A line saved on SOXS shouldn't show on SOXX |
| 2026-11-16 | Fees: 0.1% on each buy and sell separately, none when the order total is under $10; chart labels bars by start time; trades entered by hand; typed-in average price allowed | User decisions for phase 1 |
| 2026-11-16 | Screen reads tickers and poll intervals from `config.yaml` via `GET /api/ui-config` (no secrets in it) | One place to change settings |
| 2026-11-16 | Phase 1 P&L period selector is Today / All only; week and month come later | Backend supports `today` and `all` so far |
| 2026-11-16 | Average price override (`avg_override`) is edited in `positions.json`; plans are added on screen | Matches the approved phase 1 plan |
| 2026-11-16 | "Pick Fib" snaps each click to the clicked bar's high or low | Levels come from real prices, not mouse position |
| 2026-11-16 | Frontend: React 19.3.1, Lightweight Charts 5.2.1, Vite 8.3.5, TypeScript 7.0.2 (pinned) | Installed and type-checked |
| 2026-11-17 | Prompt order: tools + system prompt fixed and cached; ticker basics and the day's fixed levels go in a `<context>` block inside the conversation (re-sent only when ticker or trading day changes); every turn appends a `<live>` block; history is append-only | Current Claude models (Sonnet/Opus 5.5, Fable 5.2, Haiku 5.5) invalidate the cache and drop earlier reasoning if earlier history changes |
| 2026-11-17 | Model IDs: normal `claude-sonnet-5-5`, deep `claude-opus-5-5`, very deep `claude-fable-5-2`, background `claude-haiku-5-5`. Prices (incl. 5m/1h cache writes, cache reads, Haiku >100k tier, web search $10/1k) verified 2026-11-16 on the official pricing page; IDs checked with `check_models.cmd` (Models API, free) | Pricing page lists names, not ID strings |
| 2026-11-17 | AI budget: USD 100/month (user), USD 5/day (Claude's pick: ~100 over 20 trading days); warn at 80%; at 100% Deep/Very deep blocked, Normal asks; Haiku summaries allowed | User decision |
| 2026-11-17 | API usage is billed to Anthropic Console credits, separate from the user's Claude Max plan | Max does not cover API keys |
| 2026-11-17 | Web search built in phase 2, switch off by default, max 3 searches per message; toggling it changes the tool list (one cache rebuild) | User decision; keeps tool definitions fixed otherwise |
| 2026-11-17 | Server-side refusal fallback on (`fallbacks: "default"`, not for Haiku); cost priced per attempt from `usage.iterations`, unknown fallback model priced at the tier's price and flagged est. | Recommended default for these models |
| 2026-11-17 | AI line prices and scenario prices are checked in code against get_levels values or numbers the user typed; lines are saved at the code value (4 dp); trendline/Fib points must be swing points; the AI can delete only `source:"ai"` lines | "Code calculates, AI interprets" |
| 2026-11-17 | Scenario R/R and expected move after costs computed in code: 0.1% fee each side + 2 x FX spread bp (est.; <$10 fee exemption ignored since qty is unknown) | Numbers must come from code |
| 2026-11-17 | "Open orders" for get_levels = plans from `positions.json` (no real orders until phase 4) | Nothing else exists yet |
| 2026-11-17 | Chat history in `logs/chat/chat.sqlite`; long chats (>60k prompt tokens) summarized by Haiku, keeping the last 6 turns with their reasoning blocks removed | Summary edits history once; stripped reasoning avoids API rejects |
| 2026-11-17 | AI credits counted in P&L totals only (not per ticker); KRW uses the Toss reference rate logged with each call | Spend is a desk cost |
| 2026-11-17 | Chart last-price label shows time into the current candle (`37/60s`, `2:37/5:00`, `closed`) | User request |
| 2026-11-17 | Candle A/B test runs as a separate script with the dashboard closed | One Toss token at a time |
| 2026-11-17 | Faster chart: Toss quotes every 2 s, 1m bars re-fetched every 5 s, screen redraws every 2 s; the forming candle is updated from the latest quote (display only, indicators use finished bars); tick-by-tick WebSocket stays in phase 3 | User asked for a more real-time chart; well inside Toss rate limits |
| 2026-11-17 | Chat answers rendered as formatted text (react-markdown 10.1.0 + remark-gfm 4.0.1, raw HTML off); levels carry code-calculated `vs_last`, `diff_from_last`, `diff_from_last_pct` | Answers looked like raw Markdown; the model was subtracting prices itself |
| 2026-11-17 | Raw candles sent to the AI by default (`llm.candles.mode: B`: 5m bars since the pre-market open, new ones appended each message, + last 60 1m bars) | User choice; the A/B test stays available but was not run |
| 2026-11-17 | Screen in English or Korean: picked on first open, switch at top right, saved in the browser; AI answers in the chosen language (`answer_language` in each <live> block, so the cached system prompt does not change); `README.ko.md`. `.cmd` window text stays English (Windows console may garble Korean) | User wants everything in Korean |
| 2026-11-17 | All `.cmd` scripts moved to `scripts\`; `.gitattributes` keeps them CRLF | User request; tidier project folder |
| 2026-11-17 | Orderbook + trades over the Toss WebSocket (same token as REST; PING 60 s; backoff 1→60 s with jitter; REST snapshot on every (re)connect; REST polling every 2 s while down). Our own token renewal kills the socket by design, so it just reconnects | Toss docs: no snapshot, no sequence numbers, lossy frames, one token per client |
| 2026-11-17 | Tickers follow the screen (any US stock/ETF) plus holdings; `news.related` is optional extras, not a fixed list | User wants the whole market (and Korean stocks later), not just SOXS/SOXX |
| 2026-11-17 | The forming candle also moves with streamed trades (high/low from every trade in it) | Tick-by-tick chart, as asked |
| 2026-11-17 | News: Finnhub company-news only (key in header); `datetime` accepted only as plausible Unix seconds, else "date unverified"; Massive backup off (base URL unverified); user has no Massive key | Docs-first rule |
| 2026-11-17 | News tagging by Haiku with structured JSON output (priority, helps/hurts for holdings incl. inverse ETFs, reason in EN+KO), batches of 20, paused at 100% budget; untagged leftovers get "not labelled" so they are never retried | Low cost, no retry loops |
| 2026-11-17 | Scenario "after costs" now adds the current bid/ask spread (crossing it in and out ≈ one spread, est.) | Phase 3 prompt |
| 2026-11-17 | Orders tab (buy / sell / reserved, market / limit / current price, chat-updated suggestions) recorded for phase 4; always draft → approve → send | User request; order safety rules |
| 2026-11-17 | Toss returns 1 ask / 1 bid level for US stocks (checked live on SOXS); kept as is, no paid depth source for now | Best bid/ask + sizes + spread cover current needs; Level 2 would be paid and needs a docs check |
| 2026-11-17 | Phase 4 decisions: "예약" = Toss conditional orders (SINGLE, plus OCO and OTO); no per-order/day/count limits (optional settings, off); price band and draft expiry set on the Orders page; any US ticker; MARKET allowed outside the regular session with a thin-book warning; Claude Code ask rule for send declined | User decisions |
| 2026-11-17 | Prices with more decimals than Toss accepts are rejected (not truncated); "% of buying power / holding" is converted to whole shares in code and shown before approval; P&L automation moved up to right after the draft step | Never change the user's numbers; user asked why P&L showed zeros |
| 2026-11-17 | P&L "All" became "Holdings": only stocks held now, counted from when each was last bought from zero shares (partial sells inside that holding count); sold-out stocks and earlier round trips are left out; AI credits counted since the oldest holding was bought. "Today" unchanged | User choice: P&L should show the current portfolio, not the whole trading history |
| 2026-11-17 | Chat scenarios shown as cards (entry / stop / target side by side with their level, then reasoning, then order buttons); the Orders tab shows them full width | User found the table hard to read |
| 2026-11-17 | Trade journal scope: only round trips (0 shares -> owned -> 0) that include an order sent by this program (new orders, modifies, orders triggered by its conditional orders); all fills of such a round trip count, incl. earlier buys of shares it sold; unrelated older trades left out. AI credits per trade = usage while it was open (the usage log has no ticker); totals count each call once. DRY RUN sends never fill, so they never appear | User choice |
| 2026-11-17 | Chart "Orders" toggle: real Toss fills as B/S markers on the bar they happened in; open orders, reserved-order triggers and DRY RUN simulated orders as dashed lines | ROADMAP phase 4 item |
| 2026-11-17 | Phone access via Tailscale **serve** (tailnet-only HTTPS proxy to 127.0.0.1:8000); the server still listens on localhost only (Tailscale docs recommend this). Never Funnel. Remote requests must carry Tailscale's `Tailscale-User-Login` identity header (documented: tailnet traffic only, absent for Funnel, stripped if forged); without it they are refused. Host-header rewriting by serve is undocumented, so both cases are handled | Docs-first; no public exposure in any phase |
| 2026-11-17 | One server: start.cmd builds the screen and serves it from the backend on :8000 (`npm run desk` on :5173 stays for development) | One address for the phone; same-origin login cookie |
| 2026-11-17 | Login passcode on every device (also the home PC), scrypt hash in `secrets/login.json` set by the user with set_passcode.cmd; sessions in memory (home 30 d, phone 24 h by user choice); 5 wrong tries -> 15 min lockout. Order PIN for phone sends. Phone sends off by default, limit-only when on | CLAUDE.md phase 5 rules; user never types secrets in chat |
| 2026-11-17 | Home IP change: shown through Toss's existing "IP not allowed" error, no outside IP-lookup service | No extra third-party calls |
| 2026-11-17 | Work laptop: no copy of the app; same Tailscale access as the phone only if the company allows VPN apps | Keys and records stay on one PC; one Toss token |
| 2026-11-20 | Scenario buttons: single orders always (buy / reserved buy for long, sell / reserved sell for short, price on the button); OTO / OCO only when the AI sets the row's plan to entry_exits (entry with take-profit / stop recommended together) or exits (protect held shares). Missing / unknown plan = entry only | User found the combined buttons confusing |
| 2026-11-20 | `remote.allow_remote` (default false): the dashboard works only on the computer it runs on; all requests from other devices refused. The user runs it on their laptop only (Tailscale not allowed there) | User choice |
| 2026-11-20 | Scenario quantity: only when the user asks for one (checked in code against the message), the AI may suggest whole shares in the scenario table, sized from a new read-only `get_account` tool (USD buying power, shares held). Code calculates cost, loss at stop and P&L at target, and warns when it exceeds buying power / shares held. Buttons pre-fill it into the order ticket labeled "AI suggestion"; the user checks it, creates the draft and approves. `create_order_draft` still takes only the user's own numbers. Exception recorded in CLAUDE.md | User choice |
| 2026-11-20 | Chat order drafts: share counts must be whole numbers (fractional share counts are refused in code; the AI asks for a whole number). Reserved buy / sell, OCO and OTO drafts always get an expiry date: the user's date if given, otherwise the AI picks one (today to 1 year, checked in code), says so, and the draft shows an "expiry chosen by the AI" warning (`expire_date_ai`) on the approval screen | User choice |
| 2026-11-20 | Chart drawings Toss-style: AI / user lines, zones, Fibonacci (incl. the default one) are drawn by a canvas primitive (`chart/segments.ts`) only from their start point to just past the latest candle; trendlines continue on their slope; Fibonacci = box with per-ratio colors and bands; labels written on the chart at each line's left end, no price-axis tags. Order lines and the average-price line stay full width. AI hline/zone points get the bar time of the level they match (earliest), else the drawing time (then the line starts 60 bars back). Line key in the bottom-left: press a line or its label to see what it means (type, source, AI note, Fibonacci colors) | User request (Toss reference) |
| 2026-11-20 | Lines belong to one timeframe: optional `timeframe` in the overlay format; `GET /api/overlays?timeframe=` returns that timeframe's lines plus timeframe-less ones. AI lines get the timeframe of the level set their points came from (screen timeframe breaks ties / typed prices); the user's own Fibonacci gets the chart's timeframe; the AI's level list only includes that timeframe's lines. Old lines: timeframe guessed from the label ("1분", "5m", "일봉"; only when exactly one is named), else every timeframe. "Fact" lines (orders, reserved triggers, average price, plans) stay on every timeframe, full width, labeled on the right axis; drawn lines are labeled on the left | User choice |
| 2026-11-20 | Phase 6 plan (`docs/plans/PHASE6_PLAN.md`) adopted; Part A built. Toss spec pinned as `docs/specs/toss_openapi.json` (1.2.28): every Toss fact for new paths is checked against that file | User asked to build Part A; the fetch tool only returned parts of the 478 KB spec |
| 2026-11-20 | Explore is US only and on demand (no background polling): rankings cached 10 s, sector pages 60 s, sector tree and stock names a day; failed name / previous-close lookups wait 5 min / 60 s before retrying | Toss is called only while someone looks; review findings |
| 2026-11-20 | Sector lists show no change % (Toss gives none); computing it from daily candles was offered and left for later | Avoids one extra Toss call per stock per minute |
| 2026-11-20 | Watchlist folders replace the fixed SOXS / SOXX slots: the pinned folder gives the quick buttons (first 12), the strip stock slots (first 8) and joins the news tickers; active ticker and last view remembered in the browser | Phase 6 plan point 5 |
| 2026-11-20 | Automatic rules allowed (CLAUDE.md "Order safety" rewritten): a rule armed once by the user (one tap; PIN on the phone) sends its order when its price condition fires, with no further approval. The AI chat may propose rules and create buy/sell drafts with its own numbers (labeled "AI-proposed") but never approves a draft or arms a rule; Claude Code never sends or arms. Fire-once, expiry, disarm + kill switch, draft checks at fire time, no fire on stale data, DRY_RUN simulates. Quantity exception widened to drafts and rules. Feature not built yet (new phase, plan first) | User choice: sell automatically when a level is hit and then rejected, without being at the screen |
| 2026-11-20 | Automatic rules built ahead of finishing phase 6 (user: "start developing"). Answers to the plan's open questions: rule types as planned + candle-close confirmation (1m / 5m close must stay beyond the level, so a wick doesn't fire); market orders allowed; armed rules stay armed after a restart (half-done touches reset); may fire in pre / after market (optional "regular session only" per rule); AI may pick prices (code levels only), quantities and validity in drafts and rules, labeled AI-proposed; no cap by default, optional Settings cap = max % of USD buying power per rule buy. Plain "price reaches Y" stays a Toss reserved order (refused as a rule). Fire = saved as firing before the send (never twice), normal draft checks against the live price, one send path (OrderSender, DRY RUN included); blocked rules never retry; stale feed (15 s) or a computer sleep pauses rules and drops half-done touches. Tests redirect logs/ to temp folders (conftest) | User decisions 2026-11-20 |
| 2026-11-20 | RSI(14) added to the chart (outside any phase, user request): Wilder smoothing, calculated in the backend (`indicators/rsi.py`, unit tests) on finished bars for 1m / 5m / 1d, warmed up on the full 1m history like EMA; shown in its own panel under the candles with 30 / 70 guides, toggle "RSI" (off by default, saveable on the Settings page). No movement at all = 50. The AI chat does not read RSI yet | User asked for an RSI that can be switched on and off |
| 2026-11-20 | Scenario prices that match no code level (or the user's numbers) are now a warning (`price_warnings`, amber "AI-proposed ... check it") instead of making the row invalid, so the order buttons stay on; wrong price order (e.g. long stop above entry) still blocks. Drafts and rules still require level prices | User viewed such a scenario (stop 66, target 67.27), judged it fine, and asked to allow these orders |
| 2026-11-20 | The chat AI may arm automatic rules when the user's latest message asks for it (new `arm_rules` tool: arm only with arm words in the user's message, e.g. "arm them" / "걸어줘" / "활성화해줘", negations refused; disarm any time; delete unarmed only). Same arm checks as the button; `armed_by: ai` saved, shown in the panel and logged. Replaces "the AI can never arm" | User asked that the AI fill / arm the rules for each trade instead of tapping Arm on each one |
| 2026-11-21 | Chat memory = the last N earlier turns (default 5, Settings -> AI, 1-20) instead of a Haiku summary of long chats. Older turns stay on disk and on screen but are no longer sent; kept turns lose reasoning blocks and their <context> / <candles> blocks, which are re-sent in full with the new message. Past N turns the history part of the prompt is re-cached every message (tools + system stay cached). `llm.summarize_after_tokens` / `keep_recent_turns` in config.yaml are no longer used | User choice |
| 2026-11-21 | Options-implied context started from "Later" (user: "lets start adding the option idea", plan steps 1-4). Source yfinance option chains (Toss / Finnhub free have none). IV = at-the-money straddle mid with the closed-form approximation sigma = straddle / (0.798 x S x sqrt(T)) (not option pricing); Yahoo IV only as a cross-check. Expected move: 1 trading day = S x IV / sqrt(252); to an expiry = S x IV x sqrt(T). Quality per expiry from the ATM spread (good <= 10 %, fair <= 25 %, else thin = not used). Open interest off (unverified, see Data sources); most traded strikes by volume instead. Cache 5 min in the regular session, 30 min otherwise, backoff + stale marker | Live check 2026-11-21 |
| 2026-11-21 | Phase 6 Part B built (steps 5-9). Statements from SEC EDGAR first (D1), yfinance as the only labeled fallback, used only when SEC has no data for the company (an SEC error is shown, never swapped for yfinance). Finnhub `/stock/financials-reported` dropped as a fallback: its own spec says it is the SEC filings dataset. Targets from yfinance, labeled unofficial (D2; Finnhub price-target is premium). History 5 fiscal years + 8 quarters (D3). Peers = top 5 by market cap in the same Toss leaf sector (D7) via a daily Toss sector index (user picked this over Finnhub `/stock/peers`) | User decisions 2026-11-20 / 21 |
| 2026-11-21 | Fundamentals rules: TTM = 4 consecutive quarters, else the latest fiscal year labeled as such (no "TTM" column on screen then); market cap = Toss price x Toss shares outstanding, P/E = market cap / TTM net income; no multiples when the filer reports in another currency than the price; ADR market cap "est."; stock splits flagged from a diluted-share jump (>= 1.5x), never adjusted, own-history P/E stops at the jump; own-history P/E refused when Toss and SEC share counts differ > 5 %; every n/a carries its reason; display strings made in code; Toss daily prices' split adjustment unverified (shown) | Review findings in steps 6-9 |
| 2026-11-21 | One app-wide Finnhub gate for news + fundamentals (1.1 s spacing, a 429 pauses every Finnhub call 60 s incl. queued ones). Research data is fetched only while a Company view is open (on-demand caches, errors keep the last good value); no background polling | Free-plan call cap undocumented |
| 2026-11-21 | "Next event" box linked to real events (user request): BLS (CPI / jobs / PPI / JOLTS / ECI, ET times), FRED (GDP / PCE, date only), FOMC dates in fomc.yaml (no Fed feed), Toss holidays / early closes, calculated monthly options expiry, Finnhub earnings for holdings / screen / watchlist + big semiconductors + top 30 US by market cap (user choice; top 30 from Toss sector lists, daily), your own events on the Settings page (user choice: editor, not config.yaml). Date-only events never get a made-up time. The AI gets the next 7 days in the cached `<context>` block (re-sent when the list changes) | User decisions 2026-11-21 |
| 2026-11-21 | App data files moved into `data/` (user request, tidy-up): `positions.json`, `overlays.json`, `user_settings.json`, `watchlist.json`, `cache/` and `positions.example.json`. Paths set once in `settings.py` (`DATA_DIR`, `CACHE_DIR`); on start the backend moves old root-level files there (`data_dir.py`, never overwrites, tests). `config.yaml`, `.env`, `logs/` and `secrets/` stay where they are | User wanted a tidier main folder |
| 2026-11-21 | Docs moved into `docs/` (user request, tidy-up): `docs/ROADMAP.md`, plans in `docs/plans/` (old Phase 6 draft in `docs/plans/archive/`), the first prompts in `docs/prompts/`, pinned API specs in `docs/specs/`. `CLAUDE.md` and the READMEs stay in the main folder | User wanted a tidier main folder |
| 2026-11-21 | `.env.example` renamed to `env.example` (no leading dot) so Claude Code can read and check the template while the `.env` / `.env.*` block rules stay unchanged; it holds variable names only and stays committed | User wanted Claude to see the template; nothing confidential in it |
| 2026-11-21 | Chat feature ideas added as a roadmap section (not started): 3 fixes to do first, then 8 ideas in priority order (journal access and risk / sizing first); details in `docs/plans/CHAT_FEATURE_IDEAS.md`. "Later" items for journal stats and alerts now point there | User wrote the list after a review of the chat, orders and rules code |
| 2026-11-21 | Cloud server approved (`docs/plans/CLOUD_PLAN.md`): AWS Lightsail Seoul, Tailscale-only (CLAUDE.md security rule extended to the user's own cloud server); PC becomes the workshop (`deploy.role`), per-machine settings in `data/config.local.yaml`; trusted devices replace "PIN for every order" (trusted browser = home-PC order rules; other devices keep the phone rules); Yahoo blocking on the server accepted for now | User choices 2026-11-21 |
| 2026-11-21 | Phase 6 Part C built (C1-C6). D5: each ticker has its own chats per page (desk / research); a chat refuses another ticker's question; the chat database is upgraded once (symbol + kind columns, old chats filed under the ticker of their first question, nothing deleted). D8: the research chat defaults to Normal (Sonnet) like the Desk | User decisions 2026-11-21 |
| 2026-11-21 | D4 changed: a thesis is saved by the user's button, or by the AI only when the user's own latest message asks for it (save words checked in code; refusals, questions, talk about a saved thesis and the noun "store" do not count), and only for a valuation card whose text passes the number check. A thesis holds only numbers the code made (the server loads the card from the saved chat; the AI cannot rewrite the summary). Thesis words, saved cards and candidate reasons never count as sources for the number check | User choice 2026-11-21 + review findings |
| 2026-11-21 | Research chat: separate fixed prompt and tool list (own prompt cache), no order / rule / drawing tools; the fundamentals pack goes in a <fundamentals> block re-sent only when its values change (timestamps ignored); price-dependent values ride in <live>. Number check for research answers covers percents, multiples (x, ×, 배), scaled amounts with their unit (B / M / T / bn / 억 / 조) and prices with or without decimals; dates, times and ids in the data never vouch for a number | Plan 6.1 + review findings |
| 2026-11-21 | Thesis store in `logs/research/research.sqlite` (gitignored, like the chat history), notes capped at ~1,500 tokens per ticker; Desk shows ticked reference prices as derived dashed lines (own "Thesis lines" toggle, USD only) and gets them as get_levels kind "research"; the Desk prompt got a short static thesis section (one cache rewrite) | Plan 6.2 / 6.3 |
| 2026-11-21 | Share repo for friends: a separate private, history-free copy pushed by `scripts/share/update_share.py` (committed files only; `docs/prompts/`, `docs/plans/archive/` and the tool itself left out; stops on key files or key-like text); MIT license + disclaimer and a friends' quick-start at the top of the READMEs; chat risk profile became a setting (default "standard", the owner sets "aggressive") | User choices 1a (MIT), 2a (GitHub noreply email), 3 standard |
