---
name: bharat-scanner
description: >-
  Chart Scanner for NSE. Scans thousands of Indian stock charts for setups:
  opening-range breakouts, flags, oversold bounces. Builds the list of
  EQ-series NSE stocks (the ones that trade intraday) from NSE's EQUITY_L.csv,
  runs a pre-market scan of daily closes (20-day closing-high breakout, bull
  flag, reclaimed the 20-day average, oversold, oversold bounce) and an
  intraday opening-range breakout scan on 5-minute bars from 9:15 IST, flags
  each hit's price band and F&O ban, then writes trader/watchlist.json for
  bharat-news and bharat-entry. Free Yahoo data, bar by bar. Use when the
  user says "scan the market", "scan NSE", "find me setups", "what is
  breaking out", "opening range breakouts", "scan the Nifty 50", "pre-market
  scan", "build my watchlist", "what should I look at today", or at the start
  of a trading day before /bharat-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# bharat-scanner

The Chart Scanner is the first step of an intraday trade on NSE. It reads
free charts and lists the ones that match a rule. That is all it does. A scan
is a list of charts that match a rule, not a list of stocks to buy, and a
setup on a chart is not a prediction. SEBI's studies show most retail
intraday traders lose money; the scanner does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                            # -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                               # before 9:15 -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,TCS,INFY   # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                                 # after 9:35 AM IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                      # cut the clock at 10:15 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--min-price 50` (Rs) and `--min-turnover` in Rs crore (daily 5, orb
0), `--or-minutes 15`, `--max-confirm 200` (orb stage 2), `--now "2026-10-19
10:15"` (IST) to pin the clock, `--offline PATH` to read saved Yahoo files,
`--nse-files DIR` for saved `sec_list.csv` / `fo_secban.csv`, `--no-write`,
`--show N`, `--json`. Exit code 0 when the scan found something, 1 when it
found nothing or could not run (the reason is one plain line).

Symbols are NSE symbols (`RELIANCE`, `M&M`, `BAJAJ-AUTO`); the tool adds
Yahoo's `.NS` itself. `NIFTY` and `BANKNIFTY` map to the indices.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line, read by `daily` and `orb` |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. bharat-news and bharat-entry read `hits[].symbol` |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 14:45) |

## The universe

`universe` downloads `EQUITY_L.csv` from nsearchives.nseindia.com and keeps
the EQ series: the stocks you can trade intraday (MIS). BE and BZ (trade for
trade) and SME series are out. On 4 Nov 2026 that is 2,392 of 2,627 rows.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request. Liquidity:
last price at least `--min-price` and the last day's turnover (price x
volume) at least `--min-turnover` crore. Run it before 9:15 or after the
close: during the session the volume is the day's so far, and today's daily
bar is left out until 15:30.

Yahoo often leaves the latest NSE daily close empty for hours after the
close. The scanner then takes the final price from the chart's meta, which is
stamped at the close (15:29:39). It never does that during the session.

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Hits are listed most traded first (price times volume, in rupees).

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 9:15,
9:20 and 9:25 five-minute bars. A hit is the first completed 5-minute bar to
CLOSE above the range high, at that bar's close time.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range (a necessary condition).
2. Stage 2, each candidate: the v8 chart with real highs and lows. At most
   `--max-confirm` (200), the most traded first; PARTIAL is printed with the
   count skipped. `--max-confirm 0` checks them all.

Each hit carries `band` (the stock's NSE price band: 5, 10, 20 %, or None
when it has no fixed band, as F&O stocks do) and `fno_ban`. A stock in the
F&O ban period cannot take new F&O positions; cash intraday trading in it is
still allowed, so it is flagged, not dropped. A 5 % band stock can freeze at
its limit and leave you unable to exit: the scan prints the band for those.

A chart is usable only when every 5-minute bar from 9:15 to the cutoff is
there; a missing bar is a freeze, a halt or a thin stock. Null closes are
dropped, the live tick and the 15:30 closing print are dropped, and a bar
counts only once it is complete. Before 9:35 AM, on a weekend or NSE
holiday, after the close, or after `no_entries_after` it prints one plain
line and writes nothing. Yahoo reports volume 0 for the 9:15 bar.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars are
  5-minute bars, never tick by tick. query2 first, query1 on a 429 or 5xx, one
  retry after a 5-second wait on a 429.
- Partial results are said as partial. Never round a partial scan up to "the
  whole market".
- NSE holidays for 2026 (nseindia.com, read 2026-11-04). Other years read as
  "holiday list unknown" until they are added to `india.py`.

## Steps

1. Once a week or so: `scan.py universe`.
2. Before 9:15: `scan.py daily`. Tell the user the counts and the top few by setup.
3. After 9:35 AM IST (10:15 is the classic): `scan.py orb`. Tell the user how
   many usable charts broke their range and when, and any band or ban flags.
4. The user picks one chart. Hand it on: `/bharat-news SYMBOL`, then
   `/bharat-entry SYMBOL`. Never pick for them.

## Example

Monday 19 Oct 2026, 10:15 AM IST, the NIFTY 50
(`tests/fixtures/bharat-scanner/wall5m-2026-10-19.json.gz`, real Yahoo charts):

```
python3 scan.py orb --offline wall5m-2026-10-19.json.gz --now "2026-10-19 10:15" --nse-files nse
Fetched 50 of 50 charts; 50 usable; 24 candidates, 24 confirmed with real highs and lows.
8 of 50 charts closed above their opening-range high by 10:15 AM:
   9:35 AM  SBIN           Rs 978.95  range 960.90 to 977.70  +0.13% over
   ...
   9:50 AM  RELIANCE     Rs 1,227.80  range 1,207.30 to 1,223.40  +0.36% over
```

RELIANCE's range is the 9:15 to 9:25 bars, high 1,223.40, low 1,207.30; the
9:45 bar closed at 1,227.80, 0.36 % over the high, at 9:50 AM. The user picks
RELIANCE and Claude hands it to `/bharat-news RELIANCE`, then
`/bharat-entry RELIANCE`.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data (IST).
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## Index futures (P2)

`scan.py orb --symbols NIFTY,BANKNIFTY` scans the indices themselves
(`^NSEI`, `^NSEBANK`). A hit there is the signal for an index futures
trade: `/bharat-entry check NIFTY --instrument fut` prices the contract.
Index volume on Yahoo is always 0, so do not use a volume filter on them.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice, and not from a SEBI-registered adviser. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except bharat-broker, and only when all three hold: bharat-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (ask rules in the trading folder's .claude/settings.json). Never add an allow rule for an order tool.
- No F&O, margin (MTF), short selling or MIS leverage unless the config allows it and the user confirms.
- Broker keys live in the Kite server's key file (~/.config/kite-mcp-server/env), never in the chat.
