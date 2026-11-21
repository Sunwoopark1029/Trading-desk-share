# Personal Trading Desk

A dashboard that runs on your own computer and is only reachable from it (`127.0.0.1`; your phone can join through a private Tailscale link, section 8). It shows a chart with indicators, a market strip, your P&L, an orderbook and news, a **Research** view (rankings, sectors, watchlist folders, company financials), and an AI chat that can draw on the chart and propose scenarios.
**Orders:** Toss orders go draft → your approval → send (section 7), plus optional automatic rules that only fire once armed (section 7.8). **DRY RUN is the default**: nothing is really sent until you switch it off yourself. The full design is in [docs/ROADMAP.md](docs/ROADMAP.md).

> **Disclaimer:** this is a personal project shared as-is under the [MIT License](LICENSE), with no warranty.
> It is not investment advice, and the AI chat can be wrong. You alone are responsible for every order you
> approve or rule you arm. Use **DRY RUN** until you trust it.

**Got this from a friend?** You need your own of everything; nothing of the owner's is in this copy:
1. A Windows PC (section 1), and your own **Toss Securities Open API** access with your PC's IP registered (2.1).
2. Your own **Anthropic API key** for the chat (2.3): you pay for your own usage, and you set the budget in Settings.
   Finnhub (news), FRED and the SEC line are optional (2.2, 2.2b, 2.2c).
3. Put your keys in `.env` yourself (2.4). Never share `.env`, never paste keys into a chat, never commit it.
4. Keep **DRY RUN on** (the default) and try everything with simulated orders first (section 7.1).
5. **⚙ Settings → AI chat → Risk profile** is **Standard** by default. "Aggressive" tells the AI that losing the
   whole account is acceptable; choose it only if that is true for you.

한국어 설명서: [README.ko.md](README.ko.md)

All the double-click scripts are in the **`scripts`** folder.

---

## 1. Install (one time)

### 1.1 Python 3.15
1. Download Python 3.15 for Windows from https://www.python.org/downloads/ and run the installer.
2. On the first installer screen, tick **"Add python.exe to PATH"**, then click **Install Now**.
3. Check it: open **Command Prompt** (press Windows key, type `cmd`, press Enter) and run:
   ```
   py --list
   ```
   You should see a line with `3.15`.

### 1.2 Node.js
1. Download the **LTS** version from https://nodejs.org/ and run the installer with the default options.
2. Check it in a **new** Command Prompt window:
   ```
   node -v
   ```
   You should see a version number such as `v26.x.x`.

### 1.3 Project packages
Double-click **`scripts\setup.cmd`** in the project folder. It:
- creates the Python environment in `backend\.venv` and installs the Python packages
- installs the screen's packages in `frontend\node_modules`

It ends with "Setup finished OK". If it says FAILED, read the message just above it (usually Python or Node is missing; repeat 1.1 or 1.2).

---

## 2. API keys

Keys live **only** in the file `.env` in the project folder. They are never sent to the browser, never shown on screen, and never committed to git.
The screen only ever shows whether each key is "set" or "empty".

### 2.1 Toss Securities Open API (required: quotes, candles, FX rate)
1. Sign in to the Toss Securities Open API developer site: https://developers.tossinvest.com/
2. Create an app and copy its **Client ID** and **Client Secret**.
3. **Register this computer's IP address** in Toss WTS: **Settings > Open API > allowed IPs**.
   To find your public IP, open https://www.whatismyip.com/ in the browser (the "IPv4" value).
   Without this step, Toss refuses every call and the screen shows "Register this PC's IP in Toss WTS".
   If your home IP changes (for example after a router restart), register the new one.

Only **one copy** of the dashboard may run at a time: each copy gets its own Toss token, and a new token cancels the old one.
If the screen says "token replaced elsewhere", close the other copy, then click **Retry Toss** in the yellow bar.

### 2.2 FRED (optional: official US 10-year yield)
1. Create a free account at https://fredaccount.stlouisfed.org/ and request an API key under **API Keys**.
2. Without this key the "10Y FRED" box shows "no key"; everything else still works.

### 2.2b Finnhub (news feed)
1. Create a free account at https://finnhub.io/ and copy your API key from the dashboard.
2. Put it in `.env` as `FINNHUB_API_KEY=...` (and the name `FINNHUB_API_KEY=` in `env.example`).
3. Without it the News panel says "Add FINNHUB_API_KEY to .env"; everything else works.
The Research view also uses this key for the company profile, the last 4 EPS surprises, the earnings calendar and analyst ratings.
Finnhub covers North American companies (US stocks and ETFs). Massive (`MASSIVE_API_KEY`) is an optional backup that is **off** for now: its API address wasn't confirmed in the docs.

### 2.2c SEC EDGAR (Research: company financials, no key)
SEC gives financial statements for free, but asks every app to say who is calling.
1. In `.env`, set `SEC_USER_AGENT=` to a name and your email, e.g. `SEC_USER_AGENT=Trading Desk your.name@example.com` (letters, digits and normal symbols only).
2. It is sent only to SEC, never logged or shown.
3. Without it the Research view has no financial statements or metrics from SEC; prices, rankings and news still work.

### 2.3 Anthropic API key (for the AI chat)
The chat uses the Claude API. **A Claude Pro or Max subscription doesn't cover API use.** API calls are billed separately, as prepaid credits on an Anthropic Console account.
1. Sign in at https://console.anthropic.com/ (it can be the same login as claude.ai).
2. Under **Billing**, buy credits. Optionally set a spend limit there as a second safety net. The dashboard has its own budget too (section 6.5).
3. Under **API Keys**, create a key and copy it. It starts with `sk-ant-`.
4. Put it in `.env` (next step). Without it, the chat shows "key not set" and makes no calls.

### 2.4 Fill in `.env`
1. In the project folder, copy `env.example` and name the copy `.env`.
   (In File Explorer: select `env.example`, Ctrl+C, Ctrl+V, then rename the copy (`env - Copy.example`) to `.env`.)
2. Open `.env` in Notepad and put each value after the `=` sign, with no spaces or quotes:
   ```
   TOSS_CLIENT_ID=your-client-id
   TOSS_CLIENT_SECRET=your-client-secret
   FRED_API_KEY=your-fred-key
   ANTHROPIC_API_KEY=your-anthropic-key
   FINNHUB_API_KEY=your-finnhub-key
   ```
   If your `env.example` has no `ANTHROPIC_API_KEY=` line yet, add that line (name only, no value) to `env.example` too.
3. Save and close. Never paste these values into a chat, an issue, or a screenshot.

---

## 3. Your trades and average price (`data/positions.json`)

The app keeps its own files (trades, saved lines, Settings page values, watchlist folders, downloaded data) in the `data` folder.

The P&L panel and the average-price line come from trades you type into `data/positions.json`.
1. In the `data` folder, copy `positions.example.json` and name the copy `positions.json`.
2. Replace the example trades with your own: `time` (must include the UTC offset, e.g. `+09:00`), `symbol`, `side` (`buy`/`sell`), `qty`, `price`, `currency` (`USD`/`KRW`).
   `fee` and `fx_rate` (the KRW-per-USD rate actually applied) are optional. A missing fee is estimated and labelled **est.**
3. Optional: `avg_override` sets an average price by hand. It replaces the computed one and is labelled **typed**.
4. Scheduled buy/sell prices ("plans") can be typed here or added on screen (section 5).

If the file has a JSON mistake, the screen shows the line and column. The file is never overwritten while it's broken.
`positions.json` is in `.gitignore`, so it stays on your computer.
If you used the app before these files moved into `data` (2026-11-21), the backend moves your old files there by itself the next time it starts. It never overwrites a file that is already in `data`.

---

## 4. Run and stop

- **First time only:** double-click **`scripts\set_passcode.cmd`** and choose a **login passcode** (at least 8 characters) and an **order PIN** (4–8 digits). You type them in that window; nothing is shown, and only a scrambled hash is saved (in `secrets\login.json`). Run it again any time to change them.
- **Start:** double-click **`scripts\start.cmd`**. It builds the screen (a few seconds), then runs the dashboard at `http://127.0.0.1:8000` (talks to Toss, yfinance, FRED, Finnhub and Claude; holds the keys).
- **Open:** go to **http://127.0.0.1:8000** in your browser and log in with your passcode. This computer stays logged in for 30 days.
- **Stop:** click the `scripts\start.cmd` window and press **Ctrl+C** (answer `Y` if asked), then close the window.

If it says "Port 8000 is already in use", the dashboard is probably already running in another window. Use that one, or close it first.
---

## 5. Using the screen

**Views:** the buttons at the top switch between **Desk** (chart, P&L, chat, orderbook, news), **Research** (section "Research view" below), **Orders** (section 7), **Journal** and **⚙ Settings**. The app remembers the last view and ticker.

**Clicking a ticker** almost anywhere (top strip boxes, P&L, open and reserved orders, Orders page tables, rules, journal, events list, scenario titles, tickers named in chat answers) makes it the current ticker and opens Research.

**Language:** the first time you open the dashboard, choose **English** or **한국어**. The button at the top right switches it at any time, and the choice is saved in your browser. In Korean the AI also answers in Korean (level ids like `vwap_5m`, `USD` and `KST` stay as they are). Korean answers use more tokens than English, so each message costs a little more.

| Area | What it shows |
|---|---|
| Yellow bar (top) | Only appears when something is wrong: a key not set, Toss IP not registered, a source stale or failing |
| Top strip | USD/KRW (Toss reference rate), NQ and ES futures, ICESEMI, VIX, US 10Y (FRED daily + ^TNX intraday), the first 8 tickers of your **pinned watchlist folder** (Toss quotes), and the **Next event** box (below). Each box shows its source and delay (e.g. "yfinance ~15 min") and when it last updated. Orange means **stale**; red means **error**. Hover a box for details |
| Chart | Quick buttons (the first 12 tickers of your pinned watchlist folder) or type any US ticker in the search box. **1m / 5m / Daily.** Top right: **Screen high / Screen low**: the highest and lowest price of the candles on screen and how far the current price is from each, in % (follows scrolling and zooming). Checkboxes **under the chart**: EMA 9/20/60, VWAP, RSI (RSI 14 in its own small panel under the candles, with 30 / 70 guide lines and the current value in the key), Fib, Swings (arrows at swing highs and lows), My lines, AI lines, Orders (your fills and open orders, section 7.4), Options bands (section 6.7), Sessions (shading: purple = day market, blue = pre-market, no shading = regular, orange = after-hours), Volume. The last-price label on the right shows how far the current candle has run, e.g. `37/60s` on 1m or `2:37/5:00` on 5m (`closed` when the latest candle is over). The newest candle moves with the latest Toss quote (about every 2 s); EMA, VWAP and RSI use finished bars. The Settings page decides which checkboxes start ticked |
| Pick Fib | Click **Pick Fib**, then click two bars. Each click snaps to that bar's high or low. The backend calculates the levels and saves your Fibonacci for that ticker. Delete it under "Saved lines" below the chart |
| P&L | Realized, unrealized, fees (confirmed vs **est.**), FX cost (est.), AI credits (Claude API spend), and **Net P&L**, in USD and KRW, with the FX rate and time used. "Today" / "Holdings" (only the stocks you hold now; section 7.4) |
| My plans | Add a scheduled buy/sell price for the current ticker in **USD or KRW** (you must choose). It's saved to `positions.json` and drawn as a green (buy) or red (sell) line. KRW prices are converted with the Toss reference rate, shown next to them. Your average price is drawn as an orange line |
| Chat | AI chat (section 6) |
| Orderbook | Live ask/bid ladder for the chart's ticker (Toss stream; any US stock or ETF). Red rows = asks (sellers), green rows = bids (buyers), bars = size. The middle row is the **spread** (best ask − best bid, in USD and % of the price); it turns **orange** when wider than `spread.warn_pct` (0.20%), which means market orders would get a worse price. The top line shows the session (day / pre / regular / after), how many levels Toss returned, and the stream status: **live**, or "disconnected, retrying in N s · polling every 2 s" (it reconnects by itself and keeps working by polling meanwhile). Below: recent trades (green = uptick, red = downtick) |
| News | News fetched by code from Finnhub for the tickers on screen, held, related (`news.related` in `config.yaml`) and `news.extra`. Each item: headline (link), source, time in KST (or **date unverified**), tickers, and an AI tag: priority **high** (red) / **medium** (amber) / **low** (grey), effect on what you hold (**helps** green / **hurts** red / unclear), and a one-line reason. Duplicates are merged; items older than 48 h are hidden. Filter by All / this ticker and by priority |

- All times are Korea time (KST). Intraday bars are labelled by their **start** time. Daily bars are labelled by US trading date.
- The default Fibonacci runs from today's session low to high (pre-market through after-hours). Before pre-market opens, it uses the previous trading day.
- VWAP starts at the pre-market open (18:00 KST in winter, 17:00 KST in summer). It includes pre-market, regular and after-hours, and leaves out the Korean day market.
- On a phone-width window (≤ 430 px) the layout switches to tabs at the bottom: Chart / P&L / Chat / Orderbook·News / Research / Orders / Journal / Settings.

---

### Next event box and the event calendar

The **Next event** box (top right of the strip) shows the next scheduled event with its KST time and a countdown;
"+N" = more events in the next 14 days. **Click it** for the full list, grouped by day:

| Event | Source | Time |
|---|---|---|
| US CPI, jobs report, PPI, JOLTS, employment cost index | BLS release calendar | exact (e.g. CPI 08:30 ET = 22:30 KST in winter) |
| US GDP, PCE inflation | FRED (BEA release dates) | date only |
| FOMC rate decisions (+ projections) | Federal Reserve calendar (kept in `backend/app/events/fomc.yaml`; add new years from the Fed page) | date only |
| Earnings | Finnhub, for your holdings, the screen ticker, your watchlist, big semiconductors (NVDA, AVGO, AMD, TSM, MU, ...) and the top 30 US stocks by market cap | before the open / after the close |
| US market holidays and early closes | Toss market calendar | exact |
| Monthly options expiry (quarterly = quad witching) | calculated (third Friday, 16:00 ET) | exact |
| Your own events (elections, ...) | **Settings → My events** (and the old `events:` list in config.yaml) | as you enter it |

"date only" = the source publishes no time; the desk never makes one up. Sources refresh in the background (earnings
every 6 h, the rest daily); a source that fails keeps its last list and is named at the bottom of the popup.
The AI chat sees the next 7 days and mentions events that fall inside a plan's time frame.
Delete the "Example: US CPI release" entry from `config.yaml` (`events:`) once you no longer want it.

### Research view

Press **Research** at the top. Left: the **Explore** rail. Middle: your saved **thesis** and the **Company view** of the current ticker. Right: the **research chat** (below the company view on narrower screens; on a phone, switch with **Explore / Company / Chat** at the top). **Open on Desk →** takes the ticker to the chart.

**Explore rail** (three tabs):
- **Rank**: Toss rankings, top 100: Volume, Amount, Gainers, Losers, Toss popular (Toss executions only), for Live / 1D / 1W / 1M / 3M / 6M / 1Y (gainers and losers have no "Live"). Change is vs the previous close, or over the period for gainers / losers. ETFs fill most of the volume ranking.
- **Sectors**: Toss's own sector list (top sectors or all), names in Korean. Click a sector for its stocks by market cap and related ETFs. Amounts are in KRW (Toss converts US stocks too); market cap updates daily; Toss gives no change % here, so prices are fetched for the top stocks only.
- **Watch**: your watchlist folders (saved in `data/watchlist.json`). **+ Folder**, rename, delete, and **Pin**. The pinned folder gives the chart's quick buttons and the first 8 stock boxes in the top strip, and its tickers join the news feed. Add or remove a ticker with **★** (next to any ticker) or the search box. Up to 20 folders of 100 tickers.

**Company view** (search any US ticker in its header; no folder needed):

| Tab | What it shows | Source |
|---|---|---|
| Overview | Tiles: market cap, enterprise value, P/E, P/S, P/B, EV/EBIT, FCF yield, growth (revenue YoY and 3 / 5-year CAGR, EPS YoY), margins, ROE, net debt, free cash flow, share count change, P/E vs its own history and vs peers with the price each implies, analyst target, next earnings | Calculated in code from SEC figures and the live Toss price |
| Financials | Income statement, balance sheet, cash flow: 5 years (Annual) or 8 quarters (Quarterly), plus TTM. `*` = derived in code from reported figures (hover for the source); `—` = not reported | SEC EDGAR (yfinance, labelled unofficial, when SEC has none) |
| Earnings | EPS surprises (actual vs estimate, last 4 quarters), earnings calendar, diluted EPS as reported to SEC | Finnhub, SEC |
| Analysts | Rating counts (strong buy … strong sell) and price targets (low / median / mean / high vs current) | Finnhub, yfinance |
| News | Company news from the last 30 days (date unverified is marked) | Finnhub |
| Peers | Top companies by market cap in the same Toss sector, with P/E medians | Toss sectors + the sources above |

- ETFs have no company financials; the view shows the Toss basics instead.
- Every value shows its source; unofficial (yfinance) values are labelled. The AI never makes these numbers.
- The first visit of the day builds a sector index for Peers (a short "Building the sector index" note); it's saved in `data/cache/`.

**Research chat** (its own chats per ticker, separate from the Desk chat):
- Ask about the company: "is it cheap?", growth, margins, earnings, peers, news. The AI only quotes numbers the code calculated (the Company view's values, with their ids); a number it can't trace is listed under the answer as "unverified".
- **Valuation** button (or ask): a **valuation card** with three separate verdicts (vs its own history, vs peers, vs growth: cheap / fair / expensive / unclear), the values it rests on, risks, what would change the view, and reference prices (price at the median P/E, analyst targets) with their source and time. "Unclear" means the data for that axis is missing.
- Finding stocks: ask for today's most traded, top gainers, a sector's leaders, or a name; suggested tickers appear as buttons that open the ticker. They are suggestions to look at, not trade advice.
- The research chat has **no order tools**: to trade, press **Open on Desk**.

**Thesis** (one per ticker, history kept; saved in `logs/research/research.sqlite`):
- **Save thesis** on a valuation card: you can edit the summary (max 400 characters) and tick which reference prices to show on the Desk chart. The AI saves one only when your own message asks for it ("save it", "저장해줘"), and only a card whose numbers check out.
- The thesis card (top of the Company view) shows the verdicts, the price when saved and how much it moved since, a warning when a newer SEC filing exists, and days to earnings. **Edit**, **Delete**, **History**.
- On the **Desk**: a short thesis card above My info (**Open in Research**), the ticked reference prices as dashed purple **Thesis lines** on the chart (own chart toggle), and the Desk AI can see the thesis and cite its prices (ids `thesis_...`). Order rules don't change.

**Notes** (under the thesis): short dated lines you type (max 300 characters); the app adds facts it knows ("Thesis saved by you…"). Both chats see the thesis and notes. No AI writes notes.

## 6. AI chat

Each ticker has its own chats (on the Desk and, separately, on Research). Opening a ticker shows its chats and the one you had open last; **New** starts another one for that ticker. A chat stays with its ticker: to ask about another ticker, open that ticker. An answer still being written keeps going if you switch ticker and is saved in its own chat.

### 6.1 Asking
- Type a question and press **Enter** (**Shift+Enter** for a new line). The answer appears as it's written.
- The chat knows which ticker and timeframe your chart shows, and which lines are visible.
- Examples: "where are the support levels on SOXS 5m?", "draw a trendline on the last two swing lows", "redo the Fibonacci from the last swing high to the last swing low", "give me scenarios for SOXS".
- **News:** ask "any important news on NVDA?". The chat searches the News feed and answers with links, dates and the AI tags. Scenario tables now include the current bid/ask spread in "after costs".
- **Prices come only from the dashboard's own calculations** (EMA, VWAP, Fibonacci, swings, previous day, your average price and plans) or from numbers you typed.
  - The AI cites a level id next to each price, e.g. `4.12 (vwap_5m)`.
  - Any lines it draws are checked in code. A price that isn't a calculated level is refused, and the AI has to fix it.
  - If an answer contains a number that isn't in the dashboard's data, the message shows **"Unverified number"**.
- **Drawing:** AI lines appear on the chart right away (light blue, with an "AI" tag). Hide or show them with the **AI lines** toggle, separately from your own lines. The AI can only delete its own lines.
- **Scenarios:** asking for scenarios gives a real table: direction, entry, invalidation, target, R/R, move after costs (est.), and the levels behind each price.
  - R/R and the after-cost move are calculated in code: 0.1% fee on each side plus the FX spread estimate.
  - Clicking a row sends nothing. Each scenario card has buttons that only fill in the order form (section 7.3).
- **Chats** are saved on your computer in `logs/chat/chat.sqlite`. Use the drop-down to switch chats, **New** to start one, and **Delete** to remove one.
- **Long chats:** past about 60,000 tokens, Claude Haiku summarizes the older part. Summarized messages are shown faded.

### 6.2 Model switch
| Switch | Model | Use for |
|---|---|---|
| **Normal** (default) | Claude Sonnet 5.5 | Everyday questions, lines, scenarios |
| **Deep** | Claude Opus 5.5 | Important entry or exit decisions (about 2x the cost) |
| **Very deep** | Claude Fable 5.2 | Big calls, e.g. around earnings (about 4x the cost) |

Summaries of long chats run in the background on Claude Haiku 5.5.
Model IDs and prices are in `config.yaml` (`llm:`). To check that the IDs are valid, double-click **`scripts\check_models.cmd`**. It's free and uses no tokens.

If a model's safety filter declines a question, Anthropic retries it on a fallback model automatically (`refusal_fallback` in `config.yaml`). The cost line then shows the model that actually answered.

### 6.3 Web search
The **Web search** box in the chat turns on Claude's web search for that message. It's off by default.
- It costs USD 10 per 1,000 searches, plus the result text (billed as input). At most 3 searches per message (`config.yaml`).
- Results are background only, with links and dates. Prices and levels never come from the web.
- Turning it on or off changes the request, so the next message pays once to rebuild the cache.

### 6.4 Reading the cost line (under every answer)
`Sonnet 5.5 · in 310 · out 52 · cache write 2,000 / read 2,000 · hit 46% · USD 0.0093 · KRW 13 (1 USD = 1,375.00 KRW, Toss ref. 09:30:00 KST)`
- **in:** new input tokens at full price.
- **out:** answer tokens, including the model's thinking.
- **cache write / read:** tokens stored to, or re-used from, Anthropic's prompt cache. Reads cost 1/20 of the normal input price; writes cost 1.25x (1-hour writes 2x).
- **hit:** the share of the prompt served from the cache. It's high in a continuing chat. It drops for the next message after you switch models or turn web search on or off, because each model has its own cache.
- **USD / KRW:** exact cost from the token counts and the prices in `config.yaml`, converted with the Toss reference rate at that moment.

Every API call is also written to `logs/usage.jsonl`: time, model, tokens, cost, and FX rate. Message text and keys are never written there.

### 6.5 Budget
- The header above the chat shows **today's** and **this month's** spend (KST days and months), the limits, and the share by model.
- Limits are in `config.yaml` under `budget:` (now USD 5 per day and USD 100 per month).
- At **80%** the header turns orange.
- At **100%**, Deep and Very deep are blocked, and Normal asks you to confirm before each message.
- AI credits also appear in the **P&L** panel and are subtracted from **Net P&L**.

### 6.6 Raw candles and the A/B test
The chat now sends the AI **raw candles** as well as the levels (`llm.candles.mode: B` in `config.yaml`, chosen 2026-11-17). That means every 5m bar since the pre-market open, only the new ones each message, plus the last 60 1m bars. To go back to levels only, set `mode: A`. The A/B test below compares the two (optional).
1. Close the dashboard (the test opens its own Toss connection).
2. Double-click **`scripts\ab_test.cmd`**. It prints a **free** cost estimate and asks you to type `YES` before it spends anything.
3. It asks 15 questions (`backend/ab/questions.yaml`) twice:
   - A = levels only
   - B = levels + raw candles
4. It then checks every price in each answer against the dashboard's data.
5. The report is saved to `logs/ab/ab_<date>.md`. Pick A or B, then set `llm.candles.mode` in `config.yaml`.

---

### 6.7 Quick analysis button and options data

**⚡ Full analysis** (next to Send) sends a saved request about the ticker on screen: 1m and 5m technicals, news, the
options expected move, a buy / wait / sell verdict, a scenario table with a sized quantity, and a rule or draft.
Rules are only proposed; say "걸어줘" / "arm them" afterwards if you want them armed. Change the text in
**Settings → AI → Quick analysis prompt** (empty = the built-in text).

**Options (implied)** panel on the Desk (phone: Book tab) and the chart toggle **Options bands**:

- **Expected move**: how big a typical move (1 sigma, about 68 % of outcomes) the options market prices in, for today
  and to the next weekly / monthly expiry, with the price range. It is **size, not direction**.
- Calculated in code from **yfinance option chains (~15 min delayed)**. Outside the US regular session the numbers are
  from the **last session** (labeled). IV comes from the at-the-money straddle; Yahoo's own IV is shown only as a check.
- **Quality**: from the bid-ask spread. "thin" = quotes too wide; that ticker gets no expected move (SOXX on 2026-11-21).
- Also: IV vs the last 20 days' actual moves, term structure ("inverted" = an event is expected), put vs call price,
  put/call volume, most traded strikes. Open interest is **not** used (Yahoo's numbers failed a check).
- The chat sees the short version with every message and can ask for the details (`get_options`). The bands are levels
  `em_day_up` / `em_day_down` (and week / month) that it can cite, draw, and use in scenarios.

## 7. Orders (phase 4)

### 7.1 Safety first: DRY RUN
- **DRY RUN is on by default** (`config.yaml` → `orders:` → `dry_run: true`). The Orders tab shows a blue bar: *"DRY RUN: orders are simulated. Nothing is sent to Toss."*
- In DRY RUN the backend **never calls Toss's order addresses**. They are switched off in code and refused even if something asked for them. A "send" is simulated on your computer only:
  - it appears under **Simulated orders** on the Orders tab, where you can change or cancel it like a real one;
  - it shows as a dashed **DRY RUN** line on the chart;
  - it never fills, so it never shows up in P&L or the Journal.
- Every order goes **draft → you approve → send**. There is no one-press order. The AI can only make drafts; it has no way to send.
- Every send request and its result is written to `logs/trades.jsonl` (keys are never written).

### 7.2 Placing an order (Orders tab)
1. **Ticker:** pick one (search by ticker or name). The live bid / ask / spread and last price are shown next to it.
2. **Kind:**
   - **Buy** or **Sell**;
   - **Reserved buy / Reserved sell**: placed automatically when the price reaches a trigger, valid until an expiry date (max 1 year);
   - **OCO**: two sells on shares you hold, one above the price (take-profit) and one below (stop). The first to trigger sells and the other is cancelled;
   - **OTO**: a buy; once it fills, a sell is placed automatically.
   - "Reserved" (예약) orders are Toss *conditional orders*.
3. **Type:** limit or market. **Current price** copies the latest trade price into the price box so you can see it before approving.
4. **Size:** shares, % of buying power (buy), % of shares held (sell), or a USD amount (market orders only). Percent and amount are turned into a share count in code and shown to you before approval.
5. **Validity:** *Today* or *Until the close* for normal orders. Reserved orders need an expiry date instead.
6. **Create draft.** Code checks everything (7.6). A draft lasts 3 minutes by default.
7. **Review & approve.** The approval screen shows one plain sentence, every value, and the estimated cost. When you press approve, **all checks run again against the latest price**, then the order is sent (or simulated in DRY RUN).

- **Sent only once:** each draft can be sent only once. Toss gets the draft id as `clientOrderId`, so even an automatic retry after a lost connection can't place it twice.
- **Changing or cancelling:** use **Change price / Cancel** on an open order and **Edit / Cancel** on a reserved order. They make a draft and use the same approval step.
  - US orders can only change their **price**, not the quantity.
  - Changes and cancels are **never retried automatically**. If one comes back *unknown*, check your open orders on Toss before trying again.
- **Settings:** on the ⚙ Settings page you can set the **price band ±%** (a limit price must be this close to the current price) and the **draft expiry**.

### 7.3 Orders from the chat
- **Scenario cards:** each card has simple buttons with the price on them: *Buy · limit …* and *Reserved buy · when … is reached* (long), or *Sell · limit …* and *Reserved sell · when … is reached* (short).
  - **Together:** combined buttons appear only when the AI recommends the entry with take-profit / stop as one plan: *Buy, then take-profit sell (OTO)* and *Take-profit + stop (OCO)*.
  - For protecting shares you already hold, the card shows *Take-profit sell*, *Stop-loss reserved sell* and *Take-profit + stop (OCO)*.
  - A button only **fills in the order form**, and prices are rounded to Toss's price step. Nothing is created or sent.
  - You enter the size (and the expiry for reserved orders), then press **Create draft** and **Review & approve**.
  - The Orders tab also shows the latest scenarios at the top.
- **Typing an order** (e.g. *"SOXS 10주 4.25 지정가 당일 매수"*):
  - The AI makes a **draft** only if every number it uses is in your message and you said *today* or *until the close* (for reserved orders, the expiry date).
  - Anything missing, it asks for. It never fills in numbers itself and never sends.
  - A note with an **Open Orders tab** button appears when a draft was made.

### 7.4 Journal, P&L and the chart
- **Journal:** the top button.
  - Shows only trades made **with this program**. One trade = a stock going from 0 shares to owned and back to 0.
  - A trade is shown if at least one of its orders was sent from here. Earlier buys of the shares it sold are included.
  - Each card shows entry → exit (KST), holding time, average prices, realized / unrealized P&L, fees, FX cost (est.), AI credits used while the trade was open, and net.
  - AI credits are counted by time, because the usage log doesn't know which stock you asked about. The totals count each call once.
  - In DRY RUN the Journal stays empty, because simulated orders never fill.
- **P&L:** comes from your Toss fills and holdings.
  - **Today** = today's trades.
  - **Holdings** = only the stocks you hold now, counted from when each was last bought from 0 shares.
- **Chart, Orders toggle:**
  - real fills show as **B / S** markers on the bar where they happened;
  - open orders, reserved-order triggers and DRY RUN simulated orders show as dashed lines.

### 7.5 Real orders: switching on, and back to DRY RUN
**Only you do this. Claude never changes it, and the Settings page can't change it (on purpose).** Toss has no test environment: once DRY RUN is off, approved orders are real money.

Before switching, do the Phase 4 check-list (section 9) in DRY RUN.

**Switch to real orders**
1. Stop the dashboard: click the `scripts\start.cmd` window and press **Ctrl+C**.
2. Open **`config.yaml`** in Notepad. Find the `orders:` section and change `dry_run: true` to **`dry_run: false`**. Save.
3. Start `scripts\start.cmd` again. The window prints **"REAL ORDERS ARE ON"**, and on the Orders tab:
   - the bar turns red: *"REAL ORDERS: approved orders are sent to Toss."*;
   - the approve button reads **"Approve & SEND REAL ORDER"**.
4. Start small: for example a 1-share limit buy far below the price, then cancel it. Check it on Toss too.
   - If Toss refuses every real order with an access or permission message, check your API key's settings on the Toss developers site.

**Back to DRY RUN**
1. Stop the dashboard (Ctrl+C).
2. In `config.yaml` set `dry_run: true` and save.
3. Start it again and check the Orders tab bar is **blue** (DRY RUN) again.

Orders already sent to Toss stay on Toss after switching back. Change or cancel them in the Toss app.

### 7.6 What the order check messages mean
The screen shows these as plain sentences. Errors stop the draft; **warnings** let it through but tell you the risk.

| Code | Message on screen |
|---|---|
| `action_invalid` | Choose modify or cancel |
| `target_not_found` | That order is no longer open (filled or cancelled?) |
| `target_not_open` | This order can't be changed now (status …) |
| `modify_price_only` | US orders: only the price can be changed, not the quantity |
| `nothing_changed` | Nothing changed |
| `kind_invalid` | Choose an order type |
| `symbol_required` | Choose a ticker |
| `currency_usd_required` | US orders are in USD |
| `order_type_required` | Choose limit or market |
| `price_required` | Enter a price |
| `price_positive` | The price must be above 0 |
| `price_decimals` | …: at most … decimals (Toss would cut the rest off, so it is not accepted) |
| `price_out_of_band` | … is …% from the current price …; your band is ±…% |
| `no_current_price` | No current price available, so the price can't be checked. Try again in a moment |
| `quantity_required` | Enter the number of shares |
| `quantity_positive` | Shares must be above 0 |
| `percent_range` | Percent must be between 0 and 100 |
| `percent_zero_shares` | That percent is less than 1 share |
| `no_buying_power` | Buying power not loaded yet |
| `size_mode_invalid` | Choose a size type |
| `amount_market_only` | USD-amount orders are market orders only |
| `amount_required` | Enter a USD amount |
| `amount_decimals` | At most 2 decimals for a USD amount |
| `amount_outside_window` | USD-amount orders only from the regular open to 1 hour before the close |
| `fraction_conditional_off` | Fractional shares aren't available for reserved orders |
| `fraction_market_sell_only` | Fractional shares only for market sell orders |
| `fraction_too_many_dp` | At most 6 decimals for fractional shares |
| `fraction_outside_window` | Fractional orders only from the regular open to 1 hour before the close |
| `tif_required` | Choose today or until the close |
| `cls_limit_only` | Until the close is for limit orders only |
| `market_no_price` | Market orders have no price |
| `market_regular_only` | Market orders only in the regular session |
| `oco_oto_limit_only` | OCO and OTO are limit orders only |
| `oco_order` | OCO needs take-profit trigger > current price … > stop trigger |
| `expire_date_required` | Choose an expiry date |
| `expire_date_past` | The expiry date is in the past |
| `expire_date_too_far` | At most 1 year ahead |
| `not_enough_shares` | You can sell at most … shares (open sell orders counted) |
| `not_enough_buying_power` | Needs ≈ USD …; USD buying power is … |
| `high_value_confirm` | Over KRW 100,000,000: tick the high-value confirmation |
| `over_max_order` | Over your per-order limit (USD …) |
| `over_max_day` | Over your daily limit (USD …) |
| `over_max_orders` | Daily order count limit reached (…) |
| `market_thin_book` (warning) | Outside the regular session (…): thin order book, the fill price can be far off |
| `market_closed_now` (warning) | The market is closed now; Toss may reject it (order hours) |
| `oto_sell_below_buy` (warning) | The sell price is not above the buy price |

### 7.7 Send results
| Result | Meaning | What to do |
|---|---|---|
| Simulated (DRY RUN) | Recorded on your computer only. Nothing went to Toss | Nothing |
| Sent to Toss: order … | Toss accepted it | Watch it on the Orders tab / Toss app |
| Not sent: `rejected` | Toss said no (the reason is shown) | Fix what Toss says and make a new draft |
| `unknown` / `transport` | The connection dropped and the answer never came back. New orders were already retried once, safely (same `clientOrderId`) | **Check your open orders before trying again** |
| `recheck_failed` | Right before sending, a check failed against the latest price (e.g. price moved out of your band) | Read the message, make a new draft |
| `size_changed` | A % or USD-amount order would now be a different number of shares | Make a new draft so you see the new size |
| `draft_expired` | The draft is older than its expiry time | Make a new draft |
| `draft_invalid` / `draft_not_found` | The draft had errors, or was discarded / the program restarted | Make a new draft |
| `no_account` | The Toss account isn't loaded | Check the yellow bar and the account box on the Orders tab |
| `order-writes-disabled` | A real send was attempted while DRY RUN is on (blocked in code) | Nothing. This is the safety lock working |

### 7.8 Automatic rules

A rule is "if the price does this, send this order". It does **nothing until you press Arm**. After that, the
app sends the order by itself the moment the condition is met: no second approval.

**Where:** Orders page, table **Automatic rules** (under "From chat"; on the phone: the Orders tab). Armed rules still show as lines on the Desk chart.

**Rule types**

| Type | Fires when | Typical use |
|---|---|---|
| Touch, then fall back | price reaches X or higher, then drops to Y or lower within N minutes | take profit when a level is rejected |
| Touch, then bounce | price reaches X or lower, then rises to Y or higher within N minutes | buy when support holds |
| Candle closes beyond | a finished 1m / 5m candle closes at / above (or below) Y | breakout / breakdown |

**Confirm by:** *a trade* fires on the first trade at Y. *1m / 5m candle close* waits until the candle ENDS and
checks its close, so a quick wick through Y and back does not fire (use it to tell a real move from a short spike).
If the window runs out after the touch, the rule waits for a new touch (it stays armed).

For a plain "when the price reaches Y", use a Toss reserved order instead: it runs on Toss's servers even with
this app closed. The rule editor refuses that case and says so.

**Steps**
1. Press **New rule**, fill it in, press **Save rule (not armed)**. Or ask the AI chat to propose one: it appears
   with an **AI-proposed** badge and lists the numbers the AI picked.
2. Press **Arm**. The arm screen shows the exact order, the DRY RUN / REAL banner and any problems. Press **Arm rule**.
3. While armed, the panel shows its state ("touched 4.30 USD at 23:41, waiting until 23:56"). Armed rules are
   dashed yellow lines on the chart.
4. **Disarm** stops one rule. **Disarm all** is the kill switch.

**Safety**
- You arm a rule with the Arm button, or by asking the AI in chat ("arm them", "걸어줘", "활성화해줘"). The AI can only arm when your message asks for it (checked in code); such rules show **armed by AI on your request**. It also disarms / deletes rules it replaces so the same shares aren't covered twice.
- Fires once. When it fires, the same checks as a manual draft run against the live price (shares held,
  buying power, your price band, ticker), plus the session (rules marked "regular session only") and an optional
  buy cap (Settings → Orders: "Automatic rules: max % of buying power per buy"). If a check fails the rule is
  **blocked** and nothing is sent; it never retries.
- No firing on stale data: if live prices stop for 15 s, rules pause and forget a half-finished touch.
- **The app must be running.** If the computer sleeps or the app is closed, nothing fires. After a restart, armed
  rules come back armed but wait for a new touch.
- DRY RUN: fired rules are simulated. With `dry_run: false`, a fired rule sends a **real** order.
- Every create / arm / touch / fire / result / disarm is written to `logs/trades.jsonl`. Rules are saved in
  `logs/rules.json`.

**Limits:** 10 armed rules, 5 tickers across armed rules, a rule lasts at most 30 days (`rules:` in
`config.yaml`: `enabled`, `max_armed`, `max_proposed`, `stale_seconds`, `max_days`).

---

## 8. Phone access (phase 5)

The dashboard keeps running on the home PC and keeps listening **only on that PC**. **Tailscale** (a free private network app) links your phone to it, encrypted, and only your own signed-in devices can reach it. Your keys, the Toss calls and all your records stay on the home PC; the phone only shows the screen.

> ⛔ **Never run `tailscale funnel`** and never set up router port forwarding. Funnel puts a service on the public internet. The dashboard also refuses any request that doesn't come from one of your own Tailscale devices, but don't rely on that alone.

**Phone access is off by default** (`remote.allow_remote: false` in `config.yaml`): the dashboard then works only on the computer it runs on, and every request from another device is refused. To use the phone, set it to `true` and restart, then follow the steps below.

### 8.1 Home PC (one time)
1. Set your passcode and PIN if you haven't: `scripts\set_passcode.cmd` (section 4).
2. Install **Tailscale for Windows** from https://tailscale.com/download and sign in (e.g. with your Google account). Use the same account on the phone later.
3. In the Tailscale admin console, open the **DNS** page (https://console.tailscale.com/admin/dns):
   - turn on **MagicDNS** if it's off;
   - under **HTTPS Certificates**, press **Enable HTTPS**.
   - Note: this publishes your machine names in public certificate logs. If the PC's name contains your real name, rename the machine on the **Machines** page first (e.g. `desk`).
4. Start the dashboard with `scripts\start.cmd`.
5. Open **Command Prompt** (Start menu → type `cmd`) and run:
   ```
   tailscale serve --bg 8000
   ```
   It prints your address, like `https://desk.tailXXXX.ts.net`. Write it down.
   - If it shows a link to enable HTTPS, open it and approve, then run the command again.
   - `--bg` keeps it on in the background, and Tailscale turns it on again after a restart.
   - To check it: `tailscale serve status`. To turn it off: `tailscale serve --https=443 8000 off` (or `tailscale serve reset`, which clears all serve settings).

### 8.2 Phone (one time)
1. Install the **Tailscale** app (App Store / Play Store), sign in with the **same account**, and switch it on.
2. Open your `https://….ts.net` address in Safari (iPhone) or Chrome (Android) and log in with your passcode. A phone login lasts 24 hours (`remote.remote_session_hours` in `config.yaml`).
3. Put it on the home screen:
   - **iPhone (Safari):** Share button → **Add to Home Screen**.
   - **Android (Chrome):** ⋮ menu → **Install app** (or **Add to Home screen**).

   It then opens full screen like an app. It stores no market or account data on the phone.

### 8.3 Work laptop (only if your company allows it)
Same as the phone: install Tailscale, sign in with the same account, open the address. Many companies don't allow VPN apps on work laptops or block them on the office network. If so, use the phone.

### 8.4 What works from the phone
- **Everything to look at:** chart, P&L, chat, orderbook, news, journal. You can also make drafts.
- **Sending orders from the phone is OFF.** The approval screen says so; approve on the home PC instead. When you've used orders on the PC enough and want it, switch it on yourself:
  1. In `config.yaml` → `remote:`, set `orders_enabled: true`.
  2. Restart the dashboard.

  From then on:
  - every send from the phone asks for your **PIN**;
  - the approval screen uses big text;
  - **market orders are blocked** from the phone (`block_market_orders: true`).

  DRY RUN applies the same as on the PC.
- 5 wrong passcodes or PINs lock that device out for 15 minutes.
- **Log out** (top right) ends the session on that device.
- Optional: list your Tailscale account under `remote.allowed_logins` in `config.yaml` to refuse every other account.

### 8.5 Keep the home PC awake during market hours
- **Settings → System → Power & battery → Screen, sleep & hibernate timeouts:** set "when plugged in, put my device to sleep after" to **Never**.
- **Laptop:** Control Panel → Power Options → **Choose what closing the lid does** → *When plugged in: Do nothing*.
- If Windows restarts for updates, Tailscale serve comes back by itself, but the dashboard doesn't: run `scripts\start.cmd` again.
- When the PC can't be reached, the phone shows a red bar: "Connection to the home PC lost … Last update hh:mm:ss KST". It reconnects by itself.

### 8.6 If it doesn't work
| You see | What to do |
|---|---|
| The address doesn't open at all | Tailscale on in both the phone and the PC? Dashboard running? `tailscale serve status` shows port 8000? |
| "only your own Tailscale devices can open this" | The request didn't come from one of your signed-in devices (e.g. Funnel). Use `serve`, never `funnel` |
| "Connection to the home PC lost" | The PC is asleep or off, or the dashboard stopped (section 8.5) |
| "Wrong passcode … tries left" / locked | Wait 15 minutes. Forgot it? Run `set_passcode.cmd` on the PC |
| "Register this PC's IP in Toss" | Your home internet address changed. Register the new one in Toss (section 2.1, step 3) |
| The approval button stays grey on the phone | Phone sending is off (section 8.4), or it's a market order, or the PIN isn't entered |

## 8b. Cloud server (always on)

The dashboard can run all the time on a small AWS Lightsail server in Seoul, so data, the chat and armed
automatic rules keep working while your PC is off. Only your own devices reach it, through Tailscale; nothing
is open to the internet. The design and the reasons are in [docs/plans/CLOUD_PLAN.md](docs/plans/CLOUD_PLAN.md).

**Two copies, two jobs:** the **server is the live copy**. Your **PC becomes the workshop**: changes are made
and tested there, pushed to GitHub, and the server updates with one command. Never edit files on the server.

### 8b.1 AWS (one time)
1. AWS account: turn on two-step login (MFA) and create a billing alert (e.g. USD 20 a month).
2. **Lightsail → Create instance**: Region **Seoul (ap-northeast-2)**, platform **Linux/Unix**, blueprint
   **OS only → Ubuntu** (newest LTS offered), plan **2 GB**. Give it a name, e.g. `trading-desk`.
3. **Networking → Create static IP** and attach it to the instance. Write the IP down.
4. Instance → **Snapshots → Automatic snapshots: on**.
5. Keep the default firewall for now (SSH is needed until Tailscale works). You remove it in 8b.3.

### 8b.2 Toss (one time)
Register the server's static IP in the Toss Open API console. While there, check two things the docs don't
answer: whether your home IP can stay registered too, and whether running on a cloud server is allowed.

### 8b.3 Tailscale on the server (one time)
1. Lightsail → instance → **Connect using SSH** (a terminal opens in the browser).
2. Install Tailscale with the one command shown at https://tailscale.com/download/linux, then run
   `sudo tailscale up --ssh` and open the link it prints. Sign in with **the same account** as your PC and phone.
3. On your PC, open PowerShell and run `ssh ubuntu@trading-desk` (the Tailscale name of the server). Tailscale
   may open a browser to confirm. From now on you use this instead of the browser terminal.
4. Lightsail → instance → **Networking**: delete the **SSH** and **HTTP** firewall rules. The server now has no
   public way in.

### 8b.4 Get the code (one time)
In the server terminal (`ssh ubuntu@trading-desk`), line by line:
```
sudo useradd --create-home --shell /bin/bash trading
sudo -u trading -H ssh-keygen -t ed25519 -N "" -f /home/trading/.ssh/id_ed25519
sudo cat /home/trading/.ssh/id_ed25519.pub
```
Copy the printed line (it starts with `ssh-ed25519`; it is a public key, safe to copy). On GitHub: your repo →
**Settings → Deploy keys → Add deploy key**, paste it, name it `trading-desk server`, leave **Allow write access
off**. Then:
```
sudo install -d -o trading -g trading /opt/trading-desk
sudo -u trading -H git clone git@github.com:<your-github-name>/Trading-desk.git /opt/trading-desk
```
Answer `yes` to the GitHub fingerprint question the first time.

### 8b.5 Install and start (one time, about 10 minutes)
```
cd /opt/trading-desk
sudo bash scripts/linux/setup.sh
```
It installs everything and then lists what is still missing. Do these three, then run `setup.sh` again:
1. **Keys:** `sudo -u trading -H nano /opt/trading-desk/.env` and type the same lines as in `env.example`
   with your values (best: a **separate Anthropic key** with a monthly spend limit). Save with Ctrl+O, Enter,
   Ctrl+X. Only you type this; never paste keys into a chat.
2. **Passcode and PIN:** `sudo -u trading -H bash -c 'cd /opt/trading-desk/backend && .venv/bin/python -m app.set_passcode'`
3. **Server settings:** `sudo -u trading -H nano /opt/trading-desk/data/config.local.yaml` with:
   ```yaml
   remote:
     allow_remote: true
     allowed_logins: ["you@example.com"]   # your Tailscale login
   ```
   This file is not in git, so updates never overwrite it. Any key from `config.yaml` can go here.

When `setup.sh` says **"The dashboard is running"**, run `sudo tailscale serve --bg 8000`. It prints the
address, `https://trading-desk.<your-tailnet>.ts.net`. Open it on your PC and log in.

### 8b.6 Trusted devices
On the server every device counts as "from another device", so orders and arming ask for the PIN. On your PC
browser open **⚙ Settings → Trusted devices**, type a name and the PIN once: that browser then works like the
home PC (no PIN, normal order rules). Your phone keeps asking for the PIN unless you trust it too. Remove a
device there at any time; running `set_passcode` again removes them all.

### 8b.7 DRY RUN first
The server starts in DRY RUN (`config.yaml` from git). Let it run for a few days. Real orders only when you
decide: add `orders:` / `  dry_run: false` to `data/config.local.yaml` and run
`sudo systemctl restart trading-desk`. Consider setting the per-order and per-day limits in Settings first:
armed rules fire while you sleep.

### 8b.8 The PC as the workshop
On the PC create `data/config.local.yaml` with:
```yaml
deploy:
  role: workshop
```
and restart `start.cmd`. A purple **WORKSHOP** bar appears; DRY RUN is forced on and automatic rules are off.
**Toss allows one login token per key**: if the PC and the server run at the same time with the same key, one
logs the other out. To test with live Toss data on the PC, pause the server first and resume it afterwards
(commands below).

### 8b.9 Everyday commands (in `ssh ubuntu@trading-desk`, from `/opt/trading-desk`)
| What | Command |
|---|---|
| Update to the newest version on GitHub (tests first; goes back by itself if something fails) | `sudo bash scripts/linux/update.sh` |
| Go back to the previous good version | `sudo bash scripts/linux/update.sh --rollback` |
| Pause / resume the live copy (armed rules don't fire while paused) | `sudo bash scripts/linux/pause.sh` · `sudo bash scripts/linux/resume.sh` |
| Is it running? | `systemctl status trading-desk` |
| Live log (Ctrl+C to leave) | `journalctl -u trading-desk -f` |
| Backup now (also runs every day at 07:00 KST, kept 14 days) | `sudo -u trading bash scripts/linux/backup.sh` |
| Copy a backup to the PC | on the server `sudo install -m 600 -o ubuntu backups/desk-….tar.gz ~/`, on the PC `scp ubuntu@trading-desk:desk-….tar.gz .`, then on the server `rm ~/desk-….tar.gz` |
| Restore a backup | `sudo systemctl stop trading-desk` → `sudo -u trading tar -xzf backups/desk-….tar.gz -C /opt/trading-desk` → `sudo systemctl start trading-desk` |

Backups hold `data/` and `logs/` only. `.env` and `secrets/` stay out of them; the Lightsail snapshots cover
the whole server.

### 8b.10 If it doesn't work
| Problem | What to do |
|---|---|
| `setup.sh` stops at Python 3.15 | Run it again later; if it keeps failing, tell Claude Code the last lines |
| The address doesn't open | `systemctl status trading-desk` running? `tailscale serve status` shows port 8000? Tailscale on in the PC? |
| "Access from other devices is off" | `data/config.local.yaml` is missing `remote: allow_remote: true`; fix it and `sudo systemctl restart trading-desk` |
| "This Tailscale account is not allowed" | Your login in `allowed_logins` is spelled differently than the one Tailscale shows |
| Toss errors about the IP | The static IP isn't registered in the Toss console (8b.2) |
| Toss "token replaced" on one copy | The PC and the server used the same key at the same time (8b.8) |
| Strip boxes / options panel show errors | Yahoo may block cloud servers (known, accepted for now) |
| `update.sh` says files were changed on the server | Someone edited files in git on the server. Don't; put server settings in `data/config.local.yaml` |

---

## 9. Check-lists

### Phase 1 check-list

1. ☐ Run `scripts\start.cmd`, open http://127.0.0.1:8000, log in, and confirm no yellow bar complains about Toss.
2. ☐ The chart shows **SOXS** on **5m** with:
   - ☐ EMA 9/20/60 lines (light blue, orange, purple)
   - ☐ VWAP (white dotted line)
   - ☐ Fibonacci levels (dashed olive lines labelled "Fib 0.618" etc.)
   - ☐ the orange **Avg** line (needs at least one buy in `positions.json`)
3. ☐ The top strip updates: the "updated N s ago" text under SOXS / SOXX stays within a few seconds, and USD/KRW within about a minute.
4. ☐ Switch to SOXX, then 1m, then Daily: the chart changes each time.
5. ☐ Add a plan (e.g. Buy, `3.95`, USD): a green line appears and `positions.json` now contains it. Delete it again.
6. ☐ Make the browser window narrow (or press F12 and pick a phone size): tabs appear at the bottom.

---

### Phase 2 check-list
1. ☐ `scripts\check_models.cmd` says "All model IDs found".
2. ☐ Ask **"where are the support levels on SOXS 5m?"**: the answer cites levels with ids, and if you ask it to draw them, light-blue AI lines appear on the chart.
3. ☐ The **AI lines** toggle hides and shows only the AI's lines.
4. ☐ Ask for **scenarios**: a table appears with R/R and after-cost move.
5. ☐ Switch **Normal -> Deep -> Very deep** and ask a short question on each: the cost line shows Sonnet, then Opus, then Fable.
6. ☐ Each answer shows its cost. The header shows today's and this month's spend, and the P&L panel shows AI credits.

### Phase 3 check-list
1. ☐ The **Orderbook** panel shows asks and bids for SOXS, the spread, and the session; during a session it says **live** and the trades list moves. Note how many levels Toss returned (shown at the top).
2. ☐ Switch the chart to another ticker: the orderbook follows it.
3. ☐ With `FINNHUB_API_KEY` set, the **News** panel fills within a few minutes; items get a priority, effect and reason (a high-priority one is red).
4. ☐ Ask the chat **"any important news on NVDA?"**: it answers with headlines, links, dates and priorities from the feed.
5. ☐ Pull the network cable or turn Wi-Fi off for a minute: the orderbook says "disconnected, retrying…", the rest keeps working, and it comes back to **live** by itself.

### Phase 4 check-list (DRY RUN)
1. ☐ The Orders tab shows the blue **DRY RUN** bar, your account (masked), buying power, holdings and open orders, the same as the Toss app.
2. ☐ Make a **Buy** draft for 1 share of SOXS a little below the price: it shows a summary sentence and a countdown. Try a price 10% away: it says the price is outside your band.
3. ☐ **Review & approve** it: the result says *Simulated (DRY RUN)*. It appears under **Simulated orders** and as a dashed DRY RUN line on the chart. Nothing appears in the Toss app.
4. ☐ **Change price** on that simulated order, approve: the old one shows *REPLACED*, the new one *OPEN*. Then **Cancel** it and approve: it shows *CANCELED*.
5. ☐ Make a **Reserved sell** and an **OCO**: they need an expiry date; an OCO with the triggers the wrong way round is refused.
6. ☐ Ask the chat for two scenarios, press **Buy at entry** on a card: the order form is filled in, you add the size, create and approve the draft.
7. ☐ Type in the chat *"SOXS 1주 4.25 지정가 당일 매수"* (use a price near the current one): a draft appears on the Orders tab. Type *"SOXS 4.25에 매수"*: the AI asks for the size and the validity instead.
8. ☐ Open `logs\trades.jsonl` in Notepad: each send has a `request` and a `result` line with `"dry_run":true`, and no keys.
9. ☐ The P&L panel matches the Toss app (Holdings), the chart shows B/S markers for your past fills, and the Journal says there are no trades made with this program yet.

### Phase 5 check-list
1. ☐ `set_passcode.cmd` done; http://127.0.0.1:8000 asks for the passcode; a wrong one says how many tries are left.
2. ☐ `tailscale serve --bg 8000` printed your `https://….ts.net` address; `tailscale serve status` shows it.
3. ☐ On the phone with Tailscale on (try it on mobile data, not home Wi-Fi), the address opens the login; after logging in you see the chart, P&L, chat, orderbook and news.
4. ☐ Switch Tailscale off on the phone: the address no longer opens.
5. ☐ On the phone, create a draft and press Review & approve: it says sending from the phone is off.
6. ☐ The app is on the phone's home screen and opens full screen.

### Phase 6 check-list
1. ☐ Research → Rank: today's US volume ranking shows; a sector opens with its stocks; ★ three tickers into a folder, pin it: after a restart they are the chart's quick buttons and strip slots.
2. ☐ Search NVDA in the Company view header: Overview tiles, 5 years of statements, the last 4 EPS surprises, rating counts and the (unofficial) target range. SOXS shows the ETF card; TSM shows TWD statements with multiples refused and the reason.
3. ☐ Research chat on NVDA: press **Valuation**: a card with three verdicts; every number on it is also on the Company view.
4. ☐ Press **Save thesis**, keep "Price at own median P/E" ticked, save: the thesis card appears; on the Desk the card shows above My info and a dashed purple line is on the chart; **Thesis lines** hides it.
5. ☐ Ask the research chat "don't save it": nothing is saved. Ask "save it" after a new card: saved, marked "saved by the AI on your request".
6. ☐ Switch the Desk to SOXX and back to NVDA: each ticker shows its own chat.

---

## 10. When something goes wrong

| You see | What to do |
|---|---|
| "Can't reach the backend" | `scripts\start.cmd` isn't running or crashed. Start it again and read its window for errors |
| "Not set in .env: TOSS_CLIENT_ID, …" | Fill in `.env` (section 2.4) and restart |
| "Register this PC's IP in Toss WTS" | Section 2.1 step 3, then wait a minute |
| "token replaced elsewhere" | Another copy is running. Close it, then click **Retry Toss** |
| A strip box shows **stale** | That source stopped updating. yfinance is unofficial and sometimes fails; it retries automatically with growing pauses |
| "10Y ^TNX" shows **unit?** | ^TNX and FRED differ by more than 1 point (a units problem at the source). Trust FRED's daily value |
| P&L shows an error about `positions.json` | Fix the JSON at the line and column shown |
| Chat says "key not set" | Add `ANTHROPIC_API_KEY` to `.env` (section 2.3) and restart |
| Chat says "rate limit or no credits left" | Check Billing in the Anthropic Console |
| Chat says "model not found" | Run `scripts\check_models.cmd` and fix the model ID in `config.yaml` |
| "Budget limit reached" | Wait for the next day or month (KST), or raise `budget:` in `config.yaml` |
| Orderbook stays "disconnected" | Check the yellow bar for Toss problems (IP, token). It keeps polling every 2 s meanwhile |
| Orderbook shows only 1–2 levels | That is what Toss returns for US stocks (from some US exchanges, not the full market book) |
| News says "rate-limited" | Finnhub's free limit was hit; it retries on the next round (every 5 min). Fewer tickers in `news.related` helps |

| Orders tab: "Buying power not loaded yet" or no account | Wait 10 s; check the yellow bar for Toss problems. With several accounts, set `orders.account_seq` in `config.yaml` |
| A send result says `unknown` | Check open orders (Orders tab / Toss app) before trying again (section 7.7) |
---

## 11. For developers

**Folders** (main folder):

| Folder / file | What's in it |
|---|---|
| `backend/` | Python server (`app/`) and its tests (`tests/`) |
| `frontend/` | The screen (React + TypeScript, `src/`) |
| `scripts/` | Double-click `.cmd` files: setup, start, passcode, model check; `linux/` holds the cloud server scripts (section 8b) |
| `data/` | Your app data: `positions.json`, `overlays.json`, `user_settings.json`, `watchlist.json`, `cache/`, plus `positions.example.json` (only the example is in git) |
| `logs/` | Trade log, AI usage, chat and news databases (not in git) |
| `docs/` | `ROADMAP.md`, `plans/`, `prompts/`, `specs/` (pinned copies of the official Toss / Finnhub API specs) |
| `secrets/` | Login passcode hash (not in git) |
| `config.yaml`, `.env`, `env.example` | Settings, your keys (never in git), and the list of key names |

```
backend\.venv\Scripts\python.exe -m pytest backend      # backend unit tests
cd frontend && npm.cmd run typecheck                     # frontend type check
cd frontend && npm.cmd test                              # frontend unit tests (chart screen high / low)
cd frontend && npm.cmd run desk                          # development: live-reload screen on :5173 (log in there too)
```

Data attribution: This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.
Toss market data is for your own trading only. Don't share it with anyone, even for free (Toss terms).
