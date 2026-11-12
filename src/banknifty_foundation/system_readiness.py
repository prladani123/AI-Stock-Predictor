"""Read-only operational truth. Never fetch, migrate, train, approve or refresh."""
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path

from .calendar import IST
from .calendar_bootstrap import TradingCalendarBootstrapService
from .paths import memory_root


def enabled(name, default='false'):
    return os.getenv(name, default).lower() == 'true'


def database_health(database_url=None):
    url = database_url if database_url is not None else os.getenv('DATABASE_URL', '')
    if not url:
        return 'NOT_CONFIGURED'
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=2, options='-c statement_timeout=1000') as db:
            db.execute('SET TRANSACTION READ ONLY')
            return 'UP' if db.execute('SELECT 1').fetchone() == (1,) else 'DOWN'
    except Exception:
        return 'DOWN'


def runtime_configuration(simulated=False, worker=None):
    state = worker.status() if worker else {}
    return {'liveMarketEnabled': enabled('LIVE_MARKET_ENABLED'),
            'bigMoveRuntimeEnabled': enabled('BIG_MOVE_RUNTIME_ENABLED'),
            'newsPollingEnabled': enabled('NEWS_POLLING_ENABLED'),
            'memoryRollupEnabled': enabled('MEMORY_ROLLUP_ENABLED'),
            'optionStrikeWidth': int(os.getenv('LIVE_OPTION_STRIKE_WIDTH', '5')),
            'universeRefreshSeconds': int(os.getenv('LIVE_UNIVERSE_REFRESH_SECONDS', '300')),
            'newsPollSeconds': max(60, int(os.getenv('NEWS_POLL_INTERVAL_SECONDS', '90'))),
            'evidenceMinimumSample': max(1, int(os.getenv('EVIDENCE_MINIMUM_SAMPLE', '20'))),
            'rawTickRetentionDays': int(os.getenv('RAW_TICK_RETENTION_DAYS', '14')),
            'currentDataMode': 'SIMULATED' if simulated else 'LIVE' if state.get('connected') else 'OFFLINE'}


def feature_scope(schema):
    from .big_move_features import big_move_feature_definition
    definition = big_move_feature_definition()
    futures = set(definition['futures_features']) & set(schema)
    options = set(definition['option_features']) & set(schema)
    return 'DERIVATIVES_ENRICHED_V2' if futures and options else 'PARTIAL_DERIVATIVES_V2' if futures or options else 'PRICE_CORE_V2'


class SystemReadinessService:
    def __init__(self, database_url=None, root=None, live_worker=None, runtime_worker=None, news=None):
        self.url = database_url if database_url is not None else os.getenv('DATABASE_URL', '')
        self.root = Path(root) if root is not None else memory_root()
        self.live_worker, self.runtime_worker, self.news = live_worker, runtime_worker, news

    def memory(self):
        from .market_memory import FileMarketMemoryRepository
        from .memory_builder import load_intraday
        from .memory_enrichment import validate_minutes
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        entries = manifest['files']
        dates = {item['date'] for item in entries}
        repo = FileMarketMemoryRepository(self.root)
        actual = {day.trading_date.isoformat() for day in repo.days()}
        checksum_ok = len(entries) == len(dates) == 50
        for item in entries:
            path = (self.root / item['file']).resolve()
            checksum_ok = checksum_ok and path.is_relative_to(self.root.resolve()) and path.is_file() and sha256(path.read_bytes()).hexdigest() == item['checksum_sha256']
        rows = load_intraday(self.root)
        missing = sorted(day for day in dates if not validate_minutes(rows.get(day, []), date.fromisoformat(day))['complete'])
        events = repo.events()
        return {'canonicalDailyMemories': len(dates & actual), 'expected': 50,
                'minuteHistoryComplete': len(dates) - len(missing), 'minuteHistoryTarget': 50,
                'numericalEpisodes': len(repo.episodes()), 'structuredEvents': len(events),
                'reactionMeasurableEvents': sum(event.reaction.get('status') in ('REACTION_AVAILABLE', 'COMPLETE_30M') for event in events),
                'missingMinuteDates': missing, 'canonicalChecksumStatus': 'PASS' if checksum_ok else 'FAIL'}

    def snapshot(self, db, today):
        snapshot = db.execute('''SELECT id,COALESCE(last_verified_at,captured_at) AS captured_at
            FROM instrument_snapshots WHERE (COALESCE(last_verified_at,captured_at) AT TIME ZONE 'Asia/Kolkata')::date=%s
            ORDER BY COALESCE(last_verified_at,captured_at) DESC,id DESC LIMIT 1''', (today,)).fetchone()
        result = {'indexCount': 0, 'futureCandidates': 0, 'ceOptions': 0, 'peOptions': 0,
                  'optionStrikesPresent': 0, 'snapshotTimestamp': snapshot['captured_at'] if snapshot else None, 'status': 'MISSING'}
        if not snapshot:
            return result
        rows = db.execute('''SELECT exchange,tradingsymbol,instrument_type,expiry,strike FROM canonical_instruments
            WHERE snapshot_id=%s AND ((exchange='NSE' AND tradingsymbol IN ('NIFTY BANK','BANKNIFTY'))
                OR (exchange='NFO' AND name='BANKNIFTY' AND expiry>=%s))''', (snapshot['id'], today)).fetchall()
        result['indexCount'] = sum(row['exchange'] == 'NSE' for row in rows)
        for kind, key in [('FUT', 'futureCandidates'), ('CE', 'ceOptions'), ('PE', 'peOptions')]:
            result[key] = sum(row['instrument_type'] == kind and row['exchange'] == 'NFO' for row in rows)
        result['optionStrikesPresent'] = sum(row['instrument_type'] in ('CE', 'PE') and row['strike'] is not None and row['strike'] > 0 for row in rows)
        if result['indexCount'] == 1 and all(result[key] for key in ('futureCandidates', 'ceOptions', 'peOptions')) and result['optionStrikesPresent'] == result['ceOptions'] + result['peOptions']:
            result['status'] = 'READY'
        else:
            result['status'] = 'INCOMPLETE'
        return result

    def research(self, db):
        result = {'acceptedDatasetRevision': None, 'acceptedDatasetExists': False, 'researchInstrument': None,
                  'indexMinuteRows': 0, 'labels': {str(h): 0 for h in (10, 20, 30)}, 'featureRows': 0,
                  'featureDataStart': None, 'featureDataEnd': None, 'modelFeatureScope': 'PRICE_CORE_V2'}
        row = db.execute('''SELECT r.id,i.id AS instrument_id,i.tradingsymbol FROM dataset_revisions r
            JOIN dataset_revision_instruments ri ON ri.revision_id=r.id
            JOIN canonical_instruments i ON i.id=ri.instrument_id
            WHERE r.status='ACCEPTED' AND i.exchange='NSE' AND i.tradingsymbol IN ('NIFTY BANK','BANKNIFTY')
            ORDER BY r.created_at DESC,r.id DESC,i.id LIMIT 1''').fetchone()
        if not row:
            return result
        params = (row['id'], row['instrument_id'])
        result.update(acceptedDatasetRevision=str(row['id']), acceptedDatasetExists=True,
                      researchInstrument={'id': row['instrument_id'], 'symbol': row['tradingsymbol']})
        result['indexMinuteRows'] = db.execute('SELECT count(*) AS n FROM dataset_revision_candles WHERE revision_id=%s AND instrument_id=%s', params).fetchone()['n']
        labels = db.execute('''SELECT horizon_minutes,count(*) AS n FROM big_move_labels
            WHERE dataset_revision_id=%s AND instrument_id=%s GROUP BY horizon_minutes''', params).fetchall()
        result['labels'].update({str(item['horizon_minutes']): item['n'] for item in labels})
        from .big_move_features import FEATURE_SET_VERSION_V2, big_move_feature_definition
        features = db.execute('''SELECT count(*) AS n,min(as_of) AS start,max(as_of) AS end FROM feature_rows
            WHERE dataset_revision_id=%s AND instrument_id=%s AND feature_set_version=%s
                AND quality_status='PASS' AND source_max_timestamp<=as_of''', (*params, FEATURE_SET_VERSION_V2)).fetchone()
        result.update(featureRows=features['n'], featureDataStart=features['start'], featureDataEnd=features['end'])
        # Count actual numeric coverage, not keys containing null placeholders.
        definition = big_move_feature_definition()
        names = list(definition['futures_features']) + list(definition['option_features'])
        coverage = db.execute('''SELECT field.key,count(*) AS n FROM feature_rows f,
            LATERAL jsonb_each(f.feature_values) field WHERE f.dataset_revision_id=%s AND f.instrument_id=%s
                AND f.feature_set_version=%s AND f.quality_status='PASS' AND f.source_max_timestamp<=f.as_of
                AND field.key=ANY(%s) AND jsonb_typeof(field.value)='number' GROUP BY field.key''', (*params, FEATURE_SET_VERSION_V2, names)).fetchall()
        present = [item['key'] for item in coverage if features['n'] and item['n'] / features['n'] >= .9]
        result['modelFeatureScope'] = feature_scope(present)
        result['derivativesFeatureCoverage'] = {item['key']: item['n'] for item in coverage}
        return result

    def models(self, db):
        from .big_move_runtime import BigMoveArtifactStore
        store = BigMoveArtifactStore()
        result = []
        for horizon in (10, 20, 30):
            rows = db.execute('''SELECT * FROM big_move_model_definitions WHERE horizon_minutes=%s
                AND model_family='BIG_MOVE_V2' ORDER BY (status='APPROVED') DESC,created_at DESC,model_version DESC''', (horizon,)).fetchall()
            move = next((row for row in rows if row['stage'] == 'MOVE_DETECTOR'), None)
            item = {'horizonMinutes': horizon, 'candidateExists': any(row['status'] == 'CANDIDATE' for row in rows),
                    'evaluationExists': False, 'comparisonExists': False, 'approvalDecision': None,
                    'approvedModelExists': False, 'artifactExists': False, 'artifactChecksumValid': False,
                    'artifactLoadIntegrity': False, 'decisionThreshold': None, 'featureSchema': [],
                    'featureScope': 'PRICE_CORE_V2', 'status': 'BLOCKED'}
            if move:
                manifest = move['manifest']
                candidate = manifest.get('approved_from_model_checksum', move['model_checksum'])
                approval = db.execute('''SELECT * FROM big_move_approval_decisions WHERE candidate_model_checksum=%s
                    AND (%s::text IS NULL OR approval_checksum=%s) ORDER BY created_at DESC LIMIT 1''',
                    (candidate, manifest.get('approval_decision_checksum'), manifest.get('approval_decision_checksum'))).fetchone()
                evaluation = db.execute('''SELECT evaluation_checksum FROM big_move_evaluation_runs WHERE candidate_model_checksum=%s
                    AND (%s::text IS NULL OR evaluation_checksum=%s) ORDER BY created_at DESC LIMIT 1''',
                    (candidate, manifest.get('evaluation_checksum'), manifest.get('evaluation_checksum'))).fetchone()
                comparison = db.execute('''SELECT comparison_checksum FROM big_move_model_comparisons
                    WHERE dataset_revision_id=%s AND horizon_minutes=%s AND instrument_id=%s
                    AND (%s::text IS NULL OR comparison_checksum=%s) ORDER BY created_at DESC LIMIT 1''',
                    (move['dataset_revision_id'], horizon, manifest.get('instrument_id'),
                     approval['comparison_checksum'] if approval else None, approval['comparison_checksum'] if approval else None)).fetchone()
                paired = next((row for row in rows if row['stage'] == 'DIRECTION'
                    and all(row[key] == move[key] for key in ('model_version', 'artifact_checksum', 'dataset_revision_id', 'feature_set_version', 'status'))), None)
                approved = bool(move['status'] == 'APPROVED' and paired and approval and approval['decision'] == 'PASS'
                                and evaluation and approval['evaluation_checksum'] == evaluation['evaluation_checksum'] and comparison)
                path = Path(move['artifact_path']).resolve()
                exists = path.is_relative_to(store.root) and path.is_file()
                valid = exists and sha256(path.read_bytes()).hexdigest() == move['artifact_checksum']
                loaded = False
                if valid:
                    try:
                        store.load(move)
                        if paired:
                            store.load(paired)
                        loaded = True
                    except Exception:
                        pass  # Never echo artifact contents or filesystem paths.
                item.update(evaluationExists=bool(evaluation), comparisonExists=bool(comparison),
                    approvalDecision=approval['decision'] if approval else None, approvedModelExists=approved,
                    artifactExists=exists, artifactChecksumValid=bool(valid), artifactLoadIntegrity=loaded,
                    decisionThreshold=manifest.get('moveDecisionThreshold'), featureSchema=manifest.get('feature_schema', []),
                    featureScope=feature_scope(manifest.get('feature_schema', [])), modelVersion=move['model_version'])
                if approved and valid and loaded and item['decisionThreshold'] is not None:
                    item['status'] = 'READY'
            result.append(item)
        return result

    def runtime(self, db):
        last = db.execute('SELECT horizon_minutes,max(as_of) AS at FROM big_move_predictions GROUP BY horizon_minutes').fetchall()
        return {'enabled': enabled('BIG_MOVE_RUNTIME_ENABLED'),
                'workerState': 'RUNNING' if self.runtime_worker and self.runtime_worker.thread and self.runtime_worker.thread.is_alive() else 'STOPPED',
                'lastPrediction': {str(h): next((row['at'] for row in last if row['horizon_minutes'] == h), None) for h in (10, 20, 30)},
                'lastResolvedOutcome': db.execute('SELECT max(attached_at) AS at FROM big_move_prediction_outcomes').fetchone()['at'],
                'journalCount': db.execute('SELECT count(*) AS n FROM big_move_evidence_journal').fetchone()['n']}

    def live(self, now):
        from .live_product_reader import LiveProductDataReader
        state = LiveProductDataReader(self.url).current_state(now)
        worker = self.live_worker.status() if self.live_worker else {'state': 'UNAVAILABLE'}
        return {'enabled': enabled('LIVE_MARKET_ENABLED'), 'workerState': worker['state'],
                'latestIndexMinute': state.latest_at,
                'latestFuturesMinute': max((row.minute_at for row in state.minutes('FUT')), default=None),
                'latestOptionsSnapshot': max((row.minute_at for row in state.option_snapshots.values()), default=None),
                'activeSubscriptionCount': len(worker.get('activeSubscriptions', [])),
                'qualityState': state.quality, 'freshness': 'FRESH' if state.quality == 'PASS' else state.quality}

    def news_state(self):
        from .news.sources import configured_sources
        providers = [source.name for source in configured_sources()]
        result = {'pollingEnabled': enabled('NEWS_POLLING_ENABLED'), 'monitorStatus': 'UNAVAILABLE',
                  'providersConfigured': providers, 'providers': [], 'latestHighRelevanceEvent': None,
                  'reactionPendingCount': None, 'reactionCompleteCount': None}
        if self.news:
            status = self.news.status()
            result['monitorStatus'] = status['monitor']['state']
            health = {item['source']: item for item in status['sources']}
            result['providers'] = [{'source': name, 'state': health.get(name, {}).get('state', 'UNKNOWN'),
                                    'checkedAt': health.get(name, {}).get('checked_at')} for name in providers]
            events = self.news.repository.events(include_historical=False)
            high = sorted((event for event in events if event.relevance == 'HIGH' and event.first_seen_at), key=lambda event: event.first_seen_at, reverse=True)
            if high:
                event = high[0]
                result['latestHighRelevanceEvent'] = {'id': event.event_id, 'headline': event.headline_or_summary, 'firstSeenAt': event.first_seen_at}
            result['reactionCompleteCount'] = sum(event.reaction.get('status') == 'COMPLETE_30M' for event in events)
            result['reactionPendingCount'] = sum(event.predictive_eligible and event.reaction.get('status') not in ('COMPLETE_30M', 'UNMEASURABLE') for event in events)
        from .news.scheduled import scheduled_events
        try:
            schedule = scheduled_events(root=self.root)
            result['scheduledEvents'] = schedule['status']
            result['scheduledConfiguredEventCount'] = schedule['configuredEventCount']
            result['scheduledVisibleEventCount'] = schedule['visibleEventCount']
            result['scheduledContext'] = {
                'status': schedule['status'],
                'configuredEventCount': schedule['configuredEventCount'],
                'configuredDateRange': schedule['configuredDateRange'],
                'configuredEventTypes': schedule['configuredEventTypes'],
                'visibleEventCount': schedule['visibleEventCount'],
            }
        except (OSError, ValueError, TypeError):
            result['scheduledEvents'] = 'INVALID'
            result['scheduledConfiguredEventCount'] = None
            result['scheduledVisibleEventCount'] = None
            result['scheduledContext'] = {'status': 'INVALID', 'configuredEventCount': None,
                'configuredDateRange': [], 'configuredEventTypes': [], 'visibleEventCount': None}
        return result

    def inspect(self):
        now = datetime.now(timezone.utc)
        blockers = []
        result = {'observedAt': now.isoformat(), 'categories': {}, 'blockers': blockers}
        expected = sorted(path.stem for path in (Path(__file__).resolve().parents[2] / 'migrations').glob('*.sql'))
        application = {'postgres': database_health(self.url), 'api': 'UP', 'latestAppliedMigration': None,
                       'expectedLatestMigration': expected[-1] if expected else None, 'missingMigrations': expected}
        result['application'] = application
        def inspect_section(name, operation):
            try:
                result[name] = operation()
            except Exception:
                result[name] = {'status': 'UNAVAILABLE'}
                blockers.append(name.upper() + '_INSPECTION_UNAVAILABLE')
        inspect_section('marketMemory', self.memory)
        result['calendar'] = TradingCalendarBootstrapService(self.url, self.root).status(now.astimezone(IST).date())
        result['zerodha'] = {'allowRealZerodha': enabled('ALLOW_REAL_ZERODHA'),
                            'apiKeyConfigured': bool(os.getenv('ZERODHA_API_KEY')),
                            'accessTokenConfigured': bool(os.getenv('ZERODHA_ACCESS_TOKEN'))}
        zerodha = result['zerodha']
        zerodha['status'] = 'DISABLED' if not zerodha['allowRealZerodha'] else 'READY' if zerodha['apiKeyConfigured'] and zerodha['accessTokenConfigured'] else 'CREDENTIALS_MISSING'
        for name in ('instrumentSnapshot', 'research', 'runtime', 'live'):
            result[name] = {'status': 'UNAVAILABLE'}
        result['models'] = []
        if application['postgres'] == 'UP':
            try:
                import psycopg
                from psycopg.rows import dict_row
                with psycopg.connect(self.url, row_factory=dict_row, connect_timeout=2, options='-c statement_timeout=5000') as db:
                    db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    applied = [row['version'] for row in db.execute('SELECT version FROM schema_migrations ORDER BY version')]
                    application.update(latestAppliedMigration=applied[-1] if applied else None,
                                       missingMigrations=[version for version in expected if version not in applied])
                    for name, operation in [('instrumentSnapshot', lambda: self.snapshot(db, now.astimezone(IST).date())),
                                             ('research', lambda: self.research(db)), ('models', lambda: self.models(db)),
                                             ('runtime', lambda: self.runtime(db))]:
                        try:
                            with db.transaction():  # Savepoint: a missing table must not poison other checks.
                                result[name] = operation()
                        except Exception:
                            blockers.append(name.upper() + '_INSPECTION_UNAVAILABLE')
            except Exception:
                blockers.append('DATABASE_READINESS_QUERY_FAILED')
            inspect_section('live', lambda: self.live(now))
        inspect_section('news', self.news_state)
        from .research_context import research_context_readiness
        inspect_section('researchContext', lambda: research_context_readiness(self.url, self.root))
        from .market_structure_context import market_structure_readiness, MarketStructureContextRepository
        from .news.impact_study import EventImpactStudyService
        inspect_section('marketStructureContext',lambda: market_structure_readiness(self.url,self.root))
        from .context_v3 import ContextV3CoverageService, ContextV3RuntimeFeatureService
        inspect_section('contextV3',lambda: ContextV3CoverageService(MarketStructureContextRepository(self.url)).coverage())
        try: result['contextV3Runtime']=ContextV3RuntimeFeatureService(root=self.root).snapshot()
        except Exception: result['contextV3Runtime']=ContextV3RuntimeFeatureService.unavailable(now,None,'RUNTIME_CONTEXT_UNAVAILABLE')
        inspect_section('eventImpact',lambda: EventImpactStudyService(MarketStructureContextRepository(self.url)).summary())
        from .context_v3_experiments import ContextV3ExperimentService
        from .repository import PostgresRepository
        inspect_section('contextV3Experiment',lambda: ContextV3ExperimentService(PostgresRepository(self.url)).stored(5))
        from .microstructure import MicrostructureRuntimeService
        from .shadow_observations import ShadowRepository
        inspect_section('microstructure',lambda: MicrostructureRuntimeService().snapshot(now))
        inspect_section('shadow',lambda: ShadowRepository(self.url).summary())
        research_states=['CONTEXT_V3_IMPLEMENTATION_AVAILABLE','CONTEXT_V3_G3_FRAMEWORK_READY']
        profiles=result['contextV3'].get('profiles',[])
        for profile in profiles:
            price=profile.get('profileVersion')=='banknifty-price-vol-v3'
            ready=profile.get('status')=='READY_FOR_EVALUATION'
            if price:
                if ready: research_states.append('CONTEXT_V3_PRICE_VOL_DATA_READY')
                if any(c.get('generatedRows',0)>0 for c in profile.get('cohorts',[])):
                    research_states.append('CONTEXT_V3_PRICE_VOL_GENERATED')
            elif ready: research_states.append('CONTEXT_V3_RICH_DATA_READY')
            else: research_states.append('CONTEXT_V3_RICH_DATA_BLOCKED')
        experiments=result['contextV3Experiment'].get('experiments',[])
        classifications={e.get('classification') for e in experiments}
        if classifications & {'V3_BETTER','V3_WORSE','NO_CLEAR_IMPROVEMENT'}: research_states.append('CONTEXT_V3_G3_EVALUATED')
        if 'INSUFFICIENT_COVERAGE' in classifications: research_states.append('CONTEXT_V3_G3_INSUFFICIENT_COVERAGE')
        if 'INSUFFICIENT_EVIDENCE' in classifications: research_states.append('CONTEXT_V3_G3_INSUFFICIENT_EVIDENCE')
        if not experiments: research_states.append('CONTEXT_V3_G3_BLOCKED_BY_DATA' if not any(p.get('status')=='READY_FOR_EVALUATION' for p in profiles) else 'CONTEXT_V3_G3_INSUFFICIENT_EVIDENCE')
        research_states.append('MICROSTRUCTURE_RUNTIME_'+{'PASS':'AVAILABLE','PARTIAL':'PARTIAL'}.get(result['microstructure'].get('status'),'UNAVAILABLE'))
        if result['shadow'].get('captureStatus'): research_states.extend([result['shadow']['captureStatus'],result['shadow']['status']])
        result['researchReadiness']={'states':sorted(set(research_states)),
            'notice':'Implementation, activated data and empirical validation are separate; research never modifies PRICE_CORE_V2.'}
        categories = result['categories']
        categories['CONTEXT_V3']=result['contextV3'].get('status','UNAVAILABLE')
        categories['CONTEXT_V3_RUNTIME']=result['contextV3Runtime'].get('status','UNAVAILABLE')
        categories['CONTEXT_V3_EXPERIMENT']=('AVAILABLE' if result['contextV3Experiment'].get('count')
            else 'NOT_CONFIGURED' if result['contextV3Experiment'].get('available') else 'UNAVAILABLE')
        categories['SCHEDULED_CONTEXT']=result['news'].get('scheduledContext',{}).get('status','NOT_CONFIGURED')
        for key in ('CONSTITUENT_CONTEXT','FUTURES_CONTEXT','MARKET_STRUCTURE'):
            categories[key]=result['marketStructureContext'].get(key,{}).get('status','NOT_CONFIGURED')
        categories['EVENT_IMPACT']='PARTIAL' if result['eventImpact'].get('completedImpactStudies') else 'NOT_CONFIGURED'
        instrument_news=result['marketStructureContext'].get('UPSTOX_INSTRUMENT_NEWS',{})
        categories['UPSTOX_INSTRUMENT_NEWS']='READY' if instrument_news.get('sourceState')=='UP' else 'PARTIAL' if instrument_news.get('configured') else 'NOT_CONFIGURED' if enabled('NEWS_SOURCE_UPSTOX_INSTRUMENT_ENABLED') else 'DISABLED'
        categories['RESEARCH_CONTEXT'] = result['researchContext'].get('status', 'NOT_CONFIGURED')
        if categories['RESEARCH_CONTEXT'] not in ('READY', 'PARTIAL', 'DISABLED', 'NOT_CONFIGURED'):
            categories['RESEARCH_CONTEXT'] = 'NOT_CONFIGURED'
        if categories['RESEARCH_CONTEXT'] not in ('READY', 'DISABLED'):
            blockers.append('RESEARCH_CONTEXT_' + categories['RESEARCH_CONTEXT'])
        categories['APPLICATION'] = 'READY' if application['postgres'] == 'UP' and not application['missingMigrations'] else 'BLOCKED'
        if application['postgres'] != 'UP': blockers.append('POSTGRES_' + application['postgres'])
        if application['missingMigrations']: blockers.append('APPLY_PENDING_MIGRATIONS_VIA_BOOTSTRAP')
        calendar_ready = result['calendar']['status'] == 'READY'
        if not calendar_ready: blockers.append(result['calendar']['status'])
        market_ready = calendar_ready and zerodha['status'] == 'READY' and result['instrumentSnapshot'].get('status') == 'READY' and result['live'].get('qualityState') in ('PASS', 'MARKET_CLOSED') and result['live'].get('workerState') in ('CONNECTED', 'MARKET_CLOSED')
        categories['MARKET_DATA'] = 'DISABLED' if not enabled('LIVE_MARKET_ENABLED') else 'READY' if market_ready else 'BLOCKED'
        if enabled('LIVE_MARKET_ENABLED'):
            if zerodha['status'] != 'READY': blockers.append('CONFIGURE_REAL_ZERODHA_CREDENTIALS_AND_PERMISSION')
            if result['instrumentSnapshot'].get('status') != 'READY': blockers.append('CURRENT_BANKNIFTY_INSTRUMENT_SNAPSHOT_REQUIRED')
            if result['live'].get('qualityState') not in ('PASS', 'MARKET_CLOSED'): blockers.append('LIVE_DATA_' + result['live'].get('qualityState', 'UNAVAILABLE'))
            if result['live'].get('workerState') not in ('CONNECTED', 'MARKET_CLOSED'): blockers.append('LIVE_WORKER_' + result['live'].get('workerState', 'UNAVAILABLE'))
        memory = result['marketMemory']
        missing = memory.get('missingMinuteDates', [])
        categories['MARKET_MEMORY'] = 'BLOCKED' if memory.get('canonicalChecksumStatus') != 'PASS' else 'PARTIAL' if missing else 'READY'
        if memory.get('canonicalChecksumStatus') != 'PASS': blockers.append('VERIFY_CANONICAL_MARKET_MEMORY_CHECKSUMS')
        if missing: blockers.append(f'{len(missing)}_MARKET_MEMORY_SESSIONS_MISSING_MINUTE_HISTORY')
        research = result['research']
        categories['MODEL_RESEARCH'] = 'READY' if research.get('acceptedDatasetExists') and research.get('indexMinuteRows') and research.get('featureRows') and all(research.get('labels', {}).get(str(h), 0) for h in (10, 20, 30)) else 'BLOCKED'
        if not research.get('acceptedDatasetExists'): blockers.append('NO_ACCEPTED_BIG_MOVE_DATASET')
        elif categories['MODEL_RESEARCH'] != 'READY': blockers.append('GENERATE_ACCEPTED_REVISION_V2_LABELS_AND_FEATURES')
        models_ready = len(result['models']) == 3 and all(item['status'] == 'READY' for item in result['models'])
        categories['MODELS'] = 'READY' if models_ready else 'BLOCKED'
        for h in (10, 20, 30):
            model = next((item for item in result['models'] if item['horizonMinutes'] == h), {})
            if not model.get('approvedModelExists'): blockers.append(f'NO_APPROVED_{h}M_MODEL')
            elif model.get('status') != 'READY': blockers.append(f'REPAIR_APPROVED_{h}M_ARTIFACT_INTEGRITY')
        if not any(item.get('approvedModelExists') and item.get('artifactExists') for item in result['models']): blockers.append('NO_APPROVED_BIG_MOVE_V2_ARTIFACTS')
        result['runtime']['runtimeStatus'] = ('BLOCKED_MODEL_NOT_APPROVED' if not models_ready else
            'DISABLED' if not enabled('BIG_MOVE_RUNTIME_ENABLED') else
            'WORKER_STOPPED' if result['runtime'].get('workerState') != 'RUNNING' else
            result['live'].get('qualityState', 'MARKET_DATA_UNAVAILABLE'))
        news = result['news']
        categories['NEWS'] = 'DISABLED' if not enabled('NEWS_POLLING_ENABLED') else 'READY' if news.get('monitorStatus') == 'RUNNING' and news.get('providers') and all(item['state'] == 'UP' for item in news['providers']) and news.get('scheduledEvents') == 'CONFIGURED' else 'PARTIAL'
        if news.get('scheduledEvents') == 'NOT_CONFIGURED': blockers.append('SCHEDULED_EVENTS_NOT_CONFIGURED')
        if enabled('NEWS_POLLING_ENABLED'):
            if news.get('monitorStatus') != 'RUNNING': blockers.append('NEWS_MONITOR_' + news.get('monitorStatus', 'UNAVAILABLE'))
            for provider in news.get('providers', []):
                if provider['state'] != 'UP': blockers.append('NEWS_PROVIDER_' + provider['source'] + '_' + provider['state'])
        count = result['runtime'].get('journalCount')
        categories['EVIDENCE'] = 'BLOCKED' if count is None else 'READY' if count >= max(1, int(os.getenv('EVIDENCE_MINIMUM_SAMPLE', '20'))) else 'PARTIAL'
        if not count: blockers.append('NO_PROSPECTIVE_EVIDENCE_OBSERVATIONS')
        result['evidence'] = {'journalCount': count, 'minimumSample': max(1, int(os.getenv('EVIDENCE_MINIMUM_SAMPLE', '20'))), 'scope': 'Prospective observations; counts do not establish predictive value'}
        result['blockers'] = list(dict.fromkeys(blockers))
        return result
