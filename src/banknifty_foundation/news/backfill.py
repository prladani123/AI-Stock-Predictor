from datetime import datetime, timedelta, timezone
import logging
import os

from ..calendar import IST
from .reaction import NewsReactionService

log = logging.getLogger(__name__)


class StoredMarketData:
    """Read finalized, quality-passing live minutes. No network market-data lookups."""
    def __init__(self, database_url=None):
        self.database_url = database_url or os.getenv('DATABASE_URL')

    def minutes(self, day, instrument_type='INDEX', available_at=None):
        if not self.database_url: return []
        if instrument_type not in ('INDEX', 'FUT'):
            raise ValueError('Only logical BANKNIFTY INDEX or FUT minutes are supported')
        cutoff = available_at or datetime.now(timezone.utc)
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError('available_at must be timezone-aware')
        import psycopg
        with psycopg.connect(self.database_url, connect_timeout=5) as db:
            rows = db.execute('''SELECT DISTINCT ON (m.instrument_id,m.minute_at) m.payload FROM live_minutes_1m m
                JOIN canonical_instruments i ON i.id=m.instrument_id
                WHERE ((%s='INDEX' AND i.exchange='NSE' AND i.tradingsymbol IN ('NIFTY BANK','BANKNIFTY'))
                    OR (%s='FUT' AND i.instrument_type='FUT' AND i.name='BANKNIFTY'))
                AND (m.minute_at AT TIME ZONE 'Asia/Kolkata')::date=%s
                AND (m.payload->>'first_finalized_at')::timestamptz <= %s
                AND m.minute_at + interval '1 minute' <= %s
                ORDER BY m.instrument_id,m.minute_at,m.revision DESC''',
                (instrument_type,instrument_type,day,cutoff,cutoff)).fetchall()
        selected = {}
        for (item,) in rows:
            if item.get('quality_state') != 'PASS': continue
            at = datetime.fromisoformat(item['minute_at'])
            selected[(item['instrument_id'],at)] = {**item,'at':at,
                **{key:float(item[key]) for key in ('open','high','low','close')},
                'volume':item.get('volume_delta')}
        return sorted(selected.values(),key=lambda item:item['at'])

    def options(self,day):
        if not self.database_url:return []
        import psycopg
        with psycopg.connect(self.database_url,connect_timeout=5) as db:
            rows=db.execute("SELECT m.payload FROM live_option_snapshots_1m m JOIN canonical_instruments i ON i.id=m.instrument_id WHERE i.name='BANKNIFTY' AND (m.minute_at AT TIME ZONE 'Asia/Kolkata')::date=%s ORDER BY m.minute_at",(day,)).fetchall()
        return [row[0] for row in rows if row[0].get('quality_state')=='PASS']

    def session(self, day):
        row = self.calendar_session(day)
        return row if row and row[2] in ('OPEN','SPECIAL') else None

    def calendar_session(self, day):
        """Include explicit CLOSED/UNKNOWN rows for safe previous-session traversal."""
        if not self.database_url: return None
        import psycopg
        with psycopg.connect(self.database_url, connect_timeout=5) as db:
            row = db.execute('SELECT market_open_utc,market_close_utc,status FROM trading_sessions WHERE session_date=%s', (day,)).fetchone()
        return row

    def selected_future(self, day):
        if not self.database_url: return None
        import psycopg
        with psycopg.connect(self.database_url, connect_timeout=5) as db:
            row = db.execute('''SELECT s.payload,i.tradingsymbol FROM live_futures_selections s
                JOIN canonical_instruments i ON i.id=s.instrument_id WHERE i.name='BANKNIFTY'
                AND s.selected_at < (%s::date + interval '1 day') AT TIME ZONE 'Asia/Kolkata'
                AND i.expiry >= %s ORDER BY s.selected_at DESC LIMIT 1''',(day,day)).fetchone()
        return {**row[0], 'contract':row[1]} if row else None


class ReactionBackfillService:
    def __init__(self, repository, market=None):
        self.repository, self.market = repository, market or StoredMarketData()

    def update(self, now=None):
        now = now or datetime.now(timezone.utc)
        updated=0
        for event in self.repository.pending_reactions():
            from .impact_study import event_known_at, EventImpactStudyService
            known=event_known_at(event)
            session=self.market.calendar_session(event.session_date)
            if not session or session[2]!='OPEN': continue
            rows=self.market.minutes(event.session_date,available_at=now)
            rows=[r for r in rows if session[0]<=r['at'] and r['at']+timedelta(minutes=1)<=session[1]]
            reaction=NewsReactionService().progressive(known,rows,now)
            self.repository.save_reaction(event.event_id,reaction)
            try: EventImpactStudyService().complete(event,reaction)
            except Exception: log.warning('event_impact_completion_unavailable event=%s',event.event_id)
            updated+=1
        try:
            from .impact_study import EventImpactStudyService
            EventImpactStudyService().complete_pending()
        except Exception: log.warning('event_impact_retry_unavailable')
        log.info('reaction_backfill updated=%s',updated)
        return updated
