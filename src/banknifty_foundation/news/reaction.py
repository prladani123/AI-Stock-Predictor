from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from statistics import pstdev
from typing import Any

from ..big_move_labels import calculate_big_move_label, default_big_move_policy
from ..calendar import IST
from ..models import Candle, TradingSession


class NewsReactionService:
    @staticmethod
    def valid_rows(rows, day, available_at=None):
        from ..research_context import _stamp, _number
        seen,duplicates={},set()
        for row in rows:
            at=_stamp(row['at'])
            if at.astimezone(IST).date()!=day or at.second or at.microsecond: continue
            if available_at and (at+timedelta(minutes=1)>available_at or
                    (row.get('first_finalized_at') and _stamp(row['first_finalized_at'])>available_at)): continue
            values={k:_number(row.get(k),positive=True) for k in ('open','high','low','close')}
            if at in seen: duplicates.add(at)
            if any(v is None for v in values.values()) or not values['low']<=min(values['open'],values['close'])<=max(values['open'],values['close'])<=values['high']:
                duplicates.add(at); continue
            seen[at]={**row,'at':at,**values}
        return [seen[at] for at in sorted(seen) if at not in duplicates]

    @staticmethod
    def reference(known_at, rows):
        """First full tradable bar opening at/after dissemination; never a prior close."""
        if known_at is None or known_at.tzinfo is None: return None
        local=known_at.astimezone(IST)
        opening=datetime.combine(local.date(),time(9,15),IST)
        boundary=max(opening,known_at.replace(second=0,microsecond=0))
        if boundary<known_at: boundary+=timedelta(minutes=1)
        candidates=[r for r in rows if r['at']==boundary and time(9,15)<=r['at'].astimezone(IST).time()<time(15,30)]
        return candidates[0] if len(candidates)==1 else None

    def progressive(self, known_at, rows, now):
        if known_at is None:
            return {'status':'UNMEASURABLE','reason':'EVENT_TIME_UNKNOWN','availableHorizons':[],'pendingHorizons':[]}
        local=known_at.astimezone(IST)
        close=datetime.combine(local.date(),time(15,30),IST)
        if not local.time() < time(15,30):
            return {'status':'UNMEASURABLE','reason':'OUTSIDE_CONTINUOUS_SESSION','availableHorizons':[],'pendingHorizons':[]}
        # Minute stamps identify the opening boundary. A close is available one minute later.
        rows=self.valid_rows(rows,local.date(),now)
        reference=self.reference(known_at,rows)
        if not reference:
            return {'status':'UNMEASURABLE' if now>close+timedelta(minutes=15) else 'PENDING',
                    'reason':'NO_COMPLETED_REFERENCE','availableHorizons':[],'pendingHorizons':[5,10,20,30]}
        at=reference['at']
        measured=self.measure(known_at,rows)
        available=[]; pending=[]; invalid=[]
        lookup={r['at']:r for r in rows}
        for horizon in (5,10,20,30):
            expiry=at+timedelta(minutes=horizon)
            if expiry+timedelta(minutes=1)>close:
                invalid.append(horizon)
            elif now<expiry+timedelta(minutes=1):
                pending.append(horizon)
            elif all(at+timedelta(minutes=i) in lookup for i in range(1,horizon+1)):
                available.append(horizon)
            elif now>close+timedelta(minutes=15): invalid.append(horizon)
            else: pending.append(horizon)
            if horizon not in available:
                measured.get('returns',{})[str(horizon)]=None
                measured.get('horizon_metrics',{})[str(horizon)]={}
                if horizon!=5: measured.get('big_move',{})[str(horizon)]={'label':'UNKNOWN','quality_status':'PENDING_OR_MISSING'}
        measured.update(availableHorizons=available,pendingHorizons=pending,unmeasurableHorizons=invalid,
                        status='COMPLETE_30M' if len(available)==4 else f'PARTIAL_{max(available)}M' if available and pending else 'UNMEASURABLE' if not pending else 'PENDING',
                        measuredAt=now.isoformat())
        return measured

    def measure(self, known_at: datetime | None, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if known_at is None:
            return {"status": "REACTION_NOT_MEASURABLE", "reason": "EVENT_TIME_UNKNOWN"}
        rows=self.valid_rows(rows,known_at.astimezone(IST).date())
        reference_row=self.reference(known_at,rows)
        if not reference_row:
            return {"status": "REACTION_NOT_MEASURABLE", "reason": "NO_REFERENCE_MINUTE"}
        candles = [self._candle(row) for row in rows]
        reference_index = next(index for index, item in enumerate(candles) if item.timestamp == reference_row["at"])
        reference = candles[reference_index]
        day = reference.timestamp.astimezone(IST).date()
        session = TradingSession(day, datetime.combine(day, time(9, 15), IST), datetime.combine(day, time(15, 30), IST))
        lookup = {item.timestamp: item for item in candles}
        result: dict[str, Any] = {"status": "REACTION_AVAILABLE", "reference_at": reference.timestamp.isoformat(),
                                  "reference_price": float(reference.close), "returns": {}, "big_move": {},
                                  "reference_definition":"FIRST_FULL_BAR_AT_OR_AFTER_KNOWN_TIME", "horizon_metrics": {}}
        for horizon in (5, 10, 20, 30):
            future = lookup.get(reference.timestamp + timedelta(minutes=horizon))
            complete=bool(future and future.timestamp+timedelta(minutes=1)<=session.close_at
                and all(reference.timestamp+timedelta(minutes=i) in lookup for i in range(1,horizon+1)))
            result["returns"][str(horizon)] = float(future.close / reference.close - 1) if complete else None
            path=[lookup[reference.timestamp+timedelta(minutes=i)] for i in range(1,horizon+1)] if complete else []
            before_window=candles[max(0,reference_index-20):reference_index+1]
            before=self._returns(before_window)
            prior_complete=len(before_window)==21 and all(b.timestamp-a.timestamp==timedelta(minutes=1) for a,b in zip(before_window,before_window[1:]))
            after=self._returns([reference,*path])
            result['horizon_metrics'][str(horizon)]={
                'max_up_excursion':float(max(c.high for c in path)/reference.close-1) if path else None,
                'max_down_excursion':float(min(c.low for c in path)/reference.close-1) if path else None,
                'volatility_expansion':pstdev(after)/pstdev(before) if complete and prior_complete and pstdev(before)>0 else None}
        known = candles[:reference_index + 1]
        future = candles[reference_index + 1:reference_index + 31]
        policy = default_big_move_policy()
        for horizon in (10, 20, 30):
            label = calculate_big_move_label(dataset_revision_id="market-memory-v2", instrument_id=0,
                as_of_candle=reference, horizon=horizon, session=session,
                candles_known_at_as_of=known, future_candles=future, policy=policy)
            result["big_move"][str(horizon)] = label.to_dict() if label else {"label": "UNKNOWN", "quality_status": "OUTSIDE_SESSION"}
        path = [item for item in candles if reference.timestamp < item.timestamp <= reference.timestamp + timedelta(minutes=30)]
        result["max_up_excursion"] = float(max(item.high for item in path) / reference.close - 1) if path else None
        result["max_down_excursion"] = float(min(item.low for item in path) / reference.close - 1) if path else None
        before = self._returns(candles[max(0, reference_index - 20):reference_index + 1])
        after = self._returns(path[:11])
        result["volatility_expansion"] = (pstdev(after) / pstdev(before)
            if len(after) > 1 and len(before) > 1 and pstdev(before) > 0 else None)
        return result

    @staticmethod
    def _candle(row: dict[str, Any]) -> Candle:
        return Candle(row["at"], *(Decimal(str(row[key])) for key in ("open", "high", "low", "close")),
                      int(row.get("volume") or 0), None)

    @staticmethod
    def _returns(candles: list[Candle]) -> list[float]:
        return [float(candles[index].close / candles[index - 1].close - 1) for index in range(1, len(candles))]
