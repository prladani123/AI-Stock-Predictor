---
name: bharat-exit
description: >-
  Exit Planner for NSE intraday trades. Sets your stop and your target before
  you enter, so you know when to get out. Reads the entry from bharat-entry
  (or a price you give), puts the stop at the opening-range midpoint by
  default (or the range low, your price, a percent, or a multiple of the ATR),
  rounds it down to NSE's Rs 0.05 tick, and sets the target at 2R (or 1R, or
  your price). Out by 15:15 IST, before the broker's 15:20 MIS square-off.
  Refuses a stop at or above the entry, a target at or below it, or a stop
  too tight to survive the spread, and says why. Writes trader/plan.json for
  the time machine, the budget manager and price alerts. Long side only. Use
  when the user says "where's my stop", "set my stop and target", "plan the
  exit", "when do I get out", "stop loss", "what's my R", "risk reward", or
  right after /bharat-entry triggers, before /bharat-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X]"
---

# bharat-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set before the entry, written down and handed on, so nobody has to
decide anything in the middle of a fast move. A stop is a plan, not a
guarantee: a gap, a fast market or a price-band freeze can fill past it, or
leave you unable to exit. SEBI's studies show most retail intraday traders
lose money, and a neat plan does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                       # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5        # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --entry 1227.80 --or-high 1223.40 --or-low 1207.30
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                       # the current trader/plan.json
```

Other flags: `--flat-by 15:15`, `--json`, `--now "2026-10-19 09:50"` (IST),
`--entry PRICE` with a waiting entry.json to plan ahead, `--setup level`,
`--offline FILE` (a saved Yahoo chart JSON for an ATR stop).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from bharat-entry: symbol, entry price, time, bar, opening range |
| `trader/config.json` | `flat_by` (15:15) and `min_reward_risk` (1.0: a note here, bharat-risk enforces it) |
| Yahoo chart API | only for an `atr:K` stop: the session's bars up to the entry bar, free and unofficial |
| `trader/plan.json` | with `--write`: entry, stop and its rule, target and its R, risk a share, reward:risk, flat by. bharat-timemachine, bharat-budget and bharat-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **The tick.** NSE equity prices move in Rs 0.05. The stop rounds away from
  the entry to the tick (down, for a long). Risk = entry minus stop. An R
  target = entry + R x risk, rounded UP to the tick; your own target price
  rounds to the nearest tick. `2R` by default; `1R`, `1.5R` or `price:X` also
  work.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under 0.05% of the price: too tight to survive the spread,
  normal wiggle would stop you out. An entry.json that has not triggered
  (unless you give `--entry`). A short: P1 plans longs only.
- **Flat by.** 15:15 IST by default (or `flat_by` in the config). Out by
  then, win or lose, no overnight hold. Zerodha squares off MIS positions
  from 15:20, at its price and with a charge, so a flat-by after 15:20 gets a
  note. A flat-by at or after the 15:30 close, or an entry at or after the
  flat-by time, is refused.

## Steps

1. Run it right after `/bharat-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule, the
   target and its rule, the risk a share, reward:risk and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther target).
   Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/bharat-timemachine` to replay the plan, then
   `/bharat-budget` to size it to the max loss per trade, costs included.
   Offer `/bharat-alerts` for the stop, the target and the flat-by time.

Claude can keep `entry.py watch` running in the background or on a `/loop`
until a setup triggers, then come here.

## Example

RELIANCE, Monday 2026-10-19. The entry is 1227.80 at 9:50 AM (the 9:45 bar,
bar 6) and the opening range is 1207.30 to 1223.40.
`exit.py plan --stop or-mid --target 1R --write`:

```
Exit plan: RELIANCE long, Mon 2026-10-19, opening-range breakout on 5-minute bars
  Entry         Rs 1,227.80 at 9:50 AM (bar 6)
  Stop          Rs 1,215.35  opening-range midpoint: (Rs 1,223.40 + Rs 1,207.30) / 2 = Rs 1215.35
  Target        Rs 1,240.25  1R (one times the risk)
  Risk a share  Rs 12.45 (1.01% of the entry)
  Reward:risk   1.0
  Flat by       15:15 IST, win or lose: no overnight hold
```

The midpoint lands on the tick, so nothing is rounded. The default 2R target
would be 1227.80 + 2 x 12.45 = 1,252.70. An `atr:1.5` stop on the seven bars
to the entry (ATR 7.457) is 1216.6142, rounded down to the tick: 1,216.60.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk. It never
  prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Long side only. P1 plans longs only.

## Index futures (P2)

From a futures `trader/entry.json` the stop and target rules run on the
index (the or-mid stop is the index range's midpoint), then move to the
future: stop = futures entry - the index risk in points x the basis (tick
down), target = futures entry + the reward in points x the basis (tick up).
The plan carries `instrument: fut`, `contract`, `lot`, `expiry`,
`basis_ratio`, `basis_date`, `basis_estimated`, `fut_price_source`, the
index levels under `index`, `risk_per_unit` and `risk_per_lot`.

`--entry PRICE` on a futures plan is the live futures price from Kite: the
stop and target keep their distance in points from it. Give it before
sizing. Without it, size a limit order at the estimate (`size.py --limit`):
bharat-risk blocks a market entry on the bhavcopy estimate.

## Index options (P3, buying only)

From an options `trader/entry.json` the stop and target rules run on the
index (a put's are mirrored: its stop goes above the index, its target
below), then each level becomes a premium: Black-76 on the forward at the
entry time, with the entry's implied volatility held constant. The premium
stop ticks down (never under Rs 0.05: a clamped stop can lose the whole
premium), the target ticks up. A target that the model says cannot pay even
if the index reaches it at the flat-by (the time decay eats the move) is
refused. The plan keeps both: `entry`/`stop`/`target` as premiums and
`index_entry`/`index_stop`/`index_target`. `--entry P` on an options plan is
the premium you paid; it sets the implied volatility. Read later than the
trigger? Add `--premium-index I`, the index when you read it.

## What it never does

It never places an order, never sizes a position, never widens a stop to make
a plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice, and not from a SEBI-registered adviser. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except bharat-broker, and only when all three hold: bharat-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (ask rules in the trading folder's .claude/settings.json). Never add an allow rule for an order tool.
- No F&O, margin (MTF), short selling or MIS leverage unless the config allows it and the user confirms.
- Broker keys live in the Kite server's key file (~/.config/kite-mcp-server/env), never in the chat.
