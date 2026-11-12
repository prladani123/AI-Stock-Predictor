# AI BANKNIFTY Intelligence Workstation

Analysis-only BANKNIFTY workstation. Big Move V2 is the primary signal; Market Memory V2 provides separate numerical and timestamped news/event evidence. Kronos remains experimental. No order, position, holding, margin, GTT, or paper-trading endpoint is exposed.

## Stack

- Python 3.12, FastAPI, PostgreSQL migrations through `021` (including append-only prospective shadow evidence)
- Symfony thin public API proxy
- React/Vite workstation UI
- read-only Zerodha market-data adapters

## Research development status (G2B–J)

CODE IMPLEMENTED is separate from DATA ACTIVATED and EMPIRICALLY VALIDATED.
G2B input manifests are content-addressed by exact dates, revision/profile checksums and evidence pins;
overlapping identical rows retain their original lineage, while changed evidence conflicts.
G3 validates every referenced generation manifest; multiple disjoint manifests are supported.
No real G3 experiment, historical provider request, migration application or live shadow capture
was performed as part of this implementation. PRICE_CORE_V2 artifacts, thresholds and probabilities are unchanged.

From `our-product`, local official historical membership import (CSV or bounded ZIP containing CSV):

```powershell
banknifty-foundation import-official-banknifty-history --input C:\evidence\nifty_bank.csv --effective-from 2025-01-01 --effective-to 2025-01-31 --source-publication-date 2024-12-31 --source-observed-at 2025-01-01T08:00:00+05:30 --source-reference "Official archive reference" --source-url https://www.niftyindices.com/reports/historical --effective-range-basis "Explicit dates in official evidence" --weight-unit PERCENT
```

Replace example provenance with actual evidence, never backdate observation times. The URL is provenance only and is never downloaded.
Use `FRACTION` or `NONE` explicitly as appropriate; absent weights stay null. A CSV must identify NIFTY BANK by its index column or filename.
Official membership without weights uses the explicitly named equal-weight research proxy, not official index weights.

Planning only (no network, generation or training, even if `--allow-network` is supplied):

```powershell
banknifty-foundation context-v3-research-run --revision-id REVISION_UUID --instrument-id 1 --from 2025-01-01 --to 2025-01-31 --profiles all --algorithms LOGISTIC_REGRESSION,LIGHTGBM --horizons 10,20,30 --output-dir artifacts/context-research --dry-run
```

Remove `--dry-run` to explicitly run preparation/activation and eligible research experiments.
Network remains disabled unless `--allow-network` is explicitly supplied. Missing evidence and
profile failures are reported independently; COMPLETE/PASS evidence is reused, not refetched.
Provider auth/subscription failures remain blockers. No historical current-contract substitution is permitted.

```powershell
banknifty-foundation evaluate-context-v3 --revision-id REVISION_UUID --instrument-id 1 --profiles all --from 2025-01-01 --to 2025-01-31 --horizons 10,20,30 --algorithms LOGISTIC_REGRESSION,LIGHTGBM --evaluation-step-days 1 --minimum-training-days 5 --holdout-days 5 --output artifacts/context-experiments.json
python -m compileall -q src/banknifty_foundation
python -m pytest tests/test_research_integrity_extensions.py tests/test_context_v3_experiments.py tests/test_scheduled_and_local_context.py tests/test_migrations.py -q -p no:cacheprovider
npm.cmd --prefix frontend run typecheck
```

H: prospective local Zerodha top-five microstructure research is implemented; Greeks remain unavailable.
I: best-effort shadow capture follows committed V2 prediction/journal persistence in a separate transaction.
Migration `021_live_shadow_observations.sql` must be applied using the existing migration workflow before capture can save.
Existing outcomes are joined, never copied. Summary metrics cover the latest 10,000 captures and overlapping descriptive cohorts;
100 resolved observations is an operational display threshold, not proof of predictive value.
J: authenticated read-only research endpoints, Symfony proxies, typed Settings panels and evidence-based readiness are implemented.
Missing runtime depth, official historical evidence, provider subscriptions or sufficient resolved samples remain real blockers.

Internal endpoints: `/internal/research/microstructure/runtime`, `/internal/research/shadow/summary`,
`/internal/research/shadow/recent`; public proxies use `/api/research/...`. All are read-only.

## Application setup

```powershell
cd our-product
Copy-Item .env.example .env
# Set POSTGRES_PASSWORD, INTERNAL_API_TOKEN and APP_SECRET in .env.
docker compose up --build
```

Open http://localhost:3000, then **System Readiness** (`/settings`). Bootstrap waits for PostgreSQL, applies unapplied migrations under a database lock, materializes the source-backed calendar, verifies all 50 curated checksums, and ingests changed memories. Existing derived records are retained; missing records or changed source fingerprints trigger rebuilding. No broker history is fetched by bootstrap. FastAPI lifespan owns independently configured live, Big Move, news/reaction and daily-rollup workers. The optional live worker may perform index catch-up only after its explicit opt-in guards pass. Disable NEWS_POLLING_ENABLED for an offline startup.

Stage F is Operational Readiness + Fresh-Install Bootstrap, not final integration. `/api/system/readiness` inspects PostgreSQL, migrations, calendar, actual memory coverage, snapshot metadata, workers, accepted research data, labels/features, model approval and artifact checksum/load integrity, news and evidence. It performs no exchange fetch, training, approval or refresh. Runtime configuration is read-only and secondary. Application health does not imply prediction readiness: Stage G research preparation and explicit activation remain necessary. Without derivatives training coverage the actual model scope is `PRICE_CORE_V2`.

`market_memory/config/nse_trading_calendar.json` records the official 2026 NSE F&O schedule and amendments (January 15 holiday and February 1 special session). August 26 is open, not a trading holiday. November 8 Muhurat hours remain explicitly UNKNOWN until verified. Migration 017 permits UNKNOWN dates with null bounds; strict calendar consumers treat them as unresolved. Bootstrap inserts every covered date without replacing conflicting stored sessions. Conflicts, unresolved dates and out-of-range years block live collection, not application startup. Corrections require explicit source-backed configuration and stored-row reconciliation; no silent SPECIAL-session replacement occurs.

`.env.example` DATABASE_URL is for host CLI use only. Compose constructs the container URL from POSTGRES_* and passes Symfony SYMFONY_DATABASE_DSN. Use a URI-safe POSTGRES_PASSWORD for Compose, and URL-encode special characters when constructing a host CLI URL. Health endpoints perform a small SELECT 1 check, not full readiness calculations.

Runtime dependencies are declared and locked in `pyproject.toml` / hashed `requirements.lock`, `frontend/package.json` / `package-lock.json`, and `backend/composer.json` / `composer.lock`. Docker installs these locks; temporary development tooling is not required. Linux bind mounts must allow UID 10001 to write `market_memory`, `artifacts`, and `model_artifacts`.

## Market Memory V2

The canonical dataset is `market_memory/daily`: 50 rich, source-backed daily records from 2026-07-10 through 2026-09-18. Original raw Yahoo minute responses are retained in `market_memory/raw/index_history`. The V1 dataset is archived under `artifacts/archive/legacy-market-memory-v1`.

Daily Markdown is immutable source evidence. These commands regenerate only derived records; they never overwrite the curated daily files:

```powershell
banknifty-foundation memory-build
banknifty-foundation memory-build-events
banknifty-foundation memory-ingest --directory market_memory
```

`memory-build` produces point-in-time numerical episodes using the exact Big Move V2 10/20/30-minute high/low barrier policy. `memory-build-events` produces timestamped structured events and reactions when minute data exists. PostgreSQL migration 012 stores V2 fields, live articles, event/source links, and reactions.

To create a factual rollup from stored data without overwriting an existing curated day:

```powershell
banknifty-foundation memory-finalize-day 2026-09-18
```

With `MEMORY_ROLLUP_ENABLED=true`, the scheduler checks every minute after `MEMORY_ROLLUP_AFTER=15:45` IST. It requires an explicitly OPEN/SPECIAL session and complete PASS finalized index minutes, skips no-data days, never overwrites an existing day, and logs failures. News comes from PostgreSQL when configured. Futures and options summaries use stored PASS observations, identify the contract/expiry/strike universe and observation times, and leave missing facts NOT_VERIFIED. Option totals are for the selected universe, not the entire chain.

Coverage is available at `/api/memory/coverage` and on Market Memory. Historical enrichment remains explicit:

```powershell
docker compose exec python-service banknifty-foundation memory-enrich-intraday --provider zerodha --missing-only
```

This requires `ALLOW_REAL_ZERODHA=true`, a valid API key and access token. Otherwise the command reports pending without making a broker call. Coverage and episode/event counts are read from actual evidence in System Readiness; daily-memory presence must not be mistaken for complete minute history. Enrichment remains explicit Stage G work.

Maintain sourced announcements in `market_memory/config/scheduled_events.json`. The versioned object keeps configuration state independent of visibility: `/api/news/scheduled` returns upcoming point-in-time-visible events plus `configuredEventCount`. Each event requires `eventType`, `title`, `scheduledDate`, `source`, `sourceUrl`, `sourceObservedAt`, `bankniftyRelevance` and `country`; use `timingPrecision: "EXACT_TIME"` with an offset-aware `scheduledAt` only when the source establishes the time. Otherwise use `DATE_ONLY` and leave `scheduledAt` null. Historical knowledge uses `historicalKnowledgeAvailableFrom` with explicit publication provenance. Context V3 exposes neutral calendar evidence only; it does not score event importance or predict direction.

## Live news intelligence

Configured sources are NSE official announcements, RBI official publications, Moneycontrol, ET Markets, and Livemint. Fetches run concurrently, failures are isolated per source, first-seen time is immutable, headlines are deduplicated, and only deterministic BANKNIFTY-relevant classifications are stored.

```powershell
banknifty-foundation news-refresh
banknifty-foundation service
```

Set `NEWS_POLLING_ENABLED=true` to start the bounded in-service poller. The minimum interval is 60 seconds; default is 90. Individual sources can be disabled with the variables in `.env.example`. Public sites may return HTTP 403; this is surfaced in source health and does not stop other sources.

APIs are available through Symfony under `/api/news/*` and internally under `/internal/news/*`. News/event similarity is supporting evidence and never changes Big Move V2 probabilities.

## Context V3 activation

Context V3 remains explicit and per-profile. Activation writes each plan, revalidates every pinned input, generates only READY profiles, and reports isolated outcomes:

```powershell
banknifty-foundation context-v3-activate --revision-id <revision> --instrument-id <id> --profiles all --output-dir artifacts/context-v3
banknifty-foundation context-v3-activate --revision-id <revision> --instrument-id <id> --profiles banknifty-price-vol-v3 --output-dir artifacts/context-v3 --dry-run
```

Coverage includes ready dates, missing dates grouped by source and ambiguous dates. Existing immutable rows or input manifests generated under different configuration checksums are never overwritten; activation reports the conflict.

Historical preparation is explicit and provider-free by default:

```powershell
banknifty-foundation context-v3-history-preflight --revision-id <revision> --instrument-id <id> --from 2026-01-01 --to 2026-03-31 --output artifacts/context-v3/preflight.json
banknifty-foundation prepare-context-v3-history --revision-id <revision> --instrument-id <id> --from 2026-01-01 --to 2026-03-31 --output-dir artifacts/context-v3
```

`--allow-network` is required for a one-day provider capability probe. Provider configuration and local membership status are otherwise inspected without network access. A blocked preflight never generates rows.

Stage G3 compares V2 and V2 + Context V3 only on the same revision, instrument, reference minute, horizon and label. Training-only schema selection, chronological partitions, paired day-block bootstrap and immutable stored manifests are mandatory. The command never trains a production artifact, approves a model or changes runtime probabilities:

```powershell
banknifty-foundation evaluate-context-v3 --revision-id <revision> --instrument-id <id> --profile banknifty-full-context-v3 --minimum-training-days 20 --holdout-days 10
```

Classifications are `INSUFFICIENT_COVERAGE`, `INSUFFICIENT_EVIDENCE`, `V3_BETTER`, `NO_CLEAR_IMPROVEMENT` and `V3_WORSE`. Stored evidence is exposed read-only in Settings. Stage G3 status is "evaluation framework implemented", not a claim that V3 improves prediction.

Local historical observations can be imported without provider/network calls:

```powershell
banknifty-foundation research-context-import-local --input data/local.json --instrument-id <id> --source-name "verified local export" --source-observed-at 2026-10-23T12:00:00+05:30
banknifty-foundation backfill-research-context --revision-id <revision> --instrument-id <id> --input-dir data/local-days --source-name "verified local export" --source-observed-at 2026-10-23T12:00:00+05:30
```

Inputs may be JSON (an observation list or `{"observations": [...]}`), JSONL, or CSV. Each observation needs `instrument`, `observed_at`, `values`, and optionally `effective_at`, `provider`, `source_latency_seconds`, `granularity_seconds`, `metadata`, `raw_checksum`; a deterministic checksum is derived when it is absent. Future timestamps, unsupported option providers/instruments, malformed values and conflicting duplicate identities fail closed. Non-canonical or incomplete-session dates are reported as skipped rather than silently imported. Imports never forward-fill, interpolate, or use a provider. Backfill processes chronological dates and never overwrites stored runs.

## Diagnostics

```powershell
python -m compileall -q src tests
python -m pytest -q
banknifty-foundation memory-build
banknifty-foundation memory-build-events
banknifty-foundation news-refresh
npm --prefix frontend run typecheck
npm --prefix frontend run build
php backend/bin/console debug:router
```

Real Zerodha history/live collection remains explicit, confirmation-gated, and read-only. Never use fixtures as market history.
