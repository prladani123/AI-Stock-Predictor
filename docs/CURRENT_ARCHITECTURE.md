# Current Architecture

## G2B–J research integration

G2B implementation includes date-scoped immutable generation manifests, local historical official Nifty Indices
CSV/ZIP membership, explicit weight units, independent profile preflight and opt-in preparation.
Scoped research runs declare their required blocks; they cannot satisfy a richer profile. Data activation may still be pending.
G3 framework supports all referenced generation manifests and matched reference-minute cohorts; empirical results require real experiments.
Stages H/I/J integration is implemented, not empirically validated: retained Zerodha derivative ticks feed neutral top-five calculations;
after V2 and mandatory journal commit, a separate best-effort transaction freezes runtime V3/microstructure evidence in migration 021.
Shadow failure never changes the production result. Memory/news references remain in the linked immutable journal.
No duplicate outcome storage, broker execution, feed, model promotion or probability adjustment was introduced.
Read-only microstructure/shadow endpoints and Settings sections fail independently. Missing Greeks are explicitly unavailable.
Runtime/SQL deployment validation and sufficient real prospective samples remain operator work.

**CODE IMPLEMENTED ≠ DATA ACTIVATED ≠ EMPIRICALLY VALIDATED.** No V3 performance improvement is claimed.

```text
React workstation
  -> Symfony public API proxy
    -> Python intelligence service
       |-- Big Move V2 (primary 10/20/30m intelligence)
       |-- numerical Market Memory V2
       |-- news/event Market Memory V2
       |-- market confirmation
       |-- guarded LiveMarketWorker -> stored sessions/master -> full-mode ticker -> finalized minutes
       `-- experimental Kronos comparison
         -> PostgreSQL
         -> read-only market/news sources
```

The numerical channel is:

```text
preserved minute data -> point-in-time state | separate future outcome
current state + historical episodes -> missing-aware numerical similarity
```

The news channel is:

```text
NSE/RBI/Moneycontrol/ET/Livemint
  -> concurrent isolated fetch + source health
  -> deduplication + deterministic relevance classification
  -> article/event storage with publication and first-seen time
  -> reaction measurement when candles exist
  -> event similarity + independent market confirmation
```

Daily V2 Markdown remains inspectable evidence; PostgreSQL migration 012 is the indexed production layer and committed JSON is the local fallback. React never calls Python directly. No memory result mutates model probabilities, and no component exposes trading actions.

Stage C's implemented foundation is the opt-in live worker, which reuses the collector and contract selector, persists index/futures/options evidence, and rotates the stored ATM universe. Index-only catch-up fills missing minutes without replacing live revisions. LIVE_MARKET_ENABLED and ALLOW_REAL_ZERODHA default to false. Simulation stays in memory and never writes live evidence.

Stage G2B is implemented and Stage G3 is current: strict paired V2 versus V2 + Context V3 evaluation, historical preflight/preparation and read-only experiment evidence. G1 (historical research context) and G2A (constituents, futures, market structure and event impact) are implemented foundations. Stage F's operational readiness/bootstrap remains in place. The official NSE calendar materializes every configured-year date, including UNKNOWN unresolved specials without guessed hours (migration 017). Strict calendar lookup, stored-row conflicts and canonical-memory cross-checks guard collection. Unsupported years do not prevent application startup but block real live collection.

Complete PASS live index sessions are archived immutably under `raw/index_history/live_<date>_<checksum>.json`. Daily rollup preserves curated evidence, resolves previous close only from the immediately preceding verified session, and derives expiry context only when actual selected contracts establish it. The memory builder reads validated, checksum-matching complete archives; partial/conflicting archives are not learning inputs. Scheduler restart resumes archive/derived-memory synchronization without overwriting daily Markdown.

`GET /api/system/readiness` proxies `GET /internal/system/readiness`. Readiness separates application health, market/calendar availability, research coverage, model integrity, news and prospective evidence; it never starts workers, fetches providers, generates datasets or trains/evaluates/approves models. Docker health uses a small database probe and collector state. Historical `banknifty-bigmove-v2` remains immutable PRICE_CORE_V2 and its training/evaluation/approval/runtime services remain V2-only.

`context_v3.py` owns shared pure PRICE_VOL, INDIA_MARKET, GLOBAL, DERIVATIVES, BANK_STRUCTURE and SESSION_STRUCTURE blocks. Five immutable profiles have independent required blocks, explicit optional numeric groups and missing-aware availability. NIFTY50 uses Upstox's `NSE_INDEX|Nifty 50`. Historical generation requires an accepted revision and explicitly pinned context runs, membership checksums, scheduled configuration and CAS rules. `ContextV3ActivationService` plans per profile, writes the plan, revalidates pins and generates READY profiles with isolated outcomes. Coverage reports ready dates, source-grouped missing dates and ambiguous dates. Definitions and input manifests use existing `feature_set_definitions`; vectors and lineage use existing `feature_rows`. No migration 021. V3 `as_of` is the decision availability boundary after completion; `referenceMinute` joins V2/labels by candle opening time. Retrospective provider timestamps never claim prospective receipt verification.

`OfficialConstituentImportService.import_csv(path, effective_from=..., effective_to=..., source_observed_at=..., source_url=...)` accepts a local official Nifty Bank CSV without downloading. CSV checksum, caller-attested dates and provenance form an immutable snapshot under `config/official_constituents`. Official evidence supersedes tracking proxies; overlapping official ranges fail closed. A current CSV does not establish historical membership or weights. V3 requires date-valid and point-in-time observed OFFICIAL_INDEX membership, labels its basket EQUAL_WEIGHT_PROXY and excludes fixed heavyweight display groups.

CAS configuration records the supplied NSE phases and observation date: post-effective-date history before that observation remains unavailable without earlier source evidence. Index horizons cannot cross index close. Scheduled configuration uses a versioned source-backed list; visibility is point-in-time and `CONFIGURED` is independent of upcoming-event count. `DATE_ONLY` events never invent a release time, and Context V3 uses neutral event/time/meeting evidence rather than importance scores or directional predictions. IV expected moves stay null unless stored metadata explicitly establishes annualized units and provenance. News/event impact remains separate evidence, not V3 sentiment.

Read-only `/api/research/context-v3/profiles`, `/api/research/context-v3/coverage`, `/api/research/context-v3/preflight` and `/api/research/context-v3/experiments` proxy Python equivalents. Settings reports actual coverage, ready/missing/ambiguous dates by source, conflicts, pinned lineage and identical-reference-minute/label common support with V2. Scheduled configuration state is also exposed independently of upcoming visibility. Local historical Research Context import (`JSON`/`JSONL`/`CSV`) and chronological backfill/audit reuse the existing immutable run repository; they never call providers, interpolate, or forward-fill. G3 uses only rows matched by revision, instrument, reference minute, horizon and label; candidate schemas are selected on training dates only and every evaluation row is scored by both cohorts. Experiment manifests are immutable research evidence under `feature_set_definitions`; they are not model artifacts and cannot approve or alter runtime state. Historical preflight reports provider/local-membership capability without network access unless `--allow-network` is explicit. H is prospective microstructure/Greeks/order-flow, including optional later Dhan 20/200-level depth research (no added WebSocket); I is live shadow; J is final hardening.
