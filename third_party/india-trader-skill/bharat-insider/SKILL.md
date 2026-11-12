---
name: bharat-insider
description: >-
  Insider and Big-Money Tracker for NSE stocks. Reads three public NSE feeds:
  SEBI insider-trading (PIT) disclosures (promoters, directors, employees and
  their relatives buying, selling, pledging or gifting their own company's
  shares), the day's bulk and block deals (who traded 0.5 % or more of a
  company, and the large block-window trades), and the day's FII/FPI and DII
  buying and selling in rupees crore. Shows only what is new since the last
  run for the stocks you watch, flags promoter-group market buys and sales,
  and never calls any of it inside information. Use when the user says "are
  insiders buying", "promoter selling", "any pledges", "bulk deals today",
  "block deals", "who bought RELIANCE", "FII DII data", "are FIIs buying",
  "smart money", or before taking a trade in a stock.
argument-hint: "[insider SYMBOL | deals | flows | new]"
---

# bharat-insider

Promoters and directors must report their own trades to the exchange (SEBI's
Prohibition of Insider Trading Regulations), bulk and block deals are
published every trading day, and NSE posts how much foreign (FII/FPI) and
domestic (DII) institutions bought and sold in the cash market. This skill
reads all three, for the stocks on your watchlist, and tells you only what is
new. They are public filings, not inside information, and not a reason to
trade on their own.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/insider.py" insider --symbol RELIANCE         # its recent disclosures
python3 "${CLAUDE_SKILL_DIR}/insider.py" deals                             # today's bulk and block deals
python3 "${CLAUDE_SKILL_DIR}/insider.py" deals --symbol RELIANCE --min-crore 10
python3 "${CLAUDE_SKILL_DIR}/insider.py" flows                             # FII/DII, latest day
python3 "${CLAUDE_SKILL_DIR}/insider.py" new                               # all three, new since the last run
```

## What it reads and writes

| feed / file | what |
| --- | --- |
| NSE `corporates-pit` | SEBI PIT disclosures for a symbol: person, category (Promoter Group, Immediate relative, Employees...), Buy / Sell / Pledge, mode (Market Purchase, Market Sale, Off Market, Gift, Pledge...), shares, value, dates |
| NSE `snapshot-capital-market-largedeal` | the latest day's bulk and block deals: client, side, shares, weighted average price |
| NSE `fiidiiTradeReact` | the latest day's FII/FPI and DII buy, sell and net, Rs crore |
| `--symbols`, else `trader/watchlist.json`, `trader/plan.json`, `trader/portfolio.json` | which stocks `new` watches |
| `trader/state/insider.json` | what has been shown (90 days), so each row shows once |

The deal and FII/DII feeds give one day only. Run `new` every day after the
close (a scheduled task) and the state file builds the history.

## How to read it

- **Promoter-group market buys and sales** are marked `!`. They are the
  filings most worth reading. A gift, an off-market transfer between family
  members, or a pledge is not the same as a sale on the market: say which.
- **Pledges.** A promoter pledging shares (borrowing against them) or the
  lender invoking a pledge (selling them) can matter more than a small sale.
- **Bulk and block deals** often come in pairs: the same client buying and
  selling the same stock the same day is usually a trader, not an investor.
  A block deal between two named parties is a negotiated transfer.
- **FII/DII** is the whole market, not one stock. It says which side the big
  money leaned on yesterday, nothing about today.
- **NSE blocking.** NSE refuses some scripted requests. A feed that fails is
  noted and the other feeds still print. Never fill a gap from memory.

## Steps

1. For one stock before a trade: `insider.py insider --symbol SYMBOL` and
   `insider.py deals --symbol SYMBOL`. Say what the latest filings show, with
   the person, their category, buy or sell, the mode, the shares and the
   value, and when they traded (not only when they filed).
2. For the watchlist: `insider.py new` after the close. Brief only the new
   rows, promoter-group market trades and pledges first.
3. For the market: `insider.py flows`. One line: FII/FPI net and DII net, in
   rupees crore, for that date.

## Example

RELIANCE (NSE, as of 4 Nov 2026): the newest disclosure is from 18 Feb 2026,
an off-market sale of 2,320 shares (Rs 32,94,168) by a person in the "Other"
category, traded 13 Feb. The largest recent market sale in the list is an
immediate relative selling 1,00,000 shares for Rs 13,85,00,000 on 11 Sep
2025. RELIANCE had no bulk or block deal on 3 Nov. That day FIIs/FPIs were
net buyers of Rs 6,836.81 crore in the cash market and DIIs net sellers of
Rs 3,678.85 crore.

## Rules

- These are public filings, not inside information. Say so once.
- Quote the person, the category, the mode and the dates as filed. Never
  guess why someone traded.
- A filing is old news by the time it is filed (PIT disclosures can come
  days after the trade). Say the trade date.

## What it never does

It never says a stock is a buy because an insider bought it, never treats
FII/DII flows as a signal for one stock, and never invents a filing.

## Safety

- Education and tooling, not financial advice, and not from a SEBI-registered
  adviser. Nothing in this pack is a recommendation to buy or sell anything.
- Paper trading by default (`"paper": true` in `trader/config.json`).
- Nothing places an order except `/bharat-broker`, and only after
  `/bharat-risk` passes AND the user types an explicit yes for that order in
  the chat, AND the brokerage tool is set to "Needs approval" (ask rules in
  the trading folder's .claude/settings.json) so Claude's own "Allow once /
  Deny" card appears too. Never add an allow rule for an order tool.
- No F&O, margin (MTF), shorting or MIS leverage unless the config allows it
  and the user confirms.
- Insider disclosures, bulk and block deals and FII/DII figures are PUBLIC
  data that NSE publishes. This skill reads them fast; it has no inside
  information, and it says so.
