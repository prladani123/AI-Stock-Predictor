"""Best-effort prospective evidence. No outcome writes, model registry or providers."""
from copy import deepcopy
from collections import Counter
from datetime import datetime, timezone
import logging
import os

from .repository import digest
from .microstructure import stamp

SCHEMA_VERSION='banknifty-shadow-v1'
log=logging.getLogger(__name__)


def validate_evidence(value,decision,market_limit=None):
    """Check actual observation/receipt watermarks, not future scheduled-event dates."""
    if isinstance(value,dict):
        if value.get('marketAsOf'):
            local_market=stamp(value['marketAsOf'])
            market_limit=min(market_limit,local_market) if market_limit else local_market
        for key,item in value.items():
            if key in ('sourceMaxTimestamp','source_max_timestamp','marketAsOf','decisionAt','receivedAt','first_seen_at','firstSeenAt','sourceObservedAt','effectiveAt','referenceMinute','asOf') and item:
                if stamp(item)>decision: raise ValueError('SHADOW_FUTURE_TIMESTAMP')
            if key in ('sourceMaxTimestamp','source_max_timestamp') and item and market_limit and stamp(item)>market_limit:
                raise ValueError('SHADOW_SOURCE_AFTER_MARKET_AS_OF')
            validate_evidence(item,decision,market_limit)
    elif isinstance(value,(list,tuple)):
        for item in value: validate_evidence(item,decision,market_limit)


class MemoryShadowRepository:
    def __init__(self): self.rows={}

    def save(self,value):
        if digest({k:v for k,v in value.items() if k!='shadow_checksum'})!=value['shadow_checksum']:
            raise ValueError('SHADOW_CHECKSUM_MISMATCH')
        current=self.rows.get(value['prediction_id'])
        if current and current!=value: raise ValueError('IMMUTABLE_SHADOW_CONFLICT')
        self.rows.setdefault(value['prediction_id'],deepcopy(value))
        return 'REUSED' if current else 'SAVED'


class ShadowRepository:
    def __init__(self,database_url=None):
        from .repository import PostgresRepository
        self.repository=PostgresRepository(database_url if database_url is not None else os.getenv('DATABASE_URL',''))
        self.repository.connection_options={'connect_timeout':5,'options':'-c statement_timeout=5000'}

    def save(self,value):
        from psycopg import sql
        from psycopg.types.json import Jsonb
        if digest({k:v for k,v in value.items() if k!='shadow_checksum'})!=value['shadow_checksum']:
            raise ValueError('SHADOW_CHECKSUM_MISMATCH')
        with self.repository._connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('shadow:'+value['prediction_id'],))
            existing=db.execute('SELECT shadow_checksum FROM big_move_shadow_observations WHERE prediction_id=%s',(value['prediction_id'],)).fetchone()
            if existing:
                if existing[0]!=value['shadow_checksum']: raise ValueError('IMMUTABLE_SHADOW_CONFLICT')
                return 'REUSED'
            columns=tuple(value)
            db.execute(sql.SQL('INSERT INTO big_move_shadow_observations ({}) VALUES ({})').format(
                sql.SQL(',').join(map(sql.Identifier,columns)),sql.SQL(',').join(sql.Placeholder() for _ in columns)),
                [Jsonb(value[k]) if isinstance(value[k],dict) else value[k] for k in columns])
        return 'SAVED'

    def recent(self,limit=50):
        from .live_product_reader import LiveProductDataReader
        with LiveProductDataReader(self.repository.database_url)._connection() as db:
            rows=db.execute('''SELECT s.prediction_id,s.market_as_of,s.decision_at,s.horizon_minutes,
                s.context_v3_evidence->>'status' AS context_status,
                s.microstructure_evidence->>'status' AS microstructure_status,
                p.state,p.direction,p.big_move_probability,o.actual_label,o.quality_status AS outcome_quality
                FROM big_move_shadow_observations s JOIN big_move_predictions p USING(prediction_id)
                LEFT JOIN big_move_prediction_outcomes o ON o.prediction_id=s.prediction_id
                    AND o.attached_at<=now() AND p.expires_at<=now()
                ORDER BY s.market_as_of DESC,s.prediction_id LIMIT %s''',(max(1,min(int(limit),10000)),)).fetchall()
        return [dict(row,market_as_of=row['market_as_of'].isoformat(),decision_at=row['decision_at'].isoformat(),
                     big_move_probability=float(row['big_move_probability'])) for row in rows]

    def summary(self):
        rows=self.recent(10000)
        cohorts={}; horizons={}; reasons=Counter()
        for row in rows:
            context=row['context_status'] in ('PASS','READY')
            micro=row['microstructure_status']=='PASS'
            names=['ALL_CAPTURED_BASELINE','BASELINE_PLUS_CONTEXT_V3_AVAILABLE' if context else 'CONTEXT_V3_UNAVAILABLE',
                   'BASELINE_PLUS_MICROSTRUCTURE_AVAILABLE' if micro else 'MICROSTRUCTURE_UNAVAILABLE']
            if not context and not micro: names.append('BASELINE_ONLY')
            if context and micro: names.append('BASELINE_PLUS_CONTEXT_AND_MICROSTRUCTURE')
            resolved=row['actual_label'] in ('BIG_UP','BIG_DOWN','NO_BIG_MOVE') and row['outcome_quality']=='VALID'
            for name in names:
                c=cohorts.setdefault(name,{'captured':0,'resolved':0,'decided':0,'stageACorrect':0,'brierSum':0.0})
                c['captured']+=1
                if resolved:
                    actual=row['actual_label']!='NO_BIG_MOVE'; c['resolved']+=1
                    if row['state'] in ('BIG_MOVE','NO_BIG_MOVE'):
                        c['decided']+=1
                        c['stageACorrect']+=int((row['state']=='BIG_MOVE')==actual)
                    c['brierSum']+=(row['big_move_probability']-int(actual))**2
            h=horizons.setdefault(str(row['horizon_minutes']),{'captured':0,'resolved':0,'contextAvailable':0,'microstructureAvailable':0})
            h['captured']+=1; h['resolved']+=int(resolved); h['contextAvailable']+=int(context); h['microstructureAvailable']+=int(micro)
            if not context: reasons['CONTEXT_V3_UNAVAILABLE']+=1
            if not micro: reasons['MICROSTRUCTURE_UNAVAILABLE']+=1
        for c in cohorts.values():
            correct=c.pop('stageACorrect'); brier=c.pop('brierSum')
            c['stageAAccuracy']=correct/c['decided'] if c['decided'] else None
            c['stageABrier']=brier/c['resolved'] if c['resolved'] else None
        resolved=sum(h['resolved'] for h in horizons.values())
        return {'status':'SHADOW_DATA_AVAILABLE' if resolved>=100 else 'SHADOW_DATA_INSUFFICIENT',
            'captureStatus':'SHADOW_CAPTURE_ACTIVE' if rows else 'SHADOW_CAPTURE_READY',
            'capturedCount':len(rows),'resolvedCount':resolved,'minimumSample':100,
            'minimumSampleState':'AVAILABLE' if resolved>=100 else 'INSUFFICIENT',
            'sampleWindow':'LATEST_10000_CAPTURED_OBSERVATIONS','horizons':horizons,'cohorts':cohorts,'missingness':dict(reasons),
            'notice':'DESCRIPTIVE ONLY; overlapping cohorts; no causal or predictive improvement claim'}


class ShadowObservationService:
    def __init__(self,repository=None,context=None,microstructure=None):
        from .context_v3 import ContextV3RuntimeFeatureService
        from .microstructure import MicrostructureRuntimeService
        self.repository=repository or ShadowRepository()
        self.context=context or ContextV3RuntimeFeatureService()
        self.microstructure=microstructure or MicrostructureRuntimeService()

    def capture(self,prediction,journal):
        if prediction['quality_status']!='PASS' or journal['prediction_id']!=prediction['prediction_id']:
            raise ValueError('SHADOW_REQUIRES_VALID_PREDICTION')
        decision=stamp(journal['decision_at'])
        market=stamp(journal['market_as_of'])
        if market!=stamp(prediction['as_of']): raise ValueError('SHADOW_MARKET_CUTOFF_MISMATCH')
        if market>decision: raise ValueError('SHADOW_FUTURE_TIMESTAMP')
        if decision>=stamp(prediction['expires_at']): raise ValueError('SHADOW_NOT_PROSPECTIVE')
        context=self.context.snapshot(decision)
        micro=self.microstructure.snapshot(decision)
        validate_evidence(context,decision); validate_evidence(micro,decision)
        value={'prediction_id':prediction['prediction_id'],'journal_id':journal['journal_id'],
            'instrument_id':prediction['instrument_id'],'market_as_of':market,'decision_at':decision,
            'horizon_minutes':prediction['horizon_minutes'],'context_v3_evidence':context,
            'microstructure_evidence':micro,'greeks_evidence':{'status':'GREEKS_UNAVAILABLE'},
            'source_watermarks':{'journalChecksum':journal['journal_checksum'],
                'contextMarketAsOf':context.get('marketAsOf'),'microstructureSourceMaxTimestamp':micro.get('sourceMaxTimestamp')},
            'schema_versions':{'shadow':SCHEMA_VERSION,'microstructure':micro.get('schemaVersion'),
                'contextProfiles':sorted(context.get('profiles',{}))}}
        value['shadow_id']=digest({'prediction':prediction['prediction_id'],'schema':SCHEMA_VERSION})
        value['shadow_checksum']=digest(value)
        return self.repository.save(value)


def capture_best_effort(prediction,journal,service=None):
    try: return (service or ShadowObservationService()).capture(prediction,journal)
    except Exception:
        log.exception('SHADOW_EVIDENCE_UNAVAILABLE prediction_id=%s',prediction.get('prediction_id'))
        return 'SHADOW_EVIDENCE_UNAVAILABLE'
