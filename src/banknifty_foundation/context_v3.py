"""Context V3 profiles: shared pure calculations, explicit availability, no models.

V3 as_of is a decision/availability boundary. A candle stamped t opens at t
and is usable at t+1 minute. referenceMinute retains V2's candle/label key.
No retrospective REST receipt is represented as prospective evidence.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite, log, sqrt
from pathlib import Path
from statistics import mean, pstdev
from types import MappingProxyType
import json

from .calendar import IST
from .config import ResearchContextSettings
from .paths import memory_root
from .research_context import _hash, _plain, _stamp, HistoricalResearchContextReader, ResearchContextRepository, RESEARCH_CONTEXT_VERSION
from .market_structure_context import MarketStructureContextReader, MarketStructureContextRepository, market_structure, MARKET_STRUCTURE_CONTEXT_VERSION
from .research_models import FeatureRow, FeatureSetDefinition

UTC=timezone.utc
CONTEXT_V3_PLAN_VERSION='context-v3-plan-v1'
HORIZONS=(10,20,30)
BLOCKS=('PRICE_VOL','INDIA_MARKET','GLOBAL','DERIVATIVES','BANK_STRUCTURE','SESSION_STRUCTURE')
PHASES=('CONTINUOUS','CAS_REFERENCE','CAS_ORDER_ENTRY_MARKET_LIMIT','CAS_ORDER_ENTRY_LIMIT_ONLY','CAS_MATCHING','POST_INDEX_CLOSE','UNKNOWN')
EVENT_TYPES=('RBI_MPC','INDIA_CPI','INDIA_WPI','INDIA_GDP','INDIA_IIP','FED_DECISION')
RETROSPECTIVE='RETROSPECTIVE_PROVIDER_TIMESTAMP'
PROSPECTIVE='PROSPECTIVE_RECEIPT_VERIFIED'
PROFILES=MappingProxyType({
    'banknifty-price-vol-v3':('PRICE_VOL','SESSION_STRUCTURE'),
    'banknifty-global-context-v3':('PRICE_VOL','SESSION_STRUCTURE','INDIA_MARKET','GLOBAL'),
    'banknifty-derivatives-context-v3':('PRICE_VOL','SESSION_STRUCTURE','DERIVATIVES'),
    'banknifty-structure-context-v3':('PRICE_VOL','SESSION_STRUCTURE','BANK_STRUCTURE'),
    'banknifty-full-context-v3':BLOCKS,
})
LIMITS=MappingProxyType({'NIFTY50':120,'INDIA_VIX':180,'GIFT_NIFTY':300,'BRENT':900,'USDINR':900,'FUTURES':120,'OPTIONS':120,'BANKS':120})


def number(value):
    if value is None or isinstance(value,bool): return None
    try: result=float(value)
    except (ValueError,TypeError,OverflowError): return None
    return result if isfinite(result) else None


def ratio(left,right):
    return left/right if left is not None and right is not None and right>0 else None


@dataclass(frozen=True)
class FeatureBlock:
    values: dict
    status: str
    sourceMaxTimestamp: datetime | None
    missingReasons: tuple[str,...]
    sourceLineage: dict

    def payload(self): return _plain(asdict(self))


def block(values,required,timestamps=(),reasons=(),lineage=None):
    clean={key:number(value) for key,value in values.items()}
    missing=[key for key in required if clean.get(key) is None]
    status='PASS' if not missing else 'PARTIAL' if any(clean.get(key) is not None for key in required) else 'UNAVAILABLE'
    return FeatureBlock(clean,status,max(timestamps,default=None),tuple(dict.fromkeys([*reasons,*('MISSING:'+key for key in missing)])),lineage or {})


def _session_bounds(session):
    if not session or session.get('status')!='OPEN': return None
    day=date.fromisoformat(str(session['session_date']))
    opening=max(_stamp(session['market_open_utc']),datetime.combine(day,time(9,15),IST).astimezone(UTC))
    close=min(_stamp(session['market_close_utc']),datetime.combine(day,time(15,30),IST).astimezone(UTC))
    return (opening,close) if opening<close else None


def _candles(inputs):
    as_of=_stamp(inputs['as_of']); bounds=_session_bounds(inputs['session'])
    rows,conflicts={},set()
    if not bounds: return {},['EXPLICIT_INDEX_SESSION_REQUIRED']
    for original in inputs.get('candles',[]):
        row=dict(original); at=_stamp(row['at'])
        if at.second or at.microsecond or not bounds[0]<=at<bounds[1] or at+timedelta(minutes=1)>as_of: continue
        receipt=row.get('first_finalized_at')
        if inputs['mode']==PROSPECTIVE and (not receipt or _stamp(receipt)>as_of): continue
        values={key:number(row.get(key)) for key in ('open','high','low','close','volume')}
        if any(values[key] is None or values[key]<=0 for key in ('open','high','low','close')): conflicts.add(at); continue
        if not values['low']<=min(values['open'],values['close'])<=max(values['open'],values['close'])<=values['high']:
            conflicts.add(at); continue
        if values['volume'] is not None and values['volume']<0: values['volume']=None
        if at in rows: conflicts.add(at)
        rows[at]={**values,'receipt':_stamp(receipt) if receipt and inputs['mode']==PROSPECTIVE else None,
            'checksum':row.get('checksum'),'revision':row.get('revision')}
    return {at:r for at,r in rows.items() if at not in conflicts},['SOURCE_CONFLICT_PRICE'] if conflicts else []


def _window(rows,at,count):
    keys=[at-timedelta(minutes=i) for i in reversed(range(count))]
    return [rows[k] for k in keys] if all(k in rows for k in keys) else []


def _return(rows,at,horizon):
    window=_window(rows,at,horizon+1)
    return window[-1]['close']/window[0]['close']-1 if window else None


def _log_returns(window):
    return [log(b['close']/a['close']) for a,b in zip(window,window[1:])]


def price_vol_block(inputs):
    rows,reasons=_candles(inputs); as_of=_stamp(inputs['as_of'])
    at=as_of.replace(second=0,microsecond=0)-timedelta(minutes=1)
    values={f'return_{h}m':_return(rows,at,h) for h in (1,2,3,5,10,15,20,30,60)}
    required=list(values)
    for h in (5,10,20,30,60):
        window=_window(rows,at,h+1); returns=_log_returns(window)
        variance=sum(r*r for r in returns) if window else None
        values[f'realized_variance_{h}m']=variance
        values[f'realized_volatility_{h}m']=sqrt(variance) if variance is not None else None
        required.extend((f'realized_variance_{h}m',f'realized_volatility_{h}m'))
        if h<=30:
            up=sum(max(r,0)**2 for r in returns) if window else None
            down=sum(min(r,0)**2 for r in returns) if window else None
            values.update({f'upside_semivariance_{h}m':up,f'downside_semivariance_{h}m':down,
                f'downside_upside_variance_ratio_{h}m':ratio(down,up)})
    for h in (5,10,20,30,60):
        window=_window(rows,at,h)
        hl=[log(r['high']/r['low'])**2 for r in window]
        gk=[.5*log(r['high']/r['low'])**2-(2*log(2)-1)*log(r['close']/r['open'])**2 for r in window]
        values[f'high_low_range_volatility_{h}m']=sqrt(mean(hl)) if hl else None
        values[f'parkinson_range_variance_{h}m']=mean(hl)/(4*log(2)) if hl else None
        values[f'garman_klass_variance_{h}m']=mean(gk) if gk and all(v>=0 for v in gk) else None
    values['volatility_expansion_5_20']=ratio(values['realized_volatility_5m'],values['realized_volatility_20m'])
    values['volatility_expansion_10_30']=ratio(values['realized_volatility_10m'],values['realized_volatility_30m'])
    rolling=[_window(rows,at-timedelta(minutes=i),6) for i in range(20)]
    values['volatility_of_volatility_20m']=pstdev(sqrt(sum(r*r for r in _log_returns(w))) for w in rolling) if all(rolling) else None
    recent,prior=_window(rows,at,5),_window(rows,at-timedelta(minutes=5),20)
    values['range_compression_ratio']=ratio(mean((r['high']-r['low'])/r['close'] for r in recent) if recent else None,
        mean((r['high']-r['low'])/r['close'] for r in prior) if prior else None)
    current=rows.get(at)
    w15=_window(rows,at,15)
    tr=[max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])) for a,b in zip(w15,w15[1:])]
    values['atr_normalized_14m']=ratio(mean(tr) if tr else None,current['close'] if current else None)
    def ema(window,span):
        if not window: return None
        value=window[0]['close']; alpha=2/(span+1)
        for r in window[1:]: value=alpha*r['close']+(1-alpha)*value
        return value
    w61=_window(rows,at,61)
    fast,slow,previous=ema(w61,5),ema(w61,20),ema(w61[:-1],5) if w61 else None
    values['ema_slope_5']=fast/previous-1 if previous else None
    values['ema_fast_slow_spread']=ratio(fast-slow,current['close']) if current and fast is not None and slow is not None else None
    for h in (20,30):
        window=_window(rows,at,h)
        values[f'distance_from_high_{h}m']=current['close']/max(r['high'] for r in window)-1 if current and window else None
        values[f'distance_from_low_{h}m']=current['close']/min(r['low'] for r in window)-1 if current and window else None
    bounds=_session_bounds(inputs['session'])
    opening=_window(rows,bounds[0]+timedelta(minutes=14),15) if bounds and at>=bounds[0]+timedelta(minutes=14) else []
    values['opening_range_position']=ratio(current['close']-min(r['low'] for r in opening),max(r['high'] for r in opening)-min(r['low'] for r in opening)) if opening and current else None
    day_count=int((at-bounds[0]).total_seconds()/60)+1 if bounds and at>=bounds[0] else 0
    day_rows=_window(rows,at,day_count) if day_count>0 else []
    vwap=None
    if day_rows and all(r['volume'] is not None for r in day_rows):
        vwap=ratio(sum((r['high']+r['low']+r['close'])/3*r['volume'] for r in day_rows),sum(r['volume'] for r in day_rows))
    values['vwap_distance']=current['close']/vwap-1 if current and vwap else None
    previous20=_window(rows,at-timedelta(minutes=1),20)
    volumes=[r['volume'] for r in previous20]
    valid_volume=current is not None and current['volume'] is not None and bool(volumes) and all(v is not None for v in volumes)
    baseline=mean(volumes) if valid_volume else None
    values['volume_spike_vs_prior20']=ratio(current['volume'],baseline) if valid_volume else None
    values['volume_zscore_prior20']=(current['volume']-baseline)/pstdev(volumes) if valid_volume and pstdev(volumes)>0 else None
    stamps=[stamp+timedelta(minutes=1) for stamp in rows]
    stamps += [r['receipt'] for r in rows.values() if r['receipt']]
    if current is None: reasons.append('LATEST_COMPLETED_MINUTE_UNAVAILABLE')
    return block(values,required,stamps,reasons,{'basis':inputs['mode'],'priceEvidenceChecksum':_hash([(stamp,row) for stamp,row in sorted(rows.items())]),
        'referenceMinute':at.isoformat(),'sessionDate':str((inputs.get('session') or {}).get('session_date'))})


def eligible_context(inputs,name,context):
    """Validate both effective-time and every disclosed return-window watermark."""
    if not context: return {},[],[name+':UNAVAILABLE'],{}
    as_of=_stamp(inputs['as_of']); stamps=[]
    try:
        observed,effective=_stamp(context['observed_at']),_stamp(context['effective_at'])
        if observed>effective or effective>as_of or (as_of-observed).total_seconds()>LIMITS[name]:
            return {},[],[name+':STALE_OR_FUTURE'],{}
        stamps.extend((observed,effective))
        if inputs['mode']==PROSPECTIVE:
            receipt=context.get('first_seen_at') or context.get('metadata',{}).get('first_seen_at')
            if not receipt or _stamp(receipt)>as_of: return {},[],[name+':RECEIPT_UNVERIFIED'],{}
            stamps.append(_stamp(receipt))
        values=deepcopy(context.get('values',{})); reasons=[]
        watermarks=context.get('returnSourceWatermarks',{})
        for horizon in ('1','5','15','30') if name in ('NIFTY50','INDIA_VIX','GIFT_NIFTY','BRENT','USDINR') else ():
            watermark=watermarks.get(horizon)
            valid=bool(watermark and watermark.get('effective_at') and watermark.get('observed_at')
                and _stamp(watermark['observed_at'])<=_stamp(watermark['effective_at'])<=as_of
                and observed-_stamp(watermark['observed_at'])==timedelta(minutes=int(horizon)))
            if valid and inputs['mode']==PROSPECTIVE:
                valid=bool(watermark.get('first_seen_at') and _stamp(watermark['first_seen_at'])<=as_of)
            if valid:
                stamps.extend((_stamp(watermark['observed_at']),_stamp(watermark['effective_at'])))
                if inputs['mode']==PROSPECTIVE: stamps.append(_stamp(watermark['first_seen_at']))
            else:
                values[f'return_{horizon}m']=None; values[f'change_{horizon}m']=None
                reasons.append(name+':RETURN_WINDOW_UNAVAILABLE:'+horizon)
        return values,stamps,reasons,{'provider':context.get('provider'),'rawChecksum':context.get('raw_checksum'),
            'metadata':context.get('metadata',{}),'watermarks':context.get('returnSourceWatermarks',{}),'effectiveAt':effective.isoformat()}
    except (ValueError,TypeError,KeyError): return {},[],[name+':INVALID_PROVENANCE'],{}


def india_market_block(inputs,price):
    market=inputs.get('market',{})
    nifty,nt,nr,nl=eligible_context(inputs,'NIFTY50',market.get('NIFTY50'))
    vix,vt,vr,vl=eligible_context(inputs,'INDIA_VIX',market.get('INDIA_VIX'))
    values={}
    for h in (1,5,15,30):
        value=number(nifty.get(f'return_{h}m')); bank=price.values.get(f'return_{h}m')
        values[f'nifty_return_{h}m']=value
        values[f'banknifty_minus_nifty_{h}m']=bank-value if bank is not None and value is not None else None
        if h in (5,15): values[f'banknifty_nifty_direction_agreement_{h}m']=int((bank>0)-(bank<0)==(value>0)-(value<0)) if bank is not None and value is not None else None
    values['india_vix_level']=number(vix.get('level'))
    for h in (1,5,15):
        values[f'vix_change_{h}m']=number(vix.get(f'change_{h}m'))
        values[f'vix_return_{h}m']=number(vix.get(f'return_{h}m'))
    bank,v=price.values.get('return_5m'),values['vix_change_5m']
    values['banknifty_return_vs_vix_change']=bank*v if bank is not None and v is not None else None
    return block(values,list(values),[*nt,*vt,*([price.sourceMaxTimestamp] if price.sourceMaxTimestamp else [])],[*nr,*vr],{'NIFTY50':nl,'INDIA_VIX':vl})


def global_block(inputs):
    values={}; times=[]; reasons=[]; lineage={}
    for name,prefix in (('GIFT_NIFTY','gift'),('BRENT','brent'),('USDINR','usdinr')):
        source,stamps,missing,proof=eligible_context(inputs,name,inputs.get('market',{}).get(name))
        values.update({f'{prefix}_return_{h}m':number(source.get(f'return_{h}m')) for h in (1,5,15)})
        times.extend(stamps); reasons.extend(missing); lineage[name]=proof
    return block(values,list(values),times,reasons,lineage)


def derivatives_block(inputs):
    futures,ft,fr,fl=eligible_context(inputs,'FUTURES',inputs.get('futures'))
    options,ot,orr,ol=eligible_context(inputs,'OPTIONS',inputs.get('options'))
    if not options.get('complete'):
        options={}; orr.append('OPTIONS:CONFIGURED_ATM_UNIVERSE_INCOMPLETE')
    values={key:number(futures.get(key)) for key in (
        'future_spot_basis','future_spot_basis_pct','basis_change_1m','basis_change_5m','basis_change_10m',
        'future_return_1m','future_return_5m','future_return_10m','spot_return_1m','spot_return_5m','spot_return_10m',
        'future_minus_spot_return_1m','future_minus_spot_return_5m','future_oi','future_oi_change_1m','future_oi_change_5m',
        'future_volume','future_volume_spike')}
    values['basis_pct']=values['future_spot_basis_pct']
    values['future_volume_spike_vs_prior20']=values.pop('future_volume_spike')
    buildup=futures.get('buildup'); available=buildup in ('LONG_BUILDUP','SHORT_BUILDUP','SHORT_COVERING','LONG_UNWINDING','NEUTRAL')
    for label in ('LONG_BUILDUP','SHORT_BUILDUP','SHORT_COVERING','LONG_UNWINDING'):
        values['is_'+label.lower()]=int(buildup==label) if available else None
    values['futures_buildup_available']=int(available)
    keys=('pcr_oi','ce_total_oi','pe_total_oi','ce_oi_change','pe_oi_change','normalized_oi_imbalance',
        'ce_volume','pe_volume','volume_pcr','atm_ce_oi','atm_pe_oi','atm_ce_oi_change','atm_pe_oi_change',
        'atm_straddle_premium','atm_straddle_premium_change','strongest_ce_oi_strike_above_spot',
        'strongest_pe_oi_strike_below_spot','ce_oi_concentration','pe_oi_concentration','oi_entropy','selected_strike_completeness')
    values.update({key:number(options.get(key)) for key in keys})
    spot=number(options.get('spot'))
    values['distance_to_ce_wall_pct']=ratio(number(options.get('distance_to_ce_wall')),spot)
    values['distance_to_pe_wall_pct']=ratio(number(options.get('distance_to_pe_wall')),spot)
    values['atm_straddle_pct_of_spot']=ratio(values['atm_straddle_premium'],spot)
    for kind in ('NON_EXPIRY','MONTHLY_EXPIRY','QUARTERLY_EXPIRY','UNKNOWN'):
        values['expiry_'+kind.lower()]=int(options.get('expiry_context','UNKNOWN')==kind) if options else None
    provenance=ol.get('metadata',{}).get('ivProvenance') or {}
    verified=provenance.get('unit') in ('PERCENT','FRACTION') and provenance.get('annualized') is True and bool(provenance.get('sourceUrl'))
    try:
        proof_at=_stamp(provenance['sourceObservedAt']) if provenance.get('sourceObservedAt') else None
        verified=verified and proof_at is not None and proof_at<=_stamp(inputs['as_of'])
    except (ValueError,TypeError): verified=False; proof_at=None
    if verified: ot.append(proof_at)
    for key in ('atm_ce_iv','atm_pe_iv','atm_iv_mean','put_call_iv_skew'):
        value=number(options.get(key))
        values[key]=value/(100 if provenance.get('unit')=='PERCENT' else 1) if verified and value is not None else None
    for horizon in HORIZONS:
        iv=values['atm_iv_mean']
        values[f'iv_expected_move_pct_{horizon}m']=iv*sqrt(horizon/(252*375)) if iv is not None and iv>=0 else None
    if not verified: orr.append('IV_UNITS_OR_ANNUALIZATION_UNVERIFIED')
    required=('basis_pct','future_return_1m','future_return_5m','future_return_10m','future_oi','future_oi_change_1m',
        'future_oi_change_5m','future_volume','pcr_oi','ce_total_oi','pe_total_oi','ce_volume','pe_volume','atm_straddle_pct_of_spot','selected_strike_completeness')
    return block(values,required,[*ft,*ot],[*fr,*orr],{'futures':fl,'options':ol,'ivProvenance':provenance if verified else None})


def bank_structure_block(inputs):
    source,times,reasons,lineage=eligible_context(inputs,'BANKS',inputs.get('banks'))
    proof=inputs.get('membership') or {}
    official=source.get('sourceQuality')=='OFFICIAL_INDEX' and proof.get('source_quality')=='OFFICIAL_INDEX'
    pinned=source.get('snapshotChecksum')==proof.get('source_checksum') and source.get('snapshotId')==proof.get('snapshot_id')
    day=_stamp(inputs['as_of']).astimezone(IST).date()
    valid=bool(proof.get('effective_from') and proof.get('effective_to') and date.fromisoformat(str(proof['effective_from']))<=day<=date.fromisoformat(str(proof['effective_to'])))
    observed=proof.get('source_observed_at')
    valid=valid and bool(observed and _stamp(observed)<=_stamp(inputs['as_of']))
    if not official or not pinned or not valid:
        source={}; reasons.append('OFFICIAL_DATE_VALID_PINNED_MEMBERSHIP_REQUIRED')
    elif observed: times.append(_stamp(observed))
    keys=[f'{prefix}_{h}m' for h in (1,5,10,15) for prefix in ('breadth','fraction_up','fraction_down')]
    keys += [f'{prefix}_{h}m' for h in (1,5,10) for prefix in ('median_constituent_return','return_dispersion')]
    keys += ['leader_laggard_spread','fraction_same_direction_5m','positive_volume_spike_fraction','negative_volume_spike_fraction',
        'median_pairwise_correlation_20m','equal_bank_basket_return_5m','equal_bank_basket_return_10m',
        'index_breadth_residual_5m','index_breadth_residual_10m','constituent_completeness']
    values={key:number(source.get(key)) for key in keys}
    count=source.get('constituent_count_expected')
    for h in (1,5,10,15):
        if not count or source.get(f'return_count_{h}m')!=count:
            for key in keys:
                if key.endswith(f'_{h}m'): values[key]=None
            if h==5: values['leader_laggard_spread']=None
            reasons.append(f'INCOMPLETE_CONSTITUENT_RETURN_WINDOW:{h}')
    if not count or source.get('volume_spike_constituent_count')!=count:
        values['positive_volume_spike_fraction']=values['negative_volume_spike_fraction']=None
    if not count or source.get('correlation_pair_count')!=count*(count-1)//2:
        values['median_pairwise_correlation_20m']=None
    if source.get('constituent_completeness')!=1:
        reasons.append('INCOMPLETE_CONSTITUENT_MINUTE'); values={key:None for key in keys}
    required=[k for k in keys if k not in ('positive_volume_spike_fraction','negative_volume_spike_fraction','median_pairwise_correlation_20m')]
    return block(values,required,times,reasons,{**lineage,'membership':proof,'membershipSourceQuality':'OFFICIAL_INDEX_REQUIRED',
        'weightingMethod':'EQUAL_WEIGHT_PROXY','heavyweightDisplayFeaturesExcluded':True})


def session_structure_block(inputs):
    from .news.scheduled import scheduled_events
    as_of=_stamp(inputs['as_of']); bounds=_session_bounds(inputs['session'])
    rules=inputs.get('rules',{}); configuration=inputs.get('scheduled',[])
    structure=market_structure(as_of,inputs['session'],rules=rules,scheduled_configuration=configuration)
    phase=structure['phase']; values={f'market_phase_{p.lower()}':int(phase==p) for p in PHASES}
    values['minutes_to_index_close']=(bounds[1]-as_of).total_seconds()/60 if bounds else None
    for h in HORIZONS:
        fields=structure['horizons'][str(h)]
        values[f'crosses_cas_boundary_{h}m']=int(fields['crossesCasBoundary']) if fields['crossesCasBoundary'] is not None else None
        values[f'crosses_index_close_{h}m']=int(fields['crossesIndexClose']) if fields['crossesIndexClose'] is not None else None
    schedule=scheduled_events(now=as_of,configuration=configuration)
    visible=schedule.get('visibleEvents', schedule['events'])
    configured=bool(schedule.get('configuredEventCount', len(visible)))
    today=str(as_of.astimezone(IST).date())
    today_events=[e for e in visible if e['scheduledDate']==today]
    exact=[e for e in visible if e.get('timingPrecision')=='EXACT_TIME' and e.get('scheduledAt')]
    future_exact=[e for e in exact if _stamp(e['scheduledAt'])>=as_of]
    next_event=min(future_exact,key=lambda e:_stamp(e['scheduledAt']),default=None)
    unknown=[e for e in today_events if e['timingPrecision']=='DATE_ONLY']
    values['scheduled_event_today']=int(bool(today_events)) if configured else None
    values['scheduled_event_today_exact_time']=int(any(e['timingPrecision']=='EXACT_TIME' for e in today_events)) if configured else None
    values['scheduled_event_today_date_only']=int(bool(unknown)) if configured else None
    values['inside_scheduled_meeting_window']=int(any(
        e.get('meetingStart') and e.get('meetingEnd') and e['meetingStart']<=today<=e['meetingEnd']
        for e in visible)) if configured else None
    values['minutes_to_next_known_event']=(_stamp(next_event['scheduledAt'])-as_of).total_seconds()/60 if next_event else None
    values['next_event_time_known']=int(bool(next_event)) if configured else None
    values['next_event_timing_precision_exact']=int(bool(next_event)) if configured else None
    for kind in EVENT_TYPES:
        values['scheduled_event_today_type_'+kind.lower()]=int(any(e['eventType']==kind for e in today_events)) if configured else None
        values['next_event_type_'+kind.lower()]=int(next_event['eventType']==kind) if next_event else None
    stamps=[]; reasons=[]
    if bounds: stamps.append(bounds[0])
    if structure.get('ruleKnowledgeAvailableAt'): stamps.append(_stamp(structure['ruleKnowledgeAvailableAt']))
    stamps.extend(_stamp(e.get('knowledgeAvailableAt') or e['sourceObservedAt']) for e in visible)
    if not configured: reasons.append('SCHEDULED_EVENTS_NOT_CONFIGURED')
    if unknown: reasons.append('SOURCE_BACKED_DATE_WITH_UNKNOWN_RELEASE_TIME')
    required=['minutes_to_index_close',*[f'crosses_cas_boundary_{h}m' for h in HORIZONS],*[f'crosses_index_close_{h}m' for h in HORIZONS]]
    return block(values,required,stamps,reasons,{'rulesChecksum':_hash(rules),'scheduledConfigurationChecksum':_hash(configuration),
        'scheduledConfiguredEventCount':schedule.get('configuredEventCount'),
        'scheduledVisibleEventCount':schedule.get('visibleEventCount'),
        'phase':phase,'regime':structure['regime'],'ruleSourceObservedAt':structure.get('ruleSourceObservedAt'),
        'ruleSourcePublicationDate':structure.get('ruleSourcePublicationDate'),
        'ruleHistoricalKnowledgeAvailableFrom':structure.get('ruleHistoricalKnowledgeAvailableFrom'),
        'ruleHistoricalKnowledgeBasis':structure.get('ruleHistoricalKnowledgeBasis'),
        'ruleSourceReference':structure.get('ruleSourceReference'),
        'eventSources':[{'type':e['eventType'],'url':e['sourceUrl'],'observedAt':e['sourceObservedAt'],
            'knowledgeAvailableAt':e.get('knowledgeAvailableAt'),'timingPrecision':e.get('timingPrecision'),
            'scheduledAt':e.get('scheduledAt')} for e in visible]})


def calculate_blocks(inputs):
    """No IO, provider requests, repository reads, clock reads, imputation or labels."""
    if inputs.get('mode') not in (RETROSPECTIVE,PROSPECTIVE): raise ValueError('Explicit availability semantics required')
    price=price_vol_block(inputs)
    result={'PRICE_VOL':price,'INDIA_MARKET':india_market_block(inputs,price),'GLOBAL':global_block(inputs),
        'DERIVATIVES':derivatives_block(inputs),'BANK_STRUCTURE':bank_structure_block(inputs),
        'SESSION_STRUCTURE':session_structure_block(inputs)}
    as_of=_stamp(inputs['as_of'])
    if any(b.sourceMaxTimestamp and b.sourceMaxTimestamp>as_of for b in result.values()): raise ValueError('FUTURE_SOURCE_FACT')
    return result


def profile_definition(version):
    if version not in PROFILES: raise ValueError('Unknown Context V3 profile')
    return {'version':version,'calculationVersion':'banknifty-context-v3-blocks-v1','requiredBlocks':list(PROFILES[version]),'optionalBlocks':[],
        'minimumCoverage':{'passRowFraction':.70,'commonSupportFraction':.70,'tradingDayFraction':1.0,'minimumTradingDays':20},
        'availabilitySemantics':[RETROSPECTIVE,PROSPECTIVE],'missingPolicy':'NULL_NO_IMPUTATION',
        'timestampConvention':'as_of=availability boundary; referenceMinute=last completed candle opening; V2/label joins use referenceMinute',
        'membershipSourceQuality':'OFFICIAL_INDEX','weightingMethod':'EQUAL_WEIGHT_PROXY',
        'sourceStalenessSeconds':dict(LIMITS),'sourceConflictPolicy':'FAIL_CLOSED',
        'lineageRequirements':['acceptedDatasetChecksum','researchContextRunIds','marketStructureContextRunIds','membershipSnapshotChecksums',
            'scheduledConfigurationChecksum','marketStructureRulesChecksum'],
        'formulas':{'returns':'close(t)/close(t-h)-1; every intervening bar required',
            'realizedVariance':'sum squared consecutive log returns over h minutes','realizedVolatility':'sqrt(realizedVariance)',
            'semivariance':'sum squared positive/negative log returns, zero contribution only for observed opposite-side returns',
            'rangeVariance':'mean(log(high/low)^2)/(4*log(2))',
            'garmanKlass':'mean(0.5*log(high/low)^2-(2*log(2)-1)*log(close/open)^2), nonnegative valid bars only',
            'volatilityOfVolatility':'population stddev of 20 consecutive rolling five-minute realized volatilities',
            'ema':'5 and 20 spans, seeded at first close in fixed 61-bar window; slope excludes latest bar from previous EMA',
            'volumeBaseline':'exact prior 20 complete minutes, current excluded; population z-score; zero denominator -> null',
            'vwap':'full current-session typical-price times genuine volume / volume; missing or zero volume -> null',
            'bankniftyReturnVsVixChange':'five-minute BANKNIFTY simple return times five-minute VIX level change; interaction, not causal score',
            'ivExpectedMove':'verified annualized fractional IV * sqrt(horizon/(252*375)); trading-time-252x375-v1'},
        'optionalFeatureGroups':['VOLUME_AND_VWAP_WHEN_GENUINE','IV_WHEN_UNITS_VERIFIED','SCHEDULED_CALENDAR_WHEN_CONFIGURED',
            'ZERO_DENOMINATOR_RATIOS','CONSTITUENT_VOLUME_AND_NONCONSTANT_CORRELATION'],
        'numericStorage':'float or null; block-prefixed keys; _contextV3 is metadata, never a predictor',
        'modelScalePolicy':{'preferred':['basis_pct','distance_to_ce_wall_pct','distance_to_pe_wall_pct','atm_straddle_pct_of_spot'],
            'rawContextOnly':['future_spot_basis','strongest_ce_oi_strike_above_spot','strongest_pe_oi_strike_below_spot','atm_straddle_premium']},
        'ivProvenanceRequired':['unit=PERCENT|FRACTION','annualized=true','sourceUrl','sourceObservedAt<=as_of'],
        'trainingEligibility':{'horizons':list(HORIZONS),'requiresValidExistingLabel':True,'excludeCasBoundaryCrossing':True,
            'excludeUnknownCasBoundary':True,'excludeIndexCloseCrossing':True,'noNewsSentiment':True,'noHistoricalDepth':True}}


def profile_snapshot(inputs,version,blocks=None):
    definition=profile_definition(version); blocks=blocks or calculate_blocks(inputs)
    required=[blocks[name] for name in definition['requiredBlocks']]
    status='PASS' if all(b.status=='PASS' for b in required) else 'UNAVAILABLE' if any(b.status=='UNAVAILABLE' for b in required) else 'PARTIAL'
    as_of=_stamp(inputs['as_of']); reference=as_of.replace(second=0,microsecond=0)-timedelta(minutes=1)
    values={f'{name.lower()}__{key}':value for name in definition['requiredBlocks'] for key,value in blocks[name].values.items()}
    boundary=blocks['SESSION_STRUCTURE'].values
    eligibility={str(h):boundary.get(f'crosses_cas_boundary_{h}m')==0 and boundary.get(f'crosses_index_close_{h}m')==0 for h in HORIZONS}
    source_max=max((b.sourceMaxTimestamp for b in required if b.sourceMaxTimestamp),default=None)
    if source_max and source_max>as_of: raise ValueError('FUTURE_SOURCE_FACT')
    if any('SOURCE_CONFLICT' in reason for b in required for reason in b.missingReasons): status='UNAVAILABLE'
    meta={'profileVersion':version,'profileChecksum':_hash(definition),'status':status,'mode':inputs['mode'],
        'referenceMinute':reference.isoformat(),'horizonEligibility':eligibility,
        'blocks':{name:blocks[name].payload() for name in definition['requiredBlocks']},'pinnedLineage':inputs.get('pins',{})}
    values['_contextV3']=meta
    return {'as_of':as_of,'source_max_timestamp':source_max,'status':status,'values':values,'horizonEligibility':eligibility}


def _candle_payload(candle):
    stamp=getattr(candle,'timestamp',None) or candle.minute_at
    return {'at':stamp,**{key:number(getattr(candle,key)) for key in ('open','high','low','close')},
        'volume':number(getattr(candle,'volume_delta',getattr(candle,'volume',None))),
        'first_finalized_at':getattr(candle,'first_finalized_at',None),'checksum':getattr(candle,'checksum',None),
        'revision':getattr(candle,'revision',None)}


def _session_payload(session):
    return {'session_date':str(session.session_date),'market_open_utc':session.open_at,'market_close_utc':session.close_at,'status':session.status}


def _derived_context(values,as_of,lineage,mode,receipts=()):
    receipt=max((_stamp(v) for v in receipts if v),default=None)
    return {'values':values or {},'observed_at':_stamp(as_of)-timedelta(minutes=1),'effective_at':_stamp(as_of),
        'first_seen_at':receipt.isoformat() if receipt else None,'provider':'ZERODHA_LIVE' if mode==PROSPECTIVE else 'PINNED_RESEARCH_CONTEXT',
        'raw_checksum':_hash(lineage),'metadata':lineage}


class ContextV3FeatureGenerationService:
    def __init__(self,repository,context_repository=None,structure_repository=None,root=None):
        self.repository=repository; self.root=Path(root or memory_root())
        self.context_repository=context_repository or ResearchContextRepository()
        self.structure_repository=structure_repository or MarketStructureContextRepository()
        self.last_manifest=None

    def generate(self,revision_id,instrument_id,feature_profile,*,research_context_runs,market_structure_runs,
                 membership_snapshot_checksums,scheduled_configuration,market_structure_rules,
                 date_from=None,date_to=None):
        """Pins are date->run_id mappings; absent dates mean unavailable, never latest."""
        start=date.fromisoformat(str(date_from)) if date_from else None
        end=date.fromisoformat(str(date_to)) if date_to else None
        if start and end and start>end: raise ValueError('INVALID_CONTEXT_V3_DATE_RANGE')
        self.last_manifest=None
        revision_id=str(revision_id)
        research_context_runs=dict(research_context_runs); market_structure_runs=dict(market_structure_runs)
        membership_snapshot_checksums=dict(membership_snapshot_checksums)
        scheduled_configuration=deepcopy(scheduled_configuration); market_structure_rules=deepcopy(market_structure_rules)
        definition=profile_definition(feature_profile)
        revision=self.repository.get_revision(revision_id)
        if revision.status!='ACCEPTED': raise ValueError('Context V3 requires an ACCEPTED revision')
        if instrument_id not in revision.instrument_ids: raise ValueError('Instrument is not in the accepted revision')
        instrument=self.repository.get_instrument(instrument_id)
        if instrument.exchange!='NSE' or instrument.tradingsymbol not in ('BANKNIFTY','NIFTY BANK'): raise ValueError('BANKNIFTY index required')
        pins={'acceptedDatasetChecksum':revision.checksum,'researchContextRunIds':dict(research_context_runs),
            'marketStructureContextRunIds':dict(market_structure_runs),'membershipSnapshotChecksums':dict(membership_snapshot_checksums),
            'scheduledConfigurationChecksum':_hash(scheduled_configuration),'marketStructureRulesChecksum':_hash(market_structure_rules)}
        generated_at=datetime.now(UTC)
        self.repository.save_feature_set(FeatureSetDefinition(feature_profile,generated_at,definition,_hash(definition)))
        # Existing generic definitions can hold an immutable research input manifest too.
        sessions={s.session_date:_session_payload(s) for s in self.repository.sessions_between(revision.session_start,revision.session_end)}
        candles=sorted(self.repository.revision_candles(revision_id,instrument_id),key=lambda c:c.timestamp)
        generated_dates=sorted({str(c.timestamp.astimezone(IST).date()) for c in candles
            if (not start or c.timestamp.astimezone(IST).date()>=start)
            and (not end or c.timestamp.astimezone(IST).date()<=end)
            and _session_bounds(sessions.get(c.timestamp.astimezone(IST).date()))})
        manifest={'kind':'CONTEXT_V3_GENERATION_PINS','manifestSchemaVersion':2,
            'profileChecksum':_hash(definition),'generatedDates':generated_dates,
            'dateFrom':str(start) if start else None,'dateTo':str(end) if end else None,
            'profileVersion':feature_profile,'datasetRevisionId':revision_id,
            'instrumentId':instrument_id,'pins':pins,'scheduledConfiguration':scheduled_configuration,'marketStructureRules':market_structure_rules}
        manifest_version='context-v3-inputs-'+_hash(manifest)
        manifest_checksum=_hash(manifest)
        try:
            self.repository.save_feature_set(FeatureSetDefinition(manifest_version,generated_at,manifest,manifest_checksum))
        except ValueError as exc:
            if 'immutable' in str(exc).lower(): raise ValueError('IMMUTABLE_CONTEXT_V3_INPUT_CONFLICT') from exc
            raise
        self.last_manifest={'version':manifest_version,'checksum':manifest_checksum}
        grouped={}
        for candle in candles: grouped.setdefault(candle.timestamp.astimezone(IST).date(),[]).append(candle)
        output=[]
        # The immutable V3 ceiling, not the process environment, governs replay.
        replay_policy=ResearchContextSettings(staleness_seconds={**dict(LIMITS),'BANKNIFTY':120,'MARKET_INFORMATION':3600})
        research_reader=HistoricalResearchContextReader(self.context_repository,settings=replay_policy,root=self.root)
        structure_reader=MarketStructureContextReader(self.structure_repository,self.root)
        memberships={}
        for day,day_candles in grouped.items():
            if start and day<start or end and day>end: continue
            session=sessions.get(day)
            if not _session_bounds(session): continue
            research_run=research_context_runs.get(str(day)); structure_run=market_structure_runs.get(str(day))
            if research_run:
                with self.context_repository.connection() as db:
                    run=db.execute('SELECT * FROM research_context_runs WHERE run_id=%s',(research_run,)).fetchone()
                if not run or not run['date_from']<=day<=run['date_to']: raise ValueError('Invalid research run pin')
            if structure_run:
                with self.structure_repository.connection() as db:
                    structure_manifest=db.execute('SELECT * FROM market_structure_context_runs WHERE run_id=%s',(structure_run,)).fetchone()
                if not structure_manifest or not structure_manifest['date_from']<=day<=structure_manifest['date_to']: raise ValueError('Invalid structure run pin')
            prefix=[]
            for candle in day_candles:
                prefix.append(_candle_payload(candle)); as_of=candle.timestamp+timedelta(minutes=1)
                research=research_reader.at_or_before(as_of,run_id=research_run) if research_run else {'status':'UNPINNED','context':None}
                structure=structure_reader.at_or_before(as_of,run_id=structure_run) if structure_run else None
                r=research.get('context') or {}; s=structure or {}
                market={**r.get('index_context',{}),**r.get('volatility_context',{}),**r.get('global_context',{})}
                member_id=s.get('constituent_context',{}).get('snapshotId'); membership=None
                if member_id and 'BANK_STRUCTURE' in definition['requiredBlocks']:
                    if member_id not in memberships:
                        with self.structure_repository.connection() as db:
                            memberships[member_id]=db.execute('SELECT * FROM banknifty_constituent_snapshots WHERE snapshot_id=%s',(member_id,)).fetchone()
                    membership=memberships[member_id]
                    if not membership or membership_snapshot_checksums.get(member_id)!=membership['source_checksum']:
                        raise ValueError('Membership checksum pin required or mismatched')
                if s and s['source_lineage'].get('sourceManifestChecksum')!=_hash(structure_manifest['source_manifest']):
                    raise ValueError('Structure source manifest integrity failure')
                inputs={'as_of':as_of,'mode':RETROSPECTIVE,'candles':list(prefix),'session':session,'market':market,
                    'options':r.get('options_context',{}).get('minute'),'membership':_plain(membership),
                    'futures':_derived_context(s.get('futures_context'),s['as_of'],s['source_lineage'],RETROSPECTIVE) if s else None,
                    'banks':_derived_context(s.get('constituent_context'),s['as_of'],s['source_lineage'],RETROSPECTIVE) if s else None,
                    'rules':market_structure_rules,'scheduled':scheduled_configuration,
                    'pins':{**pins,'researchContextRunIds':{str(day):research_run} if research_run else {},
                        'marketStructureContextRunIds':{str(day):structure_run} if structure_run else {},
                        'membershipSnapshotChecksums':{member_id:membership['source_checksum']} if membership else {},
                        'inputManifestVersion':manifest_version,'inputManifestChecksum':_hash(manifest),
                        'researchContextChecksum':research.get('storedContextChecksum'),
                        'structureContextChecksum':s.get('context_checksum')}}
                blocks=calculate_blocks(inputs)
                if research.get('status')=='SOURCE_CONFLICT' or s.get('quality_status')=='SOURCE_CONFLICT':
                    for name in ('INDIA_MARKET','GLOBAL','DERIVATIVES','BANK_STRUCTURE'):
                        b=blocks[name]; blocks[name]=FeatureBlock({k:None for k in b.values},'UNAVAILABLE',None,('SOURCE_CONFLICT',),b.sourceLineage)
                snapshot=profile_snapshot(inputs,feature_profile,blocks)
                source_max=snapshot['source_max_timestamp']
                if source_max is None: source_max=candle.timestamp+timedelta(minutes=1)
                quality='PASS' if snapshot['status']=='PASS' and any(snapshot['horizonEligibility'].values()) else 'UNAVAILABLE'
                output.append(FeatureRow(revision_id,instrument_id,as_of,feature_profile,source_max,snapshot['values'],quality))
        existing={row.as_of:row for row in self.repository.feature_rows(revision_id,instrument_id,feature_profile)}
        for index,row in enumerate(output):
            prior=existing.get(row.as_of)
            if prior:
                # Batch identity is not row evidence: overlapping identical rows keep
                # their original manifest reference, never rewriting history.
                def evidence(value):
                    payload=deepcopy(value.to_dict())
                    lineage=payload['feature_values']['_contextV3']['pinnedLineage']
                    lineage.pop('inputManifestVersion',None)
                    lineage.pop('inputManifestChecksum',None)
                    return payload
                if _hash(evidence(prior))!=_hash(evidence(row)):
                    raise ValueError('IMMUTABLE_V3_ROW_CONFLICT: new revision required')
                output[index]=prior
        return output,self.repository.save_feature_rows(output)


class ContextV3RuntimeFeatureService:
    def __init__(self,reader=None,context_reader=None,root=None):
        from .live_product_reader import LiveProductDataReader
        self.root=Path(root or memory_root())
        self.reader=reader or LiveProductDataReader(); self.context_reader=context_reader or MarketStructureContextReader(root=self.root)

    @staticmethod
    def _scalar(value): return value is None or isinstance(value,(str,int,float,bool))
    @staticmethod
    def _evidence_key(key):
        name=str(key).lower()
        return ('checksum' in name or name in ('provider','sourcereference','sourceobservedat','sourcemaxtimestamp',
            'sourcepublicationdate','historicalknowledgeavailablefrom','historicalknowledgebasis','ruleknowledgeavailableat',
            'effectiveat','firstseenat','referencetime','referenceminute','asof'))
    @classmethod
    def _compact_reasons(cls,reasons):
        output=[]
        for reason in reasons:
            label='MISSING_REQUIRED_FEATURES' if str(reason).startswith('MISSING:') else str(reason)
            if label not in output: output.append(label)
        return output
    @classmethod
    def _block_evidence(cls,block):
        evidence={}
        for key,value in (block.sourceLineage or {}).items():
            if isinstance(value,dict):
                nested={str(name):_plain(item) for name,item in value.items()
                    if cls._scalar(item) and cls._evidence_key(name)}
                if nested: evidence[str(key)]=nested
            elif cls._scalar(value) and cls._evidence_key(key): evidence[str(key)]=_plain(value)
        return {'status':block.status,'sourceMaxTimestamp':_plain(block.sourceMaxTimestamp),
            'missingReasons':cls._compact_reasons(block.missingReasons),'sourceEvidence':evidence}
    @classmethod
    def unavailable(cls,decision,market_as_of,reason):
        profiles={}
        for version in PROFILES:
            required=list(PROFILES[version])
            profiles[version]={'profileVersion':version,'status':'UNAVAILABLE','requiredBlocks':required,
                'horizonEligibility':{str(h):False for h in HORIZONS},
                'blockStatuses':{name:'UNAVAILABLE' for name in required},'missingReasons':[reason],
                'sourceMaxTimestamp':None,'referenceMinute':None,
                'blocks':{name:{'status':'UNAVAILABLE','sourceMaxTimestamp':None,'missingReasons':[reason],
                    'sourceEvidence':{}} for name in required}}
        return {'status':'UNAVAILABLE','asOf':decision.isoformat(),
            'marketAsOf':market_as_of.isoformat() if market_as_of else None,
            'profiles':profiles,'reasons':[reason]}

    def snapshot(self,decision_at=None):
        from .market_structure_context import ConstituentUniverseService
        decision=_stamp(decision_at or datetime.now(UTC))
        try: state=self.reader.current_state(decision)
        except Exception: return self.unavailable(decision,None,'LIVE_STATE_UNAVAILABLE')
        try: market_as_of=_stamp(state.latest_at) if state.latest_at else None
        except (AttributeError,TypeError,ValueError):
            return self.unavailable(decision,None,'LIVE_MARKET_CUTOFF_UNAVAILABLE')
        if market_as_of is None:
            return self.unavailable(decision,None,'LIVE_MARKET_CUTOFF_UNAVAILABLE')
        if market_as_of>decision:
            return self.unavailable(decision,None,'LIVE_MARKET_CUTOFF_AFTER_DECISION')
        if state.quality!='PASS' or not state.session:
            return self.unavailable(decision,market_as_of,state.quality or 'LIVE_STATE_UNAVAILABLE')
        try:
            context=self.context_reader.runtime(market_as_of,decision)
            try: snapshot=ConstituentUniverseService(self.root).resolve(market_as_of.astimezone(IST).date(),decision_at=decision)
            except (ValueError,KeyError,TypeError,OSError): snapshot=None
            membership={k:v for k,v in asdict(snapshot).items() if k!='members'} if snapshot else None
            receipts=[r['firstFinalizedAt'] for r in context.get('sourceLineage',[])]
            market={**context.get('globalContext',{}),'NIFTY50':context.get('indiaMarketContext')}
            options=deepcopy(context.get('optionsContext'))
            if options:
                seen=[r['firstSeenAt'] for r in options.get('receiptEvidence',[])]+[r['firstFinalizedAt'] for r in options.get('receiptEvidence',[])]
                options['first_seen_at']=max((_stamp(t) for t in seen),default=None)
            rules=json.loads((self.root/'config/market_structure_rules.json').read_text(encoding='utf-8'))
            scheduled=json.loads((self.root/'config/scheduled_events.json').read_text(encoding='utf-8'))
            inputs={'as_of':decision,'mode':PROSPECTIVE,'session':_session_payload(state.session),
                'candles':[_candle_payload(c) for c in state.minutes('INDEX')],'market':market,'options':options,'membership':_plain(membership),
                'futures':_derived_context(context.get('futuresContext'),market_as_of,{'liveMinutes':context.get('sourceLineage',[])},PROSPECTIVE,receipts),
                'banks':_derived_context(context.get('constituentContext'),market_as_of,{'liveMinutes':context.get('sourceLineage',[])},PROSPECTIVE,receipts),
                'rules':rules,'scheduled':scheduled,'pins':{'membershipSnapshotChecksums':{snapshot.snapshot_id:snapshot.source_checksum} if snapshot else {},
                    'scheduledConfigurationChecksum':_hash(scheduled),'marketStructureRulesChecksum':_hash(rules),'availabilitySemantics':PROSPECTIVE}}
            blocks=calculate_blocks(inputs)
            profiles={}
            for version in PROFILES:
                required=list(PROFILES[version]); evidence=profile_snapshot(inputs,version,blocks)
                meta=evidence['values'].get('_contextV3',{})
                missing=[]
                for name in required:
                    for reason in self._compact_reasons(blocks[name].missingReasons):
                        if reason not in missing: missing.append(reason)
                profiles[version]={'profileVersion':version,'status':evidence['status'],'requiredBlocks':required,
                    'horizonEligibility':{str(h):bool(evidence['horizonEligibility'].get(str(h))) for h in HORIZONS},
                    'blockStatuses':{name:blocks[name].status for name in required},'missingReasons':missing,
                    'sourceMaxTimestamp':_plain(evidence['source_max_timestamp']),
                    'referenceMinute':meta.get('referenceMinute'),'profileChecksum':meta.get('profileChecksum'),
                    'blocks':{name:self._block_evidence(blocks[name]) for name in required}}
        except Exception:
            return self.unavailable(decision,market_as_of,'RUNTIME_CONTEXT_UNAVAILABLE')
        states=[profile['status'] for profile in profiles.values()]
        if 'PASS' in states: status='READY'
        elif 'PARTIAL' in states or any(blocks[name].status=='PARTIAL' for names in PROFILES.values() for name in names): status='PARTIAL'
        else: status='UNAVAILABLE'
        return {'status':status,'asOf':decision.isoformat(),'marketAsOf':market_as_of.isoformat(),
            'profiles':profiles,'reasons':[]}


def common_support_indices(left_feature_set,right_feature_set,horizon,labels=()):
    """Join the SAME accepted revision, instrument, reference candle and immutable label.

    V2 as_of is candle-open convention; V3 as_of is availability convention.
    No labels supplied means no demonstrable common support, never an assumed match.
    """
    if horizon not in HORIZONS: raise ValueError('Unsupported horizon')
    def key(row):
        meta=row.values.get('_contextV3',{})
        reference=_stamp(meta['referenceMinute']) if meta else row.as_of
        return (row.dataset_revision_id,row.instrument_id,reference)
    def eligible(row):
        meta=row.values.get('_contextV3')
        return row.quality_status=='PASS' and (not meta or meta['status']=='PASS' and meta['horizonEligibility'].get(str(horizon)) is True)
    def index(rows):
        result={}; conflicts=set(); seen=set()
        for i,row in enumerate(rows):
            identity=key(row)
            if identity in seen: conflicts.add(identity)
            seen.add(identity)
            if eligible(row): result[identity]=i
        return {k:v for k,v in result.items() if k not in conflicts}
    left,right=index(left_feature_set),index(right_feature_set)
    outcomes={}; ambiguous=set()
    for label in labels:
        if label.horizon_minutes!=horizon or label.quality_status!='VALID' or label.label not in ('BIG_UP','BIG_DOWN','NO_BIG_MOVE'): continue
        identity=(label.dataset_revision_id,label.instrument_id,label.as_of)
        if identity in outcomes: ambiguous.add(identity)
        outcomes[identity]=label
    return [{'leftIndex':left[k],'rightIndex':right[k],'datasetRevisionId':k[0],'instrumentId':k[1],
        'referenceMinute':k[2].isoformat(),'horizon':horizon,'label':outcomes[k].label,
        'labelChecksum':_hash(outcomes[k].to_dict())} for k in sorted(left.keys()&right.keys()&outcomes.keys()-ambiguous)]


class ContextV3CoverageService:
    def __init__(self,repository=None):
        self.repository=repository or MarketStructureContextRepository()

    def profiles(self):
        return {'profiles':[{**profile_definition(v),'checksum':_hash(profile_definition(v))} for v in PROFILES],
            'newsChannel':'SEPARATE_EVIDENCE_ONLY','trainingPipeline':'UNCHANGED_V2_ONLY'}

    def coverage(self):
        with self.repository.connection() as db:
            # Project small metadata only, never load duplicate numeric vectors/provider lineage into readiness.
            rows=db.execute('''SELECT f.dataset_revision_id,f.instrument_id,f.as_of,f.feature_set_version,f.quality_status,
                jsonb_build_object('_contextV3',jsonb_build_object(
                  'status',m->'status','mode',m->'mode','profileChecksum',m->'profileChecksum',
                  'referenceMinute',m->'referenceMinute','horizonEligibility',m->'horizonEligibility',
                  'pinnedLineage',m->'pinnedLineage','blocks',
                    (SELECT jsonb_object_agg(key,jsonb_build_object('status',value->'status','missingReasons',value->'missingReasons',
                      'values',CASE WHEN key='SESSION_STRUCTURE' THEN value->'values' ELSE '{}'::jsonb END))
                     FROM jsonb_each(m->'blocks')))) AS feature_values
                FROM feature_rows f JOIN dataset_revisions d ON d.id=f.dataset_revision_id AND d.status='ACCEPTED'
                CROSS JOIN LATERAL (SELECT f.feature_values->'_contextV3' AS m) metadata
                WHERE feature_set_version=ANY(%s) ORDER BY dataset_revision_id,instrument_id,as_of''',(list(PROFILES),)).fetchall()
            definitions=db.execute('SELECT version,checksum FROM feature_set_definitions WHERE version=ANY(%s)',(list(PROFILES),)).fetchall()
            manifests=db.execute("SELECT version,definition,checksum FROM feature_set_definitions WHERE version LIKE 'context-v3-inputs-%'").fetchall()
            universe=db.execute('''SELECT c.revision_id,c.instrument_id,(c.candle_at AT TIME ZONE 'Asia/Kolkata')::date AS day,count(*) AS rows
                FROM dataset_revision_candles c WHERE EXISTS (SELECT 1 FROM feature_rows f
                  WHERE f.dataset_revision_id=c.revision_id AND f.instrument_id=c.instrument_id AND f.feature_set_version=ANY(%s))
                GROUP BY c.revision_id,c.instrument_id,day''',(list(PROFILES),)).fetchall()
            # Same revision/instrument/label identity, no concatenation of different cohorts.
            baseline=db.execute("SELECT dataset_revision_id,instrument_id,as_of FROM feature_rows WHERE feature_set_version='banknifty-bigmove-v2' AND quality_status='PASS'").fetchall()
            labels=db.execute('''SELECT dataset_revision_id,instrument_id,as_of,horizon_minutes,label,quality_status
                FROM big_move_labels WHERE quality_status='VALID' AND label IN ('BIG_UP','BIG_DOWN','NO_BIG_MOVE')''').fetchall()
        stored={r['version']:str(r['checksum']).strip() for r in definitions}
        manifest_map={r['version']:r for r in manifests if _hash(r['definition'])==str(r['checksum']).strip()}
        candidate_days={}
        for row in universe: candidate_days.setdefault((str(row['revision_id']),row['instrument_id']),{})[row['day']]=int(row['rows'])
        label_counts=Counter((str(r['dataset_revision_id']),r['instrument_id'],r['as_of'],r['horizon_minutes']) for r in labels)
        # Multiple policy versions are not interchangeable labels; require an explicitly unambiguous cohort.
        label_keys={key for key,count in label_counts.items() if count==1}
        base_keys={(str(r['dataset_revision_id']),r['instrument_id'],r['as_of']) for r in baseline}
        result=[]
        for version in PROFILES:
            selected=[r for r in rows if r['feature_set_version']==version]; definition=profile_definition(version)
            cohorts={}
            for row in selected: cohorts.setdefault((str(row['dataset_revision_id']),row['instrument_id']),[]).append(row)
            details=[]
            for (revision,instrument),items in cohorts.items():
                passed=[r for r in items if r['quality_status']=='PASS' and r['feature_values'].get('_contextV3',{}).get('status')=='PASS']
                expected=candidate_days.get((revision,instrument),{})
                dates=set(expected); pass_dates={r['as_of'].astimezone(IST).date() for r in passed}
                candidate_count=sum(expected.values())
                missing=Counter(); conflicts=0; pinned=bool(items); cas=Counter(); close=Counter(); common={}
                by_date={}
                for row in items:
                    by_date.setdefault(row['as_of'].astimezone(IST).date(),[]).append(row)
                for row in items:
                    meta=row['feature_values'].get('_contextV3',{}); pins=meta.get('pinnedLineage',{})
                    manifest=manifest_map.get(pins.get('inputManifestVersion'))
                    evidence=manifest['definition'] if manifest else {}
                    original=evidence.get('pins',{})
                    row_pinned=bool(manifest and pins.get('inputManifestChecksum')==str(manifest['checksum']).strip()
                        and evidence.get('datasetRevisionId')==revision and evidence.get('instrumentId')==instrument
                        and evidence.get('profileVersion')==version and meta.get('profileChecksum')==_hash(definition)
                        and meta.get('mode')==RETROSPECTIVE and all(key in pins for key in definition['lineageRequirements']))
                    for key in ('acceptedDatasetChecksum','scheduledConfigurationChecksum','marketStructureRulesChecksum'):
                        row_pinned=row_pinned and bool(pins.get(key)) and pins.get(key)==original.get(key)
                    for key in ('researchContextRunIds','marketStructureContextRunIds','membershipSnapshotChecksums'):
                        row_pinned=row_pinned and all(original.get(key,{}).get(k)==v for k,v in pins.get(key,{}).items())
                    if set(definition['requiredBlocks'])&{'INDIA_MARKET','GLOBAL','DERIVATIVES'}:
                        row_pinned=row_pinned and bool(pins.get('researchContextRunIds'))
                        if row['quality_status']=='PASS': row_pinned=row_pinned and bool(pins.get('researchContextChecksum'))
                    if set(definition['requiredBlocks'])&{'BANK_STRUCTURE','DERIVATIVES'}:
                        row_pinned=row_pinned and bool(pins.get('marketStructureContextRunIds'))
                        if row['quality_status']=='PASS': row_pinned=row_pinned and bool(pins.get('structureContextChecksum'))
                    if 'BANK_STRUCTURE' in definition['requiredBlocks']:
                        if row['quality_status']=='PASS': row_pinned=row_pinned and bool(pins.get('membershipSnapshotChecksums'))
                    pinned=pinned and row_pinned
                    for name,b in meta.get('blocks',{}).items():
                        if b['status']!='PASS': missing[name]+=1
                    conflicts+=int(any('SOURCE_CONFLICT' in reason for b in meta.get('blocks',{}).values() for reason in b['missingReasons']))
                    session=meta.get('blocks',{}).get('SESSION_STRUCTURE',{}).get('values',{})
                    for h in HORIZONS:
                        cas[str(h)]+=int(session.get(f'crosses_cas_boundary_{h}m')!=0)
                        close[str(h)]+=int(session.get(f'crosses_index_close_{h}m')!=0)
                for h in HORIZONS:
                    base={key for key in base_keys if key[:2]==(revision,instrument) and (*key,h) in label_keys}
                    v3={(revision,instrument,_stamp(r['feature_values']['_contextV3']['referenceMinute'])) for r in passed
                        if r['feature_values']['_contextV3']['horizonEligibility'].get(str(h))}
                    common[str(h)]={'rows':len(base&v3),'baselineEligibleRows':len(base),'coveragePercent':100*len(base&v3)/len(base) if base else 0}
                for name in definition['requiredBlocks']: missing[name]+=max(0,candidate_count-len(items))
                block_source={'PRICE_VOL':'PRICE_VOL','SESSION_STRUCTURE':'SESSION_STRUCTURE',
                    'INDIA_MARKET':'RESEARCH_CONTEXT','GLOBAL':'RESEARCH_CONTEXT',
                    'DERIVATIVES':'DERIVATIVES','BANK_STRUCTURE':'BANK_STRUCTURE'}
                missing_dates=defaultdict(set); ambiguous_dates=set()
                for day in sorted(dates):
                    day_rows=by_date.get(day,[])
                    if not day_rows:
                        for name in definition['requiredBlocks']:
                            missing_dates[block_source[name]].add(str(day))
                        continue
                    if any(row['quality_status']=='PASS'
                           and row['feature_values'].get('_contextV3',{}).get('status')=='PASS'
                           for row in day_rows):
                        continue
                    latest=max(day_rows,key=lambda row:(row['as_of'],row['quality_status']))
                    latest_meta=latest['feature_values'].get('_contextV3',{})
                    latest_blocks=latest_meta.get('blocks',{})
                    reasons=[str(reason) for row in day_rows
                        for block in row['feature_values'].get('_contextV3',{}).get('blocks',{}).values()
                        for reason in block.get('missingReasons') or ()]
                    if any('SCHEDULED' in reason for reason in reasons):
                        missing_dates['SCHEDULED_EVENTS'].add(str(day))
                    if 'BANK_STRUCTURE' in definition['requiredBlocks'] and any(
                            'MEMBERSHIP' in reason for reason in reasons):
                        missing_dates['MEMBERSHIP'].add(str(day))
                    if any('SOURCE_CONFLICT' in reason or 'AMBIGUOUS' in reason for reason in reasons):
                        ambiguous_dates.add(str(day))
                    for name in definition['requiredBlocks']:
                        block=latest_blocks.get(name,{})
                        if block.get('status')!='PASS':
                            missing_dates[block_source[name]].add(str(day))
                    if not latest_blocks:
                        missing_dates['BLOCK_METADATA'].add(str(day))
                fraction=len(passed)/candidate_count if candidate_count else 0; minimum=definition['minimumCoverage']
                ready=bool(pinned and not conflicts and stored.get(version)==_hash(definition) and fraction>=minimum['passRowFraction']
                    and len(dates)>=minimum['minimumTradingDays'] and len(pass_dates)/len(dates)>=minimum['tradingDayFraction']
                    and all(c['coveragePercent']/100>=minimum['commonSupportFraction'] for c in common.values()))
                details.append({'datasetRevisionId':revision,'instrumentId':instrument,'totalCandidateRows':candidate_count,'generatedRows':len(items),'passRows':len(passed),
                    'coveragePercent':100*fraction,'tradingDayCount':len(dates),'dateRange':[str(min(dates)),str(max(dates))] if dates else [],
                    'readyDates':sorted(str(day) for day in pass_dates),'missingDatesBySource':{
                        source:sorted(values) for source,values in sorted(missing_dates.items()) if values},
                    'ambiguousDates':sorted(ambiguous_dates),
                    'missingBlockCounts':dict(missing),'sourceConflictRows':conflicts,'casBoundaryExclusions':dict(cas),
                    'indexCloseExclusions':dict(close),'commonSupportWithPriceCoreV2':common,'pinnedLineageAvailable':bool(pinned),
                    'minimumCoverage':minimum,'passRowFraction':fraction,
                    'tradingDayFraction':len(pass_dates)/len(dates) if dates else 0,
                    'status':'READY_FOR_EVALUATION' if ready else 'PARTIAL_COVERAGE'})
            result.append({'profileVersion':version,'definitionChecksum':_hash(definition),'cohorts':details,
                'status':'READY_FOR_EVALUATION' if details and all(d['status']=='READY_FOR_EVALUATION' for d in details) else
                    'PARTIAL_COVERAGE' if details else 'NO_HISTORICAL_FEATURES' if version in stored else 'DEFINED'})
        return {'status':'READY_FOR_EVALUATION' if any(r['status']=='READY_FOR_EVALUATION' for r in result) else 'NO_HISTORICAL_FEATURES' if not rows else 'PARTIAL_COVERAGE',
            'profiles':result,'comparisonPolicy':'IDENTICAL_REVISION_INSTRUMENT_REFERENCE_MINUTE_AND_LABEL; NO_EASY_SUBSET_CLAIM'}


class ContextV3PlanningService:
    """Read-only deterministic plan: every date input is pinned from one explicit PASS candidate."""

    def __init__(self,repository,root=None):
        self.repository=repository; self.root=Path(root or memory_root())
        self.contexts=MarketStructureContextRepository()

    def _configuration(self,blockers):
        rules=self._json('market_structure_rules.json',blockers); scheduled=self._json('scheduled_events.json',blockers)
        from .news.scheduled import scheduled_events
        try:
            schedule=scheduled_events(configuration=scheduled) if scheduled is not None else None
        except (ValueError,TypeError):
            blockers.append({'code':'CONFIGURATION_INVALID','file':'scheduled_events.json'}); schedule=None
        return {'marketStructureRulesChecksum':_hash(rules) if rules is not None else None,
            'scheduledConfigurationChecksum':_hash(scheduled) if scheduled is not None else None,
            'scheduledConfigurationConfigured':bool(schedule and schedule['configuredEventCount']),
            'scheduledConfiguredEventCount':schedule.get('configuredEventCount') if schedule else None,
            'marketStructureRules':{key:rules.get(key) for key in ('casEffectiveFrom','ruleVersion','sourceUrl','sourceObservedAt',
                'sourcePublicationDate','historicalKnowledgeAvailableFrom','historicalKnowledgeBasis','sourceReference')} if rules is not None else None}

    def _json(self,name,blockers):
        path=self.root/'config'/name
        if not path.is_file(): blockers.append({'code':'CONFIGURATION_MISSING','file':name}); return None
        try: return json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError): blockers.append({'code':'CONFIGURATION_INVALID','file':name}); return None

    def _research_runs(self,day):
        with self.contexts.connection() as db:
            return db.execute('''SELECT run_id,provider,status,raw_checksum,configuration FROM research_context_runs
                WHERE context_version=%s AND date_from<=%s AND date_to>=%s ORDER BY run_id''',
                (RESEARCH_CONTEXT_VERSION,day,day)).fetchall()

    def _structure_runs(self,day):
        with self.contexts.connection() as db:
            return db.execute('''SELECT run_id,status,checksum FROM market_structure_context_runs
                WHERE context_version=%s AND date_from<=%s AND date_to>=%s ORDER BY run_id''',
                (MARKET_STRUCTURE_CONTEXT_VERSION,day,day)).fetchall()

    def _pin_research_run(self,candidates,day,blockers,required_blocks=None):
        required=set(required_blocks or ()) & {'INDIA_MARKET','GLOBAL','DERIVATIVES'}
        valid=[row for row in candidates if row['status']=='COMPLETE' and required.issubset(
            set(row.get('configuration',{}).get('requiredBlocks',('INDIA_MARKET','GLOBAL','DERIVATIVES'))))]
        if not valid:
            blockers.append({'code':'RESEARCH_CONTEXT_RUN_UNAVAILABLE','date':str(day),
                'candidates':[{'runId':row['run_id'],'status':row['status']} for row in candidates]})
            return None
        if len(valid)>1:
            blockers.append({'code':'AMBIGUOUS_INPUT','input':'RESEARCH_CONTEXT','date':str(day),
                'candidates':[row['run_id'] for row in valid]})
            return None
        return valid[0]

    def _pin_structure_run(self,candidates,day,blockers):
        valid=[row for row in candidates if row['status']=='PASS']
        if not valid:
            blockers.append({'code':'MARKET_STRUCTURE_CONTEXT_RUN_UNAVAILABLE','date':str(day),
                'candidates':[{'runId':row['run_id'],'status':row['status']} for row in candidates]})
            return None
        if len(valid)>1:
            blockers.append({'code':'AMBIGUOUS_INPUT','input':'MARKET_STRUCTURE_CONTEXT','date':str(day),
                'candidates':[row['run_id'] for row in valid]})
            return None
        return valid[0]

    def _pin_membership(self,run_id,day,first_as_of,checksums,by_date,blockers):
        with self.contexts.connection() as db:
            ids=[row['snapshot_id'] for row in db.execute('''SELECT DISTINCT constituent_context->>'snapshotId' AS snapshot_id
                FROM market_structure_context_minutes WHERE run_id=%s AND session_date=%s
                AND constituent_context->>'snapshotId' IS NOT NULL ORDER BY 1''',(run_id,day)).fetchall()]
            rows=db.execute('''SELECT snapshot_id,source_quality,source_checksum,effective_from,effective_to,source_observed_at
                FROM banknifty_constituent_snapshots WHERE snapshot_id=ANY(%s) ORDER BY snapshot_id''',(ids,)).fetchall() if ids else []
        if not ids or not rows:
            blockers.append({'code':'MEMBERSHIP_SNAPSHOT_UNAVAILABLE','date':str(day)}); return
        stored={row['snapshot_id']:row for row in rows}; valid=[]; rejected=[]
        for snapshot_id in ids:
            row=stored.get(snapshot_id)
            ok=bool(row and row['source_quality']=='OFFICIAL_INDEX' and row['effective_from']<=day<=row['effective_to']
                and _stamp(row['source_observed_at'])<=first_as_of)
            if ok: valid.append(row)
            else: rejected.append({'snapshotId':snapshot_id,'sourceQuality':row['source_quality'] if row else None,
                'effectiveFrom':str(row['effective_from']) if row else None,'effectiveTo':str(row['effective_to']) if row else None,
                'sourceObservedAt':row['source_observed_at'].isoformat() if row else None})
        if rejected:
            blockers.append({'code':'OFFICIAL_MEMBERSHIP_REQUIRED','date':str(day),
                'reason':'OFFICIAL_DATE_VALID_OBSERVED_SNAPSHOT_REQUIRED','candidates':rejected}); return
        if len(valid)>1:
            blockers.append({'code':'AMBIGUOUS_INPUT','input':'MEMBERSHIP_SNAPSHOT','date':str(day),
                'candidates':[row['snapshot_id'] for row in valid]}); return
        row=valid[0]; checksums[row['snapshot_id']]=row['source_checksum']
        by_date[str(day)]={'snapshotId':row['snapshot_id'],'checksum':row['source_checksum'],'sourceObservedAt':row['source_observed_at'].isoformat()}

    def plan(self,revision_id,instrument_id,feature_profile,*,date_from=None,date_to=None):
        revision_id=str(revision_id); instrument_id=int(instrument_id)
        start=date.fromisoformat(str(date_from)) if date_from else None
        end=date.fromisoformat(str(date_to)) if date_to else None
        if start and end and start>end: raise ValueError('INVALID_CONTEXT_V3_DATE_RANGE')
        blockers=[]; profile={'version':feature_profile,'checksum':None,'requiredBlocks':[]}
        definition=None
        if feature_profile in PROFILES:
            definition=profile_definition(feature_profile)
            profile.update(checksum=_hash(definition),requiredBlocks=list(definition['requiredBlocks']))
        else: blockers.append({'code':'UNKNOWN_PROFILE','profile':feature_profile})
        pins=self._configuration(blockers)
        pins.update(researchContextRunIds={},researchContextRuns={},marketStructureContextRunIds={},
            marketStructureContextRuns={},membershipSnapshotChecksums={},membershipByDate={})
        plan={'kind':'CONTEXT_V3_GENERATION_PLAN','planVersion':CONTEXT_V3_PLAN_VERSION,
            'createdAt':datetime.now(UTC).isoformat(),'status':'BLOCKED',
            'revisionId':revision_id,'revisionChecksum':None,'instrumentId':instrument_id,
            'featureProfile':feature_profile,'profileDefinitionChecksum':profile['checksum'],
            'dateFrom':str(start) if start else None,'dateTo':str(end) if end else None,
            'revision':None,'profile':profile,'pins':pins,'blockers':blockers}
        if definition is None: return plan
        try: revision=self.repository.get_revision(revision_id)
        except ValueError:
            blockers.append({'code':'REVISION_NOT_FOUND','revisionId':revision_id}); return plan
        plan['revisionChecksum']=revision.checksum
        plan['revision']={'id':revision.id,'checksum':revision.checksum,'status':revision.status,
            'instrumentIds':list(revision.instrument_ids),'sessionFrom':str(revision.session_start),'sessionTo':str(revision.session_end)}
        fatal=False
        if revision.status!='ACCEPTED':
            blockers.append({'code':'REVISION_NOT_ACCEPTED','revisionId':revision.id,'status':revision.status}); fatal=True
        if instrument_id not in revision.instrument_ids:
            blockers.append({'code':'INSTRUMENT_NOT_IN_REVISION','revisionId':revision.id,'instrumentId':instrument_id}); fatal=True
        try: instrument=self.repository.get_instrument(instrument_id)
        except ValueError:
            blockers.append({'code':'INSTRUMENT_NOT_FOUND','instrumentId':instrument_id}); instrument=None; fatal=True
        if instrument and (instrument.exchange!='NSE' or instrument.tradingsymbol not in ('BANKNIFTY','NIFTY BANK')):
            blockers.append({'code':'BANKNIFTY_INDEX_REQUIRED','instrumentId':instrument_id,'exchange':instrument.exchange,
                'tradingsymbol':instrument.tradingsymbol}); fatal=True
        if fatal: return plan
        sessions={session.session_date:session for session in self.repository.sessions_between(revision.session_start,revision.session_end)
            if session.status=='OPEN'}
        candle_days={}
        for candle in self.repository.revision_candles(revision.id,instrument_id):
            day=candle.timestamp.astimezone(IST).date()
            candle_days[day]=min(candle.timestamp,candle_days.get(day,candle.timestamp))
        days=sorted(day for day in candle_days if day in sessions
                    and (start is None or day>=start) and (end is None or day<=end))
        plan['revision']['planDates']=[str(day) for day in days]
        if not days: blockers.append({'code':'NO_ELIGIBLE_SESSION_DATES','revisionId':revision.id,'instrumentId':instrument_id})
        required=set(definition['requiredBlocks'])
        needs_research=bool(required&{'INDIA_MARKET','GLOBAL','DERIVATIVES'})
        needs_structure=bool(required&{'BANK_STRUCTURE','DERIVATIVES'})
        needs_membership='BANK_STRUCTURE' in required
        for day in days:
            if needs_research:
                run=self._pin_research_run(self._research_runs(day),day,blockers,required)
                if run:
                    pins['researchContextRunIds'][str(day)]=run['run_id']
                    pins['researchContextRuns'][str(day)]={'runId':run['run_id'],'provider':run['provider'],
                        'checksum':run['raw_checksum'],'status':run['status']}
            structure_run=None
            if needs_structure:
                structure_run=self._pin_structure_run(self._structure_runs(day),day,blockers)
                if structure_run:
                    pins['marketStructureContextRunIds'][str(day)]=structure_run['run_id']
                    pins['marketStructureContextRuns'][str(day)]={'runId':structure_run['run_id'],
                        'checksum':structure_run['checksum'],'status':structure_run['status']}
            if needs_membership and structure_run:
                self._pin_membership(structure_run['run_id'],day,candle_days[day]+timedelta(minutes=1),
                    pins['membershipSnapshotChecksums'],pins['membershipByDate'],blockers)
        plan['status']='READY' if not blockers else 'BLOCKED'
        return plan

class ContextV3PlanValidationService:
    """Read-only validation of a saved plan; never repins or selects latest evidence."""

    def __init__(self,repository,root=None,context_repository=None):
        self.repository=repository; self.root=Path(root or memory_root())
        self.contexts=context_repository or MarketStructureContextRepository()

    def _fail(self,code,**details):
        detail=json.dumps(details,sort_keys=True) if details else ''
        raise ValueError(code+(':'+detail if detail else ''))

    def _json(self,name):
        path=self.root/'config'/name
        if not path.is_file(): self._fail('CONFIGURATION_MISSING',file=name)
        try: return json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError): self._fail('CONFIGURATION_INVALID',file=name)

    def validate(self,plan,revision_id,instrument_id,feature_profile):
        if not isinstance(plan,dict): self._fail('INVALID_PLAN')
        if plan.get('planVersion')!=CONTEXT_V3_PLAN_VERSION:
            self._fail('UNSUPPORTED_PLAN_VERSION',expected=CONTEXT_V3_PLAN_VERSION,actual=plan.get('planVersion'))
        if plan.get('status')!='READY': self._fail('PLAN_NOT_READY',status=plan.get('status'))
        revision_id=str(revision_id); instrument_id=int(instrument_id)
        if str(plan.get('revisionId'))!=revision_id:
            self._fail('REVISION_ID_MISMATCH',expected=revision_id,actual=plan.get('revisionId'))
        if plan.get('instrumentId')!=instrument_id:
            self._fail('INSTRUMENT_ID_MISMATCH',expected=instrument_id,actual=plan.get('instrumentId'))
        if plan.get('featureProfile')!=feature_profile:
            self._fail('FEATURE_PROFILE_MISMATCH',expected=feature_profile,actual=plan.get('featureProfile'))
        if feature_profile not in PROFILES: self._fail('UNKNOWN_PROFILE',profile=feature_profile)
        definition=profile_definition(feature_profile); profile_checksum=_hash(definition)
        if plan.get('profileDefinitionChecksum')!=profile_checksum:
            self._fail('PROFILE_DEFINITION_CHECKSUM_MISMATCH',expected=profile_checksum,
                actual=plan.get('profileDefinitionChecksum'))
        profile=plan.get('profile') if isinstance(plan.get('profile'),dict) else {}
        if profile.get('version') not in (None,feature_profile):
            self._fail('FEATURE_PROFILE_MISMATCH',expected=feature_profile,actual=profile.get('version'))
        if profile.get('checksum') not in (None,profile_checksum):
            self._fail('PROFILE_DEFINITION_CHECKSUM_MISMATCH',expected=profile_checksum,actual=profile.get('checksum'))
        pins=plan.get('pins')
        if not isinstance(pins,dict): self._fail('INVALID_PLAN_PINS')
        try: revision=self.repository.get_revision(revision_id)
        except ValueError: self._fail('REVISION_NOT_FOUND',revisionId=revision_id)
        if revision.status!='ACCEPTED':
            self._fail('REVISION_NOT_ACCEPTED',revisionId=revision_id,status=revision.status)
        if revision.checksum!=plan.get('revisionChecksum'):
            self._fail('REVISION_CHECKSUM_MISMATCH',expected=plan.get('revisionChecksum'),actual=revision.checksum)
        nested=plan.get('revision') if isinstance(plan.get('revision'),dict) else {}
        if nested.get('id') not in (None,revision.id):
            self._fail('REVISION_ID_MISMATCH',expected=revision_id,actual=nested.get('id'))
        if nested.get('checksum') not in (None,revision.checksum):
            self._fail('REVISION_CHECKSUM_MISMATCH',expected=revision.checksum,actual=nested.get('checksum'))
        scheduled=self._json('scheduled_events.json'); rules=self._json('market_structure_rules.json')
        if pins.get('scheduledConfigurationChecksum')!=_hash(scheduled):
            self._fail('SCHEDULED_CONFIGURATION_CHECKSUM_MISMATCH',
                expected=pins.get('scheduledConfigurationChecksum'),actual=_hash(scheduled))
        if pins.get('marketStructureRulesChecksum')!=_hash(rules):
            self._fail('MARKET_STRUCTURE_RULES_CHECKSUM_MISMATCH',
                expected=pins.get('marketStructureRulesChecksum'),actual=_hash(rules))
        dates=[str(day) for day in nested.get('planDates') or []]
        required=set(definition['requiredBlocks'])
        needs_research=bool(required&{'INDIA_MARKET','GLOBAL','DERIVATIVES'})
        needs_structure=bool(required&{'BANK_STRUCTURE','DERIVATIVES'})
        needs_membership='BANK_STRUCTURE' in required
        run_ids=pins.get('researchContextRunIds') or {}; run_records=pins.get('researchContextRuns') or {}
        structure_ids=pins.get('marketStructureContextRunIds') or {}
        structure_records=pins.get('marketStructureContextRuns') or {}
        snapshot_checksums=pins.get('membershipSnapshotChecksums') or {}
        membership_by_date=pins.get('membershipByDate') or {}
        for value in (run_ids,run_records,structure_ids,structure_records,snapshot_checksums,membership_by_date):
            if not isinstance(value,dict): self._fail('INVALID_PLAN_PINS')
        if needs_research and (set(run_ids)!=set(dates) or set(run_records)!=set(dates)):
            self._fail('PLAN_PINS_INCOMPLETE',input='RESEARCH_CONTEXT',dates=dates)
        if needs_structure and (set(structure_ids)!=set(dates) or set(structure_records)!=set(dates)):
            self._fail('PLAN_PINS_INCOMPLETE',input='MARKET_STRUCTURE_CONTEXT',dates=dates)
        if needs_membership and set(membership_by_date)!=set(dates):
            self._fail('PLAN_PINS_INCOMPLETE',input='MEMBERSHIP_SNAPSHOT',dates=dates)
        for day,row in membership_by_date.items():
            if not isinstance(row,dict) or row.get('snapshotId') not in snapshot_checksums \
                or snapshot_checksums.get(row.get('snapshotId'))!=row.get('checksum'):
                self._fail('MEMBERSHIP_PIN_INCONSISTENT',date=str(day))
        research={}; structure={}
        with self.contexts.connection() as db:
            for day,run_id in run_ids.items():
                record=run_records.get(day) if isinstance(run_records.get(day),dict) else {}
                if record.get('runId') not in (None,run_id):
                    self._fail('RESEARCH_RUN_ID_MISMATCH',date=str(day),expected=run_id,actual=record.get('runId'))
                row=db.execute('SELECT run_id,status,raw_checksum,date_from,date_to,configuration FROM research_context_runs WHERE run_id=%s',
                    (run_id,)).fetchone()
                if not row: self._fail('RESEARCH_RUN_NOT_FOUND',date=str(day),runId=run_id)
                if row['status']!='COMPLETE': self._fail('RESEARCH_RUN_NOT_COMPLETE',date=str(day),runId=run_id,status=row['status'])
                required=set(PROFILES[feature_profile]) & {'INDIA_MARKET','GLOBAL','DERIVATIVES'}
                if not required.issubset(set(row.get('configuration',{}).get('requiredBlocks',('INDIA_MARKET','GLOBAL','DERIVATIVES')))):
                    self._fail('RESEARCH_RUN_PROFILE_SCOPE_MISMATCH',date=str(day),runId=run_id)
                if row['raw_checksum']!=record.get('checksum'):
                    self._fail('RESEARCH_RUN_CHECKSUM_MISMATCH',date=str(day),runId=run_id,
                        expected=record.get('checksum'),actual=row['raw_checksum'])
                try: day_value=date.fromisoformat(str(day))
                except ValueError: self._fail('INVALID_PLAN_PINS',input='RESEARCH_CONTEXT',date=str(day))
                if not row['date_from']<=day_value<=row['date_to']:
                    self._fail('RESEARCH_RUN_RANGE_MISMATCH',date=str(day),runId=run_id)
                research[str(day)]=run_id
            for day,run_id in structure_ids.items():
                record=structure_records.get(day) if isinstance(structure_records.get(day),dict) else {}
                if record.get('runId') not in (None,run_id):
                    self._fail('MARKET_STRUCTURE_RUN_ID_MISMATCH',date=str(day),expected=run_id,actual=record.get('runId'))
                row=db.execute('SELECT run_id,status,checksum,date_from,date_to FROM market_structure_context_runs WHERE run_id=%s',
                    (run_id,)).fetchone()
                if not row: self._fail('MARKET_STRUCTURE_RUN_NOT_FOUND',date=str(day),runId=run_id)
                if row['status']!='PASS': self._fail('MARKET_STRUCTURE_RUN_NOT_PASS',date=str(day),runId=run_id,status=row['status'])
                if row['checksum']!=record.get('checksum'):
                    self._fail('MARKET_STRUCTURE_RUN_CHECKSUM_MISMATCH',date=str(day),runId=run_id,
                        expected=record.get('checksum'),actual=row['checksum'])
                try: day_value=date.fromisoformat(str(day))
                except ValueError: self._fail('INVALID_PLAN_PINS',input='MARKET_STRUCTURE_CONTEXT',date=str(day))
                if not row['date_from']<=day_value<=row['date_to']:
                    self._fail('MARKET_STRUCTURE_RUN_RANGE_MISMATCH',date=str(day),runId=run_id)
                structure[str(day)]=run_id
            ids=sorted(snapshot_checksums)
            rows=db.execute('SELECT snapshot_id,source_checksum FROM banknifty_constituent_snapshots WHERE snapshot_id=ANY(%s)',
                (ids,)).fetchall() if ids else []
        stored={row['snapshot_id']:row['source_checksum'] for row in rows}
        for snapshot_id,checksum in snapshot_checksums.items():
            if snapshot_id not in stored:
                self._fail('MEMBERSHIP_SNAPSHOT_NOT_FOUND',snapshotId=snapshot_id)
            if stored[snapshot_id]!=checksum:
                self._fail('MEMBERSHIP_CHECKSUM_MISMATCH',snapshotId=snapshot_id,expected=checksum,actual=stored[snapshot_id])
        return {'researchContextRuns':research,'marketStructureRuns':structure,
            'membershipSnapshotChecksums':dict(snapshot_checksums),
            'scheduledConfiguration':scheduled,'marketStructureRules':rules}


class ContextV3ActivationService:
    """Explicit per-profile planning, revalidation and generation with isolated outcomes."""

    def __init__(self, repository, root=None):
        self.repository = repository
        self.root = Path(root or memory_root())

    @staticmethod
    def _profiles(value):
        if value is None or value == 'all' or value == ['all']:
            return list(PROFILES)
        values = [part.strip() for part in value.split(',')] if isinstance(value, str) else list(value)
        values = [part for part in values if part]
        if not values:
            raise ValueError('PROFILES_REQUIRED')
        if 'all' in values:
            if len(values) != 1:
                raise ValueError('ALL_PROFILES_MUST_BE_EXCLUSIVE')
            return list(PROFILES)
        unknown = [part for part in values if part not in PROFILES]
        if unknown:
            raise ValueError('UNKNOWN_PROFILE:' + ','.join(unknown))
        return list(dict.fromkeys(values))

    @staticmethod
    def _unavailable_reasons(rows):
        reasons = Counter()
        for row in rows:
            if row.quality_status == 'PASS':
                continue
            found = False
            blocks = (row.values.get('_contextV3') or {}).get('blocks') or {}
            for block in blocks.values():
                if block.get('status') == 'PASS':
                    continue
                for reason in block.get('missingReasons') or ():
                    key = 'MISSING_REQUIRED_FEATURES' if str(reason).startswith('MISSING:') else str(reason)
                    reasons[key] += 1
                    found = True
            if not found:
                reasons['NO_ELIGIBLE_HORIZON_OR_BLOCK_REASON'] += 1
        return dict(sorted(reasons.items()))

    def activate(self, revision_id, instrument_id, profiles='all', *, output_dir, dry_run=False):
        requested = self._profiles(profiles)
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        planner = ContextV3PlanningService(self.repository, self.root)
        validator = ContextV3PlanValidationService(self.repository, self.root)
        generator = ContextV3FeatureGenerationService(self.repository, root=self.root)
        outcomes = []
        for profile in requested:
            outcome = {'profileVersion': profile, 'status': 'BLOCKED', 'dryRun': bool(dry_run)}
            try:
                plan = planner.plan(revision_id, instrument_id, profile)
                plan_path = output / f'{profile}.plan.json'
                plan_path.write_text(json.dumps(plan, sort_keys=True, indent=2, default=str), encoding='utf-8')
                outcome.update(planPath=str(plan_path), planStatus=plan['status'],
                    planBlockers=plan.get('blockers', []))
                if dry_run:
                    outcome['status'] = 'READY' if plan['status'] == 'READY' else 'BLOCKED'
                    outcomes.append(outcome)
                    continue
                if plan['status'] != 'READY':
                    outcomes.append(outcome)
                    continue
                pins = validator.validate(plan, revision_id, instrument_id, profile)
                rows, inserted = generator.generate(revision_id, instrument_id, profile,
                    research_context_runs=pins['researchContextRuns'],
                    market_structure_runs=pins['marketStructureRuns'],
                    membership_snapshot_checksums=pins['membershipSnapshotChecksums'],
                    scheduled_configuration=pins['scheduledConfiguration'],
                    market_structure_rules=pins['marketStructureRules'])
                unavailable = [row for row in rows if row.quality_status != 'PASS']
                outcome.update(status='PASS' if rows and not unavailable else 'PARTIAL' if rows else 'UNAVAILABLE',
                    rows={'considered': len(rows), 'generated': inserted, 'reused': len(rows) - inserted},
                    passCount=len(rows) - len(unavailable), unavailableCount=len(unavailable),
                    unavailableReasons=self._unavailable_reasons(rows))
                if generator.last_manifest:
                    outcome['manifestVersion'] = generator.last_manifest['version']
                    outcome['manifestChecksum'] = generator.last_manifest['checksum']
            except Exception as exc:
                outcome.update(status='BLOCKED', error=str(exc))
            outcomes.append(outcome)
        coverage = None
        try:
            coverage = ContextV3CoverageService().coverage()
        except Exception as exc:
            coverage = {'status': 'UNAVAILABLE', 'error': str(exc)}
        by_profile = {}
        if isinstance(coverage, dict):
            by_profile = {row.get('profileVersion'): row for row in coverage.get('profiles', [])}
        for outcome in outcomes:
            if outcome['profileVersion'] in by_profile:
                outcome['coverage'] = by_profile[outcome['profileVersion']]
        statuses = [row['status'] for row in outcomes]
        if dry_run:
            overall = 'READY' if statuses and all(value == 'READY' for value in statuses) else \
                'PARTIAL' if any(value == 'READY' for value in statuses) else 'BLOCKED'
        else:
            overall = 'PASS' if statuses and all(value == 'PASS' for value in statuses) else \
                'PARTIAL' if any(value in ('PASS', 'PARTIAL') for value in statuses) else 'BLOCKED'
        return {'status': overall, 'revisionId': str(revision_id), 'instrumentId': int(instrument_id),
            'requestedProfiles': requested, 'dryRun': bool(dry_run), 'outputDir': str(output),
            'profiles': outcomes, 'coverage': coverage}
