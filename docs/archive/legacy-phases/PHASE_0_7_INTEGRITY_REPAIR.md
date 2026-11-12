# Phase 0–7 integrity repair

Verification date: 2026-07-26. Scope ends at Phase 7.

## Outcome

The code review confirmed sixteen defects, including critical/high
walk-forward, leakage, calibration, CLI, and immutability failures. Every
confirmed critical and high issue was repaired. The detailed evidence-to-repair
mapping is in `PHASE_0_7_CODE_REVIEW.md`.

The final fixture is verification-only, not production performance evidence.
Production ML commands require PostgreSQL and an accepted stored revision.

## Repairs made

- Replaced the fixed ML split with daily expanding walk-forward folds.
- Replaced one prediction per horizon with four horizons at every eligible test
  minute.
- Hid outer-test labels during training, calibration, and inference; attached
  actual outcomes only in a separate post-expiry pass.
- Split calibration into chronological fit and selection intervals.
- Added deterministic sigmoid/isotonic selection with explicit unavailable
  results when evidence is insufficient or holdout metrics worsen.
- Added ten exact top-confidence reliability buckets with per-bucket counts,
  accuracy, directional counts, and directional precision.
- Stored raw class, calibrated class, final policy class, system abstention,
  reasons, and the matching reliability bucket separately.
- Added all required fail-closed abstention reasons.
- Expanded model comparison to directional precision/coverage, macro/weighted
  F1, probability loss, and per-day stability. A tree cannot replace Logistic
  Regression automatically or on accuracy alone.
- Enforced immutable artifact saves and expected horizon/algorithm/schema on
  load, as well as trusted-root, format, manifest, and file checksums.
- Separated production accepted-data CLI commands from explicitly named fixture
  commands and replaced the hard-coded audit result with computed checks.
- Added PostgreSQL calibration-bucket storage and mutation-rejection triggers
  for Phase 6/7 lineage, policy, prediction, and outcome tables.
- Aligned in-memory prediction uniqueness with PostgreSQL, persisted model
  comparisons, and removed the shadowed duplicate repository implementation.

## Files changed

Implementation and data:

- `src/banknifty_foundation/__init__.py`
- `src/banknifty_foundation/calibration.py`
- `src/banknifty_foundation/cli.py`
- `src/banknifty_foundation/ml_models.py`
- `src/banknifty_foundation/ml_records.py`
- `src/banknifty_foundation/ml_walk_forward.py` (new)
- `src/banknifty_foundation/model_training.py`
- `src/banknifty_foundation/repository.py`
- `fixtures/phase45_market.json`
- `migrations/005_walk_forward_replay.sql`
- `migrations/008_phase0_7_integrity_repair.sql` (new)
- `pyproject.toml`

Tests and documentation:

- `tests/test_calibration_and_policy.py`
- `tests/test_coverage_and_cli.py`
- `tests/test_migrations.py`
- `tests/test_ml_models.py`
- `tests/test_phase07_workflow.py`
- `tests/test_repository_contract.py`
- `README.md`
- `docs/REFERENCE_USAGE_LOG.md`
- `docs/PHASE_0_7_CODE_REVIEW.md`
- `docs/PHASE_0_7_INTEGRITY_REPAIR.md` (new)

## Validation evidence

### Complete tests

```text
python -m pytest -q -p no:cacheprovider
82 passed (including the Phase 8-era regression proving runtime caches do not
make the audit self-defeating)
```

The suite directly verifies different daily pre-test contexts, four predictions
per eligible minute, hidden test labels, delayed outcomes, chronological
calibration selection, exact sparse-bucket rejection, accepted-data production
CLI routing, fixture naming, repository immutability parity, bounded estimator
inputs, artifact fail-closed behavior, no network access, and no order
capability.

### Phase 0–5 gate

The complete Phase 0–5 command set was run twice. Coverage, dataset-quality,
labels, Phase 4/5, and computed audit JSON files had identical SHA-256 values on
both runs. The computed audit status is `PASS`: explicit calendar only, no
forbidden broker calls, no feature forward-fill, no generated product
environment, required migrations present, and read-only adapter capabilities.

### Phase 0–7 gate

Two complete `fixture-phase0-7` executions produced the same file SHA-256:

```text
2B2F7E053ABE966A69DA3F944DA48A6341AB8E68FA6A58529A67DAA759B01B19
```

The report logical checksum is:

```text
99ce6ef6f63eb0bd89c86e9d1183cb953110acc36413377179f07cc0ca2d275d
```

| Test day | Training | Validation | Calibration fit | Calibration selection | Eligible minutes | Predictions |
|---|---|---|---|---|---:|---:|
| 2024-07-23 | Jul 15–17 | Jul 18 | Jul 19 | Jul 22 | 41 | 164 |
| 2024-07-24 | Jul 15–18 | Jul 19 | Jul 22 | Jul 23 | 41 | 164 |

Total: 328 predictions and 328 separately appended outcomes. Every eligible
minute has exactly four predictions.

### Static and security validation

- Python compilation succeeded.
- No lint/formatter is configured in `pyproject.toml`.
- All eight migrations pass static contract tests; migration `008` adds Phase
  6/7 immutable triggers and first-class reliability buckets.
- The artifact tamper test rewrites a copied artifact and proves checksum load
  failure.
- AST and adapter checks find no order, position, holding, margin, or GTT route
  and no production fixture fallback.
- Reference repositories remained read-only. No AGPL source was copied.

## Remaining limitations

- Docker is unavailable here, so migrations could not be executed against a
  live disposable PostgreSQL instance. SQL and repository parity are covered
  statically and in memory.
- No real Zerodha call was made, by design.
- Compact fixtures prove integrity, not generalization, profitability, or
  threshold optimality.
- Bundles remain `CANDIDATE`/`REJECTED`; production promotion is not implemented.
- Live data, futures/options features, deployment, paper trading, P&L, Symfony,
  React, and execution remain outside this phase.

## Exact recommended next task

Phase 8 only: run migrations `001`–`008` on a disposable PostgreSQL container,
add PostgreSQL-versus-memory behavioral conformance integration tests, and
define research model promotion criteria using longer accepted history—without
adding live collection, UI, or trading scope.
