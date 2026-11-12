---
name: bharat-news
description: >-
  News Monitor for NSE stocks. Tracks what every stock you watch has filed
  with NSE (corporate announcements: results, board meetings, dividends,
  splits, bonus, deals, credit ratings, resignations) plus Yahoo Finance
  headlines, and how far each stock is from yesterday's close. Shows only what
  is new since the last check, newest first, in IST, tags each item (results,
  board meeting, dividend/split, deal, block deal, rating, legal, regulatory,
  macro...) and never repeats one. Use when the user says "any news on
  RELIANCE", "why is this moving", "what's the news on my watchlist",
  "morning briefing", "pre-market news", "monitor the news", "did anything
  break", "check NSE announcements", "any corporate action", or after
  /bharat-scanner finds setups.
argument-hint: "[symbols]"
---

# bharat-news

A chart can look perfect and still be about to gap on a results date, a
board meeting or a block deal. Before a setup becomes a trade, this skill
reads what the company filed with NSE and the headlines, for every stock on
the watchlist, and tells you only what is new. It also prints how far each
stock has moved today, so an announcement and a move sit side by side.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                               # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE TCS --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-nse --json               # Yahoo headlines only
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (bharat-scanner) |
| `trader/plan.json` | the trade you are planning (bharat-exit) |
| `trader/portfolio.json` | what you hold (bharat-broker records fills there) |
| NSE corporate announcements | `https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=RELIANCE` |
| Yahoo Finance headline RSS | `https://feeds.finance.yahoo.com/rss/2.0/headline?s=RELIANCE.NS&region=US&lang=en-US` |
| Yahoo Finance chart endpoint | the last price against yesterday's close (unofficial, can lag) |
| `trader/state/news.json` | what has been shown (kept 30 days) |

## The rules

- **Announcements and headlines are leads, not facts.** Before telling the
  user something happened, open the filing (the PDF link) or the story and
  read it. Quote the source and link it.
- **Filings beat headlines.** If an NSE filing and a headline disagree, the
  filing is what the company told the exchange.
- **Say what it touches.** Every item lists the symbols it touches and its
  tags. Filings marked `!` are the categories that usually move a stock:
  financial results, outcome of a board meeting, acquisitions and mergers,
  resignations, credit ratings, buybacks, insolvency or default.
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no filing is worth saying out loud too.
- **Coverage gaps are said, not hidden.** Yahoo's headline feed is empty for
  many NSE stocks; the tool prints a note. NSE sometimes blocks scripted
  requests; then the tool notes it and shows the headlines only.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: every `!` filing, anything tagged results,
   board meeting, legal, regulatory or risk, and every MOVING symbol first.
3. Write a short brief, per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "results
   are due this week: an overnight gap can jump past any stop").
4. If a filing changes the picture, say so before `/bharat-entry` and
   `/bharat-exit`: results inside the trade, a record date, a block deal.
   Never act on a headline alone.

During the session: `/loop 15m /bharat-news`, or a scheduled task each
morning before 9:15.

## Example

Monday 19 Oct 2026, RELIANCE, checked at 9:30 AM IST: the weekend's NSE
filing is Saturday's "Updates" at 19:32 (newspaper clippings of the Jio
Platforms IPO advertisement). Yahoo has no headlines for RELIANCE. The
results board meeting (30 Oct) was only announced on 27 Oct, so that morning
nothing pointed to results inside the trade. When a symbol has no news, say
so rather than padding the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per feed per symbol, a pause between
  calls, and backoff when NSE says no.

## Safety

Education and tooling, not financial advice, and not from a SEBI-registered
adviser. This skill only reads and reports. Nothing places an order except
`/bharat-broker`, after `/bharat-risk` passes and the user types an explicit
yes for that order.
