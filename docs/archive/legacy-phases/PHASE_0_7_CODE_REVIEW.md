# Phase 0–7 code review

Review basis: complete planning files, README, every Python source module, migration, fixture, CLI parser/handler, test, Docker file and dependency declaration. Existing reports and passing tests were treated only as inputs, never as proof.

## Findings and repairs

| ID | Severity | File / component | Evidence and blueprint violation | Repair | Regression evidence | Final |
|---|---|---|---|---|---|---|
| R01 | CRITICAL | `cli.py::run_phase07_fixture` | One fixed 15–24 July split was reused for the entire ML workflow. It was not daily walk-forward retraining. | Added `DailyWalkForwardConfig`, `build_daily_partitions` and `DailyMLWalkForward`; every test day receives a distinct pre-test context. | `test_each_test_day_has_its_own_pretest_context` | FIXED |
| R02 | CRITICAL | old Phase 6/7 fixture workflow | It created exactly one ML prediction per horizon, four total. The blueprint requires four predictions at every eligible minute. | Inference now iterates every eligible point-in-time feature row and all four horizons. | `test_full_phase07_fixture_is_daily_minute_level_four_horizon_and_deterministic`; report shows 328 predictions over two 41-minute test sessions. | FIXED |
| R03 | CRITICAL | `calibration.py::fit_calibrator` | Candidate calibrators were fitted and selected/evaluated on the same labels, producing biased selection. | Split calibration chronologically into fit and selection inputs; enforce `max(fit) < min(selection)` and select only on holdout scores. | `test_calibrator_fit_and_selection_are_strictly_separate_and_deterministic` | FIXED |
| R04 | HIGH | old Phase 6/7 workflow | Test labels were loaded into the training service and test evaluation happened before minute replay. This weakened the outer-test boundary even though model selection did not explicitly use them. | Training folds run with `evaluate_test=False` and `before_date=test_date`; inference performs no label lookup; outcomes use a separate post-expiry pass. | `test_outer_test_labels_cannot_change_models_calibrators_or_predictions` | FIXED |
| R05 | HIGH | `calibration.py::apply_abstention_policy` | Reliability gating accepted a caller-provided total calibration count instead of locating the prediction’s confidence bucket. A sparse bucket could pass incorrectly. | Added top-confidence reliability buckets and exact bucket lookup; gate on that bucket’s count and directional precision. | `test_policy_uses_exact_matching_bucket_not_total_calibration_count` | FIXED |
| R06 | HIGH | `cli.py` production command handlers | `train-models` and `run-model-walk-forward` silently invoked synthetic fixtures; `calibrate-models` only read a report. | Production names now require an accepted revision, stored features/labels and the PostgreSQL repository. Synthetic use is limited to explicitly named `fixture-*` commands. | `test_fixture_and_production_cli_paths_are_explicitly_separate`; `test_production_cli_runs_through_accepted_repository_abstraction` | FIXED |
| R07 | HIGH | `cli.py::command_audit_phase05` | Returned hard-coded `PASS` without inspecting the project. | Computes generated-file, adapter-capability, forbidden-call, migration, calendar and no-fill checks; returns nonzero on failure. | `test_audit_phase05_computes_real_checks` | FIXED |
| R08 | HIGH | migrations `006`/`007` | Model, training, bundle, calibrator, policy and ML prediction tables lacked update/delete immutability triggers. Calibration buckets were not persisted as first-class evidence. | Added repair migration `008_phase0_7_integrity_repair.sql`, immutable triggers and `calibration_reliability_buckets`. | `test_integrity_repair_migration_makes_every_phase67_record_immutable` | FIXED |
| R09 | HIGH | ML prediction record | Raw class, calibrated class, policy result and system abstention were not all represented separately. | `MLPrediction` now stores raw/calibrated probabilities, raw/calibrated top classes, final policy class, system-abstention flag/reasons and exact reliability bucket. Actual class remains outcome-only. | policy and immutable-prediction tests | FIXED |
| R10 | MEDIUM | `model_training.py::_partition` | Purge removed the last 20 intraday rows even when the next boundary was an overnight session, rather than comparing against the actual chronological boundary. | Boundary cutoff is now next partition date minus purge, preserving safe overnight history. | chronological partition and daily-fold tests | FIXED |
| R11 | HIGH | `ml_models.py::ModelArtifactStore` | Checksums were enforced on load, but `save` could overwrite an existing same-path artifact. Loader could not assert the caller’s expected horizon/algorithm. | Existing content is immutable; different content fails. Loader accepts and enforces expected horizon, algorithm and schema. | `test_artifact_load_enforces_expected_horizon_algorithm_and_immutability`; tamper tests | FIXED |
| R12 | MEDIUM | `ml_models.py::composite_score` | Selection emphasized F1/accuracy and did not directly include directional precision, directional coverage or probability quality. | Composite now includes macro/weighted F1, directional precision/coverage, accuracy and log loss; per-day stability is still mandatory for tree replacement. | model metric/grid/stability tests and manifests | FIXED |
| R13 | MEDIUM | Phase 7 metrics | Reliability records were class-wise only and did not contain top-class observed accuracy/directional precision used by the policy. | Added ten exhaustive top-confidence buckets with counts, expected confidence, observed accuracy and directional precision. | calibration bucket tests | FIXED |
| R14 | MEDIUM | repository behavior | In-memory ML uniqueness was ID-only while PostgreSQL also had a semantic unique key. | Added matching semantic identity enforcement in memory. | immutable prediction parity test | FIXED |
| R15 | LOW | package metadata / migration 005 | Package version still said Phase 5/0.2; baseline schema retained `LOGISTIC_PLACEHOLDER`. | Updated version to 0.4.0/Phase 7 and removed the obsolete baseline kind, including upgrade constraint repair. | dependency/migration tests | FIXED |
| R16 | MEDIUM | old CLI/repository code organization | Obsolete fixture-training handlers and a legacy fixed-split function remained after replacement; PostgreSQL-shaped methods had previously been shadowed inside `MemoryRepository`. | Removed obsolete CLI handlers/fixed-split function; actual `PostgresRepository` exposes the full Phase 0–7 contract. Repository behavior is covered by protocol tests. | `test_repository_contract.py`; production CLI abstraction test | FIXED |

## Phase 0–5 revalidation

The detailed requirement matrix remains in `PHASE_0_5_AUDIT.md`. This review rechecked implementation rather than relying on that document. Phase 0–5 remained materially sound after the repairs above:

- explicit stored calendar records remain mandatory for accepted datasets;
- read-only Kite capability remains limited to `instruments` and `historical_data`;
- imports remain checkpointed, rate-limited, retry-classified and idempotent;
- accepted revisions, labels and feature rows remain immutable;
- labels use exact expiry candles for all four horizons;
- feature watermarks remain at or before `as_of`;
- baseline replay remains minute-by-minute with an as-of view and delayed outcomes.

The final gate evidence is recorded in `PHASE_0_7_INTEGRITY_REPAIR.md`.
