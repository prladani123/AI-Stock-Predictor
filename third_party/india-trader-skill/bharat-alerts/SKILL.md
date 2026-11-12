---
name: bharat-alerts
description: >-
  Price Alerts for NSE trades. Rings when a price, a stop, a target, a move
  or a time hits, and keeps the NSE clock (IST, the 2026 holiday list, the
  Muhurat session). Turns the plan from bharat-exit into three alerts for the
  trade's day: the stop, the target (on Zerodha the target is an alert, not a
  broker order) and flat by 15:15 IST, before the broker's 15:20 MIS
  square-off. Each alert rings once. Also writes fresh delayed quotes to
  trader/prices.json for bharat-budget and bharat-risk. Use when the user says
  "alert me", "tell me if it hits", "watch my stop", "set a price alert",
  "is the market open", "when does NSE open", "is it a holiday", "remind me to
  close by 3:15", "quote RELIANCE", or right after /bharat-exit writes a plan.
argument-hint: "[clock | quote | add | check]"
---

# bharat-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. On Zerodha only the SL-M stop sits at the
broker; the target is this alert, and taking it is a new sell ticket. The
alerts tell the user what happened and when it is time to be flat.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by 15:15 from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1215.35
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --target 1240.25
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:15 --label "flat by 15:15"
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TCS --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE --write         # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-10-19 15:15"` pins the clock (IST); `--offline DIR` reads saved
quote files (`yahoo-<SYMBOL>.json`) instead of the network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from bharat-exit, for `add --from-plan` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes, merged in, for bharat-budget and bharat-risk |
| `trader/portfolio.json` | the symbols held, for `quote --holdings` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the IST clock passed `at` ("15:15") on a trading day | once per trading day |
| `opening_bell` | NSE is open | once per trading day |
| `expiry` | on a futures contract's `expiry` day and the trading day before, from `at` (default 09:15) | once per day |

A rule with `"date"` is watched on that IST date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today.

For a futures plan, `--from-plan` puts the stop and target alerts on the
index levels (the quotes are the index; the futures level is in the note)
and adds an `expiry` rule for the contract, with no date, so it rings on
the day before expiry and on expiry day (from the bhavcopy's dates: 20 and
23 Nov 2026 for NIFTY26NOVFUT, since 24 Nov is a holiday).

For an options plan, `--from-plan` watches the INDEX levels (`index_stop`,
`index_target`; free option quotes do not exist), and every message says
they are index levels from the model. A put's stop rings when the index
rises to it. It also adds the contract's `expiry` rule and a 15:00 rule on
expiry day only: close it, because an ITM option held to expiry pays STT
on its intrinsic value.

## Quotes

- Yahoo's chart endpoint, `query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS`,
  unofficial and free, one request per symbol, half a second apart. On a 429
  it waits and tries once more; on a 429 or a 5xx after that it tries
  `query1`. It identifies itself as `india-trader-skill/0.1`.
- Free quotes are unofficial, delayed and can lag. They are fine for alerts
  and for checking a size, not for timing an order. The broker's price is the
  one that fills. Each price in `trader/prices.json` keeps its quote time, and
  bharat-risk only uses one under 15 minutes old.
- If Yahoo is down, read the price from the broker (Kite's quote tools, read
  only) and write it into `trader/prices.json` as
  `{"as_of": "...Z", "prices": {"RELIANCE": 1227.80}}`.

## The NSE clock

Regular session 9:15 a.m. to 3:30 p.m. IST (pre-open 9:00 to 9:08), closed on
weekends and on the 2026 NSE trading holidays (from nseindia.com, read
2026-11-04): Jan 15, Jan 26, Feb 15, Mar 3, Mar 21, Mar 26, Mar 31, Apr 3,
Apr 14, May 1, May 28, Jun 26, Aug 15, Sep 14, Oct 2, Oct 20, Nov 10, Nov 24,
Dec 25. Diwali, Sunday Nov 8, has the Muhurat session: 14:20 to 15:30 IST.
India has no daylight saving, so IST is always UTC+5:30. The tool ignores the
computer's own zone. For 2027 and later it says the holiday list is unknown
until NSE publishes it and `india.py` gets it.

## Steps

1. Right after bharat-exit writes the plan, run `add --from-plan`. Confirm
   the three alerts back to the user: the stop, the target and the flat-by time.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the symbols in today's
   rules and rings each alert once. A check reads the latest delayed quote;
   it is not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop alert means the SL-M stop
   has probably filled (live) or would have (paper): check and record the
   fill with bharat-broker. A target alert means it is time to take it: live,
   cancel the SL-M stop and sell with a new ticket (bharat-broker, then
   bharat-risk and the user's yes); paper, record the sell. "Flat by 15:15"
   means close anything still open before the 15:20 square-off: bharat-broker
   `flatten`, then bharat-risk, then the user's yes. Never act for them.

## Example

RELIANCE on Monday 2026-10-19: entry 1227.80 at 9:50 AM, stop 1215.35,
target 1240.25, flat by 15:15. `add --from-plan` writes
`reliance-plan-stop`, `reliance-plan-target` and `reliance-flat-by` for that
day. A check at 11:30 with RELIANCE at 1241.00 rings the target once:

```
ALERT RELIANCE is Rs 1241.00: at or above your target Rs 1240.25 (plan target: 1R (one times the risk)).
```

At 3:15 PM:

```
ALERT Flat by 15:15: it is 15:15 IST (set for 15:15), the market closes at 15:30. If RELIANCE is still open,
close it before the broker's 15:20 square-off: bharat-broker flatten --symbol RELIANCE builds the sell ticket,
then bharat-risk and your yes.
```

It rings once. On Wednesday (Tuesday is Dussehra) the plan's alerts are no
longer watched; with `add --opening-bell`, the bell rings at 9:15.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: symbol, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, and never predicts where a price is going.

## Safety

- Education and tooling, not financial advice, and not from a SEBI-registered adviser. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except bharat-broker, and only when all three hold: bharat-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (ask rules in the trading folder's .claude/settings.json). Never add an allow rule for an order tool.
- No F&O, margin (MTF), short selling or MIS leverage unless the config allows it and the user confirms.
- Broker keys live in the Kite server's key file (~/.config/kite-mcp-server/env), never in the chat.
