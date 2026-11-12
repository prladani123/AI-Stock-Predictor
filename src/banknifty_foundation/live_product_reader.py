"""One read-only, repeatable PostgreSQL snapshot for the live product and V2 worker."""
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from types import UnionType
from typing import Any, get_args, get_origin, get_type_hints

from .calendar import IST
from .live_models import FinalizedMinute, FuturesSelection, LiveInstrument, OptionMinuteSnapshot, OptionUniverse, LiveQualityIssue
from .models import TradingSession


def decode_record(cls, payload):
    """Restore stored live-market records, including depth and nullable fields."""
    def convert(kind, value):
        if value is None: return None
        origin, args = get_origin(kind), get_args(kind)
        if origin is UnionType: return convert(next(t for t in args if t is not type(None)), value)
        if origin is tuple: return tuple(convert(args[0], v) for v in value)
        if kind is datetime:
            stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
            if stamp.tzinfo is None: raise ValueError('Persisted live timestamp must be timezone-aware')
            return stamp.astimezone(timezone.utc)
        if kind is date: return date.fromisoformat(value) if isinstance(value, str) else value
        if kind is Decimal: return Decimal(str(value))
        if kind is int: return int(value)
        if is_dataclass(kind): return decode_record(kind, value)
        return value
    hints = get_type_hints(cls)
    return cls(**{f.name: convert(hints[f.name], payload[f.name]) for f in fields(cls) if f.name in payload})


@dataclass
class CurrentMarketState:
    session: TradingSession | None = None
    instruments: dict[int, LiveInstrument] = field(default_factory=dict)
    finalized_minutes: dict[str, FinalizedMinute] = field(default_factory=dict)
    option_snapshots: dict[str, OptionMinuteSnapshot] = field(default_factory=dict)
    option_universes: dict[str, OptionUniverse] = field(default_factory=dict)
    futures_selections: dict[str, FuturesSelection] = field(default_factory=dict)
    live_issues: dict[str, LiveQualityIssue] = field(default_factory=dict)
    latest_at: datetime | None = None
    quality: str = 'DATA_NOT_READY'
    status: dict[str, Any] = field(default_factory=dict)

    def minutes(self, instrument_type):
        return sorted((v for v in self.finalized_minutes.values()
                       if self.instruments[v.instrument_id].instrument_type == instrument_type), key=lambda v:v.minute_at)


class LiveProductDataReader:
    def __init__(self, database_url=None):
        self.database_url = database_url if database_url is not None else os.getenv('DATABASE_URL','')

    @contextmanager
    def _connection(self):
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5) as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            db.execute("SET LOCAL statement_timeout = '10s'")
            yield db

    @staticmethod
    def _session(db, day):
        row = db.execute('SELECT * FROM trading_sessions WHERE session_date=%s',(day,)).fetchone()
        return TradingSession(day,row['market_open_utc'],row['market_close_utc'],row['status']) if row else None

    def session(self, day):
        with self._connection() as db: return self._session(db, day)

    def latest_instrument_snapshot(self, day):
        from .market_structure_context import ConstituentUniverseService
        symbols = list(ConstituentUniverseService().symbols(day, decision_at=datetime.now(timezone.utc)))
        with self._connection() as db:
            snapshot=db.execute('''SELECT id,source_sha256 FROM instrument_snapshots
                WHERE (COALESCE(last_verified_at,captured_at) AT TIME ZONE 'Asia/Kolkata')::date=%s
                ORDER BY COALESCE(last_verified_at,captured_at) DESC,id DESC LIMIT 1''',(day,)).fetchone()
            if not snapshot: return []
            rows=db.execute('''SELECT * FROM canonical_instruments WHERE snapshot_id=%s AND
                ((exchange='NSE' AND (tradingsymbol IN ('NIFTY BANK','BANKNIFTY') OR (instrument_type='EQ' AND tradingsymbol=ANY(%s)))) OR
                 (exchange='NFO' AND (name='BANKNIFTY' OR tradingsymbol LIKE 'BANKNIFTY%%')
                  AND instrument_type IN ('FUT','CE','PE') AND expiry>=%s)) ORDER BY id''',(snapshot['id'],symbols,day)).fetchall()
        result=[LiveInstrument(r['id'],r['instrument_token'],str(snapshot['source_sha256']).strip(),r['tradingsymbol'],r['exchange'],
            'INDEX' if r['exchange']=='NSE' and r['tradingsymbol'] in ('NIFTY BANK','BANKNIFTY') else r['instrument_type'],r['expiry'],r['strike']) for r in rows]
        if sum(r.instrument_type=='INDEX' for r in result)!=1: return []
        if not any(r.instrument_type=='FUT' for r in result): return []
        if not all(any(r.instrument_type==kind and r.strike is not None for r in result) for kind in ('CE','PE')): return []
        return result

    @staticmethod
    def _minutes(db, instrument_type, day, now, instrument_id=None):
        # Rank before PASS filtering: never fall back to an older PASS revision.
        rows = db.execute('''SELECT DISTINCT ON (m.instrument_id,m.minute_at) m.payload
            FROM live_minutes_1m m JOIN canonical_instruments i ON i.id=m.instrument_id
            WHERE (m.minute_at AT TIME ZONE 'Asia/Kolkata')::date=%s
              AND m.minute_at + interval '1 minute' <= %s
              AND (m.payload->>'first_finalized_at')::timestamptz <= %s
              AND (%s::bigint IS NULL OR m.instrument_id=%s)
              AND ((%s='INDEX' AND i.exchange='NSE' AND i.tradingsymbol IN ('NIFTY BANK','BANKNIFTY'))
                OR (%s='FUT' AND i.instrument_type='FUT' AND i.name='BANKNIFTY'))
            ORDER BY m.instrument_id,m.minute_at,m.revision DESC''',
            (day,now,now,instrument_id,instrument_id,instrument_type,instrument_type)).fetchall()
        result = [decode_record(FinalizedMinute,r['payload']) for r in rows if r['payload']['quality_state']=='PASS']
        return sorted((r for r in result if r.low>0 and r.low<=min(r.open,r.close)<=max(r.open,r.close)<=r.high),key=lambda r:(r.minute_at,r.instrument_id))

    def current_session_minutes(self, instrument_type, session_date, limit=None, *, now=None, instrument_id=None):
        with self._connection() as db:
            result = self._minutes(db,instrument_type,session_date,now or datetime.now(timezone.utc),instrument_id)
        return result[-limit:] if limit and limit>0 else result

    def current_state(self, now=None):
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None: raise ValueError('now must be timezone-aware')
        day = now.astimezone(IST).date()
        state = CurrentMarketState()
        if not self.database_url: return state
        with self._connection() as db:
            state.session = self._session(db,day)
            index = self._minutes(db,'INDEX',day,now)
            # Snapshot identities can change. Use one current identity, never concatenate instruments.
            if index:
                identity = max(index,key=lambda r:(r.minute_at,r.instrument_id)).instrument_id
                index = [r for r in index if r.instrument_id==identity]
                state.latest_at = index[-1].minute_at
            boundary = state.latest_at + timedelta(minutes=1) if state.latest_at else now
            selection = db.execute('''SELECT s.payload FROM live_futures_selections s
                JOIN canonical_instruments i ON i.id=s.instrument_id
                WHERE s.selected_at<=%s AND i.name='BANKNIFTY' AND i.expiry>=%s
                ORDER BY s.selected_at DESC,s.selection_id DESC LIMIT 1''',(boundary,day)).fetchone()
            future = []
            if selection:
                value = decode_record(FuturesSelection,selection['payload'])
                state.futures_selections[value.selection_id]=value
                future = self._minutes(db,'FUT',day,now,value.instrument_id)
                future = [r for r in future if state.latest_at and r.minute_at<=state.latest_at]
            universe_row = db.execute('''SELECT u.payload FROM live_option_universes u
                WHERE u.selected_at<=%s AND u.expiry>=%s
                  AND EXISTS (SELECT 1 FROM canonical_instruments i WHERE i.name='BANKNIFTY'
                    AND u.payload->'instrument_ids' @> jsonb_build_array(i.id))
                ORDER BY u.selected_at DESC,u.universe_id DESC LIMIT 1''',(boundary,day)).fetchone()
            if universe_row:
                universe = decode_record(OptionUniverse,universe_row['payload'])
                state.option_universes[universe.universe_id]=universe
                if universe.quality_state=='PASS' and state.latest_at:
                    snapshots = db.execute('''SELECT s.payload FROM live_option_snapshots_1m s
                        JOIN canonical_instruments i ON i.id=s.instrument_id
                        WHERE s.universe_id=%s AND i.name='BANKNIFTY'
                          AND s.minute_at<=%s AND (s.minute_at AT TIME ZONE 'Asia/Kolkata')::date=%s
                        ORDER BY s.minute_at,s.instrument_id''',(universe.universe_id,state.latest_at,day)).fetchall()
                    for row in snapshots:
                        value = decode_record(OptionMinuteSnapshot,row['payload'])
                        if value.quality_state=='PASS' and value.instrument_id in universe.instrument_ids:
                            state.option_snapshots[value.snapshot_id]=value
            state.finalized_minutes = {r.minute_id:r for r in [*index,*future]}
            ids = {r.instrument_id for r in [*index,*future]} | {r.instrument_id for r in state.option_snapshots.values()}
            if selection: ids.add(selection['payload']['instrument_id'])
            if ids:
                for row in db.execute('SELECT * FROM canonical_instruments WHERE id=ANY(%s)',(list(ids),)).fetchall():
                    kind = 'INDEX' if row['exchange']=='NSE' and row['tradingsymbol'] in ('NIFTY BANK','BANKNIFTY') else row['instrument_type']
                    state.instruments[row['id']]=LiveInstrument(row['id'],row['instrument_token'],str(row['snapshot_id']),row['tradingsymbol'],row['exchange'],kind,row['expiry'],row['strike'])
            issues=db.execute('''SELECT payload FROM live_quality_issues
                WHERE (detected_at AT TIME ZONE 'Asia/Kolkata')::date=%s ORDER BY detected_at DESC LIMIT 100''',(day,)).fetchall()
            for row in issues:
                value=decode_record(LiveQualityIssue,row['payload'])
                if value.instrument_id is None or value.instrument_id in ids: state.live_issues[value.issue_id]=value
            connection=db.execute('SELECT payload FROM live_connection_events WHERE occurred_at<=%s ORDER BY occurred_at DESC,event_key DESC LIMIT 1',(now,)).fetchone()
            gaps=db.execute('SELECT payload FROM live_gaps WHERE started_at<=%s AND (resolved_at IS NULL OR resolved_at>=%s)',(now,datetime.combine(day,datetime.min.time(),IST))).fetchall()
            active=[r['payload'] for r in gaps if r['payload']['state']!='RESOLVED']
            payload=connection['payload'] if connection else {}
            fresh=bool(state.latest_at and now-state.latest_at<=timedelta(seconds=150))
            latest_recorded = db.execute('''SELECT max(m.minute_at) AS minute_at FROM live_minutes_1m m
                JOIN canonical_instruments i ON i.id=m.instrument_id
                WHERE i.exchange='NSE' AND i.tradingsymbol IN ('NIFTY BANK','BANKNIFTY')
                  AND (m.minute_at AT TIME ZONE 'Asia/Kolkata')::date=%s
                  AND m.minute_at+interval '1 minute'<=%s''',(day,now)).fetchone()['minute_at']
            open_session=bool(state.session and state.session.status in ('OPEN','SPECIAL') and state.session.open_at<=now<state.session.close_at)
            state.quality='PASS' if fresh and open_session else 'STALE_MARKET_DATA' if index else 'DATA_NOT_READY'
            if not state.session or state.session.status not in ('OPEN','SPECIAL'): state.quality='SESSION_UNAVAILABLE'
            if latest_recorded and latest_recorded!=state.latest_at: state.quality='LATEST_MINUTE_QUALITY_FAILED'
            relevant_gaps=[g for g in active if not g.get('affected_instruments') or ids.intersection(g['affected_instruments'])]
            if relevant_gaps: state.quality='LIVE_GAP'
            market_closed = bool(state.session and state.session.status in ('OPEN', 'SPECIAL', 'CLOSED') and not open_session)
            if market_closed: state.quality='MARKET_CLOSED'
            state.status={'connected':payload.get('state')=='CONNECTED' and fresh,'state':payload.get('state','STOPPED') if fresh else 'STALE' if connection else 'STOPPED',
                'collectorMode':'LIVE','credentialsConfigured':bool(os.getenv('ZERODHA_ACCESS_TOKEN') and os.getenv('ZERODHA_API_KEY')),
                'message':None if state.quality=='PASS' else state.quality,'lastTick':payload.get('last_tick_at'),
                'lastCompletedCandle':state.latest_at.isoformat() if state.latest_at else None,
                'receivedTickCount':None,'reconnectCount':payload.get('reconnect_attempt',0),
                'gapCount':len(gaps),'activeGapCount':len(relevant_gaps),'desiredSubscriptions':[],'activeSubscriptions':[],
                'startedAt':payload.get('connected_at'),'stoppedAt':payload.get('disconnected_at'),
                'recentQualityWarning':next(iter(state.live_issues.values())).code if state.live_issues else None}
            if market_closed:
                state.status.update(connected=False, state='MARKET_CLOSED')
        return state
