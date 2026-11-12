---
name: bharat-broker
description: >-
  Order Executor for NSE intraday trades. Turns an order that bharat-risk
  passed into a plain ticket (shares, stop-loss, take-profit, Zerodha's
  charges, the max loss at the stop) and waits for the user's explicit yes
  for that one order. Paper by default: India's brokers have no paper
  accounts, so the paper ledger is trader/portfolio.json, filled at the next
  bar's open with Zerodha's charges taken out. Live, it sends the order to
  Zerodha through a self-hosted Kite MCP server from the user's registered
  static IP: the MIS entry first, then one SL-M stop once it fills (Zerodha
  has no bracket orders). Claude's own Allow once / Deny card is the second
  yes. It also builds the flatten ticket that closes an open position by
  15:15 IST, before the broker's 15:20 square-off. Use when the user says
  "place it", "send the order", "buy it", "execute", "go live", "set up
  Zerodha", "connect Kite", "close my position", "flatten", or after
  /bharat-risk passes an order.
argument-hint: "[show | approve | flatten | log]"
---

# bharat-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every order:

1. **bharat-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the order tool is set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order. On paper
there is no broker and no card: the yes is recorded and the fill goes into
the ledger.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --filled-qty 73 --filled-price 1228.10
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --qty 73 --price 1240.25
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE --qty 73
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`--now` and `--today` are for tests and paper only; with `"paper": false`
every tool refuses them and uses the real clock.

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, keeps the paper ledger, builds the flatten
ticket, and keeps the log in `trader/state/broker.json`. `--now "2026-10-19
09:51"` pins the clock (IST).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (bharat-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | bharat-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, failed and filled, with each entry's max loss |
| `trader/portfolio.json` | the paper ledger (or your record of live fills): cash, shares, lots and each day's charges |
| `trader/plan.json` | the flat-by time for `flatten` |

## Paper (the default)

`"paper": true` in `trader/config.json`. Nothing goes to a broker. After the
user's yes, Claude records the fill at the next bar's open (the open of the
bar after the trigger, from `/bharat-entry` or `alerts.py quote`; add a few
paise of slippage if you like): `ticket.py result --index 1 --status placed
--filled-qty 73 --filled-price P`. Every fill takes Zerodha's charges for
that order out of the cash, so the paper results include costs. When the
stop, the target or the flat-by would have filled, record that sell with
`ticket.py fill`. Run paper for a few weeks before thinking about live.

## Live: Zerodha (self-hosted Kite MCP server)

Going live is the user's own decision. Claude never sets `"paper": false`.

1. **Kite Connect.** The user creates a Kite Connect app at
   developers.kite.trade ("Personal" is free for orders; "Connect", Rs 500 a
   month, adds market data) and gets its API key and secret.
2. **Static IP.** SEBI's retail algo rules (in force since 1 Apr 2026): every
   order placed through the API must come from a static IP the user has
   registered in Kite (Profile, IP whitelist; Kite allows one change a week).
   Orders from any other IP are rejected. Reading data works from anywhere.
   Up to 10 orders a second needs no exchange registration; this pack sends
   one at a time. Then the user sets `"static_ip_registered": true` in the
   config. Until then, live tickets stay NOT READY.
3. **The server.** Zerodha's own server is github.com/zerodha/kite-mcp-server.
   Its hosted version (mcp.kite.trade) leaves out order tools on purpose, so
   live orders need the user's own copy running on their computer (at the
   registered IP). Run it as ONE shared copy in HTTP mode
   (`APP_MODE=http` on `127.0.0.1:8080`) that every Claude session connects
   to at `http://127.0.0.1:8080/mcp`. Never stdio: its stdio mode cannot log
   in (it rejects the fixed `stdio` session ID), and one stdio copy per
   session fight over port 8080, so the login lands in the wrong copy. On
   macOS or Linux, `tools/setup-trader.sh` in the repo does it in one go
   (`bash setup-trader.sh /Volumes/MySSD/trader`): the clone, the build, a
   background service started at login (launchd or systemd), Claude Code's
   connection, and placeholder keys with the order tools off
   (`EXCLUDED_TOOLS`) until the user removes them. The Kite Connect app's
   Redirect URL must be `http://127.0.0.1:8080/callback`. The keys live in
   `~/.config/kite-mcp-server/env`, outside any repo and readable only by
   the user, who types them there and restarts the server.
   `kite-mcp-server.env.example` here shows the shape. **Keys never go in
   the chat, and never in that example:** it is tracked by git, so a key
   typed there ships with the next commit. Never ask for them, never read
   that file back into the conversation, and if the user pastes a key
   anyway, tell them to delete the message and regenerate it. Logging in:
   the `login` tool gives a link and the user signs in on Zerodha's page
   themselves, once a day per Claude session.
4. **Tool permissions.** `place_order`, `modify_order`, `cancel_order` and
   the GTT tools are **Needs approval**: the setup script writes ask rules
   for them into the trading folder's `.claude/settings.json`, so Claude's
   card asks for every call. Never add an allow rule for them. Read-only
   tools (profile, margins, positions, orders, quotes) can be allowed.
5. Set `"paper": false` and `"broker": "zerodha"` in `trader/config.json`
   only when the user says so, in their own words.

**No bracket orders on Zerodha.** Zerodha discontinued bracket orders. Two
separate exit orders (a stop and a target) could both fill and leave the
user short, so the ticket sends:

1. now: the MIS entry, `place_order` with `exchange` NSE, `product` MIS,
   `order_type` MARKET (or LIMIT), `transaction_type` BUY;
2. after the entry fills: ONE SL-M stop, `order_type` SL-M with
   `trigger_price` at the stop. The same yes covers it: it only limits the
   loss. Claude's card asks for each call.

Every MARKET and SL-M call carries `market_protection: -1` (Kite's
automatic protection). Since 1 April 2026 the exchanges reject API market
and SL-M orders without it.

**Index futures (P2).** The same two calls with `exchange` NFO,
`tradingsymbol` the contract (`NIFTY26OCTFUT`) and `quantity` in units
(lots x lot). NSE stopped SL-M for options only, so the futures stop is
SL-M too. Paper fills charge Zerodha's futures rates and move only the
charges and the P&L through the cash (the margin is blocked, not spent). A
paper futures fill price is the index price x the plan's `basis_ratio`, or
the price on Kite, never the index price itself. `flatten --symbol
NIFTY26OCTFUT` builds an NFO sell, with the lot from the plan.

**Index options (P3, buying only).** The entry is a LIMIT buy at the
premium on NFO; the stop is SL (NSE takes no SL-M on options) with its
limit from the order (`stop_loss_limit_price`), and the ticket says a
stop-limit may not fill in a fast move. Paper fills pay the premium and
Zerodha's option charges. `flatten` an option needs `--limit PRICE`: never
a market sell. A limit off NSE's 0.05 tick is rounded down (150.03 goes
at 150.00), and the ticket says so. A limit under 0.05 is refused: the lowest limit is 0.05.

The target is an alert (`/bharat-alerts`). Taking it is a new sell ticket
(through bharat-risk and a new yes), after cancelling the open SL-M stop.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/bharat-risk` (or, live, to the static-IP
   step).
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all". An entry's PASS is good for 15
   minutes and only inside NSE's session and the entry window: `approve`
   refuses a stale PASS or a late entry, so run `/bharat-risk` again.
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "haan", "yes to all", a yes that names a
   different stock, side, order type or amount, or one with words it cannot
   check, like "yes go for it"). A yes may carry only plain words (place it,
   buy, shares, max loss, target), this order's own symbol and its own
   numbers; "one share" is the number 1, "this one" is not. Ask again in
   plain words. Never turn a reply into a yes yourself.
4. Paper: record the fill (above). Live: only after "Recorded your yes",
   call `place_order` with exactly the JSON the ticket printed; after it
   fills, the SL-M stop, with its quantity set to the FILLED quantity (a
   part-filled entry gets a smaller stop, or the stop would sell more than
   is held and leave the user short). If the stop is rejected or denied, the
   position has no stop: flatten it now and tell the user.
5. Record what happened: `ticket.py result --index N --status placed
   --broker-id <id> --filled-qty Q --filled-price P`, or `--status denied`,
   or `--status failed` with `--note`. A failed order is not retried without
   a new check and a new yes. The log keeps the entry's max loss for
   bharat-risk's daily limit.

## Steps: while it is open, and out by 15:15

- bharat-alerts watches the stop, the target and the flat-by time
  (`alerts.py add --from-plan`).
- When the stop or a target sell fills, record it:
  `ticket.py fill --symbol RELIANCE --side sell --qty 73 --price 1215.35`.
- By `flat_by` (15:15 IST), anything still open gets closed. Zerodha squares
  off open MIS positions from 15:20 itself, at its price and with a charge:
  1. Check how many shares are held (live: the broker's positions, read only).
  2. `ticket.py flatten --symbol RELIANCE --qty 73` writes a market MIS sell
     to `trader/orders.json`.
  3. `/bharat-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one order, as above.
  5. Live: cancel the open SL-M stop FIRST (`get_orders`, then `cancel_order`,
     Needs approval), or the stop and the sell could both fill. Then
     `place_order` with the ticket, then `result`.

## Example

```
ORDER 1 of 1, PAPER ledger (no broker)
  BUY 73 RELIANCE, market, good for today
  stop-loss 1215.35
  take-profit 1240.25
  max loss about Rs 987.73 with costs if the stop fills at 1215.35 (it can fill worse in a fast market or a band freeze)
  costs (Zerodha, round trip): Rs 78.88 at the stop, Rs 79.94 at the target
  READY: bharat-risk PASS, waiting for your yes
```

The user replies "yes". Claude records it and the paper fill at the 9:50
bar's open, 1228.10: 73 x 1228.10 = Rs 89,651.30 plus Rs 29.95 of charges
leaves Rs 1,10,318.75 of the Rs 2,00,000. Live, the same yes would be the
`place_order` call for 73 RELIANCE MIS at market, then the SL-M stop at
1215.35, each behind Claude's Allow once / Deny card.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys, access tokens, TOTPs or passwords.
- Never switches `"paper"` to live or sets `static_ip_registered`. The user does that themselves.
- Never sends the stop and the target as two live exit orders at once.
- Never promises a fill price: a market order and an SL-M stop fill at whatever the market gives.

## Safety

- Education and tooling, not financial advice, and not from a SEBI-registered adviser. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except bharat-broker, and only when all three hold: bharat-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (ask rules in the trading folder's .claude/settings.json). Never add an allow rule for an order tool.
- No F&O, margin (MTF), short selling or MIS leverage unless the config allows it and the user confirms.
- Broker keys live in the Kite server's key file (~/.config/kite-mcp-server/env), never in the chat.
