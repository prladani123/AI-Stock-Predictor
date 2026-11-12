# Kronos and BANKNIFTY Time Machine Integration

Status: **CODE IMPLEMENTED - EXPERIMENTAL RESEARCH - NOT USED BY PRICE_CORE_V2**.
Data activation and empirical validation remain separate from implementation. No production
PRICE_CORE_V2 model, threshold, approval record or probability is changed by this work.

## Vendored upstream snapshots

| Project | Local path | Upstream | Exact fetched commit | Fetch date | Licence |
|---|---|---|---|---|---|
| Kronos | `third_party/Kronos/` | https://github.com/shiyu-coder/Kronos | `67b630e67f6a18c9e9be918d9b4337c960db1e9a` | 2026-11-12 | MIT |
| India Trader | `third_party/india-trader-skill/` | https://github.com/nawed1111/india-trader-skill | `cd9767f40241ac914cd13e2a06fd3a60b3af42d4` | 2026-11-12 | MIT |

`third_party/Kronos/UPSTREAM.md` and `third_party/india-trader-skill/UPSTREAM.md`
record provenance. Nested Git metadata and caches are excluded. Docker builds use these
committed local snapshots; Docker never clones either repository.

## Kronos model cache

Only `Kronos-mini` and `Kronos-Tokenizer-2k` are used. The Hugging Face model cards declare MIT.

| Model | Local file | Size | SHA-256 |
|---|---|---:|---|
| `NeoQuasar/Kronos-mini` | `third_party/Kronos/models/Kronos-mini/model.safetensors` | 16,440,776 | `A7D5F37E2E9FBD9891F7D7D4F72574512DD1F704FEE14223E0A8CD0FBF54197C` |
| `NeoQuasar/Kronos-Tokenizer-2k` | `third_party/Kronos/models/Kronos-Tokenizer-2k/model.safetensors` | 15,842,376 | `B97EC46B3B72160509E289183EAF7BDF5F0DAC5BB9B49522F6D46638A99A8717` |

`third_party/Kronos/models/README.md` documents the cache. The service uses the local
cache at runtime and never downloads weights. The `.gitignore` contains explicit exceptions
for these two weight files because the repository uses a broad `*.safetensors` ignore rule.

## Local Docker runtime

- Server: `third_party/Kronos/kronos_research_server.py`
- Image: `third_party/Kronos/Dockerfile.research`
- Compose service: `kronos-research` with `profiles: ["research"]`
- Internal URL: `http://kronos-research:8010`
- CPU only by default; limits 2 CPUs and 3 GB; model threads default 2
- Health: `GET /health`; prediction: `POST /predict`
- Prediction input is canonical BANKNIFTY 1-minute OHLC only. The API rejects future
  candles, invalid OHLC, duplicate reference ambiguity and non-30-minute requests. Volume
  is explicitly marked `INDEX_VOLUME_NOT_USED`.

Start the research stack explicitly:

```powershell
docker compose --profile research up -d
```

Normal Compose startup does not start Kronos. The Python application treats Kronos as optional;
an unavailable service returns `SERVICE_UNAVAILABLE` and never changes production intelligence.
The sidecar verifies both model SHA-256 values before loading, runs inference under `eval()`,
validates output OHLC ordering/completeness and rejects forecasts that cross the session close.

## Shared research contract

`src/banknifty_foundation/external_research.py` provides the 10/20/30-minute horizon contract:
meaningful-move probability, upward/downward conditional probabilities, expected return,
expected magnitude, median and p05/p25/p50/p75/p95, maximum up/down excursion, uncertainty,
path count, threshold policy and point-in-time fields. Big Move V2 thresholds are reused, not
redefined. Volume is never fabricated.

Persisted rows use `experimental_model_outputs` with migration
`022_experimental_research_identity.sql` applied. The migration makes `dataset_revision_id`
nullable for live research, replaces the old revision-scoped uniqueness with a partial identity
index, adds a dedicated accepted-revision trigger for replay-pool rows, and persists PIT,
lineage and immutable identity fields. Explicit model names remain:

- `KRONOS_MINI_ZERO_SHOT_V1`
- `BANKNIFTY_TIME_MACHINE_V1`

Live rows bind to the production `prediction_id`, `decisionAt`, exact reference minute,
instrument, source cutoff and input checksum; a V2 training revision is never stamped onto a
live research row. Time Machine replay uses `TIME_MACHINE_REPLAY_POOL_REVISION_ID`, an explicitly
selected accepted historical revision. The immutable identity covers upstream/model/tokenizer
revisions, reference minute, replay pool, input/configuration checksums, seed, path count,
sampling and threshold-policy version; `computedAt` is excluded. Query and API code separates
`kronosExperimental` from `timeMachineExperimental`; generic "latest experimental output"
selection is prohibited.

PIT classification is explicit: `PROSPECTIVE_DECISION_AVAILABLE` only when
`availableAt <= decisionAt`, otherwise `PROSPECTIVE_POST_DECISION`; backfill remains
`HISTORICAL_COUNTERFACTUAL_RESEARCH`. Post-decision evidence is never used for a decision-ready
claim.

Repeated deterministic runs retain the first persisted availability and PIT status;
runtime latency and rerun clocks do not create new identities. Different seeds/configurations
do. Conflicting deterministic payloads are rejected. Each channel has its own bounded worker,
success cache and retry lock. Repeated production polling retries failed channels against the
original committed journal decision, never the retry wall clock. Research query failures do
not hide V2 results. Revision-based CLI runs are always historical; `--prospective` is rejected
because a historical revision cannot prove committed live prediction identity.

## Evaluation

`src/banknifty_foundation/external_research_evaluation.py` evaluates
`PRICE_CORE_V2`, `KRONOS`, `TIME_MACHINE`, `V2_KRONOS`, `V2_TIME_MACHINE` and
`V2_KRONOS_TIME_MACHINE` on exact common reference-minute/horizon support. It uses a
chronological train/calibration/holdout split, training-only fitting, Stage A/B/joint/magnitude
metrics, Brier/log-loss, paired trading-day-block bootstrap versus V2 and an immutable report
checksum. It does not require historical rows in `big_move_predictions`; real empirical
validation still requires sufficient data. Absence of evidence is never reported as benefit.

V2 research scores are daily out-of-sample refits using the existing Stage D `_load` and
`_fold_fit` discipline (separate estimator, calibration-fit, calibration-selection and threshold
dates), not runtime-table lookups or promoted production models. All six comparisons require
the same three-channel cohort; missing channels are not replaced with zero predictions.
Multiple configurations/seeds for one reference must be selected before evaluation; ambiguous
mixed runs are rejected. Historical evaluation excludes all prospective evidence, including
post-decision captures. Combination baselines are equal-weight research averages; separately
reported meta-models fit only chronological training rows and choose thresholds on calibration
dates. Magnitude is maximum excursion on meaningful-move rows, in return fractions, matching
V2/G3; no signed-return estimate is fabricated for V2. Bootstrap requires five holdout days.

Focused validation (no providers, database or model inference required):

```powershell
python -m compileall -q src/banknifty_foundation third_party/Kronos/kronos_research_server.py tests/test_external_research.py tests/test_external_research_corrections.py
python -m pytest tests/test_external_research.py tests/test_external_research_corrections.py -o addopts= -q
npm.cmd --prefix frontend run typecheck
php backend/bin/console lint:container
docker compose --profile research config --quiet
```

The local predictor fixture uses pandas; SQL grammar tests use pglast (both can be installed
in an isolated test environment). SQL parsing does not replace applying migration 022 against
PostgreSQL and checking live FK/trigger, immutability and transaction behavior. The quarantined
external CSV and its provenance are documented in
`data/external/banknifty/sandeepkapri/SOURCE.md`; it has not been imported or evaluated.

## BANKNIFTY Time Machine

`src/banknifty_foundation/banknifty_time_machine.py` adapts the `bharat-timemachine`
time-of-day replay concept only. For reference date D it uses complete sessions strictly before D,
requires at least 20 prior sessions, samples bar-to-bar OHLC ratios from a default +/-2-minute
time-of-day pool, and generates 30 future one-minute bars using 10,000 deterministic NumPy paths
by default. It emits the same research contract and a sampling-pool checksum. Live capture
merges stored replay-pool candles with the current accepted session and never treats the replay
pool as the production training revision. Special, incomplete and non-375-candle sessions are
excluded. It does not use Yahoo, broker endpoints, orders, position sizing, stops, targets or
execution.

Market Memory remains separate. SimilarityEngine finds historically similar market states;
Time Machine estimates a time-of-day conditional future distribution. Neither replaces the other.
