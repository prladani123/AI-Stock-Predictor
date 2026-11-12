"""Immutable, provider-neutral evidence for completed live index sessions."""
from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path

from .calendar import IST, UTC


def checksum(payload):
    return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validated_rows(payload):
    if payload.get('source') != 'LIVE_POSTGRES' or payload.get('quality') != {'coverage': 'COMPLETE', 'status': 'PASS'}:
        raise ValueError('Archive is not complete PASS live evidence')
    body = {key: value for key, value in payload.items() if key != 'checksum_sha256'}
    if checksum(body) != payload.get('checksum_sha256'):
        raise ValueError('Live archive checksum mismatch')
    session = payload['session']
    opened, closed = (datetime.fromisoformat(session[key]) for key in ('open', 'close'))
    if session['status'] not in ('OPEN', 'SPECIAL') or opened.tzinfo is None or closed.tzinfo is None or closed <= opened:
        raise ValueError('Invalid archive session')
    if (opened.astimezone(IST).date().isoformat() != payload['session_date']
            or closed.astimezone(IST).date().isoformat() != payload['session_date']
            or opened.second or opened.microsecond or closed.second or closed.microsecond):
        raise ValueError('Archive session date/minute bounds mismatch')
    rows = [{**row, 'at': datetime.fromisoformat(row['at'])} for row in payload['rows']]
    expected = []
    at = opened
    while at < closed:
        expected.append(at)
        at += timedelta(minutes=1)
    if [row['at'] for row in rows] != expected or len({row['instrument_id'] for row in rows}) != 1:
        raise ValueError('Incomplete, duplicate, out-of-order or mixed-identity archive')
    for row in rows:
        values = [float(row[key]) for key in ('open', 'high', 'low', 'close')]
        opening, high, low, closing = values
        if (row.get('quality_state') != 'PASS' or row['at'].tzinfo is None
                or not all(math.isfinite(value) and value > 0 for value in values)
                or low > min(opening, closing) or high < max(opening, closing) or low > high
                or any(row.get(key) is not None and (not math.isfinite(float(row[key])) or float(row[key]) < 0)
                       for key in ('volume', 'oi_close'))):
            raise ValueError('Invalid live archive candle')
        row.update(session_open=opened, session_close=closed, session_status=session['status'])
    return rows


class LiveSessionArchiveService:
    def __init__(self, root):
        self.root = Path(root)

    def archive(self, day, session, minutes):
        if session is None or datetime.now(UTC) < session[1]:
            raise ValueError('Only completed explicit sessions can be archived')
        records = []
        for row in minutes:
            records.append({**{key: row.get(key) for key in
                ('instrument_id', 'revision', 'open', 'high', 'low', 'close', 'volume', 'oi_close', 'quality_state', 'first_finalized_at')},
                'at': row['at'].astimezone(UTC).isoformat()})
        body = {'schema_version': 1, 'session_date': day.isoformat(), 'source': 'LIVE_POSTGRES',
                'quality': {'coverage': 'COMPLETE', 'status': 'PASS'},
                'session': {'open': session[0].astimezone(UTC).isoformat(),
                            'close': session[1].astimezone(UTC).isoformat(), 'status': session[2]},
                'rows': records}
        payload = {**body, 'checksum_sha256': checksum(body)}
        validated_rows(payload)
        target = self.root / 'raw/index_history' / f"live_{day}_{payload['checksum_sha256']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open('x', encoding='utf-8', newline='\n') as output:
                output.write(json.dumps(payload, indent=2, allow_nan=False) + '\n')
        except FileExistsError:
            if json.loads(target.read_text(encoding='utf-8')) != payload:
                raise ValueError('Existing live archive differs from its content address') from None
        return target


def load_live_archives(root):
    """Do not choose arbitrarily between conflicting immutable session revisions."""
    result = {}
    conflicts = set()
    for path in sorted((Path(root) / 'raw/index_history').glob('live_*.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            rows = validated_rows(payload)
            day = payload['session_date']
            if day in result and result[day] != rows:
                conflicts.add(day)
            result[day] = rows
        except (ValueError, KeyError, TypeError, OSError):
            continue  # Corrupt/partial evidence is never learning input.
    return {day: rows for day, rows in result.items() if day not in conflicts}


def previous_close(root, day, market, raw_rows):
    """Never jump over a missing/UNKNOWN calendar date or missing preceding session."""
    from .market_memory import FileMarketMemoryRepository
    previous = day - timedelta(days=1)
    for _ in range(370):
        session = market.calendar_session(previous)
        if not session or session[2] == 'UNKNOWN':
            return None
        if session[2] in ('OPEN', 'SPECIAL'):
            break
        previous -= timedelta(days=1)
    else:
        return None
    for memory in FileMarketMemoryRepository(root).days():
        if memory.trading_date == previous and memory.data_quality.startswith('VERIFIED') and memory.close is not None and math.isfinite(memory.close) and memory.close > 0:
            return memory.close
    archived = load_live_archives(root).get(previous.isoformat())
    if archived:
        return float(archived[-1]['close'])
    matches = [row for row in raw_rows if row['at'].date() == previous]
    if len(matches) == 1:
        row = matches[0]
        values = [float(row[key]) for key in ('open', 'high', 'low', 'close')]
        if all(math.isfinite(value) and value > 0 for value in values) and row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']:
            return float(row['close'])
    return None
