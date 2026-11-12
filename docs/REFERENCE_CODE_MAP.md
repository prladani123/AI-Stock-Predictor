# Reference Code Map

## Scope and method

Static analysis completed 25 July 2026 against the eight nested source trees under `all github projects/<repo>/<repo>`. Reference repositories were not modified and no broker, credential, network-order, training, or untrusted project script was run. Generated assets, lock-file contents, notebooks where equivalent source exists, images, bundled third-party code, repetitive tests, and documentation with no architectural effect were excluded. “File” rows occasionally group a cohesive family of tiny files; every path in a group has the same architectural role and decision.

Decision vocabulary: **adopt** means use the concept or official package; **adapt** means reimplement the idea in our own design; **reject** means do not carry it forward; **verify** means useful only after current documentation/data tests.

## Executive comparison

| Repository | Licence | Stack | Real/mock status | Use |
|---|---|---|---|---|
| `openalgo-main` | AGPL-3.0 | Flask, SQLAlchemy/SQLite, DuckDB, React/TS, WebSockets, ZeroMQ | Broker paths are real; sandbox is simulated; many integrations are uneven | Architecture reference only; never copy or link code |
| `pykiteconnect-master` | MIT | Python, Requests, Twisted/Autobahn | Official real Kite REST/WebSocket client | Actual dependency, but install a current supported release |
| `tradingboard-main` | MIT | React/TS/Vite/Zustand, Express, PostgreSQL, TradingView widgets | Watchlists real; claimed market service is no-op/widget-owned | UI reference only |
| `trading-dashboard-master` | MIT | React, Tailwind, Recharts/CRA | Entire market dataset is hard-coded demo data | Secondary UI reference only |
| `lightweight-charts-master` | Apache-2.0 + NOTICE attribution | TypeScript, canvas | Real chart library; data must be supplied | Actual npm dependency |
| `qlib-main` | MIT | Python, pandas, LightGBM/XGBoost, MLflow, Redis/Mongo optional | Real research framework; examples mainly China equities | Conceptual architecture reference, not MVP dependency |
| `qstrader-master` | MIT | Old Python/pandas event backtester | Real framework plus synthetic generator; fill model is naive | Conceptual reference only |
| `finBERT-master` | Apache-2.0 | Python, PyTorch, Transformers, Flask | Real sentiment model code; not market-direction evidence | Later-phase reference; do not include in MVP |

## 1. `openalgo-main`

### Repository assessment

- **Usefulness:** strongest reference for broker boundary, instrument master, normalized quotes, reconnecting streams, analyzer separation, audit events, and operational checks.
- **Strengths:** explicit broker plugins; daily master-contract workflow; REST/WebSocket separation; encrypted tokens; rate limits; analyzer audit records; sandbox accounting; health/traffic/latency stores; meaningful unit tests around auth, modes, symbol mapping, and sandbox behavior.
- **Weaknesses:** huge Flask modular monolith (hundreds of routes), 34 uneven broker implementations, dynamic imports/duck typing, duplicated adapters/mappings, SQLite sprawl, ZeroMQ and multiple socket layers, trading automation far outside our scope, and hard-coded market assumptions.
- **Bugs/risks found:** AGPL network copyleft; `HISTORIFY_DATABASE_URL` documentation versus `HISTORIFY_DATABASE_PATH` implementation mismatch; analyzer GTT tables exist while endpoints return 501; WebSocket keepalive/retry values are widely hard-coded; broker response conformance is incomplete; migration startup has documented fail-open behavior; numerous broad `except Exception` paths convert faults to empty data; old security audit records historical hard-coded example keys (do not assume history is clean); API/front-end surface is too large to secure cheaply.
- **Dependencies:** snapshot declares Python >=3.12 and many tightly pinned, very recent packages. It is not “stale” overall, but its very large dependency set is unnecessary and increases compatibility/attack surface. Do not import it.
- **Tests:** broad but mixed. Good isolated pytest coverage exists; several files are manual/live scripts, and cross-broker integration is not a conformance guarantee. No evidence validates ML predictions because this is not an ML system.
- **Verdict:** **architecture reference only**. Adopt concepts; independently implement a much smaller Zerodha-only collector. Reject source reuse, Flask app shape, live-order surfaces, multi-broker generality, ZeroMQ, and database fragmentation.

### Architecturally relevant file map

| Path(s) | Purpose / important units | Inputs → outputs and dependencies | Product lesson / risk | Decision |
|---|---|---|---|---|
| `app.py`, `extensions.py`, `cors.py`, `csp.py`, `limiter.py`, `.sample.env` | Runtime factory, Socket.IO, CSRF/CORS/CSP, rate limits, environment contract | env + HTTP → initialized Flask application; Flask ecosystem, schedulers | Initialization ordering and fail-closed secrets are useful; global startup is overloaded | Adapt |
| `docker-compose.yaml`, `Dockerfile`, `start.sh`, `Caddyfile`, `DOCKER_README.md` | Multi-process local/deploy layout | images/env/volumes → web, WS proxy, storage | Health checks and persistent volumes useful; topology excessive for our laptop | Adapt |
| `utils/env_check.py`, `utils/config.py`, `utils/constants.py` | Required secret validation and shared enums | env → validated settings/constants | Central typed settings; avoid giant universal enum | Adapt |
| `utils/plugin_loader.py`, `websocket_proxy/broker_factory.py` | Dynamic broker discovery/import | broker key/plugin JSON → modules/adapters | Capability registry concept useful; Zerodha-only MVP needs no plugin system | Reject MVP |
| `database/engine_factory.py`, `database/db_init_helper.py` | SQLAlchemy engine/session setup | database URLs → pooled/scoped sessions | Teardown discipline useful; use one PostgreSQL database | Adapt |
| `database/auth_db.py`, `database/user_db.py`, `database/oauth_db.py`, `utils/oauth_*.py`, `utils/session.py`, `utils/auth_utils.py` | Users, API keys, encrypted broker/feed tokens, session expiry | credentials/session → authenticated identity/token lookup | Encrypt broker tokens, never send to browser, revoke on expiry; Symfony owns this | Adapt |
| `utils/security_middleware.py`, `utils/ip_helper.py`, `utils/traffic_logger.py` | Proxy-aware IP controls and request logging | request headers → allow/deny/audit | Trust forwarded headers only behind known proxy; avoid sensitive payload logging | Adapt |
| `database/master_contract_status_db.py`, `database/master_contract_cache_hook.py`, `database/token_db.py`, `database/symbol.py` | Instrument metadata/cache lifecycle | broker instrument dump → symbol/token lookup | Persist every daily dump; identity is exchange+symbol+validity, not reusable token | Adopt concept |
| `broker/zerodha/database/master_contract_db.py` | Downloads/transforms Kite instrument CSV (`SymToken`, symbol reformatter) | Kite CSV → normalized rows/cache | Most relevant symbol-map flow; mappings are OpenAlgo-specific and AGPL | Adapt independently |
| `broker/zerodha/api/auth_api.py` | Direct checksum/token exchange | request token + secret → access token/error; shared httpx client | Python collector can exchange only via a controlled internal call; Symfony stores secret/token | Adapt |
| `broker/zerodha/api/data.py` | `BrokerData`: quotes, batch quotes, historical chunking, depth | symbols/tokens/auth → normalized dict/DataFrame | Chunk history, preserve OI and exchange provenance; empty-on-error is unsafe | Adapt |
| `broker/zerodha/api/funds.py`, `margin_api.py`, `order_api.py`, `gtt_api.py` | Account and live-order operations | trade/account commands → broker responses | Out of MVP and dangerous; do not expose/import | Reject |
| `broker/zerodha/mapping/transform_data.py`, `margin_data.py`, `order_data.py`, `gtt_data.py` | OpenAlgo↔Kite payload translation | normalized domain objects ↔ broker payloads | Boundary mapping pattern useful, exact names/code prohibited | Reject MVP |
| `broker/zerodha/streaming/zerodha_websocket.py` | Sync WebSocket, binary tick parsing, health check, resubscribe, token refresh | token subscriptions + binary frames → ticks | Use official `KiteTicker` instead of maintaining a parser; preserve gap/reconnect events | Adapt via SDK |
| `broker/zerodha/streaming/zerodha_adapter.py`, `zerodha_mapping.py` | Normalizes modes/exchanges/topics and batches subscriptions | canonical symbol/mode → Kite tokens; ticks → canonical events | Symbol↔token map and idempotent desired subscriptions are essential | Adapt |
| `websocket_proxy/base_adapter.py`, `connection_manager.py`, `mode_utils.py`, `mapping.py`, `server.py`, `app_integration.py` | Shared adapter contract, connection pool, authenticated client WS, ZMQ fan-out | API key/subscriptions + adapter ticks → client events | Reconnect/state concepts useful; ZMQ + proxy + browser fan-out are unnecessary for first collector | Adapt small subset |
| `services/history_service.py`, `database/historify_db.py`, `services/historify_service.py`, `historify_scheduler_service.py` | API-vs-local history, DuckDB OHLCV store/import/export/aggregation | history request/download → canonical candles/catalog | Idempotent upserts/catalog useful; use PostgreSQL partitions/tables and explicit quality status | Adapt |
| `services/quotes_service.py`, `depth_service.py`, `market_data_service.py` | Validates symbols, fetches quote/depth, caches live values | normalized request → normalized market snapshot | Validate every token and mark staleness; never silently omit requested instruments | Adopt concept |
| `services/option_symbol_service.py`, `option_chain_service.py`, `expiry_service.py`, `synthetic_future_service.py` | Expiry/strike discovery, ATM selection, CE/PE batch quotes, synthetic forward | underlying/expiry/range → selected option rows | Best option-universe concept; selection must be timestamped to avoid survivorship/leakage | Adapt |
| `services/option_greeks_service.py`, `multi_strike_oi_service.py`, `oi_tracker_service.py`, `oi_profile_service.py` | Greeks/OI analytics | live option quotes/OI → calculated analytics | OI snapshots useful later; derived Greeks not initial features until verified | Verify later |
| `database/sandbox_db.py` | Orders, trades, positions, funds, daily P&L and configuration models | simulated actions/quotes → persistent paper state | Separate immutable predictions from mutable paper positions; one PostgreSQL schema | Adapt |
| `sandbox/order_manager.py`, `execution_engine.py`, `websocket_execution_engine.py`, `execution_thread.py` | Order validation and simulated trigger/fill processing | orders + quote stream → fills/status | Useful state-machine idea; platform starts with signal-level paper execution, not broker parity | Adapt later |
| `sandbox/position_manager.py`, `fund_manager.py`, `holdings_manager.py`, `squareoff_manager.py`, `catch_up_processor.py` | MTM, positions, cash, settlement and recovery | fills + quotes/time → P&L/account | Stale quote fallback and catch-up concepts useful; implementation is much broader than needed | Adapt later |
| `services/analyzer_service.py`, `sandbox_service.py`, `database/analyzer_db.py`, `database/settings_db.py` | Analyzer/live mode selection and request/response log | mode + commands → sandbox or broker | Strong separation concept; our service must have no live-order code at all | Adopt concept |
| `events/base.py`, `events/order_events.py`, `events/analyzer_events.py`, `events/sandbox_events.py`, `utils/event_bus.py` | Typed-ish in-process events with thread-pool subscribers | events → async callbacks | Domain events/audit useful; thread-pool delivery is non-durable | Adapt with DB outbox only if needed |
| `subscribers/log_subscriber.py`, `socketio_subscriber.py`, `telegram_subscriber.py`, `whatsapp_subscriber.py` | Audit/UI/notification event consumers | events → DB/socket/messages | Audit and UI notification separation useful; messaging is non-goal | Adapt audit only |
| `restx_api/__init__.py`, `schemas.py`, `data_schemas.py` | Namespace registration and request validation | HTTP JSON → validated service calls | Explicit schemas and caps useful; Symfony DTO/validator replaces Flask schemas | Adapt |
| `restx_api/history.py`, `quotes.py`, `multiquotes.py`, `depth.py`, `option_chain.py`, `option_symbol.py`, `expiry.py`, `intervals.py`, `instruments.py` | Market-data API façades | HTTP → corresponding services | Endpoint/service separation useful; do not reproduce names or generic broker API | Adapt |
| `restx_api/place_order.py`, `place_smart_order.py`, `options_order.py`, `basket_order.py`, `split_order.py`, `modify_order.py`, `cancel_order.py`, GTT modules | Automated trading API | HTTP commands → live/sandbox execution | Explicitly out of scope and material safety risk | Reject |
| `services/place_order_service.py`, `order_router_service.py`, `action_center_service.py`, `pending_order_execution_service.py`, `database/action_center_db.py` | Semi-auto approval and dynamic broker execution | order request → queue/approval/broker | Human approval is good defense, but no order placement belongs in V1 | Reject |
| `database/apilog_db.py`, `traffic_db.py`, `latency_db.py`, `health_db.py`, `utils/latency_monitor.py`, `health_monitor.py` | Operational/audit stores and checks | requests/process metrics → retained records | Correlation IDs and compact health metrics useful; avoid four databases | Adapt |
| `frontend/src/lib/MarketDataManager.ts`, `contexts/MarketDataContext.tsx`, `components/socket/SocketProvider.tsx`, `hooks/useMarketData.ts`, `useLivePrice.ts` | Reference-counted subscriptions, reconnect, visibility handling, REST fallback | components + socket → cached quotes | Useful later UI lifecycle; browser must receive our data, never broker credentials | Adapt |
| `frontend/src/pages/OptionChain.tsx`, `components/option-chain/*`, `hooks/useOptionChainLive.ts`, `useOptionChainPolling.ts` | Configurable option-chain view and live/poll fallback | option API/socket → table | Information grouping useful after engine validation | UI reference later |
| `frontend/src/components/trading/ChartPane.tsx`, `lib/trading/chart*.ts`, `pages/Trading.tsx` | Lightweight Charts wrapper, chart themes/intervals | candles/ticks → canvas chart | Use npm library directly with a small React wrapper | Adapt |
| `test/test_mode_normalization.py`, `test_order_update_adapters.py`, `test_history_format.py`, `test_index_symbols.py`, `test_option_symbol_api.py` | Unit/regression tests for market normalization | fixtures/mocks → deterministic assertions | Good contract-test categories for our collector | Adopt tests concept |
| `test/sandbox/*`, `test_smartorder_logic.py`, `test_pending_order_execution_service.py` | Simulation/order state tests | isolated DB/mocks → order/account assertions | Useful later paper-trading scenarios; live order tests excluded | Adapt later |
| `DISCOVERY_MAP.md`, `docs/audit/*`, `audit/*` | Maintainer inventories and self-audits | source review → findings | Helpful corroboration, not authoritative; documented unresolved issues matter | Verify |

## 2. `pykiteconnect-master`

### Repository assessment

- **Usefulness/strength:** official MIT client and best dependency for authentication, REST response/error behavior, instrument parsing, historical candles, WebSocket binary formats, callbacks, reconnect and resubscribe.
- **Weakness/risk:** source reports version 5.2.1 but packaging metadata still advertises old Python classifiers and pins `autobahn[twisted]==19.11.2`; the snapshot may not represent the latest PyPI release. Integration tests include write/order tests and require credentials—never run them here.
- **Data:** all library behavior is real API integration; unit tests mock HTTP/binary packets; examples are runnable and some place orders.
- **Tests:** strong REST request-shape and error tests plus ticker parser/reconnect tests. Integration write tests are unsafe for this task.
- **Verdict:** **actual dependency**, installed by version after compatibility/security verification. Only REST market data and ticker surfaces are allowed in V1.

| Path(s) | Purpose / units | Flow / dependencies | Product decision |
|---|---|---|---|
| `kiteconnect/connect.py` | `KiteConnect`, route table, login/session, instruments, quote/OHLC/LTP, `historical_data`, exceptions | API key/token + params → Requests calls → parsed dict/list/DataFrame-like records | Adopt official SDK; wrap only allow-listed read operations |
| `kiteconnect/ticker.py` | `KiteTicker`, Twisted protocols/factory, exponential reconnect, subscriptions, modes, binary parsing | tokens + WS frames → callbacks with ticks/order updates | Adopt SDK callback/reconnect; ignore order updates; persist raw ingestion metadata |
| `kiteconnect/exceptions.py` | Typed API exceptions | HTTP error type/status → Python exception | Map into retryable/auth/permanent categories |
| `kiteconnect/__init__.py`, `__version__.py` | Public exports and snapshot version | package import → client classes | Pin/record installed version |
| `setup.py`, `setup.cfg`, `pytest.ini`, `dev_requirements.txt` | Packaging/test contract | Python environment → install/test config | Verify current release; snapshot pins are stale-risk |
| `examples/simple.py`, `flask_app.py` | Login/REST flow | browser request token → session | Concept only; Symfony owns user-facing handshake |
| `examples/ticker.py`, `threaded_ticker.py` | Subscription callbacks/threading | access token + tokens → live ticks | Adapt into dedicated Python collector |
| `examples/order_margins.py`, `gtt_order.py` | Margin/order examples | credentials → broker actions | Reject and never run |
| `tests/unit/test_connect.py`, `test_exceptions.py`, `test_kite_object.py`, `conftest.py` | Mocked REST/request/error contracts | fixtures → assertions | Model our adapter tests on these boundaries |
| `tests/unit/test_ticker.py` | Binary tick/parser/subscription tests | captured packets → normalized ticks | Essential reference for parser expectations |
| `tests/integration/test_connect_read.py` | Credentialed read API checks | real session → broker data | Do not automate locally; later controlled smoke test |
| `tests/integration/test_connect_write.py` | Places/modifies/cancels real orders | real session → broker orders | Reject/never run |

## 3. `tradingboard-main`

### Repository assessment

- **Strengths:** clear three-panel resizable composition, compact dark theme, watchlist interactions, Zustand separation, PostgreSQL parameterization, non-root backend image and health checks.
- **Weaknesses/risks:** README claims WebSocket/reconnection and AI features not present; `MarketDataService` is explicitly no-op; charts/news are third-party TradingView widgets rather than owned data; no auth/multi-user ownership; virtually no tests; inconsistent comments say IndexedDB while calls use HTTP; Vite secrets such as `VITE_OPENAI_API_KEY` would be public; Docker passes runtime Vite variables to already-built static assets and uses `backend` hostname that a browser cannot resolve.
- **Data:** watchlist CRUD is real. Market presentation comes from widgets; default symbols are US hard-coded samples.
- **Verdict:** **UI reference only**.

| Path(s) | Purpose / units | Flow | Decision |
|---|---|---|---|
| `src/App.tsx` | Header + resizable watchlist/chart/info panels; persists layout | Zustand/localStorage → panel layout | Adapt layout after prediction engine |
| `src/components/Header.tsx`, `Watchlist.tsx`, `SingleTicker.tsx` | Navigation and watchlist interactions/widgets | store → user selection | Adapt interaction density, replace symbols/data |
| `src/components/ChartPanel.tsx` | TradingView advanced widget | selected symbol → third-party iframe/widget | Reject implementation; use Lightweight Charts |
| `src/components/TabbedInfoPanel.tsx` | Calendar/news/heatmap tabs | local tab state → third-party widgets | Later-phase composition only |
| `src/store/marketStore.ts`, `src/types/market.ts` | Zustand watchlist/selection types/actions | UI actions ↔ storage service | Adapt small domain stores; server remains source of truth |
| `src/services/watchlistStorage.ts` | Fetch wrapper for CRUD/bulk endpoints | HTTP JSON ↔ typed watchlists | Adapt API client/error pattern |
| `src/services/marketDataService.ts` | No-op compatibility singleton | log-only subscribe/unsubscribe | Reject; unsupported “live” claim |
| `backend/server.js`, `routes/watchlists.js`, `middleware/watchlistValidation.js` | Express middleware/CRUD routes | HTTP → validation/service | Symfony replaces it; validation concepts only |
| `backend/services/databaseService.js`, `watchlistService.js`, `mappers/watchlistMappers.js`, `utils/errorHandler.js` | pg pool, parameterized CRUD, mapping/error responses | SQL rows ↔ API objects | Adapt transaction/mapping discipline |
| `init.sql` | watchlists/alerts and default US symbols | startup → PostgreSQL schema/sample rows | Reject schema; use migrations and per-user ownership |
| `Dockerfile`, `backend/Dockerfile`, `docker-compose*.yml`, `nginx.conf` | frontend/backend/Postgres deployment | builds/env → containers | Adapt health/non-root ideas; reject VITE secret/runtime config pattern |
| `package.json`, `backend/package.json`, `vite.config.ts`, `tsconfig*.json` | React 19/Vite/Zustand dependency/build config | source → bundle | Reference current patterns; do not copy package set |

## 4. `trading-dashboard-master`

### Repository assessment and map

MIT demo. Its only meaningful component (`src/App.js`) embeds chart, indices, and watchlist constants; README explicitly calls data mock and watchlist unfinished. CRA/react-scripts 5 is maintenance-heavy. One default render test has negligible behavioral value. **Secondary UI reference only.**

| Path(s) | Purpose / flow | Risks / decision |
|---|---|---|
| `src/App.js` | Hard-coded summary cards, Recharts line, index cards and watchlist → rendered dashboard | Adopt compact hierarchy/colors only; reject all data behavior |
| `src/App.css`, `src/index.css`, `tailwind.config.js`, `postcss.config.js` | Responsive styling/theme | Adapt spacing, not code |
| `src/index.js`, `public/index.html`, `package.json` | CRA boot/build | Reject CRA; use modern Vite |
| `src/App.test.js`, `setupTests.js` | Boilerplate render test | Reject as insufficient; require domain/UI contract tests |
| `README.md` | Feature claims plus mock disclosure | Treat “real-time” claim as unsupported |

## 5. `lightweight-charts-master`

### Repository assessment

Apache-2.0, version 5.2.0, high-quality canvas chart library with extensive unit, type, graphics, interaction and memory-leak tests. It is real rendering infrastructure, not a feed. Node >=22.3 applies to building this source snapshot, not necessarily browser runtime. The NOTICE requires TradingView creator attribution and a link; `attributionLogo` satisfies the link requirement. **Use the published npm package, never vendor this source.**

| Path(s) | Purpose / important units | Flow / decision |
|---|---|---|
| `src/index.ts`, `src/standalone.ts` | Public exports: `createChart`, series types, markers, version | Actual dependency API; adopt |
| `src/api/create-chart.ts`, `chart-api.ts`, `ichart-api.ts` | Chart creation/lifecycle, panes, events, resize | container/options → chart; use React ref + cleanup |
| `src/api/series-api.ts`, `iseries-api.ts`, `candlestick-series-api.ts` | `setData`, incremental `update`, price lines | canonical candles → series; set full history once, update completed/live candle |
| `src/api/time-scale-api.ts`, `itime-scale-api.ts`, `price-scale-api.ts`, `pane-api.ts` | visible ranges, scales, pane controls | chart state ↔ viewport; adapt responsive UX |
| `src/model/data-layer.ts`, `data-consumer.ts`, `series-data.ts`, `time-data.ts`, `data-validators.ts` | Ordered time-series normalization/validation | input rows → internal plot rows; our API must dedupe/order before chart |
| `src/model/series/candlestick-series.ts`, `histogram-series.ts`, `line-series.ts`, `area-series.ts` | Renderable financial series definitions | OHLC/volume/probability → render model | Candles + volume + optional confidence lines |
| `src/plugins/series-markers/*`, `up-down-markers-plugin/*` | Timestamped marker primitives | predictions/results → chart markers | Adopt for prediction creation/expiry, with visual distinction |
| `src/gui/chart-widget.ts`, `pane-widget.ts`, `time-axis-widget.ts`, `mouse-event-handler.ts` | Canvas layout and interaction | model → browser canvas | Library internal; do not copy |
| `src/renderers/candlesticks-renderer.ts`, `histogram-renderer.ts`, `line-renderer.ts`, `marks-renderer.ts` | Efficient drawing | plot rows → bitmap | Library internal |
| `src/api/options/*`, `src/model/layout-options.ts`, `series-options.ts`, `price-line-options.ts` | Typed defaults/customization including attribution | options → visual config | Keep attribution enabled; theme from our tokens |
| `tests/unittests/*`, `tests/type-checks/*`, `tests/e2e/graphics/*`, `interactions/*`, `memleaks/*` | API, rendering, type, interaction, leak regression | fixtures/browser → snapshots/assertions | Evidence of strong dependency quality; app still needs wrapper tests |
| `package.json`, `rollup.config.js`, `tsconfig*.json`, `BUILDING.md` | Library build/toolchain | source → package | Do not reproduce; consume npm package |

## 6. `qlib-main`

### Repository assessment

- **Strengths:** explicit time segments, fit windows for processors, model/dataset interfaces, rolling task generation, experiment artifacts, rich cost-aware backtesting.
- **Weaknesses:** alpha-status general platform with heavy optional/runtime dependencies (MLflow, Redis, MongoDB, cvxpy, gym, LightGBM), China-equity assumptions/examples, daily/cross-sectional orientation, and many deep/RL modules irrelevant to a single-index intraday laptop.
- **Leakage note:** processors warn that fit end must exclude test data, but configuration remains caller-controlled. Qlib does not make a misconfigured experiment leakage-proof.
- **Tests:** meaningful framework coverage, but not evidence for BANKNIFTY or our targets.
- **Verdict:** **conceptual reference only**, not a dependency.

| Path(s) | Purpose / units | Product decision |
|---|---|---|
| `qlib/data/dataset/handler.py`, `loader.py`, `processor.py`, `dataset.py` | Feature/label groups, time segments, fit/infer processors and loaders | Adapt immutable as-of datasets and train-only fitted transforms |
| `qlib/data/data.py`, `base.py`, `cache.py`, `storage/*` | Provider/cache/storage abstraction | Reject platform complexity; use explicit SQL/Parquet export |
| `qlib/model/base.py`, `trainer.py` | Common fit/predict and trainer contract | Adapt a small estimator interface |
| `qlib/contrib/model/linear.py`, `gbdt.py`, `xgboost.py` | Linear, LightGBM, XGBoost wrappers using train/valid segments | Model candidates; implement directly with sklearn/current packages |
| `qlib/workflow/exp.py`, `expm.py`, `recorder.py`, `record_temp.py` | MLflow experiments, parameters, metrics, artifacts | Adapt metadata/artifact manifest; avoid MLflow initially |
| `qlib/contrib/rolling/base.py`, `manager.py`, `gen.py`; `examples/model_rolling/task_manager_rolling.py` | Generate/train/collect chronological rolling tasks | Best rolling-evaluation reference; adapt |
| `qlib/backtest/backtest.py`, `executor.py`, `decision.py`, `signal.py` | Calendar-driven decisions and nested execution | Adapt event-time boundary; reject nested complexity |
| `qlib/backtest/exchange.py`, `account.py`, `position.py` | Tradability, deal price, costs, cash/positions | Adapt cost/accounting concepts with Indian charges |
| `qlib/backtest/report.py`, `contrib/evaluate.py` | Returns, cost, turnover, drawdown/risk/indicator reports | Adapt metrics; add classification/calibration/regime reports |
| `qlib/contrib/data/highfreq_handler.py`, `highfreq_processor.py`; `examples/highfreq/*` | Intraday handlers and train/test split example | Verify concepts; China session conventions do not transfer |
| `examples/benchmarks/{Linear,LightGBM,XGBoost}/*.yaml` | Config-driven dataset/model/record workflows | Adapt declarative run manifests; no wholesale configs |
| `qlib/contrib/model/pytorch_*`, `qlib/rl/*`, deep/RL example configs | Deep/RL research | Reject MVP for compute and evidence reasons |
| `tests/data/*`, `tests/model/*`, `tests/workflow/*`, `tests/backtest/*` | Framework contract coverage | Mine test categories, not fixtures/assumptions |

## 7. `qstrader-master`

### Repository assessment

MIT alpha-era event backtester with very old pins (pandas 0.18, numpy 1.10, Python 2 classifiers). Architecture is educational; implementation is not a safe dependency. It correctly streams chronologically through an event queue and separates signal/order/fill/portfolio, but `IBSimulatedExecutionHandler` explicitly fills immediately with no latency, slippage, or fill ratio. Synthetic price generator and sample strategies are not performance evidence. Tests cover price/portfolio math but not realistic execution. **Conceptual reference only.**

| Path(s) | Purpose / units | Decision |
|---|---|---|
| `qstrader/event.py`, `trading_session.py` | TICK/BAR→SIGNAL→ORDER→FILL event loop | Adopt strict time progression concept |
| `price_handler/base.py`, `generic.py`, `historic_csv_tick.py`, `iq_feed_intraday_csv_bar.py`, `yahoo_daily_csv_bar.py` | Chronological price iterators and last-known prices | Adapt deterministic replay and out-of-order validation |
| `price_handler/iterator/base.py`, `iterator/pandas/bar.py`, `tick.py` | DataFrame-to-event iterators | Adapt iterator contract; old Panel API rejected |
| `strategy/base.py`, `portfolio_handler.py`, `order/suggested.py` | Signals → sizing → risk → order queue | Adapt separation only for paper phase |
| `position_sizer/*`, `risk_manager/*` | Sizing/risk interfaces | Later paper-trading concepts |
| `execution_handler/base.py`, `ib_simulated.py` | Naive fills and IB commission | Reject fill assumptions; implement spread/slippage/latency |
| `portfolio.py`, `position.py`, `price_parser.py` | Cash, positions, realized/unrealized P&L, fixed precision | Adapt accounting tests; use decimal/paise and Indian rules |
| `statistics/performance.py`, `simple.py`, `tearsheet.py`, `report.py` | Sharpe/drawdown/reporting | Adapt drawdown/stability; beware TODOs and frequency assumptions |
| `compliance/base.py`, `example.py` | Append-only fill log | Adapt immutable audit log |
| `sentiment_handler/*` | Date-gated sentiment events | Later reference only |
| `scripts/generate_simulated_prices.py`, `examples/*` | Synthetic feed and demo strategies | Reject as validation evidence |
| `tests/test_price_handler.py`, `test_portfolio*.py`, `test_position.py`, `test_statistics.py` | Deterministic component tests | Adapt test categories |
| `requirements.txt`, `setup.py` | Obsolete dependency/package metadata | Reject dependency |

## 8. `finBERT-master`

### Repository assessment and map

Apache-2.0 research code for financial-text positive/negative/neutral sentiment. It is not evidence that news predicts BANKNIFTY. Dependencies are severely stale (Python 3.7, Torch 1.1, pandas 0.23, sklearn 0.21); code mixes legacy `pytorch_pretrained_bert` and modern `transformers`; `FinBert.prepare_model` references `self.base_model` although configuration owns `base_model`; the Flask entry downloads NLTK data at startup, uses a fixed model path and permissive CORS; no automated tests are present. **Later-phase reference only; use a maintained model/package if approved, not this application.**

| Path(s) | Purpose / flow | Decision |
|---|---|---|
| `finbert/finbert.py` | `Config`, `FinBert`, train/evaluate/predict, softmax sentiment confidence | Later concept; reject code and raw probability-as-confidence |
| `finbert/utils.py` | TSV processors, tokenization/features, class metrics | Adapt label/metric semantics only |
| `scripts/datasets.py` | Random train/test/valid split of phrases | Acceptable for iid phrases but reject for market-time validation |
| `scripts/predict.py` | CLI text→sentence probabilities CSV | Later batch interface concept |
| `main.py` | Flask POST scoring server | Reject insecure/stale wrapper |
| `config.json`, `environment.yml`, `Dockerfile` | BERT architecture and obsolete environment/image | Reject |
| `notebooks/*` | Training demonstrations | Research provenance only; not production |
| `examples.csv`, `test.txt` | Samples | Synthetic/demo, no architectural value |

## Cross-repository conclusions

### Best reference by required component

| Component | Best reference | Use |
|---|---|---|
| Zerodha auth/REST/history/ticks/errors | `pykiteconnect-master` | Current official package + current official docs |
| Instrument mapping and selected option universe | OpenAlgo Zerodha master-contract and option services | Reimplement minimal concepts |
| Reconnect/subscription state | pykiteconnect `ticker.py`; OpenAlgo adapter | SDK plus our persisted gap/audit state |
| Historical research workflow | Qlib dataset/rolling/workflow modules | Small custom chronological pipeline |
| Leakage-resistant replay | QSTrader event loop + Qlib train segments | Custom as-of replay, stronger invariants |
| Paper accounting | QSTrader portfolio plus OpenAlgo sandbox | Custom cost-aware ledger |
| Dashboard composition | TradingBoard | UI layout only |
| Compact cards | trading-dashboard | Visual hierarchy only |
| Candles/volume/markers | Lightweight Charts | Published npm dependency |
| News sentiment | FinBERT | Later feasibility reference only |

### Licence warnings

1. **Largest risk: OpenAlgo AGPL-3.0.** Do not copy, translate, link, subclass, import, or derive product code/architecture wholesale. Keep only independently expressed concepts and this factual map. Obtain legal advice before any reuse.
2. Lightweight Charts is Apache-2.0 but its NOTICE/README requires visible TradingView creator attribution and link. Keep `attributionLogo` enabled or provide equivalent notice/link.
3. MIT/Apache notices must be preserved for actual dependencies/distributions. Dataset and broker-market-data rights are separate from source licences.
4. FinBERT’s model/datasets have their own provenance/access terms; Apache on code does not grant Reuters/Financial PhraseBank data rights.

### Duplicated functionality

- Broker REST/streaming is duplicated by OpenAlgo’s custom Zerodha client and official pykiteconnect: use the official SDK.
- Event/backtest/accounting appears in Qlib, QSTrader, and OpenAlgo sandbox: build one small replay/evaluation path.
- Dashboard/chart patterns appear in both UI repos and OpenAlgo frontend: take only information architecture.
- Symbol/option handling appears in OpenAlgo services and broker plugin layers: centralize one canonical instrument table.
- Experiment/reporting overlaps Qlib workflow and backtests: use one run manifest plus PostgreSQL metadata/artifacts.

### Missing across all repositories

The biggest missing component is a trustworthy, point-in-time BANKNIFTY dataset—especially timestamped ATM/near-strike option quote/OI/depth snapshots with daily instrument-master history. No repository supplies it. Also absent is an end-to-end, calibrated four-horizon UP/DOWN/NO-TRADE engine with purged walk-forward evaluation, Indian fee model, immutable prediction expiry evaluation, and model stop criteria.

### Exclude from future active context

- Exclude `finBERT-master` until a separately approved news phase.
- Exclude `trading-dashboard-master` after extracting visual hierarchy.
- Exclude `qstrader-master` once the replay/accounting specification is implemented.
- Exclude most of `qlib-main`; retain only dataset processors, rolling workflow, lightweight model wrappers, and backtest reporting concepts.
- Exclude all OpenAlgo brokers other than Zerodha and all order/GTT/strategy/MCP/messaging surfaces.
- Do not load Lightweight Charts internals during app work; use its public v5 API documentation and npm package.

