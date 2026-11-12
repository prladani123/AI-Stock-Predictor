---
name: bharat-analyst
description: >-
  The Results Analyst for NSE stocks. Reads a company's quarterly results
  straight from what it filed with NSE (SEBI's integrated filing, XBRL) and
  sums up what changed: revenue, operating profit (EBITDA), profit before
  tax, net profit and EPS, the margins, quarter on quarter and year on year,
  in rupees crore. Consolidated by default, standalone on request. Reads a
  results press release or a concall transcript when you paste one. Use when
  the user says "how were Reliance's results", "analyse the quarter", "what
  changed this quarter", "is revenue growing", "compare margins", "sum up the
  concall", "when is the board meeting for results", or before trading a
  stock around its results.
argument-hint: "[symbols]"
---

# bharat-analyst

Results move stocks more than anything else on the calendar, and the press
release is written to put the best number first. This skill reads the numbers
the company actually filed with NSE, lines them up quarter by quarter, and
says plainly what changed. For an intraday trader it answers two questions
before a trade: what did the last results say, and is the next board meeting
for results inside this trade?

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/results.py" RELIANCE                  # last 5 quarters, consolidated
python3 "${CLAUDE_SKILL_DIR}/results.py" RELIANCE TCS --quarters 6
python3 "${CLAUDE_SKILL_DIR}/results.py" RELIANCE --standalone     # the parent company alone
python3 "${CLAUDE_SKILL_DIR}/results.py" RELIANCE --json
```

## What it reads and writes

| source | what |
| --- | --- |
| NSE integrated-filing results | `nseindia.com/api/integrated-filing-results?index=equities&symbol=RELIANCE`: the list of quarterly filings, each with its XBRL link. Since 2025 results come through SEBI's integrated filing; NSE's older results list stops at December 2024 |
| NSE XBRL files | `nsearchives.nseindia.com/corporate/xbrl/...xml`: one per quarter, Ind-AS tags (`in-capmkt:`) |

It writes nothing. `--json` gives every number for notes or a sheet.

## How a quarter is read

- Each XBRL file holds the quarter and the year to date. The tool takes the
  three-month period (80 to 100 days) that ends on the quarter's date and
  ignores the year to date, so a September file shows July to September, not
  April to September.
- **Revenue**: `RevenueFromOperations`.
- **EBITDA** (operating profit before depreciation): profit before
  exceptional items and tax + finance costs + depreciation - other income.
  The formula prints under the table.
- **Net profit**: the profit attributable to the owners of the parent (for
  consolidated results), else the profit for the period.
- **EPS**: basic EPS from continuing operations.
- Growth: QoQ against the quarter before, YoY against the same quarter a year
  earlier. YoY needs that older filing in NSE's integrated-filing list; before
  2025 there is none, so it shows n/a.
- A tag the company did not file shows as n/a, never as 0. Amounts print in
  rupees crore (1 crore = 1,00,00,000).

## The rules

- **Filed numbers only.** Every figure comes from the XBRL the company filed.
  Never fill a gap from memory or a news story.
- **Say the basis.** Consolidated and standalone numbers differ a lot for
  groups like Reliance; say which one.
- **Say what changed**, not what it means for the share price: revenue up or
  down QoQ and YoY, margins wider or narrower, EPS growth.
- **Results inside the trade.** Check `/bharat-news` for a "board meeting"
  announcement: results are usually declared after the board meets, often in
  the evening. An intraday trade is flat by 15:15, but the next morning can
  gap through any plan.
- **NSE blocking.** If NSE blocks the request, the tool says so for that
  symbol. Do not guess the numbers instead.

## Steps

1. Run `results.py SYMBOL`. Read the table.
2. Write three to five lines: the latest quarter's revenue, EBITDA margin, net
   profit and EPS, each with its QoQ and YoY change, and anything that stands
   out (a margin jump, a big other-income line, an exceptional item).
3. If the user pastes a press release or a concall transcript, read it and
   add what management said about the next quarter, quoted, not paraphrased
   into a forecast.
4. Check `/bharat-news` for the next results board meeting and say whether it
   falls inside the planned trade.

## Example

RELIANCE, consolidated, from its filings up to the September 2026 quarter:

```
quarter end       revenue      QoQ      YoY       EBITDA  margin   net profit  margin      EPS  EPS YoY
2026-09-30        332,861    +6.7%   +28.6%       56,616   17.0%       25,548    7.7%    18.88   +40.7%
2026-06-30        311,850    +4.4%   +25.4%       47,517   15.2%       20,946    6.7%    15.48   -22.4%
```

Revenue Rs 3,32,861 crore, up 6.7 % on the June quarter and 28.6 % on a year
earlier. EBITDA Rs 56,616 crore (38,613 + 8,481 + 15,315 - 5,793), a 17.0 %
margin, wider than June's 15.2 %. Net profit to shareholders Rs 25,548 crore,
EPS Rs 18.88, up 40.7 % on a year earlier. The board met on 30 Oct 2026 for
these results.

## What it never does

- Never says a stock is a buy or a sell, and never gives a target price.
- Never invents a number, a guidance figure or a quote.
- Never treats a press release's adjusted number as the filed one.

## Safety

Education and tooling, not financial advice, and not from a SEBI-registered
adviser. This skill only reads filings.
Nothing places an order except `/bharat-broker`, after `/bharat-risk` passes
and the user types an explicit yes for that order.
