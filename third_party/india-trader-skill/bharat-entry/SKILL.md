---
name: bharat-entry
description: >-
  Entry Timer for NSE stocks. Watches a setup bar by bar and tells you the
  moment to get in. Reads free Yahoo 1-minute or 5-minute bars, builds the
  opening range (the first 15 minutes from 9:15 IST by default) and calls the
  entry on the first completed bar that closes above the range high, or above
  a level you give it (like a 20-day high from the scanner), with an optional
  volume check and a price-band warning. Says waiting and how far, triggered
  with the time and the price, expired, or not open yet, and writes
  trader/entry.json for /bharat-exit. Long side only. It never places an
  order. Use when the user says "tell me when to get in", "watch RELIANCE",
  "has it broken out", "did it break the opening range", "entry signal",
  "time my entry", "check my watchlist", or after /bharat-scanner and
  /bharat-news, before /bharat-exit.
argument-hint: "[symbol] [--setup orb|level] [--interval 5m|1m]"
---

# bharat-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads bars, 1-minute or 5-minute, and waits for each one to finish. A price
that pokes above the high in the middle of a bar is not an entry. A bar that
closes above it is. Then it says the time and the price, writes them down for
the exit planner, and stops. It never buys anything. SEBI's studies show most
retail intraday traders lose money, and a clean signal does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE              # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --write      # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --setup level --level 1250
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --band 5     # warn near a 5 % price band
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch RELIANCE --until 14:45  # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                   # every hit in trader/watchlist.json
```

Other flags: `--or-minutes N` (default 15), `--json`, `--offline FILE` (a
saved Yahoo chart JSON, `.json` or `.json.gz`, or a folder of
`<SYMBOL>.json`), `--now "2026-10-19 09:50"` (IST) to replay a day. `watch`
with `--now` or `--offline` runs on a simulated clock and replays the day bar
by bar without waiting.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`, and `band` when the scanner had it), for `watchlist` |
| `trader/config.json` | `no_entries_before` (09:30) and `no_entries_after` (14:45), IST. Optional |
| Yahoo chart API | today's bars, `range=1d&interval=5m` (or `1m`): free, unofficial, can lag |
| `trader/entry.json` | with `check --write`, or when `watch` triggers: symbol, setup, status, time, bar, price, level, opening range, previous close, band |

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes: the
  9:15, 9:20 and 9:25 five-minute bars, or fifteen 1-minute bars. The level
  to beat is the range high.
- **Level.** A price you give, like a 20-day high from `/bharat-scanner`.
- **The trigger** is the first completed bar after the range that closes
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time (the 9:45 bar closes at 9:50 AM). The
  price is its close, to the paisa.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Yahoo has caught up to that moment (it waits a
  minute at most). Yahoo's live tick and the 15:30 closing print are never bars.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. Yahoo reports volume 0
  for NSE's 9:15 bar, so the average leaves the 9:15 bar out.
- **Price band.** With `--band PCT`, or the band the scanner wrote on the
  watchlist hit, it warns when the price is within 1 % of the band's upper
  limit (from the previous session's close). A stock frozen at its band can
  leave you unable to exit.
- **Time limits.** No entries before 9:30 or after 14:45 (config), so a trade
  has time before the 15:15 flat-by and the broker's 15:20 MIS square-off.
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before 9:15: not open yet. Before 9:30: the
  range is still forming. Weekends and NSE holidays: the market is closed.
  On the Muhurat session the range starts at that session's open.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/bharat-scanner`. Check `/bharat-news` for it first.
2. Run `entry.py check SYMBOL`. Tell the user the status in one line: waiting
   and how far, triggered at what time and price, or expired.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 14:45` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade. Say any price-band warning.
5. Hand off to `/bharat-exit` to set the stop and the target before anything
   is bought.

## Example

RELIANCE, Monday 2026-10-19, 5-minute bars. The opening range is 1207.30 to
1223.40. At 9:47 the 9:45 bar is still open, so it waits:

```
WAITING  last close Rs 1221.80 (9:40 bar), Rs 1.60 under the range high Rs 1223.40 (-0.13%).
The 9:45 bar is still forming (Rs 1227.80 so far, closes 9:50 AM); it does not count until it closes.
```

At 9:50 the bar has closed:

```
RELIANCE  Mon 2026-10-19 09:50 IST  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag)
Opening range 9:15-9:30: high Rs 1223.40, low Rs 1207.30 (3 of 3 bars).
TRIGGERED 9:50 AM at Rs 1227.80: the 9:45 bar (bar 6) closed above the range high Rs 1223.40 (+0.36%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /bharat-exit (exit.py plan --from trader/entry.json).
```

The trigger bar traded 448,544 shares, 1.52 times the range's average
(294,168, from the 9:20 and 9:25 bars). With `--min-volume-x 2` it is too
thin and nothing triggers by 9:52.

## Rules

- Bar by bar, on free Yahoo 1-minute or 5-minute bars. They are unofficial and
  can lag. Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only. P1 plans longs only. If the user wants a short, say that.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.

## Index futures (P2)

`check NIFTY --instrument fut` (or BANKNIFTY) reads the signal on the index
bars as usual and adds a `future` block to the result and to
`trader/entry.json`: `contract`, `expiry`, `lot`, `basis_ratio`,
`basis_date`, `price`, `source`, `estimated`, `warnings`.

- The contract is the nearest future that does not expire today, from NSE's
  F&O bhavcopy (`--bhavcopy FILE`, default: download the last session's).
- `price` = the trigger close x the basis (futures close / index close in
  that bhavcopy), on the Rs 0.05 tick. It is an estimate: the basis moves in
  the day (75 points on 21 Oct 2026). `--fut-price P` (the price on Kite)
  wins and is not an estimate.
- A bhavcopy older than the last session sets `estimated` with a warning;
  bharat-risk blocks a market entry on it.
- A contract that expires on the next trading day warns that it rolls over
  and names the next one.
- `watchlist` is for stocks; check the indices one at a time.

## Index options (P3, buying only)

`check NIFTY --instrument opt --right CE` (or `PE`, or BANKNIFTY) reads the
signal on the index: a call on a close above the opening-range high, a put
on a close BELOW the opening-range low (the ORB's mirror). This pack only
buys options; it never writes them. The result adds `option`:

- `contract`, `strike` (ATM, or `--steps-otm N` strikes out of the money:
  higher for a call, lower for a put), `right`, `expiry` (the nearest that
  is not today, or `--expiry YYYY-MM-DD`), `lot`, `oi`, `volume`.
- `premium`: `--premium P` (the price on Kite) sets the implied volatility
  from it (`premium_source: user`). Without it the premium is estimated:
  Black-76 on the forward (the index x the put-call parity ratio from the
  last bhavcopy, same expiry), with the implied volatility of the last
  bhavcopy close, held constant (`premium_source: estimated`). A premium
  read after the trigger needs `--premium-index I` (the index then) so its
  implied volatility is solved where and when it was read.
- A strike under `min_option_oi` (config, default 50,000 units) or with no
  trades in the bhavcopy warns (`illiquid`); bharat-risk blocks it.
- A premium under its intrinsic value has no implied volatility: refused
  with the reason, never a number.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/bharat-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/bharat-broker` from a background
watch: nobody is there to say yes.

## Safety

- Education and tooling, not financial advice, and not from a SEBI-registered adviser. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except bharat-broker, and only when all three hold: bharat-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (ask rules in the trading folder's .claude/settings.json). Never add an allow rule for an order tool.
- No F&O, margin (MTF), short selling or MIS leverage unless the config allows it and the user confirms.
- Broker keys live in the Kite server's key file (~/.config/kite-mcp-server/env), never in the chat.
