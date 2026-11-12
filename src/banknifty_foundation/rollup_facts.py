"""Factual aggregation of PASS stored records; missing observations stay unknown."""
from collections import Counter
from datetime import date
from .big_move_features import classify_futures_buildup


def change(opening, closing):
    return closing-opening if opening is not None and closing is not None else None


def expiry_context(facts, day):
    future = facts.get('futures', {}).get('expiry')
    option = facts.get('options', {}).get('expiry')
    future = date.fromisoformat(str(future)) if future else None
    option = date.fromisoformat(str(option)) if option else None
    if future == day:
        return 'MONTHLY_EXPIRY'
    if option == day:
        # Different expiries alone do not prove weekly classification (front future may have rolled).
        return 'WEEKLY_EXPIRY' if facts.get('options', {}).get('expiryType') == 'WEEKLY' else 'UNKNOWN'
    if future and option and future > day and option > day:
        return 'NON_EXPIRY'
    return 'UNKNOWN'


def derivatives(market, day, index):
    result = {'futures': {'status':'NOT_VERIFIED'}, 'options': {'status':'NOT_VERIFIED'}}
    selection = market.selected_future(day)
    future = [r for r in market.minutes(day,'FUT') if selection and r['instrument_id']==selection['instrument_id']]
    if future:
        first,last=future[0],future[-1]
        spot={r['at']:r['close'] for r in index}
        opening=first['close']-spot[first['at']] if first['at'] in spot else None
        closing=last['close']-spot[last['at']] if last['at'] in spot else None
        oi0,oi1=first.get('oi_close'),last.get('oi_close')
        buildup=[classify_futures_buildup(b['close']-a['close'],change(a.get('oi_close'),b.get('oi_close')))
                 for a,b in zip(future,future[1:]) if a.get('oi_close') is not None and b.get('oi_close') is not None]
        result['futures']={'status':'OBSERVED_PASS_SNAPSHOTS','contract':selection['contract'],
            'expiry':selection['expiry'],'openingObservedAt':first['at'].isoformat(),'closingObservedAt':last['at'].isoformat(),
            'openingPremium':opening,'closingPremium':closing,'premiumChange':change(opening,closing),
            'openingOi':oi0,'closingOi':oi1,'oiChange':change(oi0,oi1),
            'observedVolume':sum(r['volume'] for r in future if r.get('volume') is not None) if any(r.get('volume') is not None for r in future) else None,
            'observedMinutes':len(future),'dominantBuildup':Counter(buildup).most_common(1)[0][0] if buildup else 'NOT_VERIFIED'}
    options=market.options(day)
    if options:
        expiry=min(r['expiry'] for r in options if r['expiry']>=day.isoformat()) if any(r['expiry']>=day.isoformat() for r in options) else None
        options=[r for r in options if r['expiry']==expiry]
        if not options:return result
        # Fix a single universe so strike-window rotation cannot masquerade as OI change.
        universe=options[-1]['universe_id']
        options=[r for r in options if r['universe_id']==universe]
        def snapshot(at):
            rows=[r for r in options if r['minute_at']==at]
            def total(side):
                values=[r.get('oi') for r in rows if r['option_type']==side]
                return sum(values) if values and all(v is not None for v in values) else None
            ce,pe=total('CE'),total('PE')
            atm=[r for r in rows if r['atm_offset']==0]
            pair={r['option_type']:r for r in atm}
            def strongest(side):
                values=[r for r in rows if r['option_type']==side and r.get('oi') is not None]
                return float(max(values,key=lambda r:r['oi'])['strike']) if values else None
            return {'ceOi':ce,'peOi':pe,'pcrOi':pe/ce if ce and pe is not None else None,
                'atm':float(atm[0]['strike']) if atm else None,
                'straddle':sum(float(pair[s]['last_price']) for s in ('CE','PE')) if set(pair)=={'CE','PE'} else None,
                'strongestCeOiStrike':strongest('CE'),'strongestPeOiStrike':strongest('PE'),
                'contracts':sorted(r['instrument_id'] for r in rows)}
        first_at,last_at=min(r['minute_at'] for r in options),max(r['minute_at'] for r in options)
        first,last=snapshot(first_at),snapshot(last_at)
        comparable=first['contracts']==last['contracts']
        result['options']={'status':'OBSERVED_PASS_SNAPSHOTS','scope':'SELECTED_STRIKE_UNIVERSE_NOT_ENTIRE_CHAIN',
            'expiry':expiry,'universeId':universe,'openingObservedAt':first_at,'closingObservedAt':last_at,
            'opening':first,'closing':last,'comparableUniverse':comparable,
            'ceOiChange':change(first['ceOi'],last['ceOi']) if comparable else None,
            'peOiChange':change(first['peOi'],last['peOi']) if comparable else None,
            'straddleChange':change(first['straddle'],last['straddle']) if first['atm']==last['atm'] else None}
    return result
