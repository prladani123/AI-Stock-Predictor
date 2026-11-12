# Full product architecture

> Current architecture source of truth. Historical phase reviews live under
> `docs/archive/legacy-phases/` and do not define future development.

## Product boundary

AI BANKNIFTY is an analysis-only workstation. React talks only to Symfony.
Symfony validates and shapes the public contract, then calls the private Python
service. Python owns broker market data, accepted research data, models,
predictions, and live collector state. PostgreSQL remains the durable system of
record. No service exposes order, position, portfolio, GTT, or paper-trading actions.

```text
Browser
  | same-origin /api JSON
  v
React + nginx ---> Symfony 7.4 API ---> FastAPI internal service
                                           |             |
                                           v             v
                                      PostgreSQL     Zerodha read-only
                                                    REST / KiteTicker
```

Normal Compose services are exactly `postgres`, `python-service`, `backend`,
and `frontend`. Polling is intentionally used instead of a browser WebSocket.

Market Memory V2 stores rich daily Markdown as human-readable evidence and
indexes parsed days, point-in-time numerical episodes, timestamped news/events,
source lineage, and measured reactions in PostgreSQL. Numerical retrieval uses
only mutually available feature groups; forward 10/20/30-minute outcomes are
never part of episode state. Event retrieval uses category, tags, knowledge
phase, scheduled status, and directional context. Both results are supporting
evidence and do not alter Big Move V2 probabilities.

## Ownership

| Component | Responsibilities | Does not own |
|---|---|---|
| React | Workstation layout, typed API client, interaction state, charts, filters, empty/error states | Credentials, market calculations, broker calls |
| Symfony | Public `/api` routes, input validation, Python orchestration, safe error shaping, future product/auth concerns | Market tables, ML inference, live collection |
| Python | Big Move V2, numerical/event Market Memory, news ingestion, Zerodha adapters, simulation, internal views, and live inference | Public browser API, user sessions |
| PostgreSQL | Existing market/research/prediction/live tables and immutable lineage | UI state |

Python remains in `src/banknifty_foundation` for this slice. Moving it into a
cosmetic `python-service/` subtree would break imports, packaging, tests, Docker,
and artifact paths without adding product value. The root Python package is the
`python-service` deployable; a later move should be a dedicated packaging task.

## API flow

React uses one client in `frontend/src/api.ts`. Public routes are health,
dashboard, candles, latest/history predictions, model/live status, options,
futures, data quality, configuration, memory/news evidence, and simulation start/stop. Symfony maps
these onto equivalent `/internal/*` Python routes and supplies an internal token
that is never sent to the browser. Read endpoints fail with structured
`PYTHON_SERVICE_UNAVAILABLE` output; the UI renders a compact state rather than a stack trace.

## Frontend pages and chart

The sidebar routes Dashboard, Live Market, Predictions, Option Chain, Futures,
Models, History, Data Quality, and Settings. Dashboard uses published
`lightweight-charts` for candlesticks, volume, crosshair, prediction markers,
and responsive sizing. TradingView attribution stays enabled.

The UI distinguishes OFFLINE, SIMULATED, and eventual LIVE mode. It also
distinguishes Symfony, Python, PostgreSQL, and Zerodha state. Candidate research
models are never presented as live-approved models or guaranteed performance.

## Simulation flow

```text
Start simulation button
  -> Symfony POST
  -> Python ProductRuntime
  -> existing deterministic collector scenario
  -> in-memory live repository
  -> candles / futures / selected ATM option snapshots / quality journal
  -> Symfony JSON
  -> React polling refresh
```

The runtime uses the actual collector, contract selector, minute finalizer,
subscription registry, and local deterministic scenario. Production components contain
no hard-coded prices or confidence scores. Stopping simulation stops the runtime
mode but retains its last observable snapshot for diagnosis.

The deterministic collector scenario deliberately creates no fabricated live predictions. The API reads persisted
prediction and model lineage when PostgreSQL contains it; otherwise it returns
three primary Big Move V2 horizon slots (10m, 20m, 30m) with `available=false` and `DATA_NOT_READY`. This is
an honest product state, not a fabricated `NO_TRADE` decision.

## Durable live flow — Stage C

On the target machine, an operator configures read-only Zerodha credentials and
enables the guarded LiveMarketWorker through permanent configuration. It reads the stored exchange session, refreshes the BANKNIFTY master when needed, obtains initial LTP, and selects the front future and nearest-expiry ATM ± configured strikes. Index-only catch-up fills missing completed minutes; `KiteTicker` feeds the
small selected universe, Python finalizes and stores quality-passing minutes,
then an approved live-shadow model can append predictions. The browser continues
to poll Symfony. Universe refresh reuses the subscription registry's subscribe-before-retire path. Real collector start remains absent from the UI; `collect-live --confirm-real` invokes the same worker without manual snapshot/session JSON files.

## Time, persistence, and security

Timestamps are stored and exchanged as UTC ISO-8601 values. NSE logic uses
`Asia/Kolkata`; React formats timestamps as IST. Python remains primary owner of
every existing migration/table. Symfony performs a health probe and creates no
duplicate market/prediction schema.

- Internal calls require `X-Internal-Token`; Compose does not publish port 8000.
- No BUY, SELL, place/modify/cancel order, holdings, positions, margins, or GTT route exists.
- Authentication is intentionally deferred for this local single-operator slice.
- Simulation is runtime demonstration data, not live accuracy evidence.
- Full container execution must be repeated on a target with PHP, Composer, Node, and Docker.
