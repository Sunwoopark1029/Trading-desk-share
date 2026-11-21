# Plan — Automatic rules (to build after phase 6)

Status: **plan only, nothing built.** Written 2026-11-20. Rules this plan follows: CLAUDE.md "Order safety"
(rewritten 2026-11-20) and the ROADMAP section "Automatic rules".

Before building: re-read this plan, answer the open questions (section 10), and confirm the step list.

---

## 1. What it does

You (or the AI chat) write a **rule**: "if the price does X, send this order". The rule does nothing until
**you arm it** with one tap. From then on, the backend watches live prices and sends the order by itself
the moment the condition is met, with no further approval.

Example: *"SOXS: if the price touches 68.00 USD and then falls back to 67.60 USD within 15 minutes,
sell 14 shares, limit 67.55 USD, valid today."*

Not in this phase: trailing stops, rules that send more than one order, rules that change other orders,
Korean stocks, rules that run while the app is closed (that needs the "cloud server" idea in ROADMAP "Later").

## 2. Rule types (first version)

| Type | Condition | Typical use |
|---|---|---|
| **Touch, then fall back** | price reaches X or higher, then drops to Y or lower (Y < X) within N minutes | take profit / exit when a level is rejected |
| **Touch, then bounce** | price reaches X or lower, then rises to Y or higher (Y > X) within N minutes | buy when a support level holds |
| **Bar closes beyond** | a finished 1m or 5m bar closes above (or below) X | confirmed breakout / breakdown entry or exit |

- If the time window N runs out after the touch, the rule goes back to waiting for a new touch (it stays armed).
- If the price jumps straight through both levels (a gap), that counts as touch + fall back in one go.
- Plain "price reaches Y" is left out on purpose: Toss reserved orders already do that, and they run on
  Toss's servers even when this app is closed. The rule editor will point you there for that case.

## 3. A rule's parts

- Ticker (US stocks / ETFs), condition type, levels X / Y in USD, window N minutes or bar timeframe.
- The order: buy or sell, LIMIT (price in USD) or MARKET, whole-share quantity, valid `DAY` or `CLS`.
- Rule expiry (default: the end of today's US session, in KST), sessions it may fire in (default: regular
  session only; pre/after market only if you tick it).
- Who wrote it ("you" or "AI-proposed"), and for AI rules the level ids each price came from.

States: **proposed** (not armed) → **armed** (watching) → **touched** (waiting for the fall-back / bounce) →
**fired** → **sent** / **rejected**. Side exits: **disarmed** (by you), **expired**, **blocked** (a check failed
at fire time; it does not retry).

## 4. How it works (data flow)

```
Toss WebSocket trades (tick by tick) ─┐
Toss REST quote every 2 s (backup) ───┼─> rules engine: one state machine per armed rule
Finished 1m / 5m bars (bar rules) ────┘        │ condition met
                                               v
                       fire-time checks (same as drafts + staleness, session, expiry, once only)
                                               │ pass
                                               v
          existing send path (OrderSender), source = "rule", clientOrderId = rule id
                 DRY_RUN: simulated locally   │   real: Toss order endpoint
                                               v
          logs/trades.jsonl  +  screen notice  +  account refresh
```

- Prices come only from the dashboard's data (the trade stream already used for the chart), never from the AI.
- **Staleness:** if no new trade or quote arrives for a set time (default 15 s), the rule is marked "paused,
  stale data" and cannot fire until fresh data arrives. A touch seen before the gap does not count afterwards.
- **One send path:** firing builds an internal draft and sends it through the existing `OrderSender`, so the
  validation, logging, DRY_RUN switch and duplicate guard are the same code as manual orders.
- Rules are saved to `logs/rules.json`, so they survive a restart (see open question 3).

## 5. Safety

- **Arming is a person's action:** the arm route needs your login session (and the order PIN when used from
  the phone). The AI has no tool that arms. Claude Code never arms or sends (CLAUDE.md).
- **Kill switch:** one button on the Desk and Orders pages disarms every rule; `rules.enabled: false` in
  `config.yaml` turns the whole engine off.
- **Fire-time checks** (all in code, tested):
  - sells: shares held, minus shares already in open sell orders and reserved sells (so a rule and a Toss
    reserved order can't sell the same shares twice)
  - buys: Toss buying power is enough
  - limit price within your price band of the current price; ticker allowed; session allowed; rule not expired
  - fresh data (see staleness above)
  - the rule has not fired before
- **Fires once:** after firing, the rule is finished, whatever the result.
- **Limits:** a maximum number of armed rules (default 10). Optional per-rule / per-day USD caps, off by
  default like the other limits.
- **App must be running:** every armed rule shows "fires only while this app is running on this computer".
  If the laptop sleeps, nothing fires. On wake, half-finished touches are reset (no firing on old prices).
- **DRY_RUN:** fired rules are simulated and never call Toss. Note: `config.yaml` currently has
  `dry_run: false`. If it is still false when this is built, an armed rule sends **real** orders. Plan step E
  runs in DRY_RUN first.

## 6. The AI chat

- New tool **propose_rule**: creates a rule in the "proposed" state. Prices must be code-calculated levels
  (from get_levels) or numbers you typed, checked in code like scenario prices. Quantity: yours if you gave
  one, otherwise AI-proposed, sized from buying power / shares held. Everything the AI picked is labeled
  "AI-proposed" on the arm screen.
- New tool **list_rules** (read-only), and armed rules added to each message's `<live>` block, so the AI
  doesn't propose a duplicate.
- **create_order_draft** changes: the AI may fill in its own price / quantity / expiry when you didn't give
  one, labeled "AI-proposed" on the approval screen. Your own numbers are still copied exactly. Approval
  stays the same.
- Scenario cards get a "Make rule" button that opens the rule editor pre-filled (still unarmed).
- System prompt: describe the rule types, that it can propose but never arm, and when a rule fits better
  than a Toss reserved order.

## 7. Screen

- **Rules panel on the Desk** (under "Reserved & open orders"): each rule with its state, a progress hint
  ("touched 68.00 at 23:41, waiting for 67.60 until 23:56"), Arm / Disarm buttons, the kill switch, and
  the "app must be running" notice.
- **Rule editor** on the Orders tab: pick the type, levels, window, order and expiry; shows a plain-language
  summary and the code checks before you arm it.
- **Arm screen:** large summary of exactly what will be sent and when, "AI-proposed" badges, DRY RUN / REAL
  banner, Arm button (PIN on the phone).
- **Chart:** armed rules drawn as dashed lines at X and Y (fact lines, every timeframe), with the rule state.
- Notice when a rule fires, is blocked, or expires (on screen; push to the phone only if phase 5 phone access
  is in use).
- English and Korean text for everything.

## 8. Files

New:
- `backend/app/rules/model.py` — rule data, validation of a rule (levels, order part, expiry)
- `backend/app/rules/engine.py` — state machines, staleness, fire-time checks, firing through `OrderSender`
- `backend/app/rules/store.py` — saving / loading `logs/rules.json`
- `backend/app/rules/routes.py` — list, create, edit (only while unarmed), arm, disarm, kill switch
- `backend/tests/test_rules_*.py`
- `frontend/src/components/RulesPanel.tsx`, `frontend/src/orders/RuleEditor.tsx`, `frontend/src/orders/ArmModal.tsx`

Changed:
- `backend/app/services.py`, `backend/app/main.py` — start the engine and feed it trades / bars
- `backend/app/orders/submit.py` — accept `source="rule"`; log rule events
- `backend/app/orders/validate.py` — "shares already committed" for sells (shared with manual drafts)
- `backend/app/chat/tools.py`, `backend/app/chat/prompt.py` — new tools, AI-proposed numbers, `<live>` rules
- `backend/app/settings.py`, `config.yaml` — `rules:` section (enabled, max armed, stale seconds, default window)
- `frontend/src/api.ts`, `App.tsx`, `chart/*`, `chat/ScenarioTable.tsx`, `orders/ApproveModal.tsx`, `i18n.tsx`, `styles.css`
- `README.md` / `README.ko.md` — how rules work, the "app must be running" limit, the kill switch
- `ROADMAP.md` — phase number, "Done when", decision log

No new external APIs: it uses the Toss trade stream, quotes and order endpoints already verified in phases
3–4. The Toss order rate-limit group is re-checked in the docs before step B (unverified until then).

## 9. Build steps (one commit each)

| Step | What | Sends anything? |
|---|---|---|
| A | Rule model + engine state machines + tests with made-up price ticks | no |
| B | Fire-time checks + firing through `OrderSender` (DRY_RUN tests prove Toss is never called) | DRY_RUN only |
| C | Routes, kill switch, saving / restart behaviour + tests | DRY_RUN only |
| D | Screen: rules panel, editor, arm screen, chart lines, notices | DRY_RUN only |
| E | AI tools (propose_rule, list_rules, AI-proposed drafts) + system prompt + tests | DRY_RUN only |
| F | README, ROADMAP; you run the DRY_RUN check-list with real market data | DRY_RUN only |
| — | You switch `dry_run` yourself when you're happy (Claude never does) | your choice |

Tests to include: touch → fall back inside / outside the window, window timeout resets, gap through both
levels, bar-close rules on bar boundaries (KST), stale data pauses, laptop-sleep reset, fire once, restart,
blocked by held quantity / buying power / band / session / expiry, kill switch, AI tools cannot arm, AI
prices must match code levels, DRY_RUN never calls Toss.

## 10. Open questions (answer before building)

1. **Rule types:** are the three in section 2 the right first set? Any other condition you use often?
2. **Order price when firing:** for "fall back" sells, a limit order can miss if the price keeps dropping.
   Use your exact limit price only (default), or also allow MARKET during the regular session?
3. **After a restart:** armed rules come back **armed** (keeps working, recommended) or **disarmed** (you
   re-arm each time)?
4. **Pre / after market:** may rules fire outside the regular session (thin books, bigger gaps)? Default no.
5. **AI-proposed numbers in normal drafts:** OK for the AI to also pick prices (not only quantities and
   expiry) when you don't give one? CLAUDE.md now allows it; confirm you want it on.
6. **Limits:** keep the per-rule / per-day USD caps off like the other limits, or set one for rules?
