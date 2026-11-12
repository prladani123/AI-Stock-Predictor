"""Explicit training and immutable artifacts; read-only live inference and outcome appenders."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import logging
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

import joblib
import numpy as np

from .big_move_features import FEATURE_SET_VERSION_V2, big_move_feature_definition, build_big_move_features
from .big_move_labels import BIG_MOVE_HORIZONS, BigMoveThresholdPolicy, calculate_big_move_label
from .big_move_models import BigMoveTwoStageModel, train_two_stage_model
from .calendar import IST
from .live_product_reader import LiveProductDataReader
from .models import Candle, TradingSession
from .repository import PostgresRepository, canonical_json, digest

log=logging.getLogger(__name__)


def numeric(value):
    if value is None or isinstance(value,bool): return None
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError): return None


def policy_payload(policy):
    return json.loads(canonical_json(asdict(policy)))


def restore_policy(value):
    return BigMoveThresholdPolicy(value['version'],
        {int(k):Decimal(str(v)) for k,v in value['minimum_returns'].items()},
        {int(k):Decimal(str(v)) for k,v in value['volatility_multipliers'].items()},
        int(value['volatility_lookback_minutes']))


class BigMoveArtifactStore:
    def __init__(self, root=None):
        self.root=Path(root or os.getenv('BIG_MOVE_ARTIFACT_DIR',Path(__file__).resolve().parents[2]/'model_artifacts'/'big_move_v2')).resolve()

    def save(self, model, manifest):
        self.root.mkdir(parents=True,exist_ok=True)
        version=model.model_version
        if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in version):
            raise ValueError('Unsafe model version')
        target=self.root/f'{version}.joblib'
        if not target.exists():
            with TemporaryDirectory(prefix='bigmove-',dir=self.root) as directory:
                temporary=Path(directory)/'model.joblib'
                joblib.dump({'model':model,'manifest':manifest},temporary,compress=3)
                try: os.link(temporary,target)  # Atomic creation, never overwrite a prior training run.
                except FileExistsError: pass
        payload=joblib.load(target)  # Locally generated trusted artifacts only.
        stored=payload['manifest']
        if ({k:v for k,v in stored.items() if k!='created_at'} !=
            {k:v for k,v in manifest.items() if k!='created_at'} or stored['model_version']!=version):
            raise ValueError('Immutable V2 artifact conflict')
        return {'artifact_path':str(target),'artifact_checksum':sha256(target.read_bytes()).hexdigest(),'manifest':stored}

    def load(self, definition):
        path=Path(definition['artifact_path']).resolve()
        if not path.is_relative_to(self.root) or path.suffix!='.joblib':
            raise ValueError('Artifact path is outside the trusted V2 store')
        if sha256(path.read_bytes()).hexdigest()!=definition['artifact_checksum']:
            raise ValueError('V2 artifact checksum mismatch')
        payload=joblib.load(path)
        model,manifest=payload['model'],payload['manifest']
        if not isinstance(model,BigMoveTwoStageModel) or model.model_family!='BIG_MOVE_V2':
            raise ValueError('Not a Big Move V2 artifact')
        if (manifest!=definition['manifest'] or model.model_version!=definition['model_version']
            or model.horizon_minutes!=definition['horizon_minutes'] or model.algorithm!=definition['algorithm']
            or tuple(manifest['feature_schema'])!=model.feature_schema
            or manifest['feature_set_version']!=FEATURE_SET_VERSION_V2
            or definition['feature_set_version']!=FEATURE_SET_VERSION_V2
            or manifest['status']!=definition['status']
            or str(definition['dataset_revision_id'])!=manifest['training_revision']):
            raise ValueError('V2 artifact lineage/schema mismatch')
        if manifest.get('moveDecisionThreshold') != model.move_decision_threshold:
            raise ValueError('V2 decision threshold mismatch')
        if manifest['status']=='APPROVED':
            keys=('approved_from_model_checksum','approved_from_artifact_checksum','evaluation_checksum',
                  'approval_policy_version','approval_decision_checksum')
            if any(not manifest.get(k) for k in keys) or model.move_decision_threshold is None:
                raise ValueError('Approved artifact lacks Stage D evidence')
            if model.model_version != 'big-move-v2-approved-'+digest({k:manifest[k] for k in keys})[:32]:
                raise ValueError('Approved artifact version/lineage mismatch')
        for stage in ('move','direction'):
            expected=manifest.get('calibration',{}).get(stage,{})
            calibrator=getattr(model,stage+'_calibrator',None)
            if (expected.get('status')=='CALIBRATED') != (calibrator is not None):
                raise ValueError('V2 calibration lineage mismatch')
            if calibrator is not None and expected.get('method')!=calibrator.method:
                raise ValueError('V2 calibration method mismatch')
        return model,manifest


class BigMoveTrainingService:
    MIN_ROWS=100
    MIN_CLASS_ROWS=10
    MIN_FEATURES=4
    COVERAGE=.90

    def __init__(self,repository,store=None):
        self.repository=repository
        self.store=store or BigMoveArtifactStore()

    def train(self,revision_id,instrument_id,algorithm='LOGISTIC_REGRESSION',configuration=None):
        from .big_move_evaluation import EvaluationConfiguration, train_candidates
        return train_candidates(self, revision_id, instrument_id, algorithm,
                                configuration or EvaluationConfiguration())

    def training_data(self,revision_id,instrument_id,algorithm):
        revision=self.repository.get_revision(revision_id)
        if revision.status!='ACCEPTED' or instrument_id not in revision.instrument_ids:
            raise ValueError('Training requires an ACCEPTED revision containing the instrument')
        instrument=self.repository.get_instrument(instrument_id)
        if instrument.exchange!='NSE' or instrument.tradingsymbol not in ('NIFTY BANK','BANKNIFTY'):
            raise ValueError('Training instrument must be the BANKNIFTY index')
        if algorithm not in ('LOGISTIC_REGRESSION','LIGHTGBM'): raise ValueError('Unsupported V2 algorithm')
        features=self.repository.feature_rows(revision_id,instrument_id,FEATURE_SET_VERSION_V2)
        feature_map={r.as_of:r for r in features if r.quality_status=='PASS' and r.source_max_timestamp<=r.as_of}
        if len(feature_map)!=sum(r.quality_status=='PASS' and r.source_max_timestamp<=r.as_of for r in features):
            raise ValueError('Duplicate training as_of rows')
        labels=self.repository.big_move_labels(revision_id,instrument_id)
        policy_versions={r.threshold_policy_version for r in labels}
        if len(policy_versions)!=1: raise ValueError('Training requires exactly one threshold policy lineage')
        policy=self.repository.big_move_policy(next(iter(policy_versions)))
        definition=big_move_feature_definition()
        names=[name for group in ('price_features','futures_features','option_features') for name in definition[group] if name!='futures_buildup']
        return feature_map, labels, policy, names, definition

    def fit_partition(self,aligned,horizon,algorithm):
        definition=big_move_feature_definition()
        names=[name for group in ('price_features','futures_features','option_features') for name in definition[group] if name!='futures_buildup']
        # Called only with the estimator-fit partition, never calibration or evaluation rows.
        if not aligned: raise ValueError('Insufficient estimator-fit rows')
        coverage={name:sum(numeric(f.values.get(name)) is not None for f,_ in aligned)/len(aligned) for name in names}
        schema=[name for name in names if coverage[name]>=self.COVERAGE]
        if len(schema)<self.MIN_FEATURES: raise ValueError('Inadequate training feature coverage')
        eligible=[(f,r) for f,r in aligned if all(numeric(f.values.get(name)) is not None for name in schema)]
        target=[r.label for _,r in eligible]
        if len(eligible)<self.MIN_ROWS or min(target.count(c) for c in ('BIG_UP','BIG_DOWN','NO_BIG_MOVE'))<self.MIN_CLASS_ROWS:
            raise ValueError('Insufficient estimator-fit rows/classes')
        excursions=[numeric(r.maximum_favorable_excursion) for _,r in eligible]
        if any(v is None or v<0 for v in excursions): raise ValueError('Genuine excursions required')
        matrix=np.asarray([[numeric(f.values[name]) for name in schema] for f,_ in eligible],dtype=float)
        model=train_two_stage_model(horizon=horizon,train_x=matrix,labels=target,realized_excursions=excursions,
                                    feature_schema=schema,algorithm=algorithm)
        return model, {'feature_schema':schema,'feature_coverage':coverage,'rows_used':len(eligible),
            'excluded_features':{name:'TRAINING_COVERAGE_BELOW_90_PERCENT' for name in names if name not in schema}}

    def save_candidate_models(self,trained,revision_id,algorithm):
        status='CANDIDATE'
        definitions=[]; result=[]
        for model,manifest in trained:
            artifact=self.store.save(model,manifest)
            for stage in ('MOVE_DETECTOR','DIRECTION'):
                definitions.append({'model_checksum':digest({'artifact':artifact['artifact_checksum'],'stage':stage,'version':model.model_version}),
                    'model_family':'BIG_MOVE_V2','stage':stage,'horizon_minutes':model.horizon_minutes,'algorithm':algorithm,
                    'model_version':model.model_version,'dataset_revision_id':str(revision_id),'feature_set_version':FEATURE_SET_VERSION_V2,
                    **artifact,'status':status})
            result.append(definitions[-2])
        self.repository.save_big_move_model_definitions(definitions)
        return result

def live_features(state):
    index=state.minutes('INDEX')
    if not index or not state.session or state.session.status not in ('OPEN','SPECIAL'):
        raise ValueError('DATA_NOT_READY: no explicit open session/index data')
    as_of=index[-1].minute_at
    if not state.session.open_at<=as_of<state.session.close_at: raise ValueError('DATA_NOT_READY: outside session')
    expected={state.session.open_at+timedelta(minutes=i) for i in range(int((as_of-state.session.open_at).total_seconds()/60)+1)}
    if {r.minute_at for r in index}!=expected: raise ValueError('DATA_NOT_READY: incomplete index history')
    options=list(state.option_snapshots.values())
    current=[r for r in options if r.minute_at==as_of]
    universe=next(iter(state.option_universes.values()),None)
    if not universe or set(universe.instrument_ids)!={r.instrument_id for r in current}:
        options=[]  # Never reinterpret a partial or old option universe as complete.
    return build_big_move_features(as_of=as_of,index_candles=index,session=state.session,
        futures_candles=state.minutes('FUT'),option_snapshots=options)


class BigMoveRuntimeService:
    def __init__(self,repository=None,reader=None,store=None,journal=None):
        self.repository=repository or PostgresRepository(os.environ['DATABASE_URL'])
        self.reader=reader or LiveProductDataReader()
        self.store=store or BigMoveArtifactStore()
        self.journal=journal

    def run_once(self,now=None):
        now=now or datetime.now(timezone.utc)
        state=self.reader.current_state(now)
        result={h:'DATA_NOT_READY' for h in BIG_MOVE_HORIZONS}
        if state.quality!='PASS': return result
        try: snapshot=live_features(state)
        except ValueError: return result
        if snapshot.quality_status!='PASS': return result
        reference=state.minutes('INDEX')[-1]
        pending=[]
        for horizon,pair in self.repository.approved_big_move_models().items():
            if reference.minute_at+timedelta(minutes=horizon)>=state.session.close_at: continue
            try:
                model,manifest=self.store.load(pair['move'])
                schema=manifest['feature_schema']
                vector=[numeric(snapshot.values.get(name)) for name in schema]
                if any(value is None for value in vector): continue
                prediction=model.predict(vector,float(reference.close))
                policy=restore_policy(manifest['threshold_policy'])
                known=state.minutes('INDEX')[-policy.volatility_lookback_minutes-1:]
                # Freeze threshold history and reference at inference, not at later resolution.
                lineage={'policy':policy_payload(policy),'known_closes':[str(r.close) for r in known],
                    'known_times':[r.minute_at.isoformat() for r in known],
                    'threshold':str(policy.threshold(horizon,[r.close for r in known])),
                    'reference':{k:str(getattr(reference,k)) for k in ('open','high','low','close')},
                    'session':{'date':state.session.session_date.isoformat(),'open':state.session.open_at.isoformat(),
                               'close':state.session.close_at.isoformat(),'status':state.session.status},
                    'feature_values':json.loads(canonical_json(snapshot.values)),'generated_at':now.isoformat(),
                    'raw_big_move_probability':prediction['rawBigMoveProbability'],
                    'raw_up_given_big_move_probability':prediction['rawUpGivenBigMoveProbability'],
                    'move_decision_threshold':prediction['moveDecisionThreshold'],
                    'evaluation_checksum':manifest['evaluation_checksum'],
                    'approval_checksum':manifest['approval_decision_checksum'],
                    'approval_policy_version':manifest['approval_policy_version']}
                value={'prediction_id':digest({'move':pair['move']['model_checksum'],'direction':pair['direction']['model_checksum'],
                    'instrument':reference.instrument_id,'horizon':horizon,'as_of':reference.minute_at.isoformat()}),
                    'dataset_revision_id':str(pair['move']['dataset_revision_id']),'instrument_id':reference.instrument_id,
                    'feature_set_version':FEATURE_SET_VERSION_V2,'as_of':reference.minute_at,'horizon_minutes':horizon,
                    'expires_at':reference.minute_at+timedelta(minutes=horizon),'reference_price':reference.close,
                    'move_model_checksum':pair['move']['model_checksum'],'direction_model_checksum':pair['direction']['model_checksum'],
                    'market_regime':snapshot.market_regime,'session_bias':{**snapshot.session_bias,'runtime_lineage':lineage},
                    'quality_status':'PASS','model_version':model.model_version}
                for column,key in {'big_move_probability':'bigMoveProbability','quiet_probability':'quietProbability',
                    'state':'state','direction':'direction','direction_probability':'directionProbability',
                    'up_given_big_move_probability':'upGivenBigMoveProbability','down_given_big_move_probability':'downGivenBigMoveProbability',
                    'expected_move_points':'expectedMovePoints','expected_move_percent':'expectedMovePercent'}.items(): value[column]=prediction[key]
                pending.append(value)
            except Exception:
                log.exception('big_move_inference_failed horizon=%s',horizon)
                result[horizon]='DATA_NOT_READY'
        if pending:
            from .evidence_journal import EvidenceJournalService
            if self.journal is None: self.journal=EvidenceJournalService()
            try:
                shared=self.journal.prepare(snapshot)
                for value in pending:
                    horizon=value['horizon_minutes']
                    try:
                        if shared['decision_at']>=value['expires_at']:
                            continue  # Never turn an already-matured target into a prospective prediction.
                        value['session_bias']['runtime_lineage']['generated_at']=shared['decision_at'].isoformat()
                        evidence=self.journal.capture(value,snapshot,shared)
                        result[horizon]=self.repository.save_big_move_prediction_with_evidence(value,evidence)
                        if result[horizon]=='PERSISTED':
                            # Separate transaction, after mandatory production evidence commits.
                            try:
                                from .shadow_observations import capture_best_effort
                                capture_best_effort(value,evidence)
                            except Exception:
                                log.exception('SHADOW_EVIDENCE_UNAVAILABLE')
                    except Exception as error:
                        log.exception('big_move_evidence_persistence_failed horizon=%s',horizon)
                        result[horizon]='EVIDENCE_JOURNAL_MISSING' if 'EVIDENCE_JOURNAL_MISSING' in str(error) else 'DATA_NOT_READY'
            except Exception:
                log.exception('big_move_evidence_capture_failed')
        return result


class BigMoveOutcomeResolver:
    def __init__(self,repository,reader):
        self.repository,self.reader=repository,reader

    def run_once(self,now=None):
        now=now or datetime.now(timezone.utc)
        count=0
        for prediction in self.repository.pending_big_move_predictions(now):
            # Candle stamps are opening boundaries; its completed OHLC exists one minute later.
            if prediction['expires_at']+timedelta(minutes=1)>now: continue
            try:
                frozen=prediction['session_bias']['runtime_lineage']
                s=frozen['session']
                from datetime import date
                session=TradingSession(date.fromisoformat(s['date']),datetime.fromisoformat(s['open']),datetime.fromisoformat(s['close']),s['status'])
                policy=restore_policy(frozen['policy'])
                known=[Candle(datetime.fromisoformat(at),Decimal(close),Decimal(close),Decimal(close),Decimal(close),0)
                       for at,close in zip(frozen['known_times'],frozen['known_closes'])]
                ref=frozen['reference']
                reference=Candle(prediction['as_of'],*(Decimal(ref[k]) for k in ('open','high','low','close')),0)
                rows=self.reader.current_session_minutes('INDEX',session.session_date,now=now,instrument_id=prediction['instrument_id'])
                future=[Candle(r.minute_at,r.open,r.high,r.low,r.close,r.volume_delta or 0,r.oi_close) for r in rows
                        if prediction['as_of']<r.minute_at<=prediction['expires_at']]
                label=calculate_big_move_label(dataset_revision_id=str(prediction['dataset_revision_id']),instrument_id=prediction['instrument_id'],
                    as_of_candle=reference,horizon=prediction['horizon_minutes'],session=session,
                    candles_known_at_as_of=known,future_candles=future,policy=policy)
                if label is None: raise ValueError('Persisted prediction crosses its frozen session')
                if label.threshold_value!=Decimal(frozen['threshold']): raise ValueError('Frozen threshold lineage mismatch')
                if label.label=='UNKNOWN' and now<prediction['expires_at']+timedelta(minutes=6): continue
                count+=int(self.repository.append_big_move_prediction_outcome(prediction['prediction_id'],label.label,
                    label.quality_status,now,label.to_dict()))
            except Exception:
                log.exception('big_move_outcome_failed prediction=%s',prediction['prediction_id'])
        return count


class BigMoveRuntimeWorker:
    def __init__(self):
        self.stop_event=Event()
        self.thread=None

    def start(self):
        if self.thread and self.thread.is_alive(): return
        if not os.getenv('DATABASE_URL'): raise RuntimeError('V2 runtime requires DATABASE_URL')
        self.stop_event.clear()
        self.thread=Thread(target=self._run,name='big-move-v2-runtime',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join()

    def _run(self):
        service=BigMoveRuntimeService()
        resolver=BigMoveOutcomeResolver(service.repository,service.reader)
        interval=max(5,int(os.getenv('BIG_MOVE_RUNTIME_INTERVAL_SECONDS','15')))
        while not self.stop_event.is_set():
            now=datetime.now(timezone.utc)
            try: service.run_once(now)
            except Exception: log.exception('big_move_runtime_cycle_failed')
            try: resolver.run_once(now)
            except Exception: log.exception('big_move_resolution_cycle_failed')
            self.stop_event.wait(interval)
