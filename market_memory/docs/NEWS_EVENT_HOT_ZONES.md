# News / Event Timing Hot Zones — 50-Day Research Index

These are **research buckets**, not proven predictive probabilities. The 50-day set is too small to claim a stable edge. They are intended as features/alert windows for the app.

## 1. Pre-open / overnight (07:00–09:15 IST)

Repeated high-impact inputs: global risk tone, crude oil gaps, US Fed decisions, bond-yield shocks, India inflation released on a market holiday, geopolitical escalation.

Examples: Jul 23-24, Aug 18, Sep 7-9, Sep 15, Sep 17-18.

**Use:** increase attention before the open when crude, yields or a scheduled central-bank decision changed materially overnight. Do not assume direction until BANKNIFTY price/futures/options confirm.

## 2. Opening hour (09:15–10:30 IST)

This is where overnight information is first repriced. Several large down sessions began as gap-downs and either persisted or reversed.

**Critical distinction:** a gap is not itself a signal. Jul 24 and Sep 11 show that the original negative catalyst (crude) can reverse, producing a large BANKNIFTY recovery.

## 3. Scheduled policy/data window (roughly 10:00–12:00 IST when applicable)

Example: RBI policy on Aug 5 was published at **10:50 IST**. It was broadly expected, so the later geopolitical headline mattered more than the policy hold itself.

**Use:** store calendar event time and *surprise vs expectation*, not just event text.

## 4. Midday / early afternoon (11:30–14:30 IST)

This was a frequent reversal window in sourced examples:
- Aug 17: 14:33 article documented value-buying/easing-VIX recovery.
- Sep 11: around 14:00 Bank Nifty had recovered roughly 770 points as crude retreated.
- Jul 24: 13:47 report linked sharp recovery from lows to falling crude.

**Use:** if a live macro variable driving the morning move reverses (especially crude), re-evaluate rather than treating the morning regime as fixed.

## 5. Final hour / close (14:30–15:15 IST)

Profit booking and expiry positioning repeatedly changed the closing shape. Examples include Jul 31 and Aug 25.

## 6. Closing Auction Session (CAS, post-15:15) — STRUCTURAL, NOT NEWS

From Aug 3 onward, official closes can contain auction effects. **Aug 31 is the strongest example:** Bank Nifty was around 57,428 at 15:14 and finished 58,024.95 after CAS; most of the apparent +0.92% daily gain occurred in the auction amid MSCI rebalancing. Sep 1 then reversed that distortion.

**Model rule:** keep continuous-session close and official CAS close as separate fields. Never let CAS-only jumps become a fake news pattern.

## Recommended live-news logic

1. Timestamp the first reliable publication/announcement.
2. Classify event: OIL/GEOPOLITICS, CENTRAL_BANK, INFLATION, BANK_EARNINGS, BANK_POLICY, FX/RATES, INDEX_REBALANCE/CAS, OTHER.
3. Record surprise magnitude when measurable.
4. Compare BANKNIFTY return and range in +5/+10/+20/+30 min **using actual minute data**.
5. Confirm with futures/OI/options before adjusting model confidence.
6. Keep post-close explanations as labels only; never feed them backwards into live prediction.
