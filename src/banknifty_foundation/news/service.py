from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import os
import logging
import time
from threading import RLock
from threading import Event, Thread
from typing import Any

from ..calendar import IST
from ..market_memory import FileMarketMemoryRepository
from .aggregator import NewsAggregator
from .confirmation import market_confirmation
from .event_memory import EventMemoryEngine, event_from_article, monitoring_window
from .models import MarketNewsEvent, NewsArticle, normalized_headline
from .repository import FileNewsRepository
from .storage import news_repository, NewsRepository
from .backfill import ReactionBackfillService
from .sources import configured_sources


class NewsIntelligenceService:
    def __init__(self, repository: NewsRepository | None = None) -> None:
        self.repository = repository or news_repository()
        self.aggregator = NewsAggregator(configured_sources())
        self.lock = RLock()
        self.monitor = {'state':'STOPPED','lastAttempt':None,'lastSuccessfulRefresh':None,'lastError':None,'consecutiveFailures':0,
                        **self.repository.runtime_health()}
        self.monitor['state'] = 'STOPPED'

    def refresh(self) -> dict[str, Any]:
        with self.lock:
            return self._refresh()

    def _refresh(self) -> dict[str, Any]:
        observed = datetime.now(timezone.utc)
        articles, health = asyncio.run(self.aggregator.refresh(observed))
        relevant = [item for item in articles if item.relevance in {"HIGH", "MEDIUM"}]
        events = self._events(relevant, self.repository.events(include_historical=False))
        counts = self.repository.save_refresh(relevant, events, health)
        # Capture only freshly disseminated inputs. Later refreshes must not rewrite
        # an event's pre-state or infer its original features from updated articles.
        from .impact_study import EventImpactStudyService, event_known_at
        for event in events:
            known=event_known_at(event)
            if known and known>=observed:
                try:
                    prior=self.repository.events_as_of(known) if hasattr(self.repository,'events_as_of') else []
                    EventImpactStudyService().freeze(event,prior)
                except Exception:
                    logging.getLogger(__name__).warning('event_impact_input_capture_unavailable event=%s',event.event_id)
        database_status = type(self.repository).__name__
        return {**counts, "fetched": len(articles), "relevant": len(relevant),
                "sourceHealth": [item.to_dict() for item in health], "database": database_status,
                "refreshedAt": observed.isoformat()}

    @staticmethod
    def _events(articles: list[NewsArticle], stored=None) -> list[MarketNewsEvent]:
        events: list[MarketNewsEvent] = list(stored or [])
        touched=set()
        for article in articles:
            local = article.first_seen_at.astimezone(IST)
            candidate = event_from_article(article, local.date())
            duplicate_index = next((index for index, event in enumerate(events)
                if event.event_id == candidate.event_id or (event.event_category == candidate.event_category
                and event.stance == candidate.stance and set(event.affected_symbols)==set(candidate.affected_symbols)
                and _token_similarity(event.headline_or_summary, candidate.headline_or_summary) >= .72
                and abs(((event.first_seen_at or local) - candidate.first_seen_at).total_seconds()) <= 21600)), None)
            if duplicate_index is None:
                events.append(candidate)
                touched.add(candidate.event_id)
            else:
                existing = events[duplicate_index]
                touched.add(existing.event_id)
                sources = list(existing.source_metadata.get("sources", []))
                sources.append({"name": article.source, "url": article.url, "articleId": article.article_id})
                sources = list({(source.get('name'),source.get('url')):source for source in sources}.values())
                article_ids = list(existing.source_metadata.get("article_ids", []))
                article_ids.append(article.article_id)
                events[duplicate_index] = replace(existing, event_tags=tuple(dict.fromkeys((*existing.event_tags, *candidate.event_tags))),
                    source_metadata={**existing.source_metadata, "sources": sources,
                                     "article_ids": list(dict.fromkeys(article_ids))})
        return [item for item in events if item.event_id in touched]

    def status(self) -> dict[str, Any]:
        health = self.repository.health()
        return {"available": bool(health), "pollingIntervalSeconds": max(60, int(os.getenv("NEWS_POLL_INTERVAL_SECONDS", "90"))),
                'monitor':self.monitor,'storage':type(self.repository).__name__,
                "monitoringWindow": monitoring_window(), "sources": health,
                "lastRefresh": max((item.get("checked_at") for item in health), default=None)}

    def latest(self, limit: int = 30) -> dict[str, Any]:
        articles = self.repository.articles()[:limit]
        return {"available": bool(articles), "articles": [item.to_dict() for item in articles]}

    def events(self, limit: int = 100, category: str | None = None,
               relevance: str | None = None, date_value: str | None = None) -> dict[str, Any]:
        values = self.repository.events()
        if category: values = [item for item in values if item.event_category == category]
        if relevance: values = [item for item in values if item.relevance == relevance]
        if date_value: values = [item for item in values if item.session_date.isoformat() == date_value]
        return {"available": bool(values), "events": [item.to_dict() for item in values[:limit]], "count": len(values)}

    def event(self, event_id: str) -> dict[str, Any]:
        item = next((event for event in self.repository.events() if event.event_id == event_id), None)
        return {"available": bool(item), "event": item.to_dict() if item else None,
                "reason": None if item else "EVENT_NOT_FOUND"}

    def event_memory(self, event_id: str, current_state: dict[str, Any] | None = None) -> dict[str, Any]:
        events = self.repository.events()
        try:
            from .impact_study import EventImpactStudyService
            events=EventImpactStudyService().enrich_events(events)
        except Exception: pass  # Optional study storage must not disable existing news.
        current = next((event for event in events if event.event_id == event_id), None)
        if current is None:
            return {"available": False, "reason": "EVENT_NOT_FOUND"}
        cutoff = current.known_at or current.first_seen_at
        historical = [event for event in events if event.event_id != event_id and (
            (cutoff is not None and event.known_at is not None and event.known_at < cutoff)
            or (event.session_date < current.session_date and event.known_at is None)
        )]
        memory = EventMemoryEngine().search(current, historical)
        return {"available": True, "event": current.to_dict(), "historicalMemory": memory,
                "marketConfirmation": market_confirmation(current, current_state or {})}

    def current_memory(self, current_state: dict[str, Any] | None = None) -> dict[str, Any]:
        live = [event for event in self.repository.events(include_historical=False) if event.relevance == "HIGH"]
        if not live:
            return {"available": False, "status": "NO_HIGH_RELEVANCE_EVENT",
                    "monitoringWindow": monitoring_window()}
        return {**self.event_memory(live[0].event_id, current_state), "monitoringWindow": monitoring_window()}

    def memory_as_of(self, decision_at, current_state=None):
        """Prospective-only read; never substitute publication time for first receipt."""
        if decision_at.tzinfo is None or decision_at.utcoffset() is None:
            raise ValueError('Aware decision timestamp required')
        reader=getattr(self.repository,'events_as_of',None)
        if reader is None:
            return {'available':False,'reason':'AS_OF_NEWS_STORAGE_UNAVAILABLE'}
        events=[e for e in reader(decision_at) if e.first_seen_at is not None
                and e.first_seen_at<=decision_at]
        try:
            from .impact_study import EventImpactStudyService
            events=EventImpactStudyService().enrich_events(events,decision_at)
        except Exception: pass
        live=[e for e in events if e.source_quality=='LIVE_SOURCE' and e.relevance=='HIGH']
        if not live:
            return {'available':False,'reason':'NO_HIGH_RELEVANCE_EVENT_KNOWN_AT_DECISION'}
        current=max(live,key=lambda e:(e.first_seen_at,e.event_id))
        historical=[e for e in events if e.event_id!=current.event_id and e.first_seen_at<decision_at]
        # The selected event's own future response is not prediction-time evidence.
        frozen=replace(current,reaction={})
        memory=EventMemoryEngine().search(frozen,historical)
        selected_ids={match['eventId'] for match in memory.get('topMatches',[])}
        return {'available':True,'event':frozen.to_dict(),
                'historicalMemory':memory,
                'historicalSourceWatermarks':[{'eventId':e.event_id,'firstSeenAt':e.first_seen_at.isoformat(),
                    'reactionMeasuredAt':e.source_metadata.get('reactionMeasuredAt')}
                    for e in historical if e.event_id in selected_ids],
                'marketConfirmation':market_confirmation(frozen,current_state or {}),
                'firstSeenCutoff':decision_at.isoformat()}


class NewsPollingService:
    def __init__(self, service: NewsIntelligenceService | None = None, interval_seconds: int | None = None) -> None:
        self.service = service or NewsIntelligenceService()
        self.interval = max(60, interval_seconds or int(os.getenv("NEWS_POLL_INTERVAL_SECONDS", "90")))
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self.service.monitor['state'] = 'RUNNING'
        self.service.repository.save_runtime_health(self.service.monitor)
        self._thread = Thread(target=self._run, name="banknifty-news-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread: self._thread.join(timeout=25)
        self.service.monitor['state']='STOPPED'
        self.service.repository.save_runtime_health(self.service.monitor)

    def _run(self) -> None:
        next_news=0
        backfill=ReactionBackfillService(self.service.repository)
        while not self._stop.is_set():
            monitor=self.service.monitor
            monitor['lastAttempt']=datetime.now(timezone.utc).isoformat()
            try:
                with self.service.lock:
                    if time.monotonic()>=next_news:
                        result=self.service.refresh()
                        next_news=time.monotonic()+self.interval
                        failed=[item['source'] for item in result['sourceHealth'] if item['state']!='UP']
                        any_success=any(item['state']=='UP' for item in result['sourceHealth'])
                        monitor.update(state='DEGRADED' if failed or not any_success else 'RUNNING',lastError=', '.join(failed) or None,
                                       lastSuccessfulRefresh=result['refreshedAt'] if any_success else monitor.get('lastSuccessfulRefresh'),
                                       consecutiveFailures=0 if any_success else monitor['consecutiveFailures']+1)
                    backfill.update()
                    self.service.repository.save_runtime_health(monitor)
            except Exception as exc:
                monitor.update(state='DEGRADED',lastError=f'{type(exc).__name__}: {exc}',consecutiveFailures=monitor['consecutiveFailures']+1)
                logging.getLogger(__name__).exception('news_pipeline_failed')
                try:
                    self.service.repository.save_runtime_health(monitor)
                except Exception:
                    logging.getLogger(__name__).exception('news_health_persist_failed')
            self._stop.wait(max(30,int(os.getenv('NEWS_REACTION_INTERVAL_SECONDS','30'))))


def _token_similarity(left: str, right: str) -> float:
    a, b = set(normalized_headline(left).split()), set(normalized_headline(right).split())
    return len(a & b) / len(a | b) if a | b else 0.0
