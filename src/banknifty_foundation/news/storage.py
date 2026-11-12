"""Runtime repository selection and PostgreSQL read/write parity."""
from dataclasses import replace
from datetime import datetime, timezone
import os
from typing import Protocol, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import MarketNewsEvent, NewsArticle, SourceHealth
from .repository import FileNewsRepository, PostgresNewsRepository as Writer


class NewsRepository(Protocol):
    def articles(self) -> list[NewsArticle]: ...
    def events(self, *, include_historical: bool = True) -> list[MarketNewsEvent]: ...
    def event(self, event_id: str) -> MarketNewsEvent | None: ...
    def health(self) -> list[dict]: ...
    def save_refresh(self, articles, events, health) -> dict: ...
    def save_reaction(self, event_id: str, reaction: dict) -> None: ...
    def pending_reactions(self, limit: int = 100) -> list[MarketNewsEvent]: ...
    def runtime_health(self) -> dict: ...
    def save_runtime_health(self, value: dict) -> None: ...


class PostgresNewsRepository(Writer):
    def events_as_of(self, decision_at):
        """Exclude later unversioned edits instead of inventing their previous state."""
        with self._connect() as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            rows=db.execute('''SELECT e.*,r.measured_at AS reaction_measured_at,
                CASE WHEN r.measured_at<=%s THEN r.reaction_payload ELSE NULL END AS safe_reaction
                FROM market_news_events e LEFT JOIN market_event_reactions r USING(event_id)
                WHERE e.created_at<=%s AND e.evidence_payload_seen_at<=%s AND e.event_payload<>'{}'::jsonb
                  AND (e.event_payload->>'first_seen_at')::timestamptz<=%s
                ORDER BY (e.event_payload->>'first_seen_at')::timestamptz DESC,event_id''',
                (decision_at,decision_at,decision_at,decision_at)).fetchall()
        result=[]
        for row in rows:
            event=MarketNewsEvent.from_dict(row['event_payload'])
            result.append(replace(event,
                source_metadata={**event.source_metadata,'researchSnapshot':'PAYLOAD_KNOWN_AT_DECISION',
                                 'payloadKnownAt':row['evidence_payload_seen_at'].isoformat(),
                                 'storedAt':row['created_at'].isoformat(),
                                 'reactionMeasuredAt':row['reaction_measured_at'].isoformat()
                                    if row['safe_reaction'] and row['reaction_measured_at'] else None},
                observed_banknifty_response='NOT_USED_FOR_PROSPECTIVE_CONTEXT',
                reaction=row['safe_reaction'] or {}))
        return result

    def _connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)

    def articles(self):
        with self._connect() as db:
            rows = db.execute('SELECT * FROM market_news_articles ORDER BY first_seen_at DESC LIMIT 2000').fetchall()
        result = []
        for row in rows:
            value = row['article_payload'] or {**row, 'published_at': row['published_at'].isoformat() if row['published_at'] else None,
                'first_seen_at': row['first_seen_at'].isoformat(), 'ingested_at': row['ingested_at'].isoformat()}
            result.append(NewsArticle.from_dict(value))
        return result

    def events(self, *, include_historical=True):
        with self._connect() as db:
            rows = db.execute('SELECT e.event_payload, r.reaction_payload FROM market_news_events e '
                'LEFT JOIN market_event_reactions r USING(event_id) WHERE e.event_payload <> \'{}\'::jsonb '
                'ORDER BY first_known_at DESC NULLS LAST LIMIT 5000').fetchall()
        events = [replace(MarketNewsEvent.from_dict(row['event_payload']), reaction=row['reaction_payload'] or {}) for row in rows]
        return events if include_historical else [item for item in events if item.source_quality == 'LIVE_SOURCE']

    def event(self, event_id):
        with self._connect() as db:
            row = db.execute('SELECT e.event_payload, r.reaction_payload FROM market_news_events e '
                'LEFT JOIN market_event_reactions r USING(event_id) WHERE event_id=%s', (event_id,)).fetchone()
        return replace(MarketNewsEvent.from_dict(row['event_payload']), reaction=row['reaction_payload'] or {}) if row and row['event_payload'] else None

    def pending_reactions(self, limit=100):
        with self._connect() as db:
            rows = db.execute("SELECT e.event_payload FROM market_news_events e LEFT JOIN market_event_reactions r USING(event_id) "
                "WHERE e.predictive_eligible AND e.event_payload->>'source_quality'='LIVE_SOURCE' "
                "AND COALESCE(r.reaction_quality,'PENDING') NOT IN ('COMPLETE_30M','UNMEASURABLE') "
                "ORDER BY first_known_at LIMIT %s", (limit,)).fetchall()
        return [MarketNewsEvent.from_dict(row['event_payload']) for row in rows]

    def save_refresh(self, articles, events, health):
        articles, events, health = list(articles), list(events), list(health)
        # The coordinator serializes refresh/backfill; timestamps belong to the first persisted event.
        preserved = []
        for item in events:
            previous = self.event(item.event_id)
            preserved.append(replace(item, known_at=previous.known_at, first_seen_at=previous.first_seen_at,
                published_at=previous.published_at, reaction=previous.reaction) if previous else item)
        counts = super().save(articles, preserved)
        with self._connect() as db:
            for item in articles:
                db.execute("UPDATE market_news_articles SET article_payload=%s WHERE article_id=%s AND article_payload='{}'::jsonb",
                           (Jsonb(item.to_dict()), item.article_id))
            for item in preserved:
                db.execute('UPDATE market_news_events SET event_payload=%s WHERE event_id=%s', (Jsonb(item.to_dict()), item.event_id))
            for item in health:
                db.execute('''INSERT INTO market_news_source_health VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(source) DO UPDATE SET state=EXCLUDED.state,last_attempt_at=EXCLUDED.last_attempt_at,
                    last_success_at=COALESCE(EXCLUDED.last_success_at,market_news_source_health.last_success_at),
                    last_error=EXCLUDED.last_error,latest_article_count=EXCLUDED.latest_article_count,
                    consecutive_failures=CASE WHEN EXCLUDED.state='UP' THEN 0 ELSE market_news_source_health.consecutive_failures+1 END''',
                    (item.source,item.state,item.checked_at,item.checked_at if item.state=='UP' else None,
                     item.message,0 if item.state=='UP' else 1,item.article_count))
        for item in preserved:
            if item.reaction: self.save_reaction(item.event_id, item.reaction)
        return counts

    def save_reaction(self, event_id, reaction):
        with self._connect() as db:
            db.execute('''INSERT INTO market_event_reactions(event_id,reaction_quality,reaction_payload)
                VALUES (%s,%s,%s) ON CONFLICT(event_id) DO UPDATE SET reaction_quality=EXCLUDED.reaction_quality,
                reaction_payload=EXCLUDED.reaction_payload,measured_at=now()''',
                (event_id,reaction['status'],Jsonb(reaction)))

    def health(self):
        with self._connect() as db: rows=db.execute('SELECT * FROM market_news_source_health ORDER BY source').fetchall()
        return [{**{key:value.isoformat() if isinstance(value,datetime) else value for key,value in row.items()},
                 'checked_at':row['last_attempt_at'].isoformat(),'article_count':row['latest_article_count'],
                 'message':row['last_error']} for row in rows]

    def runtime_health(self):
        with self._connect() as db: row=db.execute("SELECT payload FROM market_runtime_health WHERE component='news'").fetchone()
        return row['payload'] if row else {'state':'STOPPED','consecutiveFailures':0}

    def save_runtime_health(self,value):
        with self._connect() as db:
            db.execute("INSERT INTO market_runtime_health VALUES ('news',%s,now()) ON CONFLICT(component) DO UPDATE SET payload=EXCLUDED.payload,updated_at=now()", (Jsonb(value),))


def news_repository() -> NewsRepository:
    url=os.getenv('DATABASE_URL')
    return PostgresNewsRepository(url) if url else FileNewsRepository()
