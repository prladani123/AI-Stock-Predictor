# Phase 8 live market-data code review

Review date: 2026-08-27. Scope stops after Phase 8; no prediction, UI, paper, or
execution capability was added.

## Architecture

Phase 8 remains inside the existing Python service:

```text
KiteTickerLiveAdapter | SimulatedLiveAdapter
                 ↓ same callback contract
LiveCollector → TickNormalizer → bounded minute buckets
       ↓              ↓                 ↓
connection/gaps   raw immutable ticks   first-finalized minutes/options
       ↓              ↓                 ↓
       LiveRepository (memory or PostgreSQL)
```

`live_adapters.py` is Zerodha-only. The production wrapper exposes `connect`,
`subscribe`, `unsubscribe`, `set_mode`, and `close`; it delegates parsing and
reconnect transport to official `KiteTicker`. Application code exposes no
broker account, portfolio, GTT, or write capability.

## Domain and schema decisions

- `LiveTick` separately preserves exchange event time and local receive time,
  canonical instrument/token identity, OHLC, cumulative volume, OI fields,
  totals, five bids, five asks, connection, subscription generation, and flags.
- Depth is stored compactly in the JSON payload rather than ten child rows per
  tick. Generated PostgreSQL columns retain efficient instrument/time indexes.
- Deterministic raw-tick identity is connection + token + event timestamp +
  price + cumulative volume + OI. Exact repeats are idempotent; different late
  information remains separately observable.
- First-finalized minutes have semantic uniqueness `(instrument, minute,
  revision)`. Late ticks create immutable `live_minute_corrections` and quality
  evidence; they never rewrite the first version or prediction lineage.
- Option snapshot identity is instrument + minute + universe version.
- Connection events, subscription generations, raw ticks, universes, futures
  selections, minutes, option snapshots, corrections, and issues are append-only.
  Gaps permit exactly one transition from open to resolved.

Migration `009_phase8_live_market_data.sql` adds only Phase 8 concepts. Existing
canonical instrument snapshots, sessions, and prediction tables remain intact.

## Connection and subscription lifecycle

States are `DISCONNECTED`, `CONNECTING`, `CONNECTED`, `DEGRADED`,
`RECONNECTING`, `AUTH_EXPIRED`, `AUTH_NOT_CONFIGURED`, and `STOPPED`.
Disconnect/degradation opens a durable gap. Reconnect restores the entire
desired set in full mode. A gap closes only after fresh ticks for the desired
tokens arrive.

`SubscriptionRegistry` stores desired and active sets independently. Replace is
idempotent. ATM rotation subscribes new tokens and sets full mode first, waits
for their fresh ticks, then retires old tokens. Reconnect clears assumed active
state and reapplies the desired set without accumulating duplicates.

The configured queue/batch limit defaults to 2,048. Oversized batches fail with
an explicit backpressure issue instead of silently losing required ticks.

## Contract selection

All selection uses `LiveInstrument` rows from one explicit snapshot version:

- Future: earliest unexpired persisted BANKNIFTY future; optional next contract
  is available for rollover validation. Raw contracts are never combined.
- Expiry: earliest eligible expiry on/after the explicit session date.
- ATM: nearest actual strike to the underlying value available at selection
  time, with deterministic lower-strike tie breaking.
- Options: up to five actual strikes below and above ATM, CE/PE pairs only.
  Missing strikes/pairs are reported and no symbol/token is fabricated.
- Every selection persists time, snapshot, underlying, eligible expiries, ATM,
  selected IDs/tokens, missing pairs, rule version, and reason.

Historical ATM selections are immutable; later underlying moves create a new
universe rather than rewriting prior choices.

## Minute finalization and raw derived values

Minutes use exchange-time buckets and finalize after the boundary plus the
configured three-second allowance. OHLC uses ordered event-time ticks while
receive timestamps remain preserved for latency and late-arrival analysis.

Definitions:

```text
minute_volume = current cumulative volume - previous quality-passing minute cumulative volume
oi_change     = current OI close - previous quality-passing minute OI close
best_spread   = best ask price - best bid price
spread_pct    = best_spread / option LTP
```

First minute, missing prior values, reconnect gaps, or negative cumulative
deltas yield unavailable values and flags—never invented zero. Stored future
minute close/volume/OI plus exact future selection and same-minute index candle
are sufficient to derive premium, premium change, time to expiry, and rollover
later. These are not connected to the Phase 4 model feature schema yet.

## Quality and gaps

Implemented evidence covers unknown token, symbol mismatch, stale/out-of-order/
duplicate/late ticks, impossible price, negative volume/OI, broken/crossed
depth, outside-session timestamps, clock skew, expired/unexpected contracts,
reconnect gaps, no fresh tick after rotation, incomplete universes, missing
pairs, and invalid volume deltas. Missing values remain `None`.

The gap journal stores source, connection, start/detection/resolution, cause,
affected instruments/minutes, attempts, and state. Minutes touched while a gap
is open carry `RECONNECT_GAP` and are degraded.

## Retention

`raw-tick-retention` defaults to dry-run. Execution refuses any candidate tick
not covered by a verified aggregate/checksum range. PostgreSQL permits raw-tick
deletion only through such ranges; aggregates, snapshots, gaps, and corrections
remain immutable. Default retention configuration is 14 days, within the
recommended 7–30 day range.

## Reference decisions

OpenAlgo concepts influenced lifecycle, desired/active subscriptions, contract
selection, quote/depth validation, and OI grouping. Its AGPL code, schemas,
custom parser, Flask/multi-broker design, ZeroMQ, and globals were rejected.
Official PyKiteConnect callback/mode/tick contracts are used through the MIT
dependency. Exact evidence is in `REFERENCE_USAGE_LOG.md`.

## Offline validation

The deterministic scenario includes normal/stale/duplicate/out-of-order ticks,
ATM rotation, overlap, disconnect, reconnect, full resubscription, fresh-tick
gap closure, OI/volume changes, five-level depth, and session close. It creates
no prediction and makes no network call.

Current evidence: 106 tests pass (82 retained Phase 0–7 tests and 24 focused
Phase 8 tests). The Phase 8 report is
`artifacts/phase-8-live-data-report.json`.

Two complete simulated runs were byte-identical. Report file SHA-256:
`F15DFC1E1AB5BC5AA739D6A1D6C655615D2068FB359330617515339F165A7D8F`;
logical deterministic checksum:
`b2a059c347e95e0b31f01a7aec0b11da03531c7a3e765278fc94194c8251590d`.
The final scenario stored 76 unique ticks, rejected one exact duplicate,
identified one out-of-order arrival, two stale arrivals (including the late
tick), one immutable late correction, 72 finalized instrument-minutes, three
index candles, three future minutes, and 66 option snapshots. Its single gap
was resolved only after all 24 desired tokens produced fresh post-reconnect
ticks.

## Limitations requiring live Zerodha

- Actual exchange timestamp behavior, packet cadence, reconnect timing, token
  expiry callbacks, and partial full-mode fields must be observed in a real
  read-only session.
- PostgreSQL migration execution remains environment-dependent when Docker or
  `psql` is unavailable; static migration and memory/PostgreSQL interface parity
  tests pass.
- Snapshot acquisition/session-token orchestration will later belong behind the
  Symfony operator flow. Current `collect-live` requires explicit files,
  environment credentials, and `--confirm-real`.
- No Greeks, full option chain, model-feature integration, prediction, or
  automatic market-hours training exists.
