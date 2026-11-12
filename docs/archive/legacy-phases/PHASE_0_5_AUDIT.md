# Phase 0–5 implementation audit

Audit basis: direct inspection of every product source file, migration, test, fixture, dependency file, Docker file and README. Status is implementation status before this verification task; “repair” records changes made during this task.

Evidence abbreviations use paths relative to `our-product`.

## Issues found and repaired

| ID | Severity | Finding | Repair | Regression evidence |
|---|---|---|---|---|
| A01 | HIGH | Import retries treated only standard timeout/connection exceptions as retryable and did not distinguish authentication, rate-limit and permanent input failures. | Added `RetryCategory` and fail-closed SDK-name/code classification; only transient/rate-limit failures retry. | `src/.../rate_limit.py`; `tests/test_importer.py::test_retry_classification_distinguishes_broker_failure_types` |
| A02 | HIGH | Dataset content checksum included random source import-job UUIDs, so identical candle inputs in independent imports could hash differently. | Removed job identifiers from checksum material while retaining them as revision lineage. | `src/.../datasets.py`; `tests/test_datasets.py::test_revision_content_checksum_does_not_depend_on_random_source_job_identity` |
| A03 | MEDIUM | Fixture revision/job/decision timestamps and IDs prevented byte-identical reports across fresh repositories. | Added injectable clocks/IDs, deterministic in-memory job IDs and fixed fixture identities/times. | `repository.py`, `datasets.py`, `cli.py`; `test_phase45_fixture_is_identical_across_fresh_repositories` |
| A04 | MEDIUM | Feature definition described groups but not each feature’s source columns, lookback, availability and null policy. | Added ordered `FEATURE_SCHEMA`/`FEATURE_NAMES` and full per-feature metadata. | `features.py`; `test_feature_definition_records_order_sources_lookbacks_and_null_policy` |
| A05 | MEDIUM | Time bucket was a display string, weakening numeric schema enforcement for ML. | Stored a deterministic numeric half-hour bucket while retaining its semantic definition. | `features.py`; Phase 6 schema tests |
| A06 | MEDIUM | Two-thread ML limit was documentation only. | Added typed `ML_THREADS <= 2` validation and BLAS/OpenMP limits in Docker/Compose. | `config.py`, Docker files, `.env.example`; `test_ml_thread_limit_fails_closed` |
| A07 | LOW | PostgreSQL/in-memory parity had no explicit interface conformance regression. | Added repository contract and migration coverage during Phase 6/7 storage work. | repository contract tests and migrations |
| A08 | HIGH | PostgreSQL Phase 4–5 methods had been placed in the in-memory class and shadowed by later methods, so the actual PostgreSQL class did not expose the full contract. | Defined the complete Phase 4–7 contract directly on `PostgresRepository` and added class-level conformance regression tests. | `repository.py`; `tests/test_repository_contract.py` |

## Phase 0 — foundation

| # | Requirement | Before | Evidence / issue / repair | Final |
|---:|---|---|---|---|
| 1 | Clean Python structure | PASS | `src/banknifty_foundation`, `tests`, `fixtures`, `migrations` | PASS |
| 2 | No generated environments/caches/build output | PASS | recursive generated-path audit; `.gitignore` | PASS |
| 3 | Ignore generated files | PASS | `.gitignore` | PASS |
| 4 | Typed validated configuration | PARTIAL | `Settings`; ML thread limit missing → A06 | PASS |
| 5 | Required configuration fails closed | PASS | `Settings.from_env`, credential guard; `test_config.py` | PASS |
| 6 | UTC stored timestamps | PASS | `calendar.to_utc`; all migration event columns `timestamptz` | PASS |
| 7 | Asia/Kolkata session logic | PASS | `calendar.IST`, explicit sessions | PASS |
| 8 | Naive datetimes rejected/controlled | PASS | `ensure_aware`, `parse_candle`; timezone tests | PASS |
| 9 | PostgreSQL/in-memory domain parity | PARTIAL | common `Repository` contract existed but no complete conformance test → A07 | PASS (static/contract) |
| 10 | Lightweight Docker | PASS | two services only; slim Python image | PASS |
| 11 | No broker order capability | PASS | `ReadOnlyZerodhaAdapter`; AST/capability tests | PASS |
| 12 | No hidden offline network calls | PASS | fixture clients and guarded real factory | PASS |
| 13 | No committed secrets | PASS | `.env.example` placeholders; static scan | PASS |
| 14 | Deterministic tests | PARTIAL | individual rerun deterministic, fresh fixture reports not identical → A03 | PASS |
| 15 | Ordered reproducible migrations | PASS | `001`–`005`, schema migration rows | PASS |
| 16 | README commands match CLI | PASS | parser/README inspection | PASS |
| 17 | i5/12 GB/no GPU practicality | PASS | no heavy resident service; sequential processing | PASS |
| 18 | Worker/thread counts limited | FAIL | no enforced ML/BLAS limit → A06 | PASS |
| 19 | Existing correct code reused | PASS | Phase 0–5 modules retained and strengthened | PASS |
| 20 | No duplicate/abandoned implementations | PASS | recursive source audit | PASS |

## Phase 1 — historical data

| # | Requirement | Before | Evidence / issue / repair | Final |
|---:|---|---|---|---|
| 1–4 | Strict read-only adapter and unreachable forbidden methods | PASS | `zerodha.py`; `test_zerodha_safety.py` | PASS |
| 5 | Restart/checkpoint/idempotency/rate/retry safety | PASS | `HistoricalImporter`, `RateLimiter`, repository jobs | PASS |
| 6 | Distinct transient/auth/rate/permanent retry handling | PARTIAL | only timeout/connection retry → A01 | PASS |
| 7 | Safe duplicate deduplication | PASS | `CandleValidator`, candle PK/upsert | PASS |
| 8 | Conflicting duplicate quarantine | PASS | `CONFLICTING_DUPLICATE_CANDLE` | PASS |
| 9 | Import error/status retention | PASS | `fail_job`, job error columns | PASS |
| 10 | Tokens are not permanent identity | PASS | canonical instrument identity + snapshot FK | PASS |
| 11 | Instrument snapshots retained | PASS | snapshot service/table | PASS |
| 12 | Missing-minute detection | PASS | explicit session grid validator | PASS |
| 13 | OHLC validation | PASS | validator + database checks | PASS |
| 14 | Negative volume/OI rejected | PASS | validator + database checks | PASS |
| 15 | Deterministic coverage reports | PASS | `CoverageReporter`; fixture test | PASS |
| 16 | Import rerun does not duplicate candles | PASS | job key, checkpoint, PK; importer test | PASS |
| 17–18 | Offline fixtures only; no real Kite call | PASS | fixture adapter and guarded composition root | PASS |

## Phase 2 — dataset revisions

| # | Requirement | Before | Evidence / issue / repair | Final |
|---:|---|---|---|---|
| 1–4 | Explicit OPEN/CLOSED/SPECIAL; unknown dates fail; no weekday assumption | PASS | `TradingCalendar`, dataset validator, calendar tests | PASS |
| 5 | Candidate/accepted/rejected states | PASS | model/migration checks | PASS |
| 6 | Candidate and accepted immutability | PASS | frozen dataclass, deep copies, database triggers | PASS |
| 7–8 | Rejected blocked; corrections create revisions | PASS | downstream status guards; decision transition | PASS |
| 9 | Complete revision lineage | PASS | `DatasetRevision`, revision tables | PASS |
| 10 | Identical accepted inputs have identical checksum | PARTIAL | job UUID polluted content checksum → A02 | PASS |
| 11 | Full quality validation set | PASS | `DatasetValidationService`, `CandleValidator` | PASS |
| 12 | No forward fill | PASS | exact sets/lookups; report flag | PASS |
| 13 | Explicit decision operation | PASS | `dataset-decide`, `decide` | PASS |
| 14 | Accepted rows cannot silently change | PASS | copied revision candles + immutability triggers | PASS |
| 15 | Exact accepted revision downstream lineage | PASS | labels/features/predictions FKs and guards | PASS |

## Phase 3 — labels

| # | Requirement | Before | Evidence / issue / repair | Final |
|---:|---|---|---|---|
| 1–2 | Accepted only; candidate/rejected blocked | PASS | `LabelGenerationService.generate`; tests | PASS |
| 3–6 | Immutable versioned horizon thresholds known at t | PASS | `threshold_policies`, `Settings`, no volatility lookup | PASS |
| 7 | Idempotent generation | PASS | target-label PK/upsert; test | PASS |
| 8 | No cross-close labels | PASS | `build_label_input`; test | PASS |
| 9–11 | Missing/invalid exact expiry → UNKNOWN; no nearest substitution | PASS | exact lookup; UNKNOWN test | PASS |
| 12 | Complete label lineage | PASS | `TargetLabel`, migration | PASS |
| 13 | Hand-calculated four-horizon tests | PASS | `test_hand_calculated_labels_cover_all_four_horizons` | PASS |
| 14 | Immutable label rows | PASS | frozen records + database trigger | PASS |
| 15 | UNKNOWN retained/reported | PASS | stored `UNKNOWN`, quality status | PASS |
| 16 | Overlap purge identifiable | PASS | explicit expiry timestamps and replay purge filter | PASS |

## Phase 4 — point-in-time features

| # | Requirement | Before | Evidence / issue / repair | Final |
|---:|---|---|---|---|
| 1–2 | Complete lineage and watermark ≤ as_of | PASS | `FeatureRow`, DB check, tests | PASS |
| 3–9 | Completed past-only candles; no centred/negative/fill/future lookup | PASS | exact non-positive offsets in `_feature_values`; injection tests | PASS |
| 10 | Missing lookback unavailable | PASS | per-feature nulls + `PARTIAL_LOOKBACK` | PASS |
| 11 | Immutable versioned definition | PASS | feature-set table/triggers | PASS |
| 12–14 | Deterministic order/idempotency/future injection | PASS | sorted generation, PK, tests | PASS |
| 15 | Per-feature sources/lookback/availability/null/version | PARTIAL | group metadata only → A04/A05 | PASS |
| 16 | Replay model has no unrestricted DB access | PASS | baselines receive `FeatureRow`; `AsOfDataView` boundary | PASS |
| 17 | Matrix reproducible from revision/version | PASS | exact immutable inputs/schema | PASS |

## Phase 5 — strict walk-forward replay

| # | Requirement | Before | Evidence / issue / repair | Final |
|---:|---|---|---|---|
| 1–4 | Chronological split, future-blocking view, test rows/labels excluded | PASS | `WalkForwardReplay`, `AsOfDataView`, replay tests | PASS |
| 5–7 | Immutable predictions, append-only outcomes, all decision states | PASS | records/repository/triggers | PASS |
| 8–10 | Versioned runs/folds, deterministic checksum and rerun | PARTIAL | same-repository deterministic; fresh fixture IDs varied → A03 | PASS |
| 11 | Minimum 20-minute purge | PASS | manifest validation + expiry cutoff | PASS |
| 12 | Four horizons | PASS | fixed horizon loop and report tests | PASS |
| 13 | Required reports | PASS | `evaluation.py`, report groupings/comparisons | PASS |
| 14–16 | 23→24 scenario, injection safety, pre-expiry outcome block | PASS | `test_replay.py` | PASS |
| 17 | Baselines receive bounded data | PASS | model `predict(FeatureRow)` only | PASS |
| 18–19 | Deterministic replay and hidden current outcomes | PASS | `AsOfDataView`; tests | PASS |
| 20 | Overlapping labels purged | PASS | `expiry_at <= test_open - purge` | PASS |

## Phase 0–5 completion gate

The gate requires, before Phase 6:

- all HIGH/MEDIUM audit findings repaired;
- regression tests added for A01–A07;
- syntax compilation;
- repository/migration contract validation;
- complete Phase 0–5 tests;
- Phase 0–5 fixture workflow executed twice with identical JSON;
- leakage, no-network and no-order tests passing.

Execution evidence and final counts are recorded in `PHASE_0_7_FINAL_REVIEW.md` after the final validation run.
