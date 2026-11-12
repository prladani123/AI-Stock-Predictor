---
name: bharat-budget
description: >-
  Budget Manager for NSE intraday trades. Sizes every position to your budget
  and your max loss per trade, with Zerodha's trading costs included
  (brokerage, STT, exchange charges, SEBI fee, GST, stamp duty). Reads the
  trade plan from bharat-exit (entry, stop, target), finds the whole shares
  that fit inside four limits (max loss per trade including costs, position
  cap, max order, cash after your buffer, with optional MIS leverage up to
  5x), says which limit decided it, and writes ONE bracket order (NSE, MIS,
  entry, stop-loss, take-profit) to trader/orders.json. A stop too far away
  for your max loss is a plain refusal, not a smaller order. Nothing is
  placed. Use when the user says "how many shares", "size this trade", "how
  much should I buy", "I have 2 lakh", "I only want to risk Rs 1,000 a
  trade", "what are the charges", "set my rules", "keep 10% in cash", or
  after /bharat-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# bharat-budget

The Budget Manager turns a plan ("buy RELIANCE at 1227.80, stop 1215.35")
into an exact order ("buy 73 shares"), inside rules the user sets once. The
stop decides the size: the further away the stop, the fewer shares. Trading
costs come out of the max loss too: on a small intraday trade they can be a
large share of the risk. It writes a proposal only. bharat-risk checks it and
bharat-broker asks the user yes or no. SEBI's studies show most retail
intraday traders lose money. Sizing to a max loss keeps each loss small; it
does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                         # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                 # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write         # a limit order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1227.80 --json  # a limit order at 1227.80
```

`--now "2026-10-19 09:51"` pins the clock (IST). A plan for another day is
refused: make a new one with bharat-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, setup, flat-by time (bharat-exit writes it) |
| `trader/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (bharat-alerts writes it) |
| `trader/portfolio.json` | optional: its cash caps the size (bharat-broker keeps it) |
| `trader/orders.json` | the one proposed bracket order it writes with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each value in plain words, all in
rupees:

- **budget**: how much money this is for. Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills at
  the stop, costs included (default Rs 1,000).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest
  position (75 % of the budget), the biggest order (Rs 2,00,000) and the
  smallest order (Rs 500).
- **mis_leverage**: 1 (default) means no broker margin. Up to 5 lets the cash
  limit use Zerodha's intraday (MIS) margin; SEBI requires at least 20 %
  margin, so 5x is the ceiling. Leverage never raises the max loss or the
  position cap. Ask before turning it on.
- The day limits bharat-risk enforces: **max_daily_loss_inr** (3,000),
  **max_trades_per_day** (3), **min_reward_risk** (1.0), **max_cost_share**
  (0.25: costs at most a quarter of the expected reward), **require_stop**
  (true), **no_entries_before** / **no_entries_after** ("09:30" / "14:45")
  and **flat_by** ("15:15"), all IST.
- **blocked**: symbols they never want to trade.
- Keep **paper** true until they have watched it work on the paper ledger.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only. The size is the smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | the most shares where shares x risk a share + round-trip costs at the stop <= `max_loss_per_trade_inr` |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) x `mis_leverage` / entry, never more than the cash in `trader/portfolio.json` |

Costs are Zerodha's intraday equity charges, checked against its own
calculator: brokerage (the lower of Rs 20 and 0.03 % an order), STT 0.025 %
on the sell, NSE's transaction charge 0.00297 % plus its investor protection
charge 0.0001 %, SEBI's Rs 10 a crore, GST 18 % on those three, and stamp
duty 0.003 % on the buy. STT and stamp duty round to the rupee.

The entry it sizes at: the limit price with `--limit` (on the Rs 0.05 tick).
For a market order, the plan's entry, or the price in `trader/prices.json` if
it is fresh (under 15 minutes old) and higher. A price that ran up since the
plan means a bigger loss at the same stop, so fewer shares.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk is under `min_reward_risk`, the stop is not
below the entry, the target is not above it (the move already happened), the
plan is for another day, the symbol is blocked, `mis_leverage` is outside 1
to 5, or the plan is a short. Say the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees,
the costs at the stop and at the target, and the max loss at the stop with
costs. Say plainly that a stop is an order, not a promise: in a fast market
or a price-band freeze it can fill below the stop. Then hand over to
`/bharat-risk`.

## Example

RELIANCE on Monday 2026-10-19. Opening-range breakout: entry 1227.80 at 9:50
AM, stop 1215.35 (the opening-range midpoint), target 1240.25 (1R). Risk
12.45 a share. The example config: Rs 2,00,000 budget, 10 % cash buffer,
Rs 1,000 max loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | 73 x 12.45 + Rs 78.88 costs = Rs 987.73 (74 would be Rs 1,000.26) | **73 (decides it)** |
| position cap | 75 % of Rs 2,00,000 = Rs 1,50,000 / 1227.80 | 122 |
| max order | Rs 2,00,000 / 1227.80 | 162 |
| cash | Rs 2,00,000 minus Rs 20,000 = Rs 1,80,000 / 1227.80 | 146 |

73 shares = Rs 89,629.40. Costs at the stop: brokerage Rs 40, STT Rs 22, NSE
Rs 5.48, GST Rs 8.22, SEBI Rs 0.18, stamp Rs 3 = Rs 78.88. Max loss at the
stop: Rs 987.73. At the target, after Rs 79.94 of costs: Rs 828.91. Without
costs, the max loss would have allowed 80 shares. It writes:

```json
{"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "buy", "qty": "73", "type": "market",
 "time_in_force": "day", "order_class": "bracket", "take_profit_limit_price": "1240.25",
 "stop_loss_stop_price": "1215.35",
 "why": "opening-range breakout on RELIANCE: 73 shares, bound by your Rs 1,000 max loss per trade (73 x 12.45 + Rs 78.88 costs = Rs 987.73 at the stop)",
 "plan": {"entry": 1227.8, "stop": 1215.35, "target": 1240.25, "risk_per_share": 12.45, "max_loss": 987.73,
          "costs_at_stop": 78.88, "costs_at_target": 79.94, "setup": "orb", "entry_time": "9:50 AM", "flat_by": "15:15"}}
```

`why` and `plan` are notes for bharat-risk and bharat-broker. They are never
sent to the broker.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag. The broker's price is the one that fills.
- Costs are the published rates on the date in `india.py`. Taxes and charges change: check them against the broker's contract note.

## Index futures (P2)

A futures plan (`instrument: fut`, NIFTY or BANKNIFTY) is sized in whole
lots, and only with `"allow_fno": true`. The lots are the smallest of:

| limit | how |
| --- | --- |
| max loss | lots x lot x risk a unit + Zerodha's futures charges at the stop <= `max_loss_per_trade_inr` |
| margin | (cash - buffer) / `margin_per_lot_inr[SYMBOL]` |
| max lots | `max_lots` (default 1) |
| max order | `max_fut_notional_inr`, only if you set it |

`margin_per_lot_inr` (`{"NIFTY": ..., "BANKNIFTY": ...}`) is entered by the
user from Zerodha's margin calculator (zerodha.com/margin-calculator/SPAN);
MIS margin for F&O equals NRML. There is no default: no figure means a
refusal that names the key. The position cap and `max_order_inr` do not
apply (a lot is worth far more than its margin). The order is `exchange`
NFO, `product` MIS, `symbol` the contract, `qty` lots x lot, with
`instrument`, `underlying`, `lots`, `lot`, `expiry`, `margin`.

One NIFTY lot on the 21 Oct 2026 example risks Rs 6,201.78 with costs: the
default Rs 1,000 max loss refuses it. Say so; never raise a limit for the user.

## Index options (P3, buying only)

An options plan (`instrument: opt`) is sized in whole lots by premium. The
premium is paid in full from the cash (no margin, no leverage). Limits:
max loss (lots x lot x (premium - premium stop) + Zerodha's option charges
at the stop), cash after the buffer, `max_premium_inr` if set, `max_lots`.
The order is always a LIMIT buy at the premium on NFO, MIS, with an SL
stop whose limit is `stop_limit_ticks` (default 10) ticks under the trigger.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes F&O, margin
(MTF) or short positions.

## Safety

- Education and tooling, not financial advice, and not from a SEBI-registered adviser. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except bharat-broker, and only when all three hold: bharat-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (ask rules in the trading folder's .claude/settings.json). Never add an allow rule for an order tool.
- No F&O, margin (MTF), short selling or MIS leverage unless the config allows it and the user confirms.
- Broker keys live in the Kite server's key file (~/.config/kite-mcp-server/env), never in the chat.
