"""Bounded, idempotent container startup; runtime workers belong to API lifespan."""
import json
import logging
import os
import time
from hashlib import sha256

import psycopg

from .paths import memory_root
from .repository import PostgresRepository
from .market_memory import FileMarketMemoryRepository, PostgresMarketMemoryRepository

log = logging.getLogger(__name__)


def prepare_memory(database_url):
    root = memory_root()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    curated = manifest['files']
    if len(curated) != 50:
        raise ValueError('Expected 50 canonical curated memories')
    for item in curated:
        path = root / item['file']
        if sha256(path.read_bytes()).hexdigest() != item['checksum_sha256']:
            raise ValueError(f'Curated checksum mismatch: {path.name}')
    inputs = sorted((root / 'daily').glob('*.md')) + sorted((root / 'raw' / 'index_history').glob('*.json'))
    fingerprint = sha256(''.join(f'{p.name}:{sha256(p.read_bytes()).hexdigest()}' for p in inputs).encode()).hexdigest()
    checkpoint = root / 'derived' / 'startup_sources.json'
    previous = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    stale = bool(previous) and previous.get('fingerprint') != fingerprint
    rebuilt = False
    from .memory_builder import build_episodes, build_events
    for filename, build in [('episodes.json', build_episodes), ('structured_events.json', build_events)]:
        if stale or not (root / 'derived' / filename).exists():
            build(root)
            rebuilt = True
    repo = FileMarketMemoryRepository(root)
    # Check DB presence too: the persistent volume may be new while files are unchanged.
    with psycopg.connect(database_url) as db:
        rows = db.execute('SELECT trading_date,checksum FROM market_memory_days').fetchall()
    stored = dict(rows)
    changed = [day for day in repo.days() if stored.get(day.trading_date) != day.checksum]
    sync_derived = stale or rebuilt or not previous or bool(changed)
    result = PostgresMarketMemoryRepository(database_url).ingest(changed, repo.events() if sync_derived else [], repo.episodes() if sync_derived else [])
    from .news.storage import PostgresNewsRepository
    news = PostgresNewsRepository(database_url)
    existing = {item.event_id for item in news.events()}
    events = [item for item in repo.events() if item.event_id not in existing or stale]
    if events:
        news.save_refresh([], events, [])
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint.with_suffix('.tmp')
    temporary.write_text(json.dumps({'fingerprint': fingerprint}), encoding='utf-8')
    temporary.replace(checkpoint)
    log.info('memory_startup %s', result)


def main():
    logging.basicConfig(level=logging.INFO)
    url = os.environ['DATABASE_URL']
    for attempt in range(30):
        try:
            with psycopg.connect(url, connect_timeout=2) as db:
                db.execute('SELECT 1')
            break
        except psycopg.OperationalError:
            if attempt == 29:
                raise RuntimeError('PostgreSQL unavailable after 30 startup attempts') from None
            time.sleep(2)
    log.info('migrations_applied %s', PostgresRepository(url).apply_migrations())
    from .calendar_bootstrap import TradingCalendarBootstrapService
    try:
        log.info('calendar_sessions_materialized %s', TradingCalendarBootstrapService(url).bootstrap())
    except Exception:
        log.error('Calendar bootstrap blocked; readiness and live worker will remain blocked')
    prepare_memory(url)
    import uvicorn
    uvicorn.run('banknifty_foundation.internal_api:app', host='0.0.0.0', port=8000, workers=1)


if __name__ == '__main__':
    main()
