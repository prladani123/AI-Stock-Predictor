# Market Memory V2

Market Memory has two independent supporting-evidence channels. Neither channel adjusts Big Move V2 probabilities.

## Canonical evidence

- `market_memory/daily`: 50 immutable rich daily Markdown records, 2026-07-10 to 2026-09-18.
- `market_memory/manifest.json`: dataset identity, original daily checksums, and derived artifact checksums.
- `market_memory/raw/index_history`: preserved public minute-response JSON.
- `market_memory/derived/episodes.json`: 396 point-in-time numerical episodes.
- `market_memory/derived/structured_events.json`: 52 historical events; four currently have measurable minute reactions.
- `market_memory/derived/live_news.json` and `live_events.json`: deduplicated real-source ingestion output.
- `artifacts/archive/legacy-market-memory-v1`: preserved superseded V1 evidence.

Each V2 day contains verified OHLC, movement summary, evidence-graded causal discussion, event timeline, pattern lesson, derivatives limitations, machine-readable fields, and source metadata. `market_memory.py` validates front matter, all eight ordered sections, machine JSON, and cross-field consistency before indexing.

## Numerical memory

Episode state contains only fields observable at `as_of`. Forward outcomes are stored separately. The 10/20/30-minute outcomes use `big-move-threshold-v1`, future high/low paths, volatility-adjusted thresholds, first-barrier ordering, and the same `BIG_UP`, `BIG_DOWN`, `NO_BIG_MOVE`, `AMBIGUOUS`, and `UNKNOWN` semantics as Big Move V2. Missing feature groups are excluded from distance rather than encoded as zero.

## News/event memory

Articles retain source URL, publication time when supplied, immutable first-seen time, ingestion time, content hash, symbols, category, tags, and relevance explanation. Events distinguish `published_at`, `first_seen_at`, `known_at`, knowledge phase, timing precision, predictive eligibility, and context-only structural events.

Reaction measurement uses the completed minute at or before the event reference, +5/+10/+20/+30 returns, maximum excursions, volatility expansion, and Big Move V2 semantics at 10/20/30 minutes. Events lacking trustworthy point-in-time timing or candles remain context-only or `REACTION_NOT_MEASURABLE`; values are never invented.

Event similarity is interpretable: category, overlapping tags, knowledge phase, scheduled status, and directional context. Only predictive-eligible historical events with available reactions contribute to statistics. Market confirmation is separately reported as `CONFIRMED`, `PARTIALLY_CONFIRMED`, `CONTRADICTED`, or `INSUFFICIENT_DATA` from available price/futures/options evidence.

## Leakage and causality rules

- A publication discovered after the session does not become pre-event knowledge.
- Closing-auction/CAS effects are structural context, not ordinary intraday prediction evidence.
- Post-event candles never enter event input state.
- Daily narrative association is not proof of causality; evidence grades and timing precision stay visible.
- Missing news, derivatives, candles, or reactions remain missing.
- Generated rollups refuse to overwrite curated days unless `--force` is explicit.
