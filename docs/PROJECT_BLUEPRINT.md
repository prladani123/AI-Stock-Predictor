# AI BANKNIFTY Intelligence Workstation — Current Blueprint

## Product goal

From everything known now, estimate whether BANKNIFTY is likely to make a meaningful move over the next 10, 20, or 30 minutes, estimate direction and magnitude, and compare the current state with historically similar sessions.

## Current intelligence

For each 10/20/30-minute horizon the system reports meaningful-move probability, quiet probability, conditional direction and probability, expected move, market regime, and data quality. A current-session bias summarizes index, futures, and options evidence.

Big Move V2 is primary. Market Memory V2 supplies separate numerical and timestamped news/event evidence and never adjusts V2 probabilities. Kronos is experimental. The old 5/10/15/20 UP/DOWN/NO_TRADE models are retained only as `LEGACY_DIRECTION_BASELINE_V1`.

## Active architecture

React dashboard → thin Symfony API → Python intelligence service → PostgreSQL. Read-only Zerodha adapters and verified historical sources feed index, futures, options, quality, and memory records. No order execution exists.

Market Memory uses rich daily Markdown as inspectable source evidence and structured PostgreSQL day/article/event/reaction/episode records as the query layer. Numerical retrieval excludes unknown feature groups rather than encoding them as zero. Event retrieval uses point-in-time publication/first-seen semantics and only reaction-measurable predictive events. Historical outcomes remain separate from point-in-time state.

## Guardrails

- UTC storage; Asia/Kolkata exchange-session logic.
- Only information available at an episode's `as_of` enters its state vector.
- Future 10/20/30-minute values exist only in the outcome.
- No invented prices, OI, IV, volatility, news, or model probabilities.
- Analysis only: no broker orders or autonomous trading decisions.

## Current direction

Stages C–F foundations are implemented. Current work is G2B/G3 correctness and H/I/J research integration:
date-scoped V3 manifests, independent profile preparation, paired chronological experiments, prospective
Zerodha top-five microstructure, and append-only shadow evidence after committed predictions/journals (migration 021).
G2B implementation is complete; data activation may still be pending. G3 framework is complete; empirical
results require real experiments. H prospective microstructure, I shadow capture and J read-only integration/hardening
are implemented but require deployment/runtime validation. CODE IMPLEMENTED, DATA ACTIVATED and EMPIRICALLY VALIDATED
are separate states. PRICE_CORE_V2 production artifacts, model selection, thresholds and probabilities remain unchanged.
No broker execution, synthetic historical order books or claim of V3 predictive improvement is introduced.

Accumulate reliable live BANKNIFTY index/futures/options history, measure Big Move V2 and memory retrieval prospectively, then evaluate whether optional forecast or local reasoning layers add measurable value.
