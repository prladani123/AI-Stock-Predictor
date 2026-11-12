---
name: bharat-risk
description: >-
  Risk Manager for NSE intraday trades. Checks every trade against your
  limits and stops anything over them, before it can reach the broker. For an
  intraday trade: a stop is required, the loss at the stop plus Zerodha's
  round-trip costs must fit your max loss per trade (measured from the higher
  of the plan's entry and a fresh price), reward:risk must meet your minimum,
  costs may not eat more than a quarter of the reward, the day's total risk
  must fit your daily loss limit, no more than your trades per day, no new
  entries outside your entry window (IST) or when NSE is closed, and no entry
  at a stock's upper price band. It also keeps the basics: MIS (intraday)
  only, blocked symbols, order size, position cap, cash buffer and MIS
  margin, short sales, F&O contracts, malformed bracket orders, duplicates
  and a daily order limit. Getting out is never blocked by a day limit.
  Prints PASS or BLOCK with the reason and records the verdicts so
  bharat-broker only shows an order that passed. Use when the user says
  "check my order", "is this safe", "risk check", "does this break my rules",
  "can I take this trade", "how much have I risked today", "are the charges
  worth it", or before /bharat-broker, always.
argument-hint: "[path to orders.json]"
---

# bharat-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by bharat-budget or bharat-broker's flatten passes through it. A
BLOCK is final for that order: it does not go to the user for a yes, and it
does not go to the broker. SEBI's studies show most retail intraday traders
lose money. These limits keep each loss and each day's losses small. They do
not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-19 09:51"  # pin the clock (IST)
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (bharat-budget, or bharat-broker's flatten) |
| `trader/config.json` | the limits (bharat-budget ships `config.example.json`) |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | a fresh price raises the entry it measures risk from; also values holdings |
| `trader/entry.json` | bharat-entry's previous close and the band the scanner found, for the band check |
| `trader/state/broker.json` | what was placed today: duplicates, trades per day, daily loss |
| `trader/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

## Entries and exits

An **entry** opens or adds to a position: a buy, or a sell that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, asset and short sales
only. Every other rule (day limits, entry window, loss, costs, band,
blocked list, size, duplicates) is a warning for an exit, never a BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no symbol, side not buy or sell, no whole-share qty (NSE has no rupee-amount orders), a limit order with no limit price, a bracket that misses its stop-loss or take-profit |
| asset | an F&O contract (`NIFTY26NOVFUT`, `...CE`, `...PE`) while `allow_fno` is false; anything that is not an NSE symbol; an F&O-looking name it cannot read (a bad month code, or a weekly dated 31 Nov), exits included |
| short | a sell of more than you hold (`allow_short` false) |
| product | (entries) anything but MIS: P1 plans intraday trades only |
| blocked | (entries) the symbol is on the `blocked` list |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy would leave less than the cash buffer, or need more than `mis_leverage` x your cash |
| stop | (entries) no stop while `require_stop` is true; a stop on the wrong side of the entry; a market or limit order whose fresh price is already past its stop; live (`"paper": false`), a stop that is only in the plan and not at the broker |
| loss | (entries) qty x (entry minus stop) + round-trip costs at the stop over `max_loss_per_trade_inr` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| costs | (entries) round-trip costs at the target over `max_cost_share` (0.25) of the reward at the target |
| daily loss | (entries) the max losses at the stops of today's entries, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` (09:30) or after `no_entries_after` (14:45), or NSE is closed |
| band | (entries) the entry at or over the stock's upper price band; within 1 % of it is a warning |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a limit order. For a
  market order, the HIGHER of the plan's entry and a fresh price from
  `trader/prices.json` (under 15 minutes old); for a short sell
  (`"allow_short": true`) the LOWER, since its stop is above it. The size
  checks (`max_order_inr`, `max_fut_notional_inr`) value a short at the
  higher one all the same.
- **Costs** are Zerodha's intraday equity charges from `india.py` (brokerage,
  STT, NSE transaction and investor protection charges, SEBI fee, GST,
  stamp duty), the same numbers bharat-budget sized with. The loss at the
  stop includes them, so the two skills agree to the paisa.
- **The costs check** is for small or tight trades. 73 RELIANCE shares for a
  Rs 12.45 move pay Rs 79.94, 9 % of the reward: fine. 40 shares of a Rs 500
  stock for a Rs 1 move pay Rs 21.67 on a Rs 40 reward, 54 %: blocked.
- **Daily loss** counts the full max loss of every entry placed today, win or
  lose, as bharat-broker logged it.
- **The window** is IST, from `india.py`'s NSE calendar, never the computer's
  clock zone. Weekends, NSE holidays and years with no holiday list are
  closed, even with the entry window switched off. On the Muhurat session (14:20 to 15:30 in 2026) the default window
  leaves 14:20 to 14:45. Set both `no_entries_*` to null to switch it off.
- **The band** comes from `trader/entry.json`: bharat-entry's previous close
  and the band the scanner wrote on the watchlist hit (or `--band`). F&O
  stocks have no fixed band. When no band is known the check is off and a
  warning says so; an `entry.json` from another day is not used.
- **The clock.** `--now` and `--today` are for tests and paper only; a live
  check refuses them.
- **F&O ban.** A stock in NSE's F&O ban period can still be traded in cash,
  so the ban does not block a P1 order. It matters for F&O (P2).

Warnings do not block, but say them to the user: a LIVE account, a number
sent as a number instead of a string, a stop that lives only in the plan and
not at the broker, a price too old to use, an entry near the band.

## Steps

1. If the price may have moved since the plan, refresh it first
   (bharat-alerts: `alerts.py quote RELIANCE --write`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop with costs, reward:risk, the costs, and how much of today's
   limits it uses. For a BLOCK, give the reason and the fix (fewer shares, a
   closer stop from bharat-exit, a wider target, wait for the window, or skip
   the trade). Never change a limit yourself.
4. If the order changes in any way, run the check again. bharat-broker matches
   each order to its verdict by fingerprint, so an edited order is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example

The RELIANCE bracket from bharat-budget at 9:51 AM on Monday 2026-10-19, on a
fresh Rs 2,00,000 paper account with the example config:

```
PASS  #1 buy RELIANCE 73 shares, entry
          costs Rs 78.88 at the stop, Rs 79.94 at the target (9% of the Rs 908.85 reward)
          entry 1227.80 (the plan's entry), stop 1215.35, target 1240.25: risk 12.45 a share
          max loss Rs 987.73 incl. costs (limit Rs 1,000), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, Rs 0 of Rs 3,000 daily loss

1 passed, 0 blocked at 2026-10-19 09:51 IST. Cash after the passed orders: Rs 1,10,370.60 (paper account).
```

One more share and it is blocked: 74 x 12.45 + Rs 78.96 costs = Rs 1,000.26
at the stop, over the Rs 1,000 max loss. At 9:20 it is blocked by the window
(no new entries before 9:30). The 15:15 sell that closes the position
passes, even after the 14:45 entry cutoff: it is an exit.

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices or the broker log.
- Free Yahoo prices are unofficial and can lag; the broker's price is the one that fills, and a
  stop can fill worse than its price in a fast market or a band freeze.

## Index futures (P2)

For an NFO order (`instrument: fut`), with `allow_fno` on:

- **format** the qty must be whole lots (`lot` on the order); an entry
  without a lot size is blocked.
- **margin** lots x `margin_per_lot_inr[underlying]` must leave the cash
  buffer; no figure blocks. It replaces the cash, position and max-order
  checks (`max_fut_notional_inr` caps the notional if set).
- **lots** more than `max_lots`, counting the lots already held, blocks.
- **entry** a market order whose futures price is the bhavcopy estimate
  blocks (the basis can move 75 points in a session): send a limit order or
  give the live price (`fut_price_source: user`).
- **expiry** a new entry in a contract that expires today blocks and names
  the next one ("trade NIFTY26DECFUT").
- **basis** a market entry whose futures price is estimated from an old
  bhavcopy blocks.
- **costs** use Zerodha's futures charges.

The F&O ban list does not apply to index futures (it lists stocks only).
Getting out is never blocked: these are entry checks.

## Index options (P3, buying only)

For an option order (`instrument: opt`, or a `...CE`/`...PE` name), with
`allow_fno` on:

- **short** any sell that opens or adds to a short option blocks, whatever
  the config says: option selling is not part of this pack. Selling an
  option you hold is an exit.
- **format** an F&O order not on `exchange: NFO` blocks, and so does a
  market order on an option, entry or exit: an option is closed at a limit
  too (`ticket.py flatten --symbol C --limit PRICE`). This is a format
  rule, and a limit sell still gets you out.
- **entry** anything but a limit order blocks, and so does a stop leg
  without `stop_loss_limit_price` (NSE takes no SL-M on options). An NFO
  name the pack cannot read as a future or an option, with no
  `instrument`, blocks too.
- **premium** a premium estimated from the bhavcopy blocks: give the live
  premium (`premium_source: user`, from bharat-exit --entry).
- **liquidity** open interest under `min_option_oi`, or no trades in the
  last bhavcopy, blocks.
- **expiry** an expired contract, a missing expiry, or a new entry on
  expiry day at or after `expiry_day_cutoff` (default 13:00) blocks. A
  weekly name carries its date (`NIFTY26O2023100CE` expires 20 Oct), and
  that date wins: an order whose `expiry` disagrees blocks. A monthly name
  gives the month only, so the order's expiry must be in that month.
- **freeze** an F&O order over NSE's per-order limit (NIFTY 1,800 units,
  BANKNIFTY 900) blocks, exits too: send it as several tickets.
- **costs** use Zerodha's option charges. Round-trip costs over
  `max_option_cost_share` (default 0.10) of the premium paid block, whatever
  the target: 1 lot of a Rs 3 option pays about Rs 47 on Rs 195 (24 %);
  5 lots pay Rs 49 on Rs 975 (5 %).
- **lots** more than `max_lots`; `max_premium_inr` caps the premium.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out (an option's close must be
a limit order, which still gets them out), and never
treats a PASS as the user's yes.

## Safety

- Education and tooling, not financial advice, and not from a SEBI-registered adviser. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except bharat-broker, and only when all three hold: bharat-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (ask rules in the trading folder's .claude/settings.json). Never add an allow rule for an order tool.
- No F&O, margin (MTF), short selling or MIS leverage unless the config allows it and the user confirms.
- Broker keys live in the Kite server's key file (~/.config/kite-mcp-server/env), never in the chat.
