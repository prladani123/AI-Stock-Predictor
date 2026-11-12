"""Prospective, neutral top-five research. No broker client or model registry access."""
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import mean, median, pstdev

SCHEMA_VERSION = 'banknifty-microstructure-v1'


def stamp(value):
    result=datetime.fromisoformat(value) if isinstance(value,str) else value
    if not isinstance(result,datetime) or result.tzinfo is None: raise ValueError('AWARE_TIMESTAMP_REQUIRED')
    return result.astimezone(timezone.utc)


def number(value):
    if value is None or isinstance(value,bool): return None
    try: result=float(value)
    except (ValueError,TypeError): return None
    return result if isfinite(result) and result>=0 else None


def imbalance(left,right):
    return (left-right)/(left+right) if left is not None and right is not None and left+right>0 else None


def depth_features(tick, *, market_as_of, decision_at, previous=None, maximum_age_seconds=120):
    market,decision=stamp(market_as_of),stamp(decision_at)
    if market>decision: raise ValueError('FUTURE_MARKET_TIMESTAMP')
    reasons=[]; values={}
    if not tick: return {'status':'UNAVAILABLE','values':{},'missingReasons':['MISSING_BID','MISSING_ASK'],'sourceMaxTimestamp':None}
    source,receipt=stamp(tick['event_at']),stamp(tick['received_at'])
    if source>market or receipt>decision: raise ValueError('FUTURE_SOURCE_TIMESTAMP')
    if tick.get('instrument_type') not in ('FUT','CE','PE'): raise ValueError('DERIVATIVE_DEPTH_ONLY')
    # Freshness is measured at inference, not relative to an equally stale cutoff.
    age=(decision-source).total_seconds()
    if age>maximum_age_seconds:
        return {'status':'UNAVAILABLE','values':{},'missingReasons':['STALE_SOURCE'],
                'sourceMaxTimestamp':source.isoformat(),'ageSeconds':age}
    sides=[]
    for key,missing in (('bids','MISSING_BID'),('asks','MISSING_ASK')):
        levels=[]
        for item in (tick.get(key) or [])[:5]:
            price,quantity=number(item.get('price')),number(item.get('quantity'))
            if price is None or price<=0 or quantity is None:
                reasons.append('INVALID_DEPTH'); break
            levels.append((price,quantity,number(item.get('orders'))))
        if not levels: reasons.append(missing)
        if len(levels)!=5: reasons.append('INCOMPLETE_DEPTH')
        sides.append(levels)
    bids,asks=sides
    if (any(bids[i][0]<=bids[i+1][0] for i in range(len(bids)-1))
            or any(asks[i][0]>=asks[i+1][0] for i in range(len(asks)-1))): reasons.append('INVALID_DEPTH_ORDER')
    if bids and asks and bids[0][0]>=asks[0][0]: reasons.append('CROSSED_BID_ASK')
    if any(r in reasons for r in ('INVALID_DEPTH','INVALID_DEPTH_ORDER','CROSSED_BID_ASK')):
        return {'status':'UNAVAILABLE','values':{},'missingReasons':sorted(set(reasons)),'sourceMaxTimestamp':source.isoformat()}
    bid=bids[0][0] if bids else None; ask=asks[0][0] if asks else None
    midpoint=(bid+ask)/2 if bid is not None and ask is not None else None
    bq=bids[0][1] if bids else None; aq=asks[0][1] if asks else None
    complete=len(bids)==len(asks)==5
    bid_sum=sum(v[1] for v in bids) if complete else None
    ask_sum=sum(v[1] for v in asks) if complete else None
    orders=complete and all(v[2] is not None for v in bids+asks)
    oi,volume=number(tick.get('oi')),number(tick.get('cumulative_volume'))
    if oi is None: reasons.append('MISSING_OI')
    if volume is None: reasons.append('MISSING_VOLUME')
    oi_delta=volume_delta=None
    if previous:
        ps,pr=stamp(previous['event_at']),stamp(previous['received_at'])
        if (previous.get('instrument_id')==tick.get('instrument_id') and ps<source and pr<=decision
                and (source-ps).total_seconds()<=maximum_age_seconds and ps.date()==source.date()):
            prior_oi,prior_volume=number(previous.get('oi')),number(previous.get('cumulative_volume'))
            oi_delta=oi-prior_oi if oi is not None and prior_oi is not None else None
            volume_delta=volume-prior_volume if volume is not None and prior_volume is not None and volume>=prior_volume else None
    values={'bestBid':bid,'bestAsk':ask,'spread':ask-bid if midpoint else None,
        'spreadPct':(ask-bid)/midpoint if midpoint else None,'midpoint':midpoint,
        'l1QuantityImbalance':imbalance(bq,aq),'top5QuantityImbalance':imbalance(bid_sum,ask_sum),
        'orderCountImbalance':imbalance(sum(v[2] for v in bids),sum(v[2] for v in asks)) if orders else None,
        'depthWeightedImbalance':imbalance(sum(v[1]/(i+1) for i,v in enumerate(bids)),sum(v[1]/(i+1) for i,v in enumerate(asks))) if complete else None,
        'microprice':(ask*bq+bid*aq)/(bq+aq) if midpoint and bq+aq>0 else None,
        'bidL1Concentration':bq/bid_sum if bid_sum else None,'askL1Concentration':aq/ask_sum if ask_sum else None,
        'totalBuySellImbalance':imbalance(number(tick.get('total_buy_quantity')),number(tick.get('total_sell_quantity'))),
        'oi':oi,'volume':volume,'oiChange':oi_delta,'volumeChange':volume_delta}
    return {'status':'PASS' if not reasons else 'PARTIAL' if midpoint else 'UNAVAILABLE',
        'values':values,'missingReasons':sorted(set(reasons)),'sourceMaxTimestamp':source.isoformat(),
        'receivedAt':receipt.isoformat(),'tickId':tick.get('tick_id'),'ageSeconds':age,'depthComplete':complete}


def option_summary(items,expected_count):
    available=[row for row in items if row['features']['status']!='UNAVAILABLE']
    values=[row['features']['values'] for row in available]
    spreads=[v['spreadPct'] for v in values if v.get('spreadPct') is not None]
    atm={row['optionType']:row['features']['values'].get('top5QuantityImbalance') for row in available if row['atmOffset']==0}
    imbalances=[v['top5QuantityImbalance'] for v in values if v.get('top5QuantityImbalance') is not None]
    complete=expected_count>0 and len(items)==expected_count and all(r['features']['status']=='PASS' for r in items)
    pairs=[v['oiChange']*v['top5QuantityImbalance'] for v in values if v.get('oiChange') is not None and v.get('top5QuantityImbalance') is not None]
    return {'status':'PASS' if complete else 'PARTIAL' if available else 'UNAVAILABLE',
        'values':{'atmCeImbalance':atm.get('CE'),'atmPeImbalance':atm.get('PE'),
            'atmPairImbalance':mean([atm['CE'],atm['PE']]) if atm.get('CE') is not None and atm.get('PE') is not None else None,
            'nearAtmAggregateImbalance':mean(imbalances) if imbalances else None,
            'spreadMean':mean(spreads) if spreads else None,'spreadMedian':median(spreads) if spreads else None,
            'spreadDispersion':pstdev(spreads) if len(spreads)>1 else None,
            'depthCompleteness':sum(r['features'].get('depthComplete',False) for r in items)/expected_count if expected_count else None,
            'oiDepthProductMean':mean(pairs) if pairs else None},
        'missingReasons':sorted({reason for r in items for reason in r['features']['missingReasons']} | (set() if complete else {'UNIVERSE_INCOMPLETE'})),
        'selectedCount':expected_count,'observedCount':len(available)}


class MicrostructureRuntimeService:
    def __init__(self,reader=None):
        from .live_product_reader import LiveProductDataReader
        self.reader=reader or LiveProductDataReader()

    def snapshot(self,decision_at=None):
        decision=stamp(decision_at or datetime.now(timezone.utc))
        state=self.reader.current_state(decision)
        selections=[s for s in state.futures_selections.values() if s.selected_at<=decision]
        universes=[u for u in state.option_universes.values() if u.selected_at<=decision]
        selection=max(selections,key=lambda s:s.selected_at,default=None)
        universe=max(universes,key=lambda u:u.selected_at,default=None)
        ids=set(universe.instrument_ids if universe else ())
        if selection: ids.add(selection.instrument_id)
        ticks={}
        if ids:
            # Existing retained Zerodha ticks only. Two observations per contract;
            # receipt and event cutoffs enforce prospective knowledge.
            with self.reader._connection() as db:
                rows=db.execute('''SELECT instrument_id,payload FROM (
                    SELECT instrument_id,payload,row_number() OVER
                    (PARTITION BY instrument_id ORDER BY event_at DESC,received_at DESC,tick_id) AS n
                    FROM live_raw_ticks WHERE instrument_id=ANY(%s) AND event_at<=%s
                    AND received_at<=%s AND event_at>=%s) ranked WHERE n<=2''',
                    (sorted(ids),decision,decision,decision-timedelta(seconds=240))).fetchall()
            for row in rows:
                payload=row['payload']
                try:
                    eligible=(stamp(payload['event_at'])<=decision and stamp(payload['received_at'])<=decision)
                except (KeyError,TypeError,ValueError): eligible=False
                if eligible: ticks.setdefault(row['instrument_id'],[]).append(payload)
        cutoffs=[stamp(t['event_at']) for rows in ticks.values() for t in rows]
        for minute in getattr(state,'finalized_minutes',{}).values():
            if minute.quality_state!='PASS': continue
            boundary=stamp(minute.minute_at)+timedelta(minutes=1)
            if boundary<=decision and stamp(minute.first_finalized_at)<=decision:
                cutoffs.append(boundary)
        market=max(cutoffs,default=None)
        if market is None:
            missing={'status':'UNAVAILABLE','values':{},'missingReasons':['MARKET_CUTOFF_UNAVAILABLE'],'sourceMaxTimestamp':None}
            return {'status':'UNAVAILABLE','marketAsOf':None,'decisionAt':decision.isoformat(),
                'schemaVersion':SCHEMA_VERSION,'futures':missing,'options':option_summary([],len(universe.instrument_ids) if universe else 0),
                'greeks':{'status':'GREEKS_UNAVAILABLE'},'sourceMaxTimestamp':None,
                'missingReasons':['MARKET_CUTOFF_UNAVAILABLE'],
                'freshness':{'maximumAgeSeconds':120,'availabilityBasis':'EVENT_AND_RECEIPT_CUTOFF'},
                'notice':'RESEARCH ONLY; NOT USED BY PRICE_CORE_V2'}
        def calculate(identifier):
            rows=sorted(ticks.get(identifier,[]),key=lambda r:(r['event_at'],r['received_at']),reverse=True)
            return depth_features(rows[0] if rows else None,market_as_of=market,decision_at=decision,previous=rows[1] if len(rows)>1 else None)
        futures=calculate(selection.instrument_id) if selection else calculate(None)
        items=[]
        for identifier in sorted(universe.instrument_ids if universe else ()):
            instrument=state.instruments.get(identifier)
            if not instrument or instrument.strike is None: continue
            items.append({'instrumentId':identifier,'optionType':instrument.instrument_type,
                'atmOffset':float(instrument.strike-universe.atm_strike),'features':calculate(identifier)})
        options=option_summary(items,len(universe.instrument_ids) if universe else 0)
        if universe and (universe.missing_pairs or universe.rejection_reasons):
            options['status']='PARTIAL' if options['observedCount'] else 'UNAVAILABLE'
            options['missingReasons']=sorted(set(options['missingReasons']+['UNIVERSE_INCOMPLETE']))
        statuses=[futures['status'],options['status']]
        sources=[r['features']['sourceMaxTimestamp'] for r in items if r['features'].get('sourceMaxTimestamp')]
        if futures.get('sourceMaxTimestamp'): sources.append(futures['sourceMaxTimestamp'])
        return {'status':'PASS' if statuses==['PASS','PASS'] else 'PARTIAL' if any(s!='UNAVAILABLE' for s in statuses) else 'UNAVAILABLE',
            'marketAsOf':market.isoformat(),'decisionAt':decision.isoformat(),'schemaVersion':SCHEMA_VERSION,
            'futures':futures,'options':options,'greeks':{'status':'GREEKS_UNAVAILABLE'},
            'sourceMaxTimestamp':max(sources) if sources else None,
            'missingReasons':sorted(set(futures['missingReasons']+options['missingReasons'])),
            'freshness':{'maximumAgeSeconds':120,'availabilityBasis':'EVENT_AND_RECEIPT_CUTOFF'},
            'notice':'RESEARCH ONLY; NOT USED BY PRICE_CORE_V2'}
