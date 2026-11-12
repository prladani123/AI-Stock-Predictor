---
name: bharat-timemachine
description: >-
  Time Machine for NSE intraday trades. Replays every possible outcome of a
  trade before you risk a rupee: 10,000 simulated sessions plus every past
  time the setup fired. Takes the plan from bharat-exit (entry, stop,
  target), rebuilds the rest of the day 10,000 times from the stock's own
  5-minute bars over the last 60 days, up to the 15:15 IST flat-by, and
  re-runs the same rule on every past session. Prints the odds of target
  first, stop first, or neither by the flat-by, where the price sits then,
  the average R, the history, and one honest verdict, then hands off to
  /bharat-budget. Use when the user says "run the time machine", "replay this
  trade", "what are the odds", "simulate this trade", "monte carlo",
  "backtest this setup", "how often does this hit the target", "is this a
  coin flip", or after /bharat-exit writes a plan, before /bharat-budget.
argument-hint: "[path to plan.json, or SYMBOL]"
---

# bharat-timemachine

The Time Machine sits between the plan and the money. bharat-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the stock's own last 60
days, how often does this plan hit the target first, the stop first, or
neither? It answers with 10,000 replays and with the real record of the same
rule on every past session. It is a simulation built from the stock's own
recent bars, not a prediction. SEBI's studies show most retail intraday
traders lose money, and a good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:00      # end each replay earlier than the flat-by
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-10-19 \
    --entry 1227.80 --stop 1215.35 --target 1240.25 --entry-time "9:50 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--offline FILE` (a saved Yahoo chart JSON, `.json` or `.json.gz`),
`--now "2026-10-19 09:51"` (IST) to pin the clock.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from bharat-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range, flat_by |
| Yahoo chart API | one request: 60 days of 5-minute bars (`range=60d&interval=5m`), free and unofficial, can lag |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the price at the end |

## How it works

1. **The pool.** It keeps the sessions before the plan's date that have
   every five-minute bar from 9:15 IST to the end of the replay (the plan's
   flat-by, 15:15 by default). Sessions with a gap, today and the Muhurat
   session are left out. Yahoo's NSE data never has a 15:20 bar and often
   has no 15:25 bar, so a replay to the 15:30 close finds too few sessions:
   it refuses and says to stay at or before 15:15. Every bar becomes four
   ratios to the bar before it: open, high, low and close over the previous
   close.
2. **One replay.** Start at the entry price. For each bar left until the
   flat-by, pick a past bar from the same time of day (plus or minus 2 bars)
   at random, and scale it to the current price. Bar by bar, it checks the
   stop first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (the flat-by). Green,
   which means target first or above the entry at `--until`. R is +1 at a 1R
   target, -1 at the stop, and otherwise the distance from the entry at
   `--until` divided by the risk. A gap through the stop still counts as -1R
   here. A real fill can be worse. R leaves out trading costs:
   `/bharat-budget` adds those.
4. **The history.** The plan's own rule (the same opening range, stop rule
   and target R, on NSE's Rs 0.05 tick) runs again on every one of those past
   sessions. That is what really happened, not a simulation.
5. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them.
6. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 usable sessions, when the entry bar
is not in the data (wrong date, not closed yet, a future day), when the plan
is on the Muhurat session, or when the plan does not make sense (a stop
above a buy's entry, an entry off the 5-minute grid). A live in-progress bar
is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/bharat-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. It ends each replay at the plan's
   flat-by, so "neither" means still open at the time you would get out.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the flat-by, the average R, the history line, then
   the verdict line and any line under it exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 days and is not a
   prediction. Results, news, block deals and price-band freezes can do
   things those 60 days never did. Check `/bharat-news` before going on.
6. If the user still wants the trade, hand off to `/bharat-budget` to size it.

## Example

RELIANCE, Monday 2026-10-19. The 15-minute opening range is 1207.30 to
1223.40. The 9:45 bar closes above it: entry 1227.80 at 9:50 AM (bar 6),
stop 1215.35 (the range midpoint), target 1240.25 (1R, risk 12.45 a share).

```
10,000 replays of the rest of the session, 9:50 AM to 3:15 PM (65 five-minute bars), seed 19.
  target first   17.0 %
  stop first     17.7 %
  neither        65.3 %   still open at 3:15 PM
  green          49.3 %   target first, or above the entry at 3:15 PM
  Price at 3:15 PM: median 1227.66, 90 % of replays between 1213.03 and 1243.22.
  Average R: -0.008
History: the same rule on each of those 48 real sessions fired 22 times:
10 target, 10 stop, 2 neither. Average R -0.027.
Close to a coin flip: target first 17.0 % vs stop first 17.7 %.
65.3 % of the replays hit neither by 3:15 PM; the price at 3:15 PM decides those.
```

At 1R the target has to come first in half of the replays that hit one
side. It did in 49 %. Two in three replays are still open at 3:15 PM, so the
price then decides most of them. The real history was an even split. That is
the honest answer for this setup, before costs, and it is what the user
should hear before `/bharat-budget` sizes it.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## Index futures (P2)

A futures plan replays the index's own bars with the plan's index levels
(`plan.index`, or the futures levels divided by `basis_ratio`). A constant
basis moves every level the same way, so the odds and R are the index's.
The report adds the futures view: "futures price = index x RATIO from
DATE", the futures entry, stop and target, and the price at the end of the
replay in futures points.

## Index options (P3, buying only)

An options plan replays the index exactly as for a stock or a future, and
at every bar prices the option with Black-76 (the path's index, the time
left to expiry at that bar, the plan's implied volatility, held constant).
Stop and target are decided on the premium, so time decay can stop a trade
out while the index stands still. The odds, the green share and R are the
premium's; the report names the implied volatility, says it is held
constant, and gives the premium at the end of the replay. The history
replay does the same on past sessions with the plan's strike distance and
time to expiry (past sessions' implied volatilities are not known).

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice, and not from a SEBI-registered adviser. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except bharat-broker, and only when all three hold: bharat-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (ask rules in the trading folder's .claude/settings.json). Never add an allow rule for an order tool.
- No F&O, margin (MTF), short selling or MIS leverage unless the config allows it and the user confirms.
- Broker keys live in the Kite server's key file (~/.config/kite-mcp-server/env), never in the chat.
