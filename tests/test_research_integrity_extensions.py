from copy import deepcopy
from datetime import datetime, timedelta, timezone, date
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from banknifty_foundation.microstructure import depth_features, option_summary
from banknifty_foundation.market_structure_context import OfficialConstituentImportService, ConstituentUniverseService
from banknifty_foundation.shadow_observations import MemoryShadowRepository, ShadowObservationService, capture_best_effort
from banknifty_foundation.context_v3_experiments import ContextV3ExperimentService
from banknifty_foundation.research_models import FeatureSetDefinition
from banknifty_foundation.repository import digest
from test_context_v3_experiments import _fixture, PROFILE, PIN_VERSION

NOW=datetime(2026,1,5,5,tzinfo=timezone.utc)


def tick():
    return {'tick_id':'t','instrument_id':1,'instrument_type':'FUT','event_at':NOW.isoformat(),'received_at':NOW.isoformat(),
        'bids':[{'price':100-i,'quantity':30,'orders':3} for i in range(5)],
        'asks':[{'price':102+i,'quantity':10,'orders':1} for i in range(5)],
        'oi':1000,'cumulative_volume':100,'total_buy_quantity':150,'total_sell_quantity':50}


def test_microprice_and_top5_deterministic():
    result=depth_features(tick(),market_as_of=NOW,decision_at=NOW)
    assert result['status']=='PASS'
    assert result['values']['microprice']==101.5
    assert result['values']['top5QuantityImbalance']==.5
    assert result['values']['depthWeightedImbalance']==pytest.approx(.5)
    assert result==depth_features(tick(),market_as_of=NOW,decision_at=NOW)


@pytest.mark.parametrize('change,reason', [('empty','MISSING_BID'),('crossed','CROSSED_BID_ASK'),('stale','STALE_SOURCE')])
def test_bad_depth_missing_is_not_zero(change,reason):
    value=tick()
    if change=='empty': value['bids']=[]
    if change=='crossed': value['bids'][0]['price']=103
    if change=='stale': value['event_at']=(NOW-timedelta(minutes=5)).isoformat()
    result=depth_features(value,market_as_of=NOW,decision_at=NOW)
    assert reason in result['missingReasons']
    assert result['values'].get('top5QuantityImbalance') is None


def test_no_future_depth():
    value=tick(); value['received_at']=(NOW+timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError,match='FUTURE'): depth_features(value,market_as_of=NOW,decision_at=NOW)
    assert option_summary([],0)['values']['depthCompleteness'] is None


@pytest.mark.parametrize('unit,raw,other,expected',[('PERCENT','12.5','87.5',.125),('FRACTION','.125','.875',.125),('NONE','','',None)])
def test_official_history_units_and_idempotence(tmp_path,unit,raw,other,expected):
    path=tmp_path/'nifty_bank.csv'; path.write_text('Index Name,Symbol,ISIN Code,Weight\nNIFTY BANK,HDFCBANK,INE040A01034,'+raw+'\nNIFTY BANK,SBIN,,'+other+'\nNIFTY 50,EXCLUDED,,1\n')
    service=OfficialConstituentImportService(tmp_path)
    args=dict(effective_from='2025-01-01',effective_to='2025-01-31',source_publication_date='2024-12-31',
        source_observed_at=NOW,source_reference='Official monthly archive',source_url='https://www.niftyindices.com/reports/historical',
        effective_range_basis='Official January membership report',weight_unit=unit)
    first=service.import_history(path,**args)
    assert service.import_history(path,**args)==first
    snapshot=ConstituentUniverseService(tmp_path).resolve(date(2025,1,6))
    assert snapshot.source_quality=='OFFICIAL_INDEX'
    assert len(snapshot.members)==2 and snapshot.members[0].weight==expected
    if unit=='NONE':
        assert all(m.weight_source_quality=='NONE' for m in snapshot.members)
        assert first['weightingLineage']==['OFFICIAL_MEMBERSHIP','EQUAL_WEIGHT_RESEARCH_PROXY']
    path.write_text('Index Name,Symbol,Weight\nNIFTY BANK,ICICIBANK,'+('100' if unit=='PERCENT' else '1' if unit=='FRACTION' else '')+'\n')
    with pytest.raises(ValueError,match='AMBIGUOUS'): service.import_history(path,**args)


def test_g3_multiple_manifests_every_reference_validated():
    repository=_fixture(days=2,minutes=2)
    old=repository.feature_sets[PIN_VERSION]
    definition={**old.definition,'testDateBatch':'second'}
    checksum=digest(definition)
    repository.feature_sets['context-v3-inputs-second']=FeatureSetDefinition('context-v3-inputs-second',NOW,definition,checksum)
    candidates=repository.rows[PROFILE]
    for row in candidates[2:]:
        row.values['_contextV3']['pinnedLineage'].update(inputManifestVersion='context-v3-inputs-second',inputManifestChecksum=checksum)
    service=ContextV3ExperimentService(repository)
    result=service._generation_manifest(candidates)
    assert len(result['manifests'])==2
    assert all(row['referencedRowCount']==2 for row in result['manifests'])
    assert result==service._generation_manifest(list(reversed(candidates)))
    del repository.feature_sets[PIN_VERSION]
    with pytest.raises(ValueError,match='MANIFEST_MISMATCH'): service._generation_manifest(candidates)


def test_g3_wrong_manifest_identity_rejected():
    repository=_fixture(days=1,minutes=1)
    old=repository.feature_sets[PIN_VERSION]; definition={**old.definition,'instrumentId':99}
    checksum=digest(definition)
    repository.feature_sets[PIN_VERSION]=replace(old,definition=definition,checksum=checksum)
    repository.rows[PROFILE][0].values['_contextV3']['pinnedLineage']['inputManifestChecksum']=checksum
    with pytest.raises(ValueError,match='IDENTITY_MISMATCH'):
        ContextV3ExperimentService(repository)._generation_manifest(repository.rows[PROFILE])


def test_g3_blocked_combinations_are_not_silently_omitted():
    result=ContextV3ExperimentService(_fixture(days=1,minutes=1)).run(
        'revision',7,PROFILE,horizons=(10,20,30),algorithms=('LOGISTIC_REGRESSION','LIGHTGBM'),persist=False)
    for horizon in ('10','20','30'):
        algorithms=result['manifest']['horizons'][horizon]['algorithms']
        assert set(algorithms)=={'LOGISTIC_REGRESSION','LIGHTGBM'}
        assert all(run['status']=='BLOCKED' and run['evaluatedFoldCount']==0 for run in algorithms.values())


def test_shadow_idempotent_conflict_and_future():
    market=NOW-timedelta(minutes=2)
    prediction={'prediction_id':'p','instrument_id':1,'quality_status':'PASS','horizon_minutes':10,'as_of':market,'expires_at':NOW+timedelta(minutes=10)}
    journal={'prediction_id':'p','journal_id':'j','journal_checksum':'checksum','decision_at':NOW,'market_as_of':market}
    context=SimpleNamespace(snapshot=lambda decision:{'status':'READY','marketAsOf':decision.isoformat(),'profiles':{}})
    micro=SimpleNamespace(snapshot=lambda decision:{'status':'PARTIAL','sourceMaxTimestamp':decision.isoformat(),'schemaVersion':'v1'})
    repository=MemoryShadowRepository(); service=ShadowObservationService(repository,context,micro)
    assert service.capture(prediction,journal)=='SAVED'
    assert service.capture(prediction,journal)=='REUSED'
    assert repository.rows['p']['greeks_evidence']['status']=='GREEKS_UNAVAILABLE'
    assert repository.rows['p']['market_as_of']==market
    assert repository.rows['p']['decision_at']==NOW
    assert repository.rows['p']['context_v3_evidence']['marketAsOf']==NOW.isoformat()
    assert repository.rows['p']['microstructure_evidence']['sourceMaxTimestamp']==NOW.isoformat()
    with pytest.raises(ValueError,match='CUTOFF_MISMATCH'):
        service.capture(prediction,{**journal,'market_as_of':NOW})
    with pytest.raises(ValueError,match='FUTURE'):
        service.capture({**prediction,'as_of':NOW+timedelta(seconds=1)},
                        {**journal,'market_as_of':NOW+timedelta(seconds=1)})
    context.snapshot=lambda decision:{'status':'UNAVAILABLE','profiles':{}}
    with pytest.raises(ValueError,match='IMMUTABLE'): service.capture(prediction,journal)
    context.snapshot=lambda decision:{'sourceMaxTimestamp':(decision+timedelta(seconds=1)).isoformat()}
    with pytest.raises(ValueError,match='FUTURE'): service.capture(prediction,journal)
    assert capture_best_effort(prediction,journal,service)=='SHADOW_EVIDENCE_UNAVAILABLE'
    assert prediction['quality_status']=='PASS'


@pytest.mark.parametrize('age,receipt_future',[(30,False),(180,False),(30,True),(None,False)])
def test_microstructure_runtime_uses_evidence_cutoff(age,receipt_future):
    from contextlib import contextmanager
    from banknifty_foundation.microstructure import MicrostructureRuntimeService
    value=tick()
    if age is not None: value['event_at']=(NOW-timedelta(seconds=age)).isoformat()
    if receipt_future: value['received_at']=(NOW+timedelta(seconds=1)).isoformat()
    state=SimpleNamespace(futures_selections={'f':SimpleNamespace(selected_at=NOW-timedelta(minutes=10),instrument_id=1)},
        option_universes={},instruments={},finalized_minutes={})
    @contextmanager
    def connection():
        yield SimpleNamespace(execute=lambda *args:SimpleNamespace(fetchall=lambda:
            [{'instrument_id':1,'payload':value}] if age is not None else []))
    reader=SimpleNamespace(current_state=lambda decision:state,_connection=connection)
    result=MicrostructureRuntimeService(reader).snapshot(NOW)
    assert result['decisionAt']==NOW.isoformat()
    if age is None or receipt_future:
        assert result['marketAsOf'] is None and result['status']=='UNAVAILABLE'
    else:
        assert result['marketAsOf']==value['event_at']==result['sourceMaxTimestamp']
        assert result['futures']['ageSeconds']==age
        assert result['futures']['status']==('UNAVAILABLE' if age>120 else 'PASS')
    if age is None:
        state.finalized_minutes={'m':SimpleNamespace(quality_state='PASS',minute_at=NOW-timedelta(minutes=3),first_finalized_at=NOW-timedelta(minutes=2))}
        fallback=MicrostructureRuntimeService(reader).snapshot(NOW)
        assert fallback['marketAsOf']==(NOW-timedelta(minutes=2)).isoformat()
        assert fallback['status']=='UNAVAILABLE'  # No depth invented from a candle.


@pytest.mark.parametrize('unit,weights,accepted',[
    ('PERCENT','50,',False),('FRACTION','0.5,',False),
    ('PERCENT','50,40',False),('FRACTION','0.5,0.4',False),
    ('PERCENT','50,NaN',False),('FRACTION','0.5,invalid',False),
    ('NONE','0,',False),('NONE',',1',False),
    ('PERCENT','50,49.9',True),('FRACTION','0.5,0.501',True),
    ('PERCENT','50,49.89',False),('FRACTION','0.5,0.5011',False),
])
def test_official_history_requires_complete_explicit_weights(tmp_path,unit,weights,accepted):
    first,second=weights.split(',')
    path=tmp_path/'nifty_bank.csv'
    path.write_text(f'Symbol,Weight\nHDFCBANK,{first}\nSBIN,{second}\n')
    service=OfficialConstituentImportService(tmp_path)
    args=dict(effective_from='2025-01-01',effective_to='2025-01-31',source_publication_date='2024-12-31',
        source_observed_at=NOW,source_reference='Official archive',source_url='https://www.niftyindices.com/reports/historical',
        effective_range_basis='Official effective range',weight_unit=unit)
    if accepted:
        assert service.import_history(path,**args)['weightQuality']=='OFFICIAL_INDEX'
    else:
        with pytest.raises(ValueError): service.import_history(path,**args)
        assert not list((tmp_path/'config/official_constituents').glob('*.json'))


def test_generator_date_scoped_manifests_and_overlap(tmp_path,monkeypatch):
    import banknifty_foundation.context_v3 as module
    repository=_fixture(days=2,minutes=1)
    repository.rows={}; repository.revision.session_start=date(2026,1,5); repository.revision.session_end=date(2026,1,6)
    repository.get_instrument=lambda identifier:SimpleNamespace(exchange='NSE',tradingsymbol='BANKNIFTY')
    candles=[SimpleNamespace(timestamp=NOW+timedelta(days=d),open=100,high=101,low=99,close=100,volume=1) for d in range(2)]
    repository.revision_candles=lambda *args:candles
    repository.sessions_between=lambda *args:[SimpleNamespace(session_date=c.timestamp.date(),status='OPEN',
        open_at=c.timestamp-timedelta(hours=1),close_at=c.timestamp+timedelta(hours=4)) for c in candles]
    def save(rows):
        prior=repository.rows.setdefault(PROFILE,[]); count=0
        for row in rows:
            if not any(old.as_of==row.as_of for old in prior): prior.append(row); count+=1
        return count
    repository.save_feature_rows=save
    monkeypatch.setattr(module,'calculate_blocks',lambda inputs:{})
    monkeypatch.setattr(module,'profile_snapshot',lambda inputs,version,blocks:{'source_max_timestamp':inputs['as_of'],
        'status':'PASS','horizonEligibility':{'10':True},'values':{'price':1,'_contextV3':{'pinnedLineage':inputs['pins']}}})
    service=module.ContextV3FeatureGenerationService(repository,root=tmp_path)
    kwargs=dict(research_context_runs={},market_structure_runs={},membership_snapshot_checksums={},scheduled_configuration=[],market_structure_rules={})
    first,inserted=service.generate('revision',7,PROFILE,date_from='2026-01-05',date_to='2026-01-05',**kwargs)
    first_manifest=service.last_manifest
    assert inserted==1
    assert service.generate('revision',7,PROFILE,date_from='2026-01-05',date_to='2026-01-05',**kwargs)[1]==0
    assert service.last_manifest==first_manifest
    assert service.generate('revision',7,PROFILE,date_from='2026-01-05',date_to='2026-01-06',**kwargs)[1]==1
    assert service.last_manifest!=first_manifest
    assert repository.rows[PROFILE][0]==first[0]
    with pytest.raises(ValueError,match='IMMUTABLE_V3_ROW_CONFLICT'):
        service.generate('revision',7,PROFILE,date_from='2026-01-05',date_to='2026-01-05',**{**kwargs,'scheduled_configuration':[{'changed':True}]})


@pytest.mark.parametrize('network,dry_run,expected_calls',[(False,False,0),(True,True,0),(True,False,1)])
def test_prepare_isolates_profiles_and_network(tmp_path,monkeypatch,network,dry_run,expected_calls):
    import banknifty_foundation.context_v3_history as module
    import banknifty_foundation.research_context as research
    rich='banknifty-global-context-v3'; calls=[]
    preflight={'status':'BLOCKED','checksum':'test','membership':{},'profiles':[
        {'profileVersion':PROFILE,'requiredBlocks':['PRICE_VOL','SESSION_STRUCTURE'],'canonicalDates':['2026-01-05'],
         'researchCompleteDates':[],'structurePassDates':[],'ambiguousDates':[]},
        {'profileVersion':rich,'requiredBlocks':['PRICE_VOL','SESSION_STRUCTURE','INDIA_MARKET','GLOBAL'],
         'canonicalDates':['2026-01-05','2026-01-06'],'researchCompleteDates':['2026-01-06'],'structurePassDates':[],'ambiguousDates':[]}]}
    monkeypatch.setattr(module.ContextV3HistoryPreflightService,'inspect',lambda *a,**k:deepcopy(preflight))
    monkeypatch.setattr(module.ContextV3PlanningService,'plan',lambda self,r,i,p,**k:{'status':'READY' if p==PROFILE else 'BLOCKED'})
    monkeypatch.setattr(module.ContextV3PlanValidationService,'validate',lambda *a,**k:{'researchContextRuns':{},
        'marketStructureRuns':{},'membershipSnapshotChecksums':{},'scheduledConfiguration':[],'marketStructureRules':{}})
    monkeypatch.setattr(module.ContextV3FeatureGenerationService,'generate',lambda *a,**k:([SimpleNamespace(quality_status='PASS')],1))
    monkeypatch.setattr(module.ContextV3CoverageService,'coverage',lambda *a:{'status':'UNAVAILABLE'})
    monkeypatch.setattr(research.ResearchContextEnrichmentService,'enrich',lambda *a,**k:calls.append((a,k)))
    result=module.ContextV3HistoryPreparationService(object(),tmp_path).prepare('r',1,date_from='2026-01-05',date_to='2026-01-06',
        profiles=[PROFILE,rich],output_dir=tmp_path,dry_run=dry_run,allow_network=network)
    assert len(calls)==expected_calls
    assert result['profiles'][0]['status'] in ('PASS','READY')
    assert result['profiles'][1]['status']=='BLOCKED'
    if calls:
        assert calls[0][0][1:]==('2026-01-05','2026-01-05')
        assert 'DERIVATIVES' not in calls[0][1]['required_blocks']


def test_shadow_migration_sequence_and_guards():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]/'migrations'
    # 008a is the existing timestamp-helper prerequisite, not a duplicate migration.
    assert sorted({int(p.name[:3]) for p in root.glob('*.sql')})==list(range(1,22))
    assert sorted(p.name for p in root.glob('*.sql'))[-1]=='021_live_shadow_observations.sql'
    sql=(root/'021_live_shadow_observations.sql').read_text()
    for text in ('BEFORE UPDATE OR DELETE','prediction_id text NOT NULL UNIQUE REFERENCES big_move_predictions',
                 'decision_at >= market_as_of','SHADOW_REQUIRES_VALID_COMMITTED_PREDICTION_AND_JOURNAL'):
        assert text in sql


def test_research_services_have_no_broker_or_model_mutations():
    import inspect
    import banknifty_foundation.microstructure as micro
    import banknifty_foundation.shadow_observations as shadow
    for source in (inspect.getsource(micro),inspect.getsource(shadow)):
        for forbidden in ('place_order(', 'modify_order(', 'cancel_order(', 'save_model_bundle(', 'approve_model(', 'KiteConnect('):
            assert forbidden not in source


def test_research_endpoints_authenticated_and_failure_isolated(monkeypatch):
    from fastapi.testclient import TestClient
    from banknifty_foundation.internal_api import app
    from banknifty_foundation.microstructure import MicrostructureRuntimeService
    from banknifty_foundation.shadow_observations import ShadowRepository
    monkeypatch.setenv('INTERNAL_API_TOKEN','research-test')
    def unavailable(*args,**kwargs): raise RuntimeError('local unavailable')
    monkeypatch.setattr(MicrostructureRuntimeService,'snapshot',unavailable)
    monkeypatch.setattr(ShadowRepository,'summary',unavailable)
    monkeypatch.setattr(ShadowRepository,'recent',unavailable)
    client=TestClient(app); headers={'X-Internal-Token':'research-test'}
    for path in ('microstructure/runtime','shadow/summary','shadow/recent'):
        assert client.get('/internal/research/'+path).status_code==401
        response=client.get('/internal/research/'+path,headers=headers)
        assert response.status_code==(200 if path.startswith('microstructure') else 503)
    assert client.get('/internal/health').status_code==200


def test_shadow_analysis_excludes_unresolved_outcomes(monkeypatch):
    from banknifty_foundation.shadow_observations import ShadowRepository
    base={'horizon_minutes':10,'context_status':'READY','microstructure_status':'PASS',
          'actual_label':None,'outcome_quality':None,'state':'BIG_MOVE','big_move_probability':.8}
    repository=ShadowRepository('postgresql://unused/unused')
    monkeypatch.setattr(repository,'recent',lambda limit:[base,{**base,'actual_label':'BIG_UP','outcome_quality':'VALID'}])
    result=repository.summary()
    assert result['capturedCount']==2 and result['resolvedCount']==1
    assert result['cohorts']['ALL_CAPTURED_BASELINE']['stageABrier']==pytest.approx(.04)
    assert 'BASELINE_ONLY' not in result['cohorts']


def test_scoped_complete_run_cannot_satisfy_richer_profile():
    from banknifty_foundation.context_v3 import ContextV3PlanningService
    service=object.__new__(ContextV3PlanningService)
    run={'run_id':'r','status':'COMPLETE','configuration':{'requiredBlocks':['INDIA_MARKET','GLOBAL']}}
    assert service._pin_research_run([run],date(2026,1,5),[],{'GLOBAL'})==run
    blockers=[]
    assert service._pin_research_run([run],date(2026,1,5),blockers,{'GLOBAL','DERIVATIVES'}) is None
    assert blockers


def test_tracking_membership_does_not_pass_bank_structure():
    from banknifty_foundation.context_v3 import bank_structure_block
    result=bank_structure_block({'as_of':NOW,'mode':'RETROSPECTIVE_PROVIDER_TIMESTAMP',
        'membership':{'source_quality':'TRACKING_PROXY','effective_from':'2026-01-01','effective_to':'2026-01-31',
                      'source_observed_at':NOW,'snapshot_id':'proxy','source_checksum':'checksum'},
        'banks':{'observed_at':NOW,'effective_at':NOW,'values':{'sourceQuality':'TRACKING_PROXY',
                 'snapshotId':'proxy','snapshotChecksum':'checksum','constituent_completeness':1}}})
    assert result.status=='UNAVAILABLE'
    assert 'OFFICIAL_DATE_VALID_PINNED_MEMBERSHIP_REQUIRED' in result.missingReasons


@pytest.mark.parametrize('commit_succeeds',[True,False])
def test_production_commit_precedes_shadow_and_survives_failure(monkeypatch,commit_succeeds):
    import banknifty_foundation.big_move_runtime as runtime
    import banknifty_foundation.shadow_observations as shadow
    from banknifty_foundation.big_move_labels import default_big_move_policy
    events=[]
    reference=SimpleNamespace(minute_at=NOW-timedelta(minutes=1),instrument_id=1,open=100,high=101,low=99,close=100)
    state=SimpleNamespace(quality='PASS',minutes=lambda kind:[reference],
        session=SimpleNamespace(open_at=NOW-timedelta(hours=1),close_at=NOW+timedelta(hours=1),session_date=NOW.date(),status='OPEN'))
    snapshot=SimpleNamespace(quality_status='PASS',values={'x':1},market_regime='RANGE',session_bias={})
    monkeypatch.setattr(runtime,'live_features',lambda value:snapshot)
    prediction={key:.5 for key in ('rawBigMoveProbability','rawUpGivenBigMoveProbability','moveDecisionThreshold',
        'bigMoveProbability','quietProbability','directionProbability','upGivenBigMoveProbability','downGivenBigMoveProbability',
        'expectedMovePoints','expectedMovePercent')}
    prediction.update(state='BIG_MOVE',direction='UP')
    model=SimpleNamespace(model_version='test',predict=lambda *a:prediction)
    manifest={'feature_schema':['x'],'threshold_policy':runtime.policy_payload(default_big_move_policy()),
              'evaluation_checksum':'e','approval_decision_checksum':'a','approval_policy_version':'v'}
    pair={'move':{'model_checksum':'m','dataset_revision_id':'r'},'direction':{'model_checksum':'d'}}
    def save(value,evidence):
        events.append('save')
        if not commit_succeeds: raise RuntimeError('commit failed')
        return 'PERSISTED'
    def fail_shadow(*args):
        events.append('shadow'); raise RuntimeError('shadow failure')
    monkeypatch.setattr(shadow,'capture_best_effort',fail_shadow)
    repository=SimpleNamespace(approved_big_move_models=lambda:{10:pair},save_big_move_prediction_with_evidence=save)
    journal=SimpleNamespace(prepare=lambda value:{'decision_at':NOW},capture=lambda *a:{})
    worker=runtime.BigMoveRuntimeService(repository=repository,reader=SimpleNamespace(current_state=lambda now:state),
        store=SimpleNamespace(load=lambda value:(model,manifest)),journal=journal)
    result=worker.run_once(NOW)
    assert events==(['save','shadow'] if commit_succeeds else ['save'])
    assert result[10]==('PERSISTED' if commit_succeeds else 'DATA_NOT_READY')
