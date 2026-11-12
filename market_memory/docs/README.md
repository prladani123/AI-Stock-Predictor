# BANKNIFTY 50-Day Market Memory v2

This dataset contains **50 completed BANKNIFTY trading sessions from 10 Jul 2026 through 18 Sep 2026**. September 14 was a trading holiday and is intentionally absent. August 26 was a **settlement holiday but trading remained open**, so it is included.

## What changed from v1

- verified daily BANKNIFTY OHLC and computed gap/range/change;
- a researched causal narrative for every session;
- explicit **causal-evidence grades** instead of pretending every price move has a news cause;
- event/reaction timing when a source actually supports timing;
- special handling for Closing Auction Session (CAS) distortions;
- extra source trails for major event days;
- strict leakage note separating pre/intraday information from post-close explanations;
- no invented option-chain/OI/IV values.

## Evidence grades

- **A+**: source explicitly connects event and move and provides a useful timestamp/reaction window.
- **A/A-**: strong contemporaneous causal attribution, but timing is broader.
- **B/B-**: credible context/sector attribution without an isolated trigger.
- **C/C+**: no single verified catalyst; flow/regime/positioning is the safer explanation.

The goal is not to teach the model that every headline predicts price. The goal is to let it learn which event categories, time windows and market confirmations have historically coincided with meaningful BANKNIFTY responses.
