from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable

from .classification import classify
from .models import NewsArticle, SourceHealth, normalized_headline
from .sources import NewsSource


class NewsAggregator:
    def __init__(self, sources: Iterable[NewsSource]) -> None:
        self.sources = list(sources)

    async def refresh(self, first_seen_at: datetime | None = None) -> tuple[list[NewsArticle], list[SourceHealth]]:
        observed = first_seen_at or datetime.now(timezone.utc)
        results = await asyncio.gather(*(self._safe_fetch(source, observed) for source in self.sources))
        articles: list[NewsArticle] = []
        health: list[SourceHealth] = []
        for source, (items, error) in zip(self.sources, results):
            health.append(SourceHealth(source.name, "UP" if error is None else "DOWN", observed,
                                       len(items), error))
            articles.extend(items)
        classified = []
        for item in articles:
            values = classify(item.headline, item.raw_metadata)
            classified.append(replace(item, **values))
        return self.deduplicate(classified), health

    async def _safe_fetch(self, source: NewsSource, observed: datetime) -> tuple[list[NewsArticle], str | None]:
        try:
            items=await asyncio.wait_for(source.fetch(observed), timeout=25)
            received=datetime.now(timezone.utc)
            return [replace(item,first_seen_at=received,ingested_at=received) for item in items], None
        except Exception as exc:
            logging.getLogger(__name__).warning('news_source_failed source=%s error=%s',source.name,type(exc).__name__)
            return [], f"{type(exc).__name__}: {exc}"

    @staticmethod
    def deduplicate(articles: Iterable[NewsArticle]) -> list[NewsArticle]:
        seen: dict[tuple[str, str], NewsArticle] = {}
        for article in articles:
            publication_window = article.published_at.strftime("%Y-%m-%dT%H") if article.published_at else article.first_seen_at.strftime("%Y-%m-%dT%H")
            key = (article.content_hash, article.source)
            existing = seen.get(key)
            if existing is None or article.first_seen_at < existing.first_seen_at:
                seen[key] = article
        return sorted(seen.values(), key=lambda item: item.published_at or item.first_seen_at, reverse=True)
