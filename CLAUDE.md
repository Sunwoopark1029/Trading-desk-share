# CLAUDE.md — Personal Trading Desk

This repo is a trading dashboard used by one person, on their own computer or their own cloud server (localhost + Tailscale only; `docs/plans/CLOUD_PLAN.md`).
The full design and phases are in `docs/ROADMAP.md`. Always read `docs/ROADMAP.md` before starting work.

## The user

- Does not write code by hand. Explain setup, how to run things, and how to fix errors step by step, in plain English.
- Has limited credits. For any large task, show a plan first and get confirmation before doing it.
- The user makes every decision. Trade numbers (price, quantity, expiry) come from the user, or are AI-proposed and approved / armed by the user (see "Order safety").

## Working rules

1. **Build only the current phase.** Don't build features outside the ROADMAP phase the user named. If something looks needed, suggest it instead.
2. **Plan first.** Show the file structure, data flow, and list of files that will change, and get confirmation before writing code.
3. **Docs first, no guessing.** Only use external API endpoints, fields, and units confirmed in official docs. Mark anything unconfirmed as "unverified" and keep that feature disabled.
   - Toss Securities Open API: https://developers.tossinvest.com/llms.txt , https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
   - Verified Toss facts and chosen data sources are in the ROADMAP "Data sources" section. Keep it updated.
   - Toss: keep one shared access token in the backend (a new token revokes the old one). Respect per-group rate limits (account APIs are 1 request/second).
4. **Small commits.** Commit at each meaningful step, with a short message saying what changed.
5. **When a phase is done,** mark it complete in `docs/ROADMAP.md` and record any changed decisions (data sources, structure, etc.) in the "Decision log".
6. **When something breaks,** briefly report the cause, what you fixed, and anything the user has to do.

## Security

- API keys and tokens live only in the backend `.env`. Never put them in the frontend, logs, commits, or chat output.
- **Claude does not read or edit `.env`, key files (`*.pem`, `*.key`), or `secrets/`.** `.claude/settings.json` blocks this. If a read is blocked, do not work around it with another command (cat, type, Get-Content, printenv, opening it from Python, etc.).
- Get environment variable names from `env.example`. If a new variable is needed, add only its name to `env.example` and tell the user to put the value in `.env` themselves.
- Never print key values in code, logs, exception messages, or test output. If debugging needs it, print only "set" / "empty".
- Before committing, run `git status` and make sure no `.env` or key file is staged.
- Do not loosen or edit the deny rules in `.claude/settings.json`. If a change seems necessary, explain why and let the user make it.
- The server listens on localhost only. From phase 5, a private VPN address (Tailscale, etc.) is also allowed, but never public internet exposure (port forwarding, public 0.0.0.0 binding, external tunnel services) in any phase. The same applies on the user's own cloud server (user choice 2026-11-21): reachable only through Tailscale.
- From phase 5, a login passcode is required. Sending an order or arming a rule from a device that is not marked trusted requires re-entering the PIN. A device becomes trusted only after the PIN is entered on it, and can be removed in Settings (user choice 2026-11-21, replaces "PIN for every order from the phone" for trusted devices).
- Never ask the user to paste a key into the chat.
- `.env`, `logs/`, and `data/positions.json` are in `.gitignore` (app data files live in `data/`).

## Numbers

- Indicators and levels (EMA, VWAP, Fibonacci, swing highs/lows, trendline candidates, risk/reward) **must be calculated in code**, with unit tests.
- The AI (chat) picks from and explains values the code calculated. It never makes up numbers that weren't calculated.
- Always show a currency (USD/KRW) with every price. When converting, show the FX rate and the time it was taken.
- Show all times in Korea time (KST). If the source data is in US Eastern time, test the conversion. Toss 1m bars are stamped with the bar end time.
- Show the source and delay next to every delayed value (e.g. "yfinance · ~15 min"), and mark values "stale" when a source stops updating.
- Label the Toss USD/KRW rate as a reference rate. The applied rate comes from transaction records.

## Order safety (applies from phase 4; before that, no order code exists at all)

- Every order starts as a draft or an armed rule. Nothing is ever sent from chat text alone.
- **Manual orders:** draft → user approval → send (unchanged).
- **Automatic rules (user choice 2026-11-20):** a rule = a price condition (e.g. "touches X, then falls back to Y within N minutes") + one order (side, type, price, quantity, expiry). Once the user ARMS a rule (one tap; PIN on the phone), code sends that order when the condition fires, with no further approval.
  - Rules can be written by the user or proposed by the AI chat. An unarmed rule does nothing. The AI may arm rules only when the user's own chat message asks for it ("arm them", "걸어줘", "활성화해줘"; checked in code; user choice 2026-11-20); armed-by-AI rules are labeled and logged, and get the same checks as a user arm.
  - The order is sent exactly as armed; code and AI never change an armed rule's numbers.
  - Each rule fires at most once and has an expiry. Any rule can be disarmed at any time; one kill switch disarms all.
  - At fire time, code runs the same checks as drafts (held quantity for sells, buying power for buys, price within the user's band, ticker allowed) and does not fire on stale price data.
  - Fires only while the backend is running; the UI says so on every armed rule.
  - In DRY_RUN, fired rules are simulated locally.
- **Numbers:** if the user gives a price, quantity or expiry, it is used exactly. The AI may propose its own numbers (in drafts, rules and scenarios), always labeled "AI-proposed" and shown to the user before approval or arming. AI-proposed prices come from code-calculated levels (see "Numbers"). Exception (user choice 2026-11-20): in a scenario card, an entry / stop / target that matches no level is allowed and shown as a warning for the user to check; its order buttons stay on (the order is still validated in code like any draft).
- Check in code: currency stated, price within an allowed range of the current price (band set by the user), per-order and per-day limits (optional; off by user choice 2026-11-17), allowed tickers (any US ticker by user choice 2026-11-17), held quantity (for sells), expiry (`DAY`/`CLS` for normal orders, `expireDate` for conditional orders).
- A draft expires after a few minutes, and each draft or rule can be sent only once (no duplicate sends; use Toss `clientOrderId`).
- Default is `DRY_RUN=true`. Switching to real orders happens only when the user changes the setting themselves.
- Toss has no sandbox. In DRY_RUN the order endpoints are never called; sends are simulated locally.
- Claude Code "ask" permission rule for the send command: declined by the user (2026-11-17). Claude Code (the coding assistant) never calls the send route or send code, and never arms a rule, itself; DRY_RUN and the send-path block while DRY_RUN is on stay in force.
- Log every order request and response, and every rule armed / fired / disarmed, to `logs/trades.jsonl` (without keys).
- Never send real orders during development or testing.

## P&L

- Always show net P&L: realized + unrealized − fees − FX cost − AI credits.
- Separate confirmed values (fill price, fees) from estimates (FX spread, fill spread, currency conversion).
- P&L logic lives in code with unit tests (partial sells, average-price changes after rebuys, mixed KRW/USD).

## LLM cost (from phase 2)

- Use Claude only. Default Sonnet; "deep" uses Opus; "very deep" uses Fable; background work (news tagging/summaries, conversation summaries) uses Haiku.
- Model IDs and prices live only in `config.yaml`. Never hard-code them.
- Build prompts in "static first → changing last" order and cache the front part. Log the cache hit rate.
- Log usage from every API call to `logs/usage.jsonl` and show per-message, daily, and monthly cost in the UI.
- When the budget limit is reached, block the larger models. Limits and behavior are set in `config.yaml`.
- Don't use the LLM to draw charts, calculate indicators, or fetch news (use code and data APIs).

## Role in chat (from phase 2)

- Answer the user's questions about technical analysis, news, and scenarios. Show the indicators and levels the answer is based on.
- When drawing on the chart, add lines only in the shared overlay format from `docs/ROADMAP.md`.
- Present scenarios as a table: entry, invalidation, target, risk/reward, reasoning, and expected move after costs. Clicking a row never sends an order; it only creates a draft (or an unarmed rule).
- The AI may create buy/sell drafts and propose automatic rules, with its own numbers when the user didn't give them (labeled "AI-proposed"). It never approves a draft, and arms a rule only when the user's message asks for it.
- If a news item is unconfirmed or its date is unclear, say so.
- Web search (when switched on) adds context only, with source links. Prices and levels always come from the dashboard's code-calculated data, never from web results. Web search cost counts toward the credit budget.
- **Research chat (phase 6, user choice 2026-11-21):** no order, rule or drawing tools. A research thesis is saved by the user's button, or by the AI only when the user's own latest message asks for it ("save it", "저장해줘"; checked in code), and only from a valuation card whose numbers the code made. Thesis text, cards and notes are never a source for numbers.


**Quantities (user choice 2026-11-20, widened the same day):** the AI may propose whole-share counts in scenarios, drafts and rules. They are labeled "AI-proposed", shown to the user before approval or arming, and validated in code like any draft. The AI never changes a quantity the user gave.
