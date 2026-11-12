# The India Trader skill

11 Claude skills for intraday trading on NSE: scan the market, track announcements, time every entry and exit, and replay every outcome before you risk a rupee.

Free, MIT, no signup. The tools are plain standard-library Python that read
free public data: Yahoo Finance's charts for NSE stocks, NSE's own files and
announcements, and NSE's results filings. Trading costs are Zerodha's,
checked against its own calculator.

One scans the NIFTY 50 (or all 2,392 EQ-series stocks) for setups. Another
reads what each company filed with NSE. One watches a setup bar by bar from
9:15 IST and tells you the moment to get in, and another sets your stop and
your target, on the Rs 0.05 tick, before you do. The Time Machine replays
the rest of the session 10,000 times from the stock's own recent bars, plus
every past day the same setup fired. The budget and risk skills put
Zerodha's charges into every loss, because on an intraday trade they are not
small.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading on a local ledger is the default.

This is the India port of the Day Trader skill by Jake Schincariol
([github.com/Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill),
MIT). The method and most of the code come from there; the market, the data,
the clock, the costs and the broker are India's.

## Install

Paste this into Claude:

```
https://github.com/nawed1111/india-trader-skill

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add nawed1111/india-trader-skill
/plugin install india-trader-skill@india-trader-skill
```

Installed as a plugin they show up as `/india-trader-skill:bharat-scanner`
and so on. Copy the folders instead if you want plain `/bharat-scanner`:

```bash
git clone https://github.com/nawed1111/india-trader-skill.git
cp -r india-trader-skill/bharat-* ~/.claude/skills/
```

Every skill folder works on its own: each carries its own copy of `india.py`
(the NSE clock, holidays, symbols and costs). Project-local instead of global:
copy the same folders into your repo's `.claude/skills/`. The tools need
Python 3.8 or newer. Nothing to pip install, no API keys for the data.

### Live trading setup (Zerodha)

Paper trading needs none of this. For live orders through Kite, one script
sets everything up in a folder of your choice, an external SSD included
(macOS or Linux; it needs git, Go 1.21+ and Python 3):

```bash
curl -fsSL https://raw.githubusercontent.com/nawed1111/india-trader-skill/main/tools/setup-trader.sh -o setup-trader.sh
```

```bash
bash setup-trader.sh /Volumes/MySSD/trader
```

Use your own folder (`ls /Volumes` shows an SSD's name). `tools/setup-trader.sh`
makes the folder, clones and builds Zerodha's own
[kite-mcp-server](https://github.com/zerodha/kite-mcp-server), installs the
eleven skills for that folder, writes a paper-trading `trader/config.json`,
and runs one shared Kite server in the background (HTTP mode on
`127.0.0.1:8080`, started at login) with placeholder keys and the order tools
switched off. It connects Claude Code to that server and sets the order tools
to ask before every call. Run it again any time: it keeps your keys, and
moves the ones you typed for the old setup (in Claude Desktop's config) over,
with the order tools switched off. A key file from an earlier run whose
`EXCLUDED_TOOLS` line leaves the GTT order tools on gets them added.
Then:

1. **Keys.** Make a Kite Connect app at developers.kite.trade and set its
   Redirect URL to `http://127.0.0.1:8080/callback`: Zerodha sends you back
   there after you sign in. Type its key and secret over `PASTE_KEY_LATER`
   and `PASTE_SECRET_LATER` in `~/.config/kite-mcp-server/env` (only you can
   read it), never in this repo and never in a chat. Then restart the
   server: macOS `launchctl kickstart -k gui/$(id -u)/local.kite-mcp-server`,
   Linux `systemctl --user restart kite-mcp-server`.
2. **Log in.** Quit Claude completely and reopen it, `cd` into your folder,
   run `claude` and ask for your Kite profile. Claude gives you a login
   link, and you sign in on Zerodha's page yourself. Zerodha ends API logins
   every morning and each Claude session logs in on its own, so expect one
   link a day per session.
3. **Read-only first.** Ask Claude for your Kite profile, margins and
   positions. With `EXCLUDED_TOOLS` in the key file, Claude cannot place,
   change or cancel an order or a GTT.
4. **Live, when you decide.** Register your static IP in Kite (Profile, IP
   whitelist), set `"static_ip_registered": true` and `"paper": false` in
   `trader/config.json`, delete the `EXCLUDED_TOOLS` line from the key file,
   restart the server (step 1) and log in again. The order tools already ask
   before every call (the ask rules the script put in your folder's
   `.claude/settings.json`): never allow them always. Start with the
   smallest trade you can.

Why one shared server: the server's stdio mode cannot log in (it rejects
the fixed `stdio` session ID), and with one stdio copy per Claude session
they fight over port 8080, so a login can land in a copy that never asked
for it. The server logs to `~/Library/Logs/kite-mcp-server.log` on macOS and
to `journalctl --user -u kite-mcp-server` on Linux. Kite no longer shows in
Claude Desktop's chat: the skills run in Claude Code.

On Windows, or to do it by hand: build the server per its README, run it
with the settings in `bharat-broker/kite-mcp-server.env.example` plus
`APP_MODE=http`, `APP_HOST=127.0.0.1` and `APP_PORT=8080`, and connect Claude
Code to it with
`claude mcp add -s local --transport http kite http://127.0.0.1:8080/mcp`.
`/bharat-broker` walks through the details.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/bharat-scanner` | Chart Scanner | Scans NSE charts for setups: opening-range breakouts from 9:15, flags, oversold bounces; flags price bands and F&O bans. |
| `/bharat-news` | News Monitor | Reads each stock's NSE corporate announcements and Yahoo headlines, and flags results, board meetings, deals. |
| `/bharat-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in; warns near a price band. |
| `/bharat-exit` | Exit Planner | Sets your stop and target before you enter, on the Rs 0.05 tick, flat by 15:15. |
| `/bharat-timemachine` | Time Machine | Replays the trade 10,000 times plus every past time the setup fired. |
| `/bharat-budget` | Budget Manager | Sizes every position to your budget and max loss, Zerodha's charges included. |
| `/bharat-risk` | Risk Manager | Checks every trade against your limits, the costs, the entry window and the price band. |
| `/bharat-broker` | Order Executor | Paper ledger by default; Zerodha through a self-hosted Kite MCP server, never without your yes. |
| `/bharat-alerts` | Price Alerts | Rings on the stop, the target and 15:15; keeps the NSE clock and holidays. |
| `/bharat-analyst` | Results Analyst | Reads quarterly results from NSE's filings: revenue, EBITDA, net profit, EPS, QoQ and YoY. |
| `/bharat-insider` | Insider Tracker | SEBI insider disclosures, bulk and block deals, and FII/DII flows. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by 15:15                                           (while it is open)
news / analyst / insider                                                       (on a schedule)
```

An example, on real data: RELIANCE, Monday 19 October 2026 (2026-10-19).

1. **`/bharat-scanner`** at 10:15 AM IST: of the NIFTY 50, 8 had closed above
   their 15-minute opening range (9:15 to 9:30). RELIANCE is one: range
   1,207.30 to 1,223.40; the 09:45 bar closed at 1,227.80, 0.36 % over the
   top, at 9:50 AM.
2. **`/bharat-news`**: the weekend's NSE filing is Saturday's "Updates"
   (newspaper clippings for the Jio Platforms IPO). Yahoo has no headlines
   for RELIANCE. Nothing pointed to results inside the trade.
3. **`/bharat-entry`** watched RELIANCE bar by bar. At 9:47 it said waiting
   (the 9:45 bar was still forming). At 9:50 it said triggered: the first
   5-minute close above the range, 1,227.80. The bar traded 1.52 times the
   range's volume (Yahoo reports 0 for the 9:15 bar, so the average skips it).
4. **`/bharat-exit`** set the plan before the entry: stop at the range's
   midpoint, 1,215.35, so 12.45 a share at risk; target one times that risk,
   1,240.25; flat by 15:15 either way.
5. **`/bharat-timemachine`** replayed the rest of the day 10,000 times to
   15:15, each 5-minute candle drawn from RELIANCE's own candles at that time
   of day over its previous 48 usable sessions (seed 19): target first
   17.0 %, stop first 17.7 %, still open at 15:15 65.3 %. It also re-ran the
   same rule on those 48 real sessions: it fired 22 times, 10 hit the target,
   10 the stop, 2 neither. Its verdict: close to a coin flip, before costs.
6. **`/bharat-budget`** with a Rs 2,00,000 account and a Rs 1,000 max loss per
   trade: 73 shares. Their round-trip charges at the stop (brokerage Rs 40,
   STT Rs 22, NSE Rs 5.48, GST Rs 8.22, SEBI Rs 0.18, stamp Rs 3) are
   Rs 78.88, so the loss at the stop is 73 x 12.45 + 78.88 = Rs 987.73. Without
   costs the max loss would have allowed 80 shares. It writes one bracket
   order: buy 73 RELIANCE, NSE, MIS, stop-loss 1,215.35, take-profit 1,240.25.
7. **`/bharat-risk`** checks it against your limits: the max loss with costs,
   costs as a share of the reward (9 % here, the limit is 25 %), the daily
   loss, trades per day, reward to risk, the 9:30 to 14:45 entry window, the
   price band, position size and cash. PASS or BLOCK, with the reason.
8. **`/bharat-broker`** shows you the ticket in plain words. You type yes for
   that exact order. On paper it records the fill at the next bar's open in
   `trader/portfolio.json`, with Zerodha's charges. Live, Claude calls the
   Kite MCP server's `place_order` for the MIS entry and then one SL-M stop,
   and Claude's own "Allow once / Deny" card asks you each time.
9. **`/bharat-alerts`** rings on the stop, on the target (on Zerodha the
   target is an alert, not a broker order) and at 15:15 to flatten whatever
   is still open, before the broker's 15:20 square-off.

## Index futures

NIFTY and BANKNIFTY futures, intraday, in whole lots. Off until you set
`"allow_fno": true`. Yahoo has no free intraday futures bars, so the signal
is read on the index itself, and the futures price is the index times the
basis (futures close / index close) from NSE's F&O bhavcopy of the last
session, or the price you read off Kite.

An example, on real data: NIFTY, Wednesday 21 October 2026 (2026-10-21), the
session after the Dussehra holiday.

1. **`/bharat-entry`** `check NIFTY --instrument fut`: the index's opening
   range was 22,981.35 to 23,086.75; the 9:30 bar closed at 23,115.65, the
   trigger at 9:35 AM. The contract is NIFTY26OCTFUT (expires 2026-10-27,
   lot 65). The basis comes from the 2026-10-19 bhavcopy (20 October was a
   holiday): 22,895.20 / 22,888.95 = 1.000273, so the futures price is about
   23,121.95. The basis moves during the day (it was 0.997017 at the 21
   October close, 75 points lower), so this is an estimate: give the live
   price with `--fut-price`, or send a limit order at it.
2. **`/bharat-exit`** moves the index plan to the future: the index stop is
   the range midpoint, 23,034.05, 81.60 points under the entry. In futures
   points that is a stop at 23,040.30 and a 1R target at 23,203.60: 81.65 a
   unit, Rs 5,307.25 a lot. A bhavcopy older than the last session marks the
   price as estimated.
3. **`/bharat-timemachine`** replays the index's own bars; a constant basis
   moves every level the same way, so the odds are the index's, and it
   reports the prices at 15:15 in futures points.
4. **`/bharat-budget`** sizes in lots. With the Rs 2,00,000 account, a margin
   of Rs 1,75,953 a lot (`margin_per_lot_inr`, entered by hand from Zerodha's
   margin calculator on 4 November 2026; never guessed, and no figure means
   no order) and a Rs 7,000 max loss: 1 lot, 65 units. Futures charges at the
   stop are Rs 894.53 (STT 0.05 % on the sell, NSE 0.00173 %, stamp 0.002 %),
   so the loss at the stop is 65 x 81.65 + 894.53 = Rs 6,201.78. On this
   account that is 3 % in one trade: the default Rs 1,000 max loss refuses
   any NIFTY lot, and the daily loss limit must be raised too. The order
   (`size.py --limit`) is a limit buy of 65 NIFTY26OCTFUT at 23,121.95 on
   NFO, MIS, notional Rs 15.03 lakh.
5. **`/bharat-risk`** blocks F&O unless `allow_fno` is on, a qty that is not
   whole lots, a new entry in a contract that expires today (it names the
   next one), a market entry on the basis estimate (it can be 75 points off:
   use a limit order or give the live price), a margin above your cash after
   the buffer, and more than `max_lots` (default 1, lots held included).
6. **`/bharat-broker`** sends two Kite calls on NFO: the entry, then one SL-M
   stop (NSE dropped SL-M for options, not futures), each with
   `market_protection: -1`. Exchanges reject API market and SL-M orders
   without it since 1 April 2026; equity orders carry it too.
7. **`/bharat-alerts`** `add --from-plan` sets the stop and target alerts on
   the index levels (the quotes are the index) and an expiry alert at 9:15
   on the contract's expiry day and the trading day before. The expiry dates
   come from the bhavcopy, never from weekday rules: NIFTY26NOVFUT expires on
   Monday 23 November because 24 November is a holiday.

**F&O fine print.** Futures are leveraged: one NIFTY lot is about Rs 15 lakh
of exposure for under Rs 2 lakh of margin, and a gap or a fast market can
lose more than the margin, and more than the max loss. Intraday futures
margin is the full SPAN plus exposure (Zerodha gives no extra MIS leverage on
F&O). SEBI's study found that about 9 in 10 individual F&O traders lost
money. F&O profits are taxed as non-speculative business income. Not
financial or tax advice.

## Index options

NIFTY and BANKNIFTY options, intraday, buying only: this pack buys calls and
puts and never writes (sells to open) an option, whatever the config says.
Off until you set `"allow_fno": true`. There are no free intraday option
prices, so the signal is read on the index and the option is priced with a
model: Black-76 on the forward (the index times the put-call parity ratio
from the last F&O bhavcopy, same expiry), with an implied volatility backed
out of a premium (the one you read off Kite, or the last bhavcopy close) and
held constant.

The same session as the futures example: NIFTY, 2026-10-21.

1. **`/bharat-entry`** `check NIFTY --instrument opt --right CE`: the index
   triggers at 9:35 AM at 23,115.65. The contract is the ATM call
   NIFTY26OCT23100CE (strike 23100, expires 2026-10-27, lot 65; the 19
   October weekly expired on its day). Its 2026-10-19 bhavcopy close was
   87.25, open interest 49,34,930 units; that premium gives an implied
   volatility of 12.5 %, and at the trigger the model prices the call at
   about 162.95. That is an estimate: read the live premium and pass
   `--premium` (a put, `--right PE`, triggers on a close below the range
   low instead).
2. **`/bharat-exit`** turns the index plan into premiums: the index stop
   23,034.05 (the range midpoint) and the 1R index target 23,197.25 become a
   premium stop of 123.35 and a premium target of 209.55, at the entry time
   with the same implied volatility. Risk 39.60 a unit, Rs 2,574 a lot.
3. **`/bharat-timemachine`** replays the index 10,000 times and reprices the
   call at every bar with the time left to expiry. On the premium: target
   first 33.1 %, stop first 48.4 % (seed 19); the time decay tilts a bought
   option toward its stop, more than the index plan's 34.5 % vs 46.4 %. The
   premium at 15:15: median 149.99.
4. **`/bharat-budget`** sizes by premium, paid in full from the cash: 1 lot,
   65 units, Rs 10,591.75 of premium. Option charges at the stop are
   Rs 67.02 (brokerage Rs 40 flat, STT 0.15 % of the sell premium), so the
   loss at the stop is 65 x 39.60 + 67.02 = Rs 2,641.02. The order is always
   a limit buy at the premium on NFO, MIS.
5. **`/bharat-risk`** blocks any opening sell ("option selling is not part of
   this pack"), a market order (closing one too: an option is bought and
   sold at a limit), an order not on NFO, an estimated premium (options move
   too fast for an end-of-day price), open interest under `min_option_oi`, a
   new entry on expiry day after `expiry_day_cutoff` (13:00), an expiry that
   disagrees with the date in a weekly contract's name, and costs over
   `max_option_cost_share` (10 %) of the premium paid, whatever the target
   and with a stop or without: a Rs 3 option bought for 1 lot pays about
   Rs 47 on a Rs 195 position. An F&O order over NSE's freeze limit (NIFTY
   1,800 units, 27 lots; BANKNIFTY 900) blocks too: send it as several
   tickets. So does a contract name it cannot read (a bad month code, a
   weekly dated 31 Nov), a closing order included.
6. **`/bharat-broker`** sends a LIMIT buy, then an SL stop-limit (NSE takes no
   SL-M on options) 10 ticks under the trigger; a stop-limit may not fill in
   a fast move. Flattening an option is always a limit sell, rounded down
   to the 0.05 tick; a limit under 0.05 is refused.
7. **`/bharat-alerts`** watches the index levels the model ties to the
   premium stop and target, and says so in every message, and rings at
   15:00 on expiry day: an ITM option held to expiry pays
   STT on its intrinsic value, so close it.

**Options fine print.** A bought option can lose 100 % of its premium in
hours, and near expiry it can do that in minutes. The model is a model: one
implied volatility held constant for the whole day, no skew, no jumps, and
real premiums move with volatility as well as the index. SEBI's F&O studies
found that about 9 in 10 individual F&O traders lost money. Option profits
are taxed as non-speculative business income. Not financial or tax advice.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 bharat-scanner/scan.py universe                                # 2,392 EQ-series stocks, EQUITY_L.csv
python3 bharat-scanner/scan.py daily                                   # pre-market setups
python3 bharat-scanner/scan.py orb --at 10:15                          # opening-range breakouts so far
python3 bharat-news/news.py --tickers RELIANCE --hours 48              # NSE announcements, headlines, the move
python3 bharat-entry/entry.py check RELIANCE --setup orb               # waiting / triggered / expired
python3 bharat-entry/entry.py watch RELIANCE --every 30                # bar by bar until it triggers
python3 bharat-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 bharat-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 bharat-budget/size.py --write                                  # -> trader/orders.json
python3 bharat-risk/check.py                                           # PASS / BLOCK per order
python3 bharat-broker/ticket.py show                                   # the tickets, READY or not
python3 bharat-alerts/alerts.py add --from-plan                        # stop, target, flat by 15:15
python3 bharat-alerts/alerts.py check                                  # rings what hit, once
python3 bharat-entry/entry.py check NIFTY --instrument fut             # index signal, futures price from the bhavcopy
python3 bharat-entry/entry.py check NIFTY --instrument opt --right CE  # index signal, the ATM call and its premium
python3 bharat-analyst/results.py RELIANCE                             # quarters, margins, growth
python3 bharat-insider/insider.py new                                  # insider trades, deals, FII/DII
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real data fetched on 4 November
2026: the NIFTY 50's 5-minute bars for 19 October, RELIANCE's 60 days of
5-minute bars, NSE's equity list, price bands and ban list, NSE
announcements, insider disclosures, deals and FII/DII figures, RELIANCE's
quarterly XBRL filings, and three trades run through Zerodha's own brokerage
calculator. They reproduce every number in the example above.
`tools/probe.py` refetches the sources.

## Fine print

**Not financial advice**, and not from a SEBI-registered adviser. This is
software that reads public data and does arithmetic. It does not know your
situation and it cannot see the future. SEBI's own studies found that about
7 in 10 individual intraday traders in the cash market lost money (FY23),
and about 9 in 10 F&O traders lost money. You can lose money. Decide for
yourself, and talk to a SEBI-registered adviser if you need advice.

**No trade goes through until you say yes.** Only `/bharat-broker` can lead
to an order, and only after `/bharat-risk` passes that exact order and you
type yes for it in the chat. The setup script sets the Kite tools that
place, change or cancel orders to "Needs approval" (ask rules in your
folder's `.claude/settings.json`); never "Always allow" them, so Claude's own
"Allow once / Deny" card is a second check. No F&O, margin (MTF), short selling or MIS leverage unless you turn
them on.

**Paper first.** `"paper": true` is the default. India's brokers have no
paper accounts, so the paper ledger is `trader/portfolio.json`, with
Zerodha's charges taken out of every fill.

**Live orders need a static IP.** Under SEBI's retail algo rules (in force
since 1 April 2026), every order sent through a broker's API must come from
a static IP you have registered with the broker. Zerodha's hosted MCP server
has no order tools, so live trading needs your own copy of
zerodha/kite-mcp-server, run from that IP, with your own Kite Connect key in
`~/.config/kite-mcp-server/env`, never in the chat and never in the example
file in this repo (`bharat-broker/kite-mcp-server.env.example` is tracked by
git, so a key typed there would be pushed with your next commit). Zerodha has no bracket orders: the pack
sends the entry, then one SL-M stop, and keeps the target as an alert, so two
exit orders can never both fill.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those 60
days never did: results, a block deal, a price-band freeze, a headline. Its R
leaves out costs; the budget and risk skills put them back. Treat its odds as
a reality check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market, across a gap or when
a stock is frozen at its price band, a stop can fill well past its price, or
not at all. The max loss shown is the loss at the stop price, with costs.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API, its NSE data has gaps (no 15:20 bar, an empty latest daily
close for hours after the close, no volume on the 9:15 bar), and it can lag
or rate-limit you. NSE's website blocks some scripted requests; the tools say
so instead of guessing. Check anything that matters against your broker
before you act on it.

**Costs and taxes change.** The charges in `india.py` are Zerodha's published
rates on 4 November 2026 (including the STT rise from the 2026 Budget for
F&O). Check them against your contract note. Intraday equity profits are
taxed as speculative business income, F&O as non-speculative business
income. Nothing here is tax advice.

**Calendar.** The NSE holiday list is in `india.py` for 2026, with the
Muhurat session (Sunday 8 November 2026, 14:20 to 15:30). For other years
the tools say the holiday list is unknown and treat the day as closed until
it is added.

**Insider trades and deals are public.** SEBI's insider-trading disclosures,
bulk and block deals and FII/DII figures are published by NSE. The insider
skill reads them quickly; it has no inside information.
