"""Source-backed calendar materialization; no exchange/network calls."""
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path

from .calendar import IST, UTC
from .paths import memory_root


class TradingCalendarBootstrapService:
    def __init__(self, database_url=None, root=None):
        self.database_url = database_url
        self.root = Path(root) if root is not None else memory_root()

    def configuration(self):
        value = json.loads((self.root / 'config/nse_trading_calendar.json').read_text(encoding='utf-8'))
        if (value['exchange'] != 'NSE' or value['timezone'] != 'Asia/Kolkata'
                or value['source']['segment'] != 'FUTURES_AND_OPTIONS'
                or not value['source'].get('url') or not value['source'].get('verifiedAt')):
            raise ValueError('CALENDAR_SOURCE_INVALID')
        return value

    def sessions(self, config):
        year = int(config['calendarYear'])
        holidays = {date.fromisoformat(day) for day in config['holidays']}
        specials = {date.fromisoformat(item['date']): item for item in config['specialSessions']}
        if len(specials) != len(config['specialSessions']) or any(d.year != year for d in holidays | specials.keys()):
            raise ValueError('CALENDAR_EXCEPTIONS_INVALID')
        rows = {}
        day = date(year, 1, 1)
        while day.year == year:
            special = specials.get(day)
            status = 'CLOSED' if day.weekday() >= 5 or day in holidays else 'OPEN'
            hours = config['regularSession']
            if special:
                status = 'SPECIAL' if special.get('resolved') is True else 'UNKNOWN'
                hours = special
            if status == 'UNKNOWN':
                rows[day] = (None, None, status)
            else:
                opened = datetime.combine(day, time.fromisoformat(hours['open']), IST).astimezone(UTC)
                closed = datetime.combine(day, time.fromisoformat(hours['close']), IST).astimezone(UTC)
                if closed <= opened:
                    raise ValueError('CALENDAR_HOURS_INVALID')
                rows[day] = (opened, closed, status)
            day += timedelta(days=1)
        return rows

    def memory_conflicts(self, rows):
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        return [item['date'] for item in manifest['files']
                if rows.get(date.fromisoformat(item['date']), (None, None, 'UNKNOWN'))[2] not in ('OPEN', 'SPECIAL')]

    def bootstrap(self):
        config = self.configuration()
        rows = self.sessions(config)
        conflicts = self.memory_conflicts(rows)
        if conflicts:
            raise ValueError('CALENDAR_MEMORY_CONFLICT: ' + ', '.join(conflicts))
        import psycopg
        with psycopg.connect(self.database_url, connect_timeout=5) as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtext('verified-nse-calendar'))")
            for day, (opened, closed, status) in rows.items():
                db.execute('''INSERT INTO trading_sessions(session_date,market_open_utc,market_close_utc,status,source)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT (session_date) DO NOTHING''',
                    (day, opened, closed, status, config['source']['url']))
                stored = db.execute('SELECT market_open_utc,market_close_utc,status FROM trading_sessions WHERE session_date=%s', (day,)).fetchone()
                if tuple(stored) != (opened, closed, status):
                    raise ValueError(f'CALENDAR_STORED_CONFLICT: {day}; explicit calendar correction required')
        return len(rows)

    def status(self, today=None):
        today = today or datetime.now(IST).date()
        result = {'currentDate': today.isoformat(), 'calendarConfigured': False,
                  'calendarValidThrough': None, 'todayStatus': 'UNKNOWN', 'calendarSource': None,
                  'calendarConflict': [], 'status': 'CURRENT_TRADING_CALENDAR_NOT_CONFIGURED'}
        try:
            config = self.configuration()
            rows = self.sessions(config)
            conflicts = self.memory_conflicts(rows)
            result.update(calendarValidThrough=f"{config['calendarYear']}-12-31", calendarSource=config['source'])
            if conflicts:
                result.update(status='CALENDAR_MEMORY_CONFLICT', calendarConflict=conflicts)
                return result
            if today not in rows:
                result['status'] = 'CALENDAR_OUT_OF_RANGE'
                return result
            if not self.database_url:
                return result
            import psycopg
            with psycopg.connect(self.database_url, connect_timeout=2, options='-c statement_timeout=2000') as db:
                db.execute('SET TRANSACTION READ ONLY')
                stored = dict((day, (opened, closed, status)) for day, opened, closed, status in db.execute(
                    'SELECT session_date,market_open_utc,market_close_utc,status FROM trading_sessions WHERE session_date BETWEEN %s AND %s',
                    (min(rows), max(rows))).fetchall())
            differences = [day.isoformat() for day, row in rows.items() if stored.get(day) != row]
            stored_conflicts = self.memory_conflicts(stored)
            if stored_conflicts:
                result.update(status='CALENDAR_MEMORY_CONFLICT', calendarConflict=stored_conflicts)
                return result
            if differences:
                result.update(status='CALENDAR_STORED_CONFLICT', calendarConflict=differences)
                return result
            result.update(calendarConfigured=True, todayStatus=stored[today][2],
                          status='READY' if stored[today][2] != 'UNKNOWN' else 'CURRENT_TRADING_CALENDAR_NOT_CONFIGURED')
        except (OSError, ValueError, KeyError, TypeError):
            result['status'] = 'CALENDAR_CONFIGURATION_INVALID'
        except Exception:
            result['status'] = 'CALENDAR_DATABASE_UNAVAILABLE'
        return result
