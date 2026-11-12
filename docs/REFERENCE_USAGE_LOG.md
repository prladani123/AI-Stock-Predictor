# Reference usage log

## Stage G2B - Context V3 design influences

Official Nifty Indices: source-backed BANKNIFTY membership; official NSE CAS: phase boundaries; Upstox: NIFTY50/VIX/global context; Dhan: historical ATM-relative OI/IV/options; HAR/realized-volatility research: multiscale volatility features; order-flow/queue-imbalance research: future prospective Stage H. These are established design inputs, not new source retrievals. No reference repository code was inspected or copied in this sprint.

This log records the exact read-only reference inspection performed for the Phase 0–7 verification. Reference repositories were not imported, linked, executed as application code, or modified.

## qlib-main

Repository root: `all github projects/qlib-main/qlib-main`

Licence: MIT (`LICENSE` inspected).

Reason consulted: chronological segmentation, train-only processors, estimator contracts, rolling tasks, validation-driven LightGBM organisation, and experiment metadata.

Directories inspected:

- `qlib/data/dataset/`
- `qlib/model/`
- `qlib/contrib/model/`
- `qlib/contrib/rolling/`
- `qlib/workflow/`
- `tests/data_mid_layer_tests/`

Files and components inspected:

| File | Components | Purpose and disposition |
|---|---|---|
| `LICENSE` | MIT terms | Recorded; no source copied. |
| `qlib/data/dataset/handler.py` | `DataHandlerABC`, `DataHandler`, `DataHandlerLP.fit`, `fit_process_data`, `_run_proc_l` | Fit/process separation adapted into explicit train-only preprocessing. Heavy handler/cache design rejected. |
| `qlib/data/dataset/processor.py` | `Processor`, `DropnaProcessor`, `MinMaxNorm`, `ZScoreNorm.fit/__call__`, `RobustZScoreNorm` | Explicit fit interval and train-only statistics adopted. Cross-sectional/pandas processors rejected. |
| `qlib/model/base.py` | `BaseModel`, `Model`, `ModelFT` | Small common fit/predict interface adapted; Qlib inheritance/runtime rejected. |
| `qlib/contrib/model/linear.py` | `LinearModel.fit`, `_fit`, `predict` | Separate prepared train segment and deterministic linear estimator concept adapted. Regression-specific implementation rejected. |
| `qlib/contrib/model/gbdt.py` | `LGBModel._prepare_data`, `fit`, `predict`, `finetune` | Ordered train/validation datasets and validation early stopping adapted. Qlib recorder/global runtime rejected. |
| `qlib/contrib/rolling/base.py` | `Rolling.get_task_list`, `_train_rolling_tasks`, `_ens_rolling` | Chronological task manifests and truncation for leakage prevention adapted. Destructive experiment deletion and ensemble framework rejected. |
| `qlib/workflow/recorder.py` | `Recorder`, `save_objects`, `load_object`, `MLflowRecorder` | Parameter/metric/artifact lineage concept adapted into local checksummed manifests. MLflow and unrestricted pickle loading rejected. |
| `tests/data_mid_layer_tests/test_processor.py` | `TestProcessor`, normalization tests | Inspired train-statistics regression tests. |
| `tests/data_mid_layer_tests/test_dataset.py` | `TestDataset`, `TestTSDataSampler` | Inspired chronological slice/order tests. |
| `tests/test_workflow.py` | `WorkflowTest` | Inspired artifact-location and run-metadata tests. |

Concepts extracted: chronological partitions, fit-only-on-training transforms, estimator interface, validation-only tuning, rolling task manifests, immutable experiment metadata.

Implementation impact:

- Phase 0–5: `features.py`, `replay.py`, `research_models.py`.
- Phase 6–7: model preparation, training, artifact, calibration, and comparison modules plus their tests and migrations.

Dependency decision: Qlib is not a dependency.

Licence handling: MIT concepts were independently re-expressed. No Qlib source, structure, serialized object, or runtime component was copied.

## qstrader-master

Repository root: `all github projects/qstrader-master/qstrader-master`

Licence: MIT (`LICENSE` inspected).

Reason consulted: strict event chronology, advancing a replay clock, separating decisions from later outcomes, and deterministic handler tests.

Directories inspected:

- `qstrader/`
- `qstrader/price_handler/`
- `qstrader/execution_handler/`
- `tests/`

Files and components inspected:

| File | Components | Purpose and disposition |
|---|---|---|
| `LICENSE` | MIT terms | Recorded; no source copied. |
| `qstrader/event.py` | `Event`, `TickEvent`, `BarEvent`, `SignalEvent`, `OrderEvent`, `FillEvent` | Immutable event separation studied; only prediction/outcome separation adapted. Order/fill concepts deferred. |
| `qstrader/trading_session.py` | `TradingSession._continue_loop_condition`, `_run_session`, `start_trading` | Minute-by-minute/event-by-event clock progression adapted. Realtime polling architecture rejected. |
| `qstrader/price_handler/base.py` | `AbstractPriceHandler`, `_store_event`, last timestamp/price methods | Bounded last-known state concept adapted into `AsOfDataView`. |
| `qstrader/price_handler/generic.py` | `AbstractGenericHandler.stream_next`, `GenericPriceHandler` | Iterator exhaustion and one-event advancement adapted conceptually. |
| `qstrader/execution_handler/base.py` | `AbstractExecutionHandler` | Separation boundary inspected; no execution code adopted. |
| `qstrader/execution_handler/ib_simulated.py` | `IBSimulatedExecutionHandler.execute_order` | Immediate-fill assumptions explicitly rejected. |
| `tests/test_price_handler.py` | streaming, subscription and last-price tests | Inspired deterministic replay-clock tests. |
| `tests/test_portfolio_handler.py` | signal/order/fill separation mocks | Later paper-phase concept only; deferred. |

Concepts extracted: chronological event consumption, bounded clock, immutable decision then later outcome, deterministic iterator testing.

Implementation impact: `replay.py`, Phase 6/7 model replay, immutable prediction/outcome tests.

Dependency decision: QSTrader is not a dependency; its obsolete dependency stack and execution assumptions were rejected.

Licence handling: MIT concepts only; no source copied.

## openalgo-main

Repository root: `all github projects/openalgo-main/openalgo-main`

Licence: GNU AGPL-3.0 (`License.md` inspected).

Reason consulted: fail-closed broker boundaries, instrument master concepts, audit/health separation, analyser/sandbox separation, and Docker health ideas.

Directories inspected:

- `broker/zerodha/api/`
- `broker/zerodha/database/`
- `utils/`
- `database/`
- `services/`
- `test/`
- repository Compose configuration

Files and components inspected:

| File | Components | Purpose and disposition |
|---|---|---|
| `License.md` | AGPL-3.0 terms | Copyleft boundary recorded; no source copied. |
| `broker/zerodha/api/data.py` | `get_api_response`, `BrokerData.get_history`, permission/API exceptions | Read/history boundary and explicit error mapping studied. Custom HTTP client and broad market API rejected in favour of official SDK wrapper. |
| `broker/zerodha/database/master_contract_db.py` | `SymToken`, `copy_from_dataframe`, `master_contract_download`, `search_symbols` | Canonical instrument metadata concept adapted. Destructive table replacement, pandas dependency, token-as-identity risks rejected. |
| `utils/health_monitor.py` | `collect_metrics`, collector lifecycle | Fail-closed health evidence concept noted; background monitoring deferred. |
| `database/analyzer_db.py` | `AnalyzerLog`, `to_dict`, asynchronous log function | Timestamped audit record concept adapted to immutable research metadata. Order-oriented schema rejected. |
| `services/analyzer_service.py` | analyser status/toggle functions | Analysis/execution separation studied. Sandbox/execution imports explicitly rejected. |
| `test/test_analyzer_toggle_restriction.py` | `test_analyzer_toggle_restriction` | Fail-closed mode restriction idea adopted; network-style integration test structure rejected. |
| `docker-compose.yaml` | service health/environment boundaries | Health-check and explicit environment ideas adapted; Flask/WebSocket/multi-service topology rejected. |

Concepts extracted: canonical instruments separate from tokens, fail-closed configuration, explicit read/execute capability boundary, audit lineage, health checks.

Implementation impact: `zerodha.py`, `instruments.py`, `config.py`, Docker files, repository/migration immutability checks, safety tests.

Dependency decision: OpenAlgo is not a dependency and is not linked at runtime.

Licence handling: no AGPL code, function, class, schema, route, or architecture was copied. Only independently expressed concepts were used.

## pykiteconnect-master

Repository root: `all github projects/pykiteconnect-master/pykiteconnect-master`

Licence: MIT (`LICENSE` inspected).

Reason consulted: authoritative Zerodha method signatures, historical candle shape, exception taxonomy, reconnection concepts, and mocked unit-test boundaries.

Directories inspected:

- `kiteconnect/`
- `tests/unit/`

Files and components inspected:

| File | Components | Purpose and disposition |
|---|---|---|
| `LICENSE` | MIT terms | Recorded. |
| `kiteconnect/connect.py` | `KiteConnect.instruments`, `historical_data`, `_format_historical`, `_request`; order methods reviewed only to exclude | Exact two-method adapter contract and OHLCVOI response shape adopted through the published package. All order/portfolio/GTT capabilities excluded. |
| `kiteconnect/ticker.py` | `KiteTickerClientFactory`, retry limits and reconnect callbacks | Later live-phase concept only; deliberately deferred. |
| `kiteconnect/exceptions.py` | `TokenException`, `PermissionException`, `InputException`, `DataException`, `NetworkException` | Exception taxonomy adapted into authentication/rate-limit/transient/permanent import retry classification. |
| `tests/unit/test_connect.py` | mocked `test_instruments`, exchange instruments tests; order tests identified but not run | Mocked REST contract style adapted. Order tests explicitly not executed. |
| `tests/unit/test_ticker.py` | `TestTicker` | Inspected and deferred with live WebSockets. |
| `tests/unit/test_exceptions.py` | `test_wrong_json_response`, `test_native_exceptions` | Inspired fail-closed exception classification tests. |

Concepts extracted: exact historical/instrument contracts, response parsing, SDK exception categories, mocked HTTP tests.

Implementation impact: `zerodha.py`, `rate_limit.py`, `importer.py`, `test_zerodha_safety.py`, `test_importer.py`.

Dependency decision: the published pinned `kiteconnect` package remains the sole broker dependency.

Licence handling: MIT dependency usage; package licence remains with the dependency. No repository source copied into the product.

## Intentionally deferred repositories

- `lightweight-charts-master`: deferred because this task has no frontend/chart work.
- `tradingboard-main`: deferred because React/dashboard work is outside Phase 0–7.
- `trading-dashboard-master`: deferred because frontend composition is outside scope.
- `finBERT-master`: deferred because news/sentiment AI is expressly excluded.

## 2026-09-19 - Market Memory V2 and news intelligence

The YoloVest reference was inspected read-only. Exact files: `news/base.py`,
`news/aggregator.py`, `news/nse_official.py`, `news/moneycontrol.py`,
`news/et_markets.py`, `news/livemint.py`, `data/news_features.py`, and
`data/db/news_market.py` under `backend/src/yolovest/`. Relevant scraper tests
were consulted only where needed to understand provider behavior. Independently
adapted concepts: source interface, concurrent failure isolation, publication
time, deduplication, NSE session warmup/rate limits, source health, and
historical-as-of filtering. No code was copied and no reference package is
imported. Trading, portfolio, LLM, and broad-universe components were rejected.
`finBERT-master` remained deferred and was not integrated: this sprint uses
deterministic classification and adds no sentiment model or LLM dependency.

## Inspection totals and confirmation

| Repository | Reference files inspected |
|---|---:|
| qlib-main | 11 |
| qstrader-master | 9 |
| openalgo-main | 8 |
| pykiteconnect-master | 7 |

Reference concepts used: chronological segments, train-only transforms, validation-only tuning, rolling manifests, bounded replay clocks, delayed outcomes, fail-closed configuration, canonical instruments, broker capability restriction, SDK exception classification, and immutable artifact metadata.

Confirmations:

- No reference repository was modified.
- No AGPL source was copied, translated, imported, linked, or reproduced.
- Qlib, QSTrader and OpenAlgo are not application dependencies.
- All implementation changes are confined to `our-product`.

## Integrity-repair influence addendum

The final repair applied the inspected concepts to these exact product files:

- Qlib: `model_training.py`, `ml_models.py`, `ml_walk_forward.py`,
  `calibration.py`, `cli.py`, migrations `006`–`008`, and their model,
  calibration, workflow, migration, and repository tests.
- QSTrader: `replay.py`, `ml_walk_forward.py`, `ml_records.py`,
  `repository.py`, and the delayed immutable prediction/outcome tests.
- OpenAlgo: `zerodha.py`, `instruments.py`, `config.py`, Docker files, the
  computed audit in `cli.py`, `repository.py`, migration `008`, and
  safety/immutability tests.
- PyKiteConnect: `zerodha.py`, `rate_limit.py`, `importer.py`,
  `test_zerodha_safety.py`, `test_importer.py`, and the forbidden-call AST
  audit.

This addendum changes no inspection total: Qlib 11 files, QSTrader 9,
OpenAlgo 8, and PyKiteConnect 7.

## Phase 8 live-market-data inspection (2026-08-27)

All files below were opened read-only from the resolved roots. OpenAlgo was used
as an architecture reference only; PyKiteConnect remains the installed SDK. No
reference source, schema, route, parser, class, or function was copied.

### OpenAlgo Phase 8 evidence

Resolved root: `all github projects/openalgo-main/openalgo-main`  
Licence: GNU AGPL-3.0 (`License.md`, previously inspected).  
Decision for every row: **adapt concepts only** unless explicitly rejected.

| Exact path | Exact components inspected | Why / concept / decision | Product files influenced |
|---|---|---|---|
| `broker/zerodha/streaming/zerodha_websocket.py` | `ZerodhaWebSocket.start`, `subscribe_tokens`, `_process_pending_subscriptions`, `_subscribe_batch`, `unsubscribe`, `_on_ws_open`, `_on_ws_error`, `_on_ws_close`, `_health_check_loop`, `_resubscribe_all`, parser boundary | Connection lifecycle, batched desired subscriptions, health/stale state, and resubscription were adapted. Its custom binary parser, token refresh, threads, and raw WebSocket implementation were rejected in favor of official `KiteTicker`. | `live_adapters.py`, `live.py` |
| `broker/zerodha/streaming/zerodha_adapter.py` | `ZerodhaWebSocketAdapter.connect`, `subscribe`, `unsubscribe`, `get_subscriptions`, `_handle_ticks`, `_transform_tick`, `_transform_index_tick`, `_transform_regular_tick`, connection callbacks | Symbol/token mapping, normalized tick distinctions, idempotent subscription tracking, and callback separation were studied. Multi-mode topic publication, ZMQ, and generic broker inheritance were rejected. | `live_adapters.py`, `live.py`, `live_models.py` |
| `broker/zerodha/streaming/zerodha_mapping.py` | `ZerodhaExchangeMapper`, `ZerodhaCapabilityRegistry`, `ZerodhaDataTransformer.transform_tick` | Explicit exchange/mode mapping and normalization were adapted. Order/position transforms were inspected only to exclude and were rejected. | `live_adapters.py`, `live.py` |
| `websocket_proxy/connection_manager.py` | `ConnectionPool.connect`, `_get_adapter_with_capacity`, `subscribe`, `_subscribe_inner`, `unsubscribe`, `unsubscribe_all`, `disconnect`, `get_stats`, `subscriptions` | Desired/active state, idempotency, capacity, and recovery concepts were adapted. Browser proxy, connection pooling, ZeroMQ, and multi-broker abstractions were rejected. | `live.py::SubscriptionRegistry`, `ConnectionState` |
| `services/option_symbol_service.py` | `get_atm_strike`, `get_available_strikes`, `find_atm_strike_from_actual`, `calculate_offset_strike_from_actual`, `get_option_symbol` | Actual persisted strikes and nearest-ATM selection were adapted. Guessed symbol construction and caches were rejected. | `live.py::ContractSelector` |
| `services/option_chain_service.py` | `get_strikes_with_labels`, `get_option_symbols_for_chain`, `get_option_chain` | Small offset-labelled CE/PE sets and missing quote handling were studied. Full-chain/batch API design was rejected. | `live.py::ContractSelector`, `live_simulation.py` |
| `services/expiry_service.py` | `get_expiry_dates` | Broker-master expiry discovery and sorted eligibility were adapted; request/database service coupling was rejected. | `live.py::ContractSelector` |
| `services/quotes_service.py` | `validate_symbol_exchange`, `validate_symbols_bulk`, `get_quotes_with_auth`, `get_multiquotes_with_auth` | Missing/partial quote validation and explicit symbol/exchange identity were adapted. Dynamic broker imports and HTTP response design were rejected. | `live.py::TickNormalizer` |
| `services/depth_service.py` | `validate_symbol_exchange`, `get_depth_with_auth`, `get_depth` | Normalized depth and missing-data validation were adapted. Dynamic modules and route response format were rejected. | `live_models.py::DepthLevel`, `live.py::TickNormalizer` |
| `services/market_data_service.py` | `ConnectionStatus`, `HealthStatus`, `MarketDataValidator.validate`, `ConnectionHealthMonitor`, `MarketDataService.process_market_data`, `is_data_fresh`, `get_market_depth` | Explicit connection health, stale validation, and bounded latest-data state were adapted. Singleton/global caches and trade-management coupling were rejected. | `live.py::ConnectionState`, `TickNormalizer`, `LiveCollector` |
| `services/multi_strike_oi_service.py` | `get_multi_strike_oi_data` | Strike grouping and missing-strike reporting were adapted conceptually. Its response formula and API orchestration were not copied. | `live.py::ContractSelector`, option snapshots |
| `services/oi_tracker_service.py` | `_get_nearest_futures_price`, `get_oi_data`, `calculate_max_pain` | Timestamped OI comparison was studied. Max-pain and signal formulas were rejected; Phase 8 uses current accepted OI minus previous accepted minute OI only. | `live.py::MinuteBuilder` |
| `services/oi_profile_service.py` | `_find_futures_symbol`, `_fetch_daily_oi_changes`, `get_oi_profile_data` | Exact future-contract resolution and unavailable OI handling were studied. Daily profile/ranking calculations were deferred. | `live.py::ContractSelector`, `MinuteBuilder` |
| `broker/zerodha/database/master_contract_db.py` | `SymToken`, `download_csv_zerodha_data`, `process_zerodha_csv`, `master_contract_download`, `search_symbols` | Reconfirmed that selection must derive from a snapshot rather than guessed tokens/symbols. Destructive replacement and pandas design were rejected. | `live_models.py::LiveInstrument`, `InstrumentTokenMap`, `ContractSelector` |
| `broker/zerodha/api/data.py` | `BrokerData.get_history`, quote/depth response handling, exception paths | Reconfirmed instrument identity and read-only response validation. Custom request/service coupling was rejected. | existing `zerodha.py`; Phase 8 normalizer and safety tests |

Phase 8 OpenAlgo files inspected: **15**. Thirteen are newly inspected files;
`master_contract_db.py` and `data.py` were re-inspected. Cumulative unique OpenAlgo
reference files in this log: **21**. AGPL handling: concepts were independently
implemented; OpenAlgo is not imported, linked, depended upon, or modified.

### PyKiteConnect Phase 8 evidence

Resolved root: `all github projects/pykiteconnect-master/pykiteconnect-master`  
Licence: MIT (`LICENSE`, previously inspected).

| Exact path | Exact components inspected | Why / decision | Product files influenced |
|---|---|---|---|
| `kiteconnect/ticker.py` | `KiteTicker.__init__`, `connect`, `is_connected`, `close`, `stop_retry`, `subscribe`, `unsubscribe`, `set_mode`, `resubscribe`, `_on_connect`, `_on_close`, `_on_error`, `_on_message`, `_on_open`, `_on_reconnect`, `_on_noreconnect`, `_parse_binary` | Official callback signatures, reconnect behavior, full mode, OI/timestamps/OHLC and five-level depth were adopted through the dependency. Internal binary parsing was not reproduced. | `live_adapters.py::KiteTickerLiveAdapter`, `live.py::TickNormalizer` |
| `kiteconnect/connect.py` | `instruments`, `quote`, `ohlc`, `ltp`, `historical_data`, `_format_historical`; trading methods reviewed only as forbidden surface | Existing REST adapter remains restricted to instruments/history. Phase 8 streaming uses no broad `KiteConnect` facade. | existing `zerodha.py`, live safety tests |
| `kiteconnect/exceptions.py` | `KiteException`, `TokenException`, `PermissionException`, `InputException`, `DataException`, `NetworkException`; `OrderException` identified only to exclude | Authentication/permanent/transient state concepts were retained; trading exception paths are unreachable. | existing importer classification; live state design |
| `tests/unit/test_ticker.py` | `TestTicker.test_autoping`, `test_sendclose`, `test_sendMessage` | Mocked protocol/callback philosophy was adapted to a no-network fake ticker and simulator. | `tests/test_live_phase8.py` |
| `tests/unit/test_connect.py` | read-only `test_instruments`, `test_instruments_exchangewise`; trading tests identified but not run | Mocked REST contract style and capability exclusion were reconfirmed. | live/REST safety tests |

Phase 8 PyKiteConnect files inspected: **5** (all re-inspected from the prior
seven-file set). Cumulative unique PyKiteConnect files remain **7**. The pinned
published SDK is used under MIT; no repository source was copied or modified.

Qlib and QSTrader were not consulted for Phase 8 because the existing
point-in-time and event-order boundaries were sufficient. Their prior inspection
totals remain 11 and 9. Lightweight Charts, TradingBoard, Trading Dashboard,
and FinBERT remain deferred.

## Full product vertical slice inspection (2026-09-14)

All references were opened read-only. Concepts were independently implemented;
no source was copied.

### TradingBoard

Root: `all github projects/tradingboard-main/tradingboard-main` (MIT).
Exact 7 files: `src/App.tsx` (`App`, `PanelGroup`, persistent sizes),
`src/components/Header.tsx` (`Header`), `src/components/ChartPanel.tsx`
(`ChartPanel`), `src/components/Watchlist.tsx` (`Watchlist` handlers and states),
`src/components/TabbedInfoPanel.tsx`, `src/store/marketStore.ts` (`MarketStore`),
and `src/index.css` (theme/status tokens). Adopted/adapted: strong shell,
information-dense panels, clear selection/status, reusable state surfaces.
Rejected: widget/fake market service, IndexedDB watchlists, hidden attribution,
and extra resizable dependency. Influenced `Shell.tsx`, `States.tsx`,
`PredictionCard.tsx`, `pages.tsx`, and `styles.css`.

### Trading Dashboard

Root: `all github projects/trading-dashboard-master/trading-dashboard-master`
(MIT). Exact 3 files: `src/App.js` (`TradingDashboard`, metric cards,
watchlist), `src/App.css`, and `src/index.css`. Compact metrics and responsive
cards were adapted. Hard-coded prices, fake data, global watchlist, and Recharts
were rejected. Influenced Dashboard composition, `MetricCard`, and `styles.css`.

### Lightweight Charts

Root: `all github projects/lightweight-charts-master/lightweight-charts-master`
(Apache-2.0). Exact 4 files: `website/tutorials/how_to/price-and-volume.js`
(overlay histogram), `website/tutorials/how_to/series-markers.js`
(`createSeriesMarkers`), `tests/e2e/coverage/test-cases/chart/resize.js`, and
`tests/type-checks/series-markers.ts` (v5 types). The published package
`lightweight-charts@5.2.1`, not repository source, is used in
`MarketChart.tsx`. Candles, volume, crosshair, auto-size and prediction markers
were independently built. TradingView attribution remains enabled.

### OpenAlgo

Root: `all github projects/openalgo-main/openalgo-main` (AGPL-3.0).
Exact 18 files inspected:

- `broker/zerodha/api/data.py` — `BrokerData` and market response shaping.
- `broker/zerodha/database/master_contract_db.py` — `SymToken`/snapshot concepts.
- `broker/zerodha/streaming/zerodha_websocket.py` — `ZerodhaWebSocket` lifecycle.
- `broker/zerodha/streaming/zerodha_adapter.py` — `ZerodhaWebSocketAdapter`.
- `broker/zerodha/streaming/zerodha_mapping.py` — mapping/capability/transform classes.
- `services/option_symbol_service.py` — actual strike and ATM helpers.
- `services/option_chain_service.py` — `get_strikes_with_labels`, `get_option_chain`.
- `services/expiry_service.py` — `get_expiry_dates`.
- `services/quotes_service.py` and `services/depth_service.py` — validation/shaping.
- `services/multi_strike_oi_service.py` and `services/oi_tracker_service.py` — OI presentation.
- `services/market_data_service.py` — `HealthStatus`, `MarketDataValidator`, `ConnectionHealthMonitor`.
- `restx_api/option_chain.py` — API/resource separation.
- `frontend/src/config/navigation.ts` — typed navigation and active-route utility.
- `frontend/src/components/layout/Layout.tsx` and `Navbar.tsx` — shell/status hierarchy.
- `frontend/src/components/ui/sidebar.tsx` — responsive sidebar state.

Adopted/adapted: health DTOs, service/API boundaries, actual strike/expiry
presentation, responsive navigation. Rejected: orders/positions, multi-broker
scope, ZeroMQ, dynamic broker imports, Flask coupling and full-chain complexity.
Influenced Symfony `PythonClient`/`ApiController`, Python `ProductRuntime`, and
React shell/live/options views. **AGPL source copied: NO.** OpenAlgo is not
imported or linked; no class, route, schema, JSX, or CSS was copied/translated.

### PyKiteConnect

Root: `all github projects/pykiteconnect-master/pykiteconnect-master` (MIT).
Exact 5 files re-inspected: `kiteconnect/connect.py`
(`instruments`, `historical_data`, `_format_historical`), `kiteconnect/ticker.py`
(`KiteTicker`, callbacks, `subscribe`, `unsubscribe`, `set_mode`, `resubscribe`),
`kiteconnect/exceptions.py`, `tests/unit/test_connect.py`, and
`tests/unit/test_ticker.py`. The official package remains the broker dependency.
Every order, portfolio, margin, and GTT surface remains excluded.

| Reference | Files inspected this task |
|---|---:|
| TradingBoard | 7 |
| Trading Dashboard | 3 |
| Lightweight Charts | 4 |
| OpenAlgo | 18 |
| PyKiteConnect | 5 |

MIT/Apache dependencies retain their licences; TradingView attribution is
visible; OpenAlgo concepts only were independently expressed. No reference
repository was modified.

## Big Move V2 focused inspection (2026-09-14)

Only the requested source was inspected; all implementation is independent.

### YoloVest - HIGH

- `backend/src/yolovest/data/features.py`: `compute_features`, `_realized_vol`, `compute_rsi`, `compute_macd`, `compute_bollinger_bands`, `compute_vwap` - adapted point-in-time return/volatility/trend/session ideas into `big_move_features.py`.
- `data/fno_features.py`: `compute_fno_features`, `_classify_buildup`; `data/fno_provider.py`; `data/vix_features.py` - adapted futures buildup and explicit pending VIX slots; no provider copied.
- `strategy/inference_features.py`, `ml_signal.py`, `walk_forward_backtest.py`; `skills/model_retrain.py`, `predict_track.py`, `drift_watch.py` - used chronological schema/gating/drift concepts for `big_move_models.py` and V2 lineage; execution, portfolio, news and broad-universe code rejected.

### Kronos - HIGH EXPERIMENTAL

- `README.md`; `model/kronos.py`: `KronosPredictor.predict`, `predict_batch`, `auto_regressive_inference`; `model/module.py` transformer/tokenizer primitives - adapted OHLC(V), bounded context, `pred_len` and sampled-path contracts into `kronos_experiment.py`; model internals rejected.
- `examples/prediction_example.py`, `examples/run_backtest_kronos.py`; `finetune_csv/README.md`, `train_sequential.py`, `finetune_base_model.py`, `config_loader.py`, `configs/config_ali09988_candle-5min.yaml` - adapted path comparison and mini/small experiment configuration; mandatory install and laptop fine-tuning rejected.

### Mr.Chartist - MEDIUM

- `src/lib/oiUtils.ts`: `calculatePCR`, `getATMZoneAnalysis`, `getStrikePCR`; `src/lib/gexData.ts`: `calculateExpectedMove`; `src/components/OIHeatmap.tsx`, `SupportResistance.tsx`, `ExpectedMoveWidget.tsx`; `src/pages/OIAnalysis.tsx`, `OptionChain.tsx` - adapted OI concentration, paired ATM-zone, wall distance and honest IV-unavailable presentation into `big_move_features.py`, `pages.tsx` and `MarketChart.tsx`; generated GEX/IV and standalone directional claims rejected.

### option-chain-live - MEDIUM DOMAIN MODEL

- `option_chain_live/models.py`: `ContractSide`, `ChainRow`, `OptionChain.atm_row`, `nearest_rows`, `ChainTotals`; `client.py`: `_side`, `_side_totals`; `watch.py`: `LiveWatcher`; `fyers_chain.py`: `parse_chain`, `_row_to_side` - adapted CE/PE pairing and aggregate shape into V2 option features; its unofficial upstream source and replacement of Zerodha were rejected.

### quantbot-public - LOW CONCEPT ONLY

- `README.md`, `notebooks/ML_training_example.ipynb`, `data/sample_data.csv`, `utils/feature_engineering.py` - used only its feature checklist for candle structure, trend, VWAP, OI and volume; random splitting, performance claims, execution design and code reuse were rejected because the public implementation is incomplete and has no evident licence.

Reference repositories changed: **NO**. YoloVest was not imported; Kronos remains optional; Zerodha remains canonical.

## Product workstation UI pass (2026-09-18)

- YoloVest `frontend/src/pages/DashboardPage.tsx` and `backend/src/yolovest/strategy/inference_features.py`: used compact state/evidence grouping and strict inference-feature parity; trading controls rejected. Influenced `pages/DashboardPage.tsx` and `internal_api.py`.
- Mr.Chartist `src/components/OIHeatmap.tsx`, `SupportResistance.tsx`, and `src/pages/OptionChain.tsx`: adapted mirrored OI bars, ATM emphasis, concentration levels, and dense chain layout using only our real snapshots. Influenced `OptionOIChart.tsx`, `OptionsPage.tsx`, and `product.css`; mock data rejected.
- Kronos `examples/prediction_example.py`: retained a visually distinct forecast-path overlay with an explicit experimental toggle. Influenced `components/charts/MarketChart.tsx`; no forecast is shown when output is absent.
- TradingBoard `src/App.tsx` and `src/components/ChartPanel.tsx`: used chart-first workstation hierarchy and compact contextual panels. Influenced `DashboardPage.tsx`; its external chart widget was not used.
- OpenAlgo `frontend/src/pages/OptionChain.tsx`: adapted tabular-number density, mirrored CE/PE columns, ATM row and relative OI emphasis. Order-dialog and execution code were rejected.

No reference source was copied, imported, or modified.
## 2026-09-18 — Market Memory V1

Implemented directly from the active product's point-in-time and reproducibility conventions. No reference repository code was copied. Daily evidence, checksums, state/outcome separation, and deterministic retrieval remain explicit.

## Stage G1 — historical research context (2026-09-20)

Design concepts supplied by the completed external research: Dhan rolling expired monthly options for historical ATM-relative OI/IV; Upstox Analytics for read-only VIX/global/market-information context; Bhav for deterministic caches, futures-aware ATM concepts and provider isolation (actual provider-aligned historical spot/strike is used here); FNSPID for time-aligned news/market datasets without assuming sentiment alone suffices; Indian corporate-announcement event studies for first-tradable-minute alignment after dissemination. No reference repositories were inspected or copied in this sprint. Zerodha live collection and the immutable PRICE_CORE_V2 feature definition remain unchanged.
