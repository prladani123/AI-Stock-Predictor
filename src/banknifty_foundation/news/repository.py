from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable

from .models import MarketNewsEvent, NewsArticle, SourceHealth
from ..paths import memory_root


class FileNewsRepository:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else memory_root() / "derived"
        self.root.mkdir(parents=True, exist_ok=True)

    def _read(self, name: str) -> list[dict]:
        path = self.root / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def _write(self, name: str, payload: list[dict]) -> None:
        path = self.root / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
        temporary.replace(path)

    def articles(self) -> list[NewsArticle]:
        return sorted((NewsArticle.from_dict(item) for item in self._read("live_news.json")), key=lambda item: item.first_seen_at, reverse=True)

    def events(self, *, include_historical: bool = True) -> list[MarketNewsEvent]:
        values = self._read("live_events.json")
        if include_historical:
            values += self._read("structured_events.json")
        by_id = {item["event_id"]: item for item in values}
        return sorted((MarketNewsEvent.from_dict(item) for item in by_id.values()),
                      key=lambda item: item.first_seen_at or item.known_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    def health(self) -> list[dict]:
        return self._read("news_source_health.json")

    def event(self, event_id: str):
        return next((item for item in self.events() if item.event_id == event_id), None)

    def pending_reactions(self, limit: int = 100):
        return [item for item in self.events(include_historical=False) if item.predictive_eligible
                and item.reaction.get('status') not in {'COMPLETE_30M', 'UNMEASURABLE'}][:limit]

    def save_reaction(self, event_id: str, reaction: dict) -> None:
        events = self.events(include_historical=False)
        self._write('live_events.json', [replace(item, reaction=reaction).to_dict()
                    if item.event_id == event_id else item.to_dict() for item in events])

    def runtime_health(self) -> dict:
        values = self._read('runtime_health.json')
        return values[0] if values else {'state': 'STOPPED', 'consecutiveFailures': 0}

    def save_runtime_health(self, value: dict) -> None:
        self._write('runtime_health.json', [value])

    def save_refresh(self, articles: Iterable[NewsArticle], events: Iterable[MarketNewsEvent],
                     health: Iterable[SourceHealth]) -> dict[str, int]:
        article_map = {item.article_id: item for item in self.articles()}
        for item in articles:
            previous = article_map.get(item.article_id)
            article_map[item.article_id] = previous if previous and previous.first_seen_at <= item.first_seen_at else item
        event_map = {item.event_id: item for item in self.events(include_historical=False)}
        for item in events:
            previous = event_map.get(item.event_id)
            if previous:
                first_seen = min(value for value in (previous.first_seen_at, item.first_seen_at) if value is not None)
                known = min(value for value in (previous.known_at, item.known_at) if value is not None)
                item = replace(item, first_seen_at=first_seen, known_at=known,
                    published_at=previous.published_at or item.published_at,
                    reaction=previous.reaction or item.reaction)
            event_map[item.event_id] = item
        self._write("live_news.json", [item.to_dict() for item in article_map.values()])
        self._write("live_events.json", [item.to_dict() for item in event_map.values()])
        previous_health = {item['source']:item for item in self.health()}
        self._write("news_source_health.json", [{**item.to_dict(),
            'last_success_at':item.checked_at.isoformat() if item.state=='UP' else previous_health.get(item.source,{}).get('last_success_at'),
            'last_attempt_at':item.checked_at.isoformat(), 'last_error':item.message}
            for item in health])
        return {"articles": len(article_map), "events": len(event_map)}


class PostgresNewsRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def save(self, articles: Iterable[NewsArticle], events: Iterable[MarketNewsEvent]) -> dict[str, int]:
        import psycopg
        from psycopg.types.json import Jsonb
        counts = {"articles": 0, "events": 0}
        with psycopg.connect(self.database_url) as connection:
            for item in articles:
                connection.execute("""
                    INSERT INTO market_news_articles
                    (article_id, content_hash, headline, source, url, published_at, first_seen_at,
                     ingested_at, affected_symbols, category, tags, relevance, relevance_score, raw_metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (content_hash) DO UPDATE SET
                      first_seen_at=LEAST(market_news_articles.first_seen_at, EXCLUDED.first_seen_at),
                      raw_metadata=market_news_articles.raw_metadata || EXCLUDED.raw_metadata
                """, (item.article_id, item.content_hash, item.headline, item.source, item.url,
                      item.published_at, item.first_seen_at, item.ingested_at,
                      Jsonb(list(item.affected_symbols)), item.category, Jsonb(list(item.tags)),
                      item.relevance, item.relevance_score, Jsonb(item.raw_metadata)))
                counts["articles"] += 1
            for item in events:
                connection.execute("""
                    INSERT INTO market_news_events
                    (event_id, category, summary, session_date, first_known_at, published_at,
                     knowledge_phase, predictive_eligible, context_only, evidence_grade,
                     timing_precision, directional_context, relevance, tags, special_flags, source_metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (event_id) DO UPDATE SET
                      first_known_at=LEAST(market_news_events.first_known_at, EXCLUDED.first_known_at),
                      source_metadata=market_news_events.source_metadata || EXCLUDED.source_metadata
                """, (item.event_id, item.event_category, item.headline_or_summary,
                      item.session_date, item.known_at or item.first_seen_at, item.published_at,
                      item.knowledge_phase, item.predictive_eligible, item.context_only,
                      item.causal_evidence_grade, item.timing_precision, item.directional_context,
                      item.relevance, Jsonb(list(item.event_tags)), Jsonb(list(item.special_flags)),
                      Jsonb(item.source_metadata)))
                for article_id in item.source_metadata.get("article_ids", []):
                    connection.execute("""
                        INSERT INTO market_event_sources(event_id, article_id) VALUES (%s,%s)
                        ON CONFLICT DO NOTHING
                    """, (item.event_id, article_id))
                if item.reaction:
                    returns = item.reaction.get("returns") or {}
                    connection.execute("""
                        INSERT INTO market_event_reactions
                        (event_id, reference_at, reference_price, return_5m, return_10m,
                         return_20m, return_30m, max_up_excursion, max_down_excursion,
                         volatility_expansion, big_move_outcomes, reaction_quality)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (event_id) DO UPDATE SET
                          reference_at=EXCLUDED.reference_at, reference_price=EXCLUDED.reference_price,
                          return_5m=EXCLUDED.return_5m, return_10m=EXCLUDED.return_10m,
                          return_20m=EXCLUDED.return_20m, return_30m=EXCLUDED.return_30m,
                          max_up_excursion=EXCLUDED.max_up_excursion,
                          max_down_excursion=EXCLUDED.max_down_excursion,
                          volatility_expansion=EXCLUDED.volatility_expansion,
                          big_move_outcomes=EXCLUDED.big_move_outcomes,
                          reaction_quality=EXCLUDED.reaction_quality, measured_at=now()
                    """, (item.event_id, item.reaction.get("reference_at"),
                          item.reaction.get("reference_price"), returns.get("5"), returns.get("10"),
                          returns.get("20"), returns.get("30"), item.reaction.get("max_up_excursion"),
                          item.reaction.get("max_down_excursion"), item.reaction.get("volatility_expansion"),
                          Jsonb(item.reaction.get("big_move") or {}), item.reaction.get("status", "PENDING")))
                counts["events"] += 1
        return counts
