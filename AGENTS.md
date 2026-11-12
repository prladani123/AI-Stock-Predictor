# Agent Instructions

## Project

BANKNIFTY analysis-only research workstation. `PRICE_CORE_V2` is the production signal.
Kronos and BANKNIFTY Time Machine are optional experimental research and must never change
production probabilities, thresholds, approvals or model artifacts.

Never add broker order, position, holding, margin, GTT or execution features. Never use Yahoo
data or fabricate missing candles/volume. Accepted revision candles are the canonical input.

## Validation

Run focused checks from the repository root:

```powershell
python -m compileall -q src\banknifty_foundation
npm.cmd --prefix frontend run typecheck
docker compose config --quiet
php backend/bin/console lint:container
```

If PHP lint is unavailable, report it. Run focused pytest files relevant to the change; do not
run broad unrelated legacy suites by default.

## Research boundaries

- Historical research must not read future candles or future sessions.
- Kronos must never block the production V2 path; failures are isolated `SERVICE_UNAVAILABLE`.
- Time Machine uses complete sessions strictly before the reference date and at least 20 prior sessions.
- Keep point-in-time fields: `referenceMinute`, `marketAsOf`, `computedAt`, `availableAt`,
  `sourceMaxTimestamp`.
- Historical counterfactuals use `HISTORICAL_COUNTERFACTUAL_RESEARCH`; live decision evidence
  is only available when `availableAt <= decisionAt`.
- Persist to `experimental_model_outputs` with explicit model names:
  `KRONOS_MINI_ZERO_SHOT_V1` and `BANKNIFTY_TIME_MACHINE_V1`.
- Never infer model benefit from code availability; evaluation requires real chronological evidence.

## Vendored and Docker sources

Docker uses local snapshots under `third_party/Kronos/` and `third_party/india-trader-skill/`.
Never clone these repositories during image build. Preserve `UPSTREAM.md` provenance and model
licence/size/hash notes. Never commit credentials or downloaded secrets.

## Git

Only commit or push when explicitly requested. Prefer focused commits with a clean working tree.
Do not force push. Do not modify production model artifacts during research integration.
