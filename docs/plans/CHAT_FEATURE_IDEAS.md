# Chat feature ideas (future work)

Status: **ideas only, nothing built.** Written 2026-11-21 after a read of the chat, orders and rules code.
CLAUDE.md and ROADMAP.md rules apply. Every number the AI uses must still come from code, never from the AI.

Before building any of these: confirm the item against the current code (the "Check first" lines), then
write a short plan for that one item.

## Priority order

| # | Feature | Why it matters | New data needed? |
|---|---|---|---|
| 1 | Journal access for the chat | The only feature that shows whether the program helps | No |
| 2 | Risk and sizing calculator | Size from the stop, not from buying power | No |
| 3 | Base rates computed by code | Measured odds replace subjective probabilities | No (candles already fetched) |
| 4 | More level types in `get_levels` | Covers the zones that react most often intraday | No |
| 5 | Underlying context for leveraged ETFs | SOXS levels are really SOXX levels | No |
| 6 | Orderbook and volume state | Thin books in the Toss day session change the plan | No |
| 7 | Plan tracking | Compares what was planned with what happened | No |
| 8 | Phone alerts | No need to watch the screen | Push service (new) |

Build 1 and 2 first. Both use data the program already stores.

---

## 1. Journal access for the chat

- **What:** a read-only tool `get_journal` that returns closed round trips (entry, exit, size, P&L after
  fees, hold time, session, AI cost) and code-computed summaries.
- **Add to each trade:** a setup tag (e.g. "VWAP pullback", "breakout close", "support bounce") and the id
  of the scenario or rule it came from.
- **Questions it should answer:** which setups make money, which time of day loses, average win vs average
  loss, whether exits happened at the planned invalidation or later.
- **Rules:** summaries (win rate, averages, totals) are calculated in code. The AI only reads them.
- **Where:** `backend/app/orders/journal.py`, `backend/app/chat/tools.py`, `backend/app/chat/prompt.py`.
- **Check first:** the journal covers only trades made with this program (user choice 2026-11-17). Decide
  whether to keep that scope.

## 2. Risk and sizing calculator

- **What:** a code tool that takes entry, stop and "percent of the account I accept losing" and returns the
  whole-share quantity, the loss at the stop in USD and KRW, and the share of buying power used.
- **Also:** put today's realized P&L and the number of trades today in the `<live>` block, so the chat
  knows when the day is already negative.
- **Optional:** a daily loss line set by the user. When it is crossed, the chat says so in every answer
  about a new entry. It does not block orders unless the user asks for that.
- **Where:** `backend/app/chat/scenarios.py` (already returns loss at the stop), `backend/app/pnl.py`,
  `backend/app/chat/prompt.py`.

## 3. Base rates computed by code

- **What:** a tool `get_base_rates` that measures, over the last N sessions of this ticker:
  - how often the first touch of VWAP held (and by how much it bounced)
  - how often the opening-range break set the day's direction
  - the size distribution of overnight and weekend gaps
  - how far price usually travels from VWAP before it returns
- **Why:** the chat currently gives subjective probabilities when asked. These are measured ones, with the
  sample size shown next to each figure.
- **Rules:** show N with every rate. Under a minimum sample (e.g. 20) the tool returns "not enough data".
- **Where:** new `backend/app/indicators/stats.py`, fed by the candle data already fetched from Toss.
- **Check first:** how many days of 1m / 5m history Toss returns, and the rate-limit group for candles.

## 4. More level types in `get_levels`

Each one gets a stable id like the existing levels.

| Level | Id idea | Note |
|---|---|---|
| VWAP ±1σ / ±2σ bands | `vwap_up1`, `vwap_dn2` | Shows when price is stretched |
| Anchored VWAP | `avwap_<anchor>` | Anchor = a swing point or an earnings bar picked by the user |
| Opening range high / low | `or_high_15m`, `or_low_15m` | First 5 / 15 / 30 minutes of the regular session |
| Premarket high / low | `pre_high`, `pre_low` | |
| Volume profile | `poc`, `vah`, `val` | Point of control, value area high / low |
| Unfilled gaps | `gap_<date>_top`, `gap_<date>_bot` | |
| ATR | `atr_5m`, `atr_1d` | A size, not a price: for stop distance |

- **Where:** `backend/app/indicators/` (one file per indicator, each with tests), `backend/app/chat/levels.py`.
- **Check first:** `indicators/vwap.py` and `frontend/src/chart/bands.ts` to see what band logic exists.

## 5. Underlying context for leveraged ETFs

- **What:** when the ticker is a leveraged or inverse ETF, add to `<live>`:
  - the underlying's key levels (SOXX for SOXS) and where each one lands in the ETF's price
  - the current move of the main drivers (NVDA, AMD, QQQ)
- **Rules:** the mapping from underlying level to ETF price is an estimate and must be labeled "estimated".
  It drifts during the day and resets each session.
- **Where:** `backend/app/chat/levels.py`, `backend/app/chat/prompt.py`. Toss stock info has
  `leverageFactor` for ETFs.
- **Check first:** a config map from each leveraged ETF to its underlying (Toss does not give it).

## 6. Orderbook and volume state

- **What:** spread, depth near the price and relative volume (today vs the usual volume at this time of
  day) in the `<live>` block or a `get_orderbook` tool.
- **Why:** in the Toss day session and premarket, a market order or a tight stop behaves very differently
  from the regular session.
- **Where:** `backend/app/data/toss_ws.py`, `backend/app/market.py`, `backend/app/chat/tools.py`.
- **Check first:** whether any orderbook data already reaches the chat. No such tool was in the tool list.

## 7. Plan tracking

- **What:** when a scenario becomes an order, save the plan (entry, invalidation, target, basis level ids).
  When the round trip closes, the chat compares plan and result: filled at the planned entry or chased,
  exited at the stop or below it, target reached or closed early.
- **Where:** `backend/app/chat/scenarios.py`, `backend/app/orders/journal.py`.
- **Depends on:** item 1.

## 8. Phone alerts

- **What:** a push when the price reaches a chosen level, when a rule is touched, fires or is blocked, and
  when a scheduled event is 10 minutes away.
- **Limit:** like rules, alerts only work while the program runs on the computer.
- **Check first:** which push route to use (the phone reaches the app through Tailscale). New external
  service, so it needs a docs check and a ROADMAP "Data sources" row.

---

## Do before any of the above

These came out of the same review. They are fixes, not features.

- Set real numbers for `max_order_usd` and `rule_max_buy_pct` in the config.
- Tighten the AI arm check in `chat/tools.py` (`ARM_RE`): require an explicit phrase with the rule id.
  Today "걸어" anywhere in the message passes.
- Update `docs/AUTO_RULES_PLAN.md`: it says "nothing built" and "the AI has no tool that arms", and both
  are now in the code.
