"""Immutable event inputs and measured outcomes. Priors are not predictions."""
from datetime import datetime, timedelta, timezone
from statistics import mean, median, pstdev
import math
import os

from ..calendar import IST
from ..market_structure_context import (MarketStructureContextRepository, MarketStructureContextReader,
    _minute_map, _ret)
from ..research_context import _hash, _plain, _stamp
from .models import normalized_headline

EVENT_IMPACT_STUDY_VERSION = 'banknifty-event-impact-v1'
RELIABLE_TIMING = {'CONFIRMED_FIRST_SEEN','CONFIRMED_OFFICIAL_RELEASE','CONFIRMED_LIVE_UPDATE','PREOPEN_KNOWN'}


def event_known_at(event):
    values=[v for v in (event.known_at,event.first_seen_at) if v is not None]
    return max(map(_stamp,values)) if values else None


def source_authority(event):
    source=(event.source_name or '').upper().replace(' ','_')
    if source in ('RBI_OFFICIAL','NSE_OFFICIAL'): return 'OFFICIAL_PRIMARY'
    if source in ('MONEYCONTROL','ET_MARKETS','LIVEMINT','UPSTOX_INSTRUMENT','UPSTOX_NEWS'): return 'MAJOR_FINANCIAL_MEDIA'
    if event.source_url and event.source_quality in ('VERIFIED','OFFICIAL','PRIMARY_SOURCE'): return 'OTHER_VERIFIED'
    return 'UNKNOWN'


def similarity_dimensions(event):
    metadata=event.source_metadata
    state=metadata.get('preEventState',{})
    return {'primaryCategory':event.event_category,'stance':event.stance,'scope':event.scope,
        'affectedSymbols':list(event.affected_symbols),'sessionPhase':state.get('sessionPhase',event.knowledge_phase),
        'scheduled':event.scheduled,'marketRegime':state.get('regime'),
        'marketStructureRegime':state.get('marketStructure',{}).get('regime'),
        'expiryContext':state.get('expiryContext'),'noveltyClass':metadata.get('novelty',{}).get('noveltyClass')}


def comparable_similarity(left,right):
    scores,reasons=[],[]
    for key,value in left.items():
        other=right.get(key)
        if value is None or other is None or value in ('UNKNOWN','UNAVAILABLE','') or other in ('UNKNOWN','UNAVAILABLE',''): continue
        if key=='affectedSymbols':
            union=set(value)|set(other)
            if not value or not other: continue
            score=len(set(value)&set(other))/len(union)
        else: score=float(value==other)
        scores.append(score)
        reasons.append({'dimension':key,'current':value,'historical':other,'agreement':score})
    return (mean(scores) if scores else 0),reasons


class EventNoveltyService:
    def classify(self,event,prior_events):
        known=event_known_at(event)
        if known is None: return {'noveltyClass':'UNKNOWN','noveltyScore':None,'comparedEvents':0}
        terms=set(normalized_headline(event.headline_or_summary).split())
        similarities=[]
        for prior in prior_events:
            stamp=event_known_at(prior)
            if prior.event_id==event.event_id or stamp is None or not known-timedelta(minutes=120)<=stamp<known: continue
            if stamp.astimezone(IST).date()!=known.astimezone(IST).date(): continue
            other=set(normalized_headline(prior.headline_or_summary).split())
            text=len(terms&other)/len(terms|other) if terms|other else 0
            dimensions,_=comparable_similarity(
                {'primaryCategory':event.event_category,'stance':event.stance,'scope':event.scope,'affectedSymbols':event.affected_symbols},
                {'primaryCategory':prior.event_category,'stance':prior.stance,'scope':prior.scope,'affectedSymbols':prior.affected_symbols})
            secondary=set(event.secondary_categories)|set(prior.secondary_categories)
            secondary_score=len(set(event.secondary_categories)&set(prior.secondary_categories))/len(secondary) if secondary else None
            score=mean([text,dimensions,*([secondary_score] if secondary_score is not None else [])])
            similarities.append((text,score))
        overlap=max((text for text,_ in similarities),default=0)
        score=max((score for _,score in similarities),default=0)
        kind='DUPLICATE_OR_SYNDICATED' if overlap>=.85 and score>=.65 else 'FOLLOW_UP' if overlap>=.25 and score>=.5 else 'NEW_INFORMATION'
        return {'noveltyClass':kind,'noveltyScore':max(0,min(1,1-score)),'comparedEvents':len(similarities)}


class HistoricalReactionPriorService:
    def __init__(self,minimum=None):
        self.minimum=max(1,int(minimum if minimum is not None else os.getenv('EVENT_IMPACT_MINIMUM_SAMPLE','5')))

    def summarize(self,features,studies,decision_at):
        decision=_stamp(decision_at)
        eligible=[]
        for study in studies:
            reaction=study['reaction_summary']
            measured=reaction.get('measuredAt')
            if not measured or _stamp(measured)>decision or _stamp(study['created_at'])>decision: continue
            if _stamp(study['event_known_at'])>=decision: continue
            if reaction.get('status')!='COMPLETE_30M': continue
            eligible.append(study)
        groups=[('primaryCategory','stance','sessionPhase','marketRegime'),('primaryCategory','stance','sessionPhase'),
            ('primaryCategory','stance'),('primaryCategory',)]
        result={'kind':'HISTORICAL_REACTION_PRIOR','minimumSample':self.minimum,'horizons':{}}
        for horizon in (5,10,20,30):
            selected=[]; grouping=[]
            for keys in groups:
                if any(features.get(k) in (None,'UNKNOWN','UNAVAILABLE','') for k in keys): continue
                matched=[s for s in eligible if all(s['event_features'].get(k)==features[k] for k in keys)]
                valid=[]
                for study in matched:
                    reaction=study['reaction_summary']; value=reaction.get('returns',{}).get(str(horizon))
                    if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value): valid.append(study)
                selected,grouping=valid,list(keys)
                if len(selected)>=self.minimum: break
            n=len(selected)
            summary={'status':'AVAILABLE' if n>=self.minimum else 'INSUFFICIENT_HISTORY','sampleCount':n,'grouping':grouping}
            if n>=self.minimum:
                returns=[s['reaction_summary']['returns'][str(horizon)] for s in selected]
                summary.update(meanReturn=mean(returns),medianReturn=median(returns),positiveRate=sum(r>0 for r in returns)/n,negativeRate=sum(r<0 for r in returns)/n)
                if horizon!=5:
                    labels=[s['reaction_summary'].get('big_move',{}).get(str(horizon),{}).get('label') for s in selected]
                    labels=[label for label in labels if label in ('BIG_UP','BIG_DOWN','AMBIGUOUS','NO_BIG_MOVE')]
                    summary.update(bigMoveRate=sum(l in ('BIG_UP','BIG_DOWN','AMBIGUOUS') for l in labels)/len(labels) if len(labels)>=self.minimum else None,
                        bigUpRate=labels.count('BIG_UP')/len(labels) if len(labels)>=self.minimum else None,bigDownRate=labels.count('BIG_DOWN')/len(labels) if len(labels)>=self.minimum else None,
                        bigMoveSampleCount=len(labels))
                for source,target in (('max_up_excursion','meanMaxUpExcursion'),('max_down_excursion','meanMaxDownExcursion'),('volatility_expansion','volatilityExpansionRate')):
                    values=[s['reaction_summary'].get('horizon_metrics',{}).get(str(horizon),{}).get(source) for s in selected]
                    values=[v for v in values if isinstance(v,(int,float)) and math.isfinite(v)]
                    summary[target]=(mean(v>1 for v in values) if source=='volatility_expansion' else mean(values)) if len(values)>=self.minimum else None
                    summary[target+'SampleCount']=len(values)
            result['horizons'][str(horizon)]=summary
        return result


class EventImpactStudyService:
    def __init__(self,repository=None,reader=None):
        self.repository=repository or MarketStructureContextRepository()
        self.reader=reader or MarketStructureContextReader(self.repository)

    def _input(self,event_id):
        with self.repository.connection() as db:
            return db.execute('SELECT * FROM market_event_impact_inputs WHERE event_id=%s AND study_version=%s',
                (event_id,EVENT_IMPACT_STUDY_VERSION)).fetchone()

    def freeze(self,event,prior_events=()):
        existing=self._input(event.event_id)
        if existing: return existing
        known=event_known_at(event)
        if not event.predictive_eligible or event.context_only or event.timing_confidence not in RELIABLE_TIMING or known is None: return None
        # Even a delayed catch-up may only see revisions actually finalized before known.
        # No historical provider reader is permitted on this prospective path.
        context=self.reader.runtime(known.replace(second=0,microsecond=0),known)
        rows=context.pop('priceRows',[])
        series=_minute_map(rows,known)
        at=known.replace(second=0,microsecond=0)-timedelta(minutes=1)
        last=series.get(at)
        returns=[_ret(series,at-timedelta(minutes=i),1) for i in range(20)]
        complete=all(v is not None for v in returns)
        regime='UNKNOWN'
        if complete and context.get('session'):
            from ..big_move_features import build_big_move_features
            from ..models import Candle, TradingSession
            from decimal import Decimal
            session=context['session']
            candles=[Candle(stamp,*(Decimal(str(value[k])) for k in ('open','high','low','close')),int(value['volume'] or 0),None)
                for stamp,value in sorted(series.items())]
            regime=build_big_move_features(as_of=at,index_candles=candles,session=TradingSession(known.astimezone(IST).date(),
                _stamp(session['market_open_utc']),_stamp(session['market_close_utc']))).market_regime
        state={'price':{f'return_{h}m':_ret(series,at,h) for h in (5,10,20)},
            'realizedVolatility':pstdev(returns) if complete else None,
            'regime':regime, 'sessionPhase':event.knowledge_phase,
            'distanceFromSessionHigh':last['close']/max(r['high'] for r in series.values())-1 if last else None,
            'distanceFromSessionLow':last['close']/min(r['low'] for r in series.values())-1 if last else None,
            'marketStructure':context.get('marketStructure',{}), 'constituents':context.get('constituentContext'),
            'futures':context.get('futuresContext'),'options':context.get('optionsContext'),
            'expiryContext':(context.get('optionsContext') or {}).get('values',{}).get('expiry_context','UNKNOWN'),
            'globals':context.get('globalContext'), 'availability':context.get('status')}
        state['stalenessPolicy']=context.get('stalenessPolicy')
        state['globalContextStatus']=context.get('globalContextStatus','UNAVAILABLE')
        novelty=EventNoveltyService().classify(event,prior_events)
        features={'primaryCategory':event.event_category,'secondaryCategories':list(event.secondary_categories),
            'stance':event.stance,'scope':event.scope,'affectedSymbols':list(event.affected_symbols),
            'scheduled':event.scheduled,'knowledgePhase':event.knowledge_phase,'sessionPhase':event.knowledge_phase,
            'timingConfidence':event.timing_confidence,'sourceAuthority':source_authority(event),'relevance':event.relevance,
            'marketRegime':state['regime'],'marketStructureRegime':state['marketStructure'].get('regime'),**novelty}
        row={'event_id':event.event_id,'study_version':EVENT_IMPACT_STUDY_VERSION,'event_known_at':known,
            'pre_event_state':state,'event_features':features,'source_lineage':{'eventHash':event.content_hash,
                'decisionCutoff':known.isoformat(),'marketAsOf':context.get('marketAsOf'),
                'liveMinutes':context.get('sourceLineage',[]),'availabilityBasis':'FIRST_FINALIZED_AND_FIRST_SEEN_CUTOFF',
                'eventPayload':event.to_dict()}}
        row['input_checksum']=_hash(row)
        with self.repository.connection(write=True) as db:
            self.repository.insert(db,'market_event_impact_inputs',row,['event_id','study_version'])
        return self._input(event.event_id)

    def complete(self,event,reaction):
        if reaction.get('status')!='COMPLETE_30M': return False
        frozen=self._input(event.event_id)
        if not frozen: return False  # Never manufacture frozen inputs from final outcomes.
        at=_stamp(reaction['reference_at'])
        if at<frozen['event_known_at']: return False
        row={k:frozen[k] for k in ('event_id','study_version','event_known_at','pre_event_state','event_features','source_lineage')}
        row.update(reference_minute=at,reference_price=reaction['reference_price'],reaction_summary=reaction)
        row['study_checksum']=_hash(row)
        with self.repository.connection(write=True) as db:
            existing=db.execute('SELECT 1 FROM market_event_impact_studies WHERE event_id=%s AND study_version=%s',
                (event.event_id,EVENT_IMPACT_STUDY_VERSION)).fetchone()
            if existing: return False
            self.repository.insert(db,'market_event_impact_studies',row,['event_id','study_version'])
        return True

    def detail(self,event_id):
        with self.repository.connection() as db:
            study=db.execute('SELECT * FROM market_event_impact_studies WHERE event_id=%s AND study_version=%s',
                (event_id,EVENT_IMPACT_STUDY_VERSION)).fetchone()
            frozen=study or db.execute('SELECT * FROM market_event_impact_inputs WHERE event_id=%s AND study_version=%s',
                (event_id,EVENT_IMPACT_STUDY_VERSION)).fetchone()
            if not frozen: return {'available':False,'status':'INPUT_NOT_CAPTURED'}
            previous=db.execute('SELECT * FROM market_event_impact_studies WHERE study_version=%s AND created_at<=%s AND event_id<>%s',
                (EVENT_IMPACT_STUDY_VERSION,frozen['event_known_at'],event_id)).fetchall()
        return {'available':True,'status':'COMPLETE' if study else 'PENDING_REACTION',
            'study':_plain(frozen),'historicalReactionPrior':HistoricalReactionPriorService().summarize(
                frozen['event_features'],previous,frozen['event_known_at'])}

    def complete_pending(self):
        from .models import MarketNewsEvent
        with self.repository.connection() as db:
            rows=db.execute('''SELECT i.source_lineage,r.reaction_payload FROM market_event_impact_inputs i
                JOIN market_event_reactions r USING(event_id)
                LEFT JOIN market_event_impact_studies s ON s.event_id=i.event_id AND s.study_version=i.study_version
                WHERE i.study_version=%s AND s.event_id IS NULL AND r.reaction_quality='COMPLETE_30M'
                ORDER BY i.event_known_at LIMIT 100''',(EVENT_IMPACT_STUDY_VERSION,)).fetchall()
        for row in rows:
            self.complete(MarketNewsEvent.from_dict(row['source_lineage']['eventPayload']),row['reaction_payload'])

    def enrich_events(self,events,decision_at=None):
        """Only frozen inputs enrich similarity; outcomes remain independently gated."""
        from dataclasses import replace
        if not events: return events
        with self.repository.connection() as db:
            rows=db.execute('''SELECT event_id,pre_event_state,event_features FROM market_event_impact_inputs
                WHERE event_id=ANY(%s) AND study_version=%s AND (%s::timestamptz IS NULL OR created_at<=%s)''',
                ([e.event_id for e in events],EVENT_IMPACT_STUDY_VERSION,decision_at,decision_at)).fetchall()
        inputs={r['event_id']:r for r in rows}
        return [replace(e,source_metadata={**e.source_metadata,'preEventState':inputs[e.event_id]['pre_event_state'],
                    'novelty':{k:inputs[e.event_id]['event_features'].get(k) for k in ('noveltyClass','noveltyScore')}})
                if e.event_id in inputs else e for e in events]

    def summary(self):
        with self.repository.connection() as db:
            eligible=db.execute('''SELECT count(*) AS n FROM market_news_events WHERE predictive_eligible
                AND event_payload->>'timing_confidence'=ANY(%s)
                AND COALESCE(event_payload->>'known_at',event_payload->>'first_seen_at') IS NOT NULL''',
                (sorted(RELIABLE_TIMING),)).fetchone()['n']
            studies=db.execute('SELECT * FROM market_event_impact_studies WHERE study_version=%s ORDER BY event_known_at',
                (EVENT_IMPACT_STUDY_VERSION,)).fetchall()
            inputs=db.execute('SELECT event_id,event_known_at,event_features FROM market_event_impact_inputs WHERE study_version=%s',
                (EVENT_IMPACT_STUDY_VERSION,)).fetchall()
        prior=HistoricalReactionPriorService()
        insufficient=max(0,eligible-len(inputs))+sum(any(h['status']=='INSUFFICIENT_HISTORY' for h in prior.summarize(i['event_features'],studies,i['event_known_at'])['horizons'].values()) for i in inputs)
        return {'status':'AVAILABLE' if studies else 'INSUFFICIENT_HISTORY','studyVersion':EVENT_IMPACT_STUDY_VERSION,
            'eligibleEvents':eligible,'frozenInputEvents':len(inputs),'completedImpactStudies':len(studies),'eventsInsufficientHistory':insufficient}
