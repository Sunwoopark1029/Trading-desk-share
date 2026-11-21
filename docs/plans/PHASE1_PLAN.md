# Phase 1 plan (approved 2026-11-16)

Read-only dashboard. **No order code at all.** ROADMAP.md and CLAUDE.md rules apply; this file fixes the details.

## User decisions
- FRED: enabled (`FRED_API_KEY` in `.env`, v1 `fred/series/observations`, key as query param → never let the URL reach logs/errors).
- Fees: 0.1% (`fee_rate: 0.001`) on each buy and each sell separately; an order whose total amount is under $10 USD has no fee (`fee_free_below_usd: 10`).
- Chart labels each bar by its **start** time. Backend keeps Toss's end-time stamps internally; the API returns both `end` and `start` (= end − interval; daily = the US trading date).
- Trades entered by hand in `positions.json` (no on-screen trade form). Plan prices are entered on screen.
- Optional typed-in average price per symbol (`avg_override`), which replaces the computed average when set.

## Build order (commit after each step; `git status` check before every commit)
1. Repo files: `.gitignore` (+`overlays.json`, `cache/`), `config.yaml`, `positions.example.json`
2. Backend skeleton, settings, `/api/health`, `setup.cmd`, `start.cmd`
3. Toss client + tests
4. Market data, cache, pollers
5. Indicators + time tests
6. P&L, store, overlays API + tests
7. Frontend skeleton, proxy, layout/tabs, placeholders
8. Chart
9. Top strip
10. My-info lines, plan form, P&L panel
11. README + check-list
12. ROADMAP: Phase 1 done + decision log

## Layout
```
backend/
  requirements.txt
  app/
    main.py          FastAPI app, routes under /api, lifespan starts pollers (NO token issued at startup)
    settings.py      config.yaml (ruamel.yaml) + .env (pydantic-settings, keys as SecretStr)
    timeutil.py      KST/ET helpers, US trading date, session lookup
    cache.py         CacheEntry(value, updated_at, source, delay_label, status ok|stale|error, error_code)
    store.py         positions.json / overlays.json read+atomic write
    pnl.py           P&L
    overlays.py      overlay model (shared format) + validation
    data/
      toss.py        client (allowlist, token, rate limiter, errors) + market calls
      yf_source.py   yfinance batched strip quotes
      fred_source.py DGS10
    indicators/
      candles.py     Bar model, 1m→5m aggregation, session tagging
      ema.py vwap.py fib.py swings.py
  tests/  (+ tests/fixtures/*.json from Toss doc examples; never name a file token*.json or credentials*.json)
frontend/  Vite react-ts; src/{main.tsx,App.tsx,api.ts,format.ts,styles.css,components/*,chart/*}
config.yaml  positions.example.json  setup.cmd  start.cmd  README.md
```

## Backend rules (from review)
- **Toss allowlist** in code: only `POST /oauth2/token` and GET `/api/v1/candles`, `/api/v1/prices`, `/api/v1/market-calendar/US`, `/api/v1/exchange-rate`, `/api/v1/stocks/all`. Anything else raises. Never send `X-Tossinvest-Account`. Test it.
- **Token**: issued lazily on first call, kept in memory only, one asyncio lock; reissue when within 5 min of `expires_in` or on 401 expired/invalid token (once). On `token-revoked`: do NOT auto-reissue; set Toss status `error: token replaced elsewhere (second copy running?)`; health offers a retry endpoint `POST /api/health/toss/retry`.
- **403** `ip-not-allowed` / token `access_denied` → status message "Register this PC's IP in Toss WTS (Settings > Open API)".
- **Rate limiter**: per group, documented limits in config × `rate_limit_safety` 0.8. Groups used: AUTH 5, MARKET_DATA 15 (prices), MARKET_DATA_CHART 20 (candles), MARKET_INFO 3 (calendar, FX), STOCK_ALL 1. 429 → honor `Retry-After`, retry once. Limiter testable with an injected clock.
- Prices are decimal strings → `Decimal`. Parse timestamp offsets; never assume `+09:00`. `before` cursor must encode `+` as `%2B`.
- **No secrets in errors**: catch httpx errors and keep only status code + short code; never `str(exc)` (URLs can hold the FRED key). Set `httpx`/`httpcore` loggers to WARNING. Test: FRED 500 → key string absent from cache error text and `/api/health`.
- `TrustedHostMiddleware(allowed_hosts=["127.0.0.1","localhost"])`. Bind 127.0.0.1 only (set in start script, not config).
- yfinance is synchronous → `asyncio.to_thread`. One batched daily download for `NQ=F ES=F ^ICESEMI ^VIX ^TNX KRW=X`; `dropna()` per symbol before taking last two closes; backoff 2→4→…→900 s on errors. Label "fetched HH:MM KST · yfinance ~N min". If |^TNX − DGS10| > 1 flag "unit?".
- FRED: `series_id=DGS10&sort_order=desc&limit=5&file_type=json`, skip "." values, every 6 h. Disabled with status "no key" if key empty. README + tooltip attribution: "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis."
- FX: show and convert with `midRate` ("Toss mid"); estimated FX cost uses `basisPoint` (label est., meaning UNVERIFIED) unless `fx_spread_bp` set in config.
- Previous close: the 1d bar whose NY date = calendar previous business day.
- Live 1m merge: poll `count=5`, upsert by timestamp (replace). Calendar cached per US date; history needs ~3 trading days (5m) / 2 (1m); old-date calendar support UNVERIFIED → if it fails, no shading for those days.
- Stale = age > `stale_after_factor` (3) × poll interval. Errors keep the last good value.
- Ticker input: `^[A-Za-z0-9.\-]+$`, upper-cased, must exist in the cached stock list (NYSE, NASDAQ, AMEX, US_ETC merged; symbol match first, name best effort) → else 400.
- `positions.json`: read `utf-8-sig`, `json.loads(parse_float=Decimal)`; invalid JSON → error shown, never overwritten. `PUT /api/positions/plans` re-reads file, replaces only `plans`, writes temp + `os.replace`. Plan needs currency + positive price; never defaulted.
- Async tests: anyio pytest plugin + `httpx.MockTransport`. No real network in tests.

## API (frontend uses these)
| Path | Returns |
|---|---|
| `GET /api/health` | per-source status/updated/error; keys "set"/"empty" only |
| `POST /api/health/toss/retry` | clears token-revoked state |
| `GET /api/tickers?q=` | matches from cached stock list |
| `GET /api/candles?symbol=&tf=1m\|5m\|1d` | bars `{start,end,open,high,low,close,volume,session}` (epoch s), `ema:{9,20,60}`, `vwap` (not 1d), `fib` (default overlay with levels; not 1d), `swings` markers, `sessions` for shading, meta (source, updated, status, extended-hours note) |
| `GET /api/strip` | items: USD/KRW, NQ, ES, ICESEMI, VIX, 10Y FRED, 10Y ^TNX, SOXS, SOXX: value, change, change_pct, unit, source, delay, updated, status; plus next event |
| `GET /api/positions` | trades, plans (+USD conversion with rate & time), avg_override; plan/avg lines also as `hline` overlays (`source:"user"`, derived, not stored) |
| `PUT /api/positions/plans` | save plans as entered |
| `GET /api/pnl?period=today\|all` | per symbol + total: qty, avg, unrealized, realized, fees (confirmed/est.), fx_cost (est.), ai_credits (0, Phase 2), net; USD and KRW; FX rate + time |
| `GET/POST /api/overlays`, `DELETE /api/overlays/{id}` | shared overlay format; fib overlays returned with computed `levels` |

## Calculations
- Times: store UTC epoch seconds; display KST. US trading date = 20:00→20:00 America/New_York (tzdata required).
- Session tag: bar with end t belongs to session with start < t ≤ end (Toss calendar).
- 5m: 1m bars with end in (T−5m, T] → 5m bar ending at T (T on :00/:05…). O first, H max, L min, C last, V sum. Empty window → no bar. Never fill zeros.
- EMA(N): α=2/(N+1), seed = SMA of first N closes, earlier bars null, continuous across gaps; fetch extra history for EMA60 warm-up.
- VWAP: typical price (H+L+C)/3 weighted by volume, from 1m bars; anchor at pre-market start of the US trading date (calendar), includes pre/regular/after, excludes day market, resets at next pre-market start; zero total volume → null. 5m chart shows VWAP at each 5m end. Not on daily. If no pre-market bars exist, label "regular only".
- Fib default: low/high of current trading date pre+regular+after (before pre-market opens: previous trading date). Levels 0, .236, .382, .5, .618, .786, 1. Low before high (up move): level = high − r·(high−low); else low + r·(high−low). User-picked: two points saved as `fib` overlay; backend computes levels. Daily: no default.
- Swings: N=3 each side; high strictly above N before and ≥ N after (first bar of a flat top); mirror for lows; last N bars unmarked.
- P&L: average cost. Buy updates avg (USD; KRW via record fx_rate, or Toss reference rate at that time labeled est.). Sell: realized = (price − avg)·qty, avg unchanged. Position 0 → avg reset. Oversell → error, no crash. Unrealized USD = qty·(live − avg); unrealized KRW = qty·live·current mid − KRW cost basis. Fees: entered = confirmed; else `fee_rate`·order total, 0 if total < $10 (est.). Net = realized + unrealized − fees − fx_cost − ai_credits. Decimal throughout; round only for display (USD 2 dp, KRW 0 dp with separators). `avg_override` replaces computed avg for unrealized when set (labeled "typed").

## Tests required
Time (offset parsing, trading-date boundary, DST: 2026-03-06/03-09, 2026-10-30/11-02, yfinance tz → KST); sessions (boundary, holiday null); 5m (normal, missing minutes, empty window, end stamp, pre→regular boundary); EMA (seed, hand-calculated, too few bars); VWAP (anchor, day market excluded, reset, zero volume); Fib (up, down, user points); swings (peak, flat top, edges); P&L (partial sell, rebuy avg, mixed KRW/USD, fee confirmed vs est., <$10 free, full exit + re-entry, oversell, avg_override, net); Toss (fixtures, Decimal exact, error envelope, expired vs revoked, 429 once, allowlist, `%2B`); limiter (fake clock); secrets (FRED 500 key absent); overlays (validation, CRUD); positions (BOM, invalid JSON not overwritten, plans-only write).

## Unverified → checked with one read-only call after keys + IP are set
Whether 1m bars include pre/after/day market; old-date calendar; FX `dateTime`; orderbook level count (Phase 3).

## Dependencies
Python 3.15 venv at `backend/.venv`: fastapi, uvicorn (plain), httpx, pydantic-settings, ruamel.yaml, tzdata, yfinance, pytest (pin after install). Frontend: react 19, lightweight-charts 5.2.1, vite 8.3.5, @vitejs/plugin-react, typescript, concurrently. `start.cmd` checks port 8000 free, then runs both via `npm.cmd` + concurrently (uvicorn `--app-dir ..\backend --host 127.0.0.1 --port 8000`, vite `127.0.0.1:5173 strictPort`, proxy `/api`).
