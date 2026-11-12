from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean
from typing import Any, Iterable

from ..calendar import IST
from ..market_memory import DailyMarketMemory
from .classification import classify
from .models import MarketNewsEvent, NewsArticle
from .reaction import NewsReactionService
from .impact import EventImpactClassifier


STRUCTURAL_TAGS = {"cas-distortion", "msci-rebalance", "auction-flow", "timed-structural-event"}


def monitoring_window(now: datetime | None = None) -> str:
    current = (now or datetime.now(IST)).astimezone(IST).time()
    if current < time(9, 15): return "PRE_OPEN"
    if current < time(10, 30): return "OPENING_HOUR"
    if current < time(12, 0): return "SCHEDULED_POLICY_WINDOW"
    if current < time(14, 30): return "MIDDAY_EARLY_AFTERNOON"
    if current < time(15, 15): return "FINAL_HOUR"
    if current < time(15, 40): return "CLOSING_AUCTION"
    return "POST_CLOSE"


def _published_at(day: date, timeline: str) -> datetime | None:
    match = re.search(r"Primary source publication time:\*\*\s*(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s+IST", timeline)
    if not match:
        return None
    return datetime.combine(date.fromisoformat(match.group(1)), time.fromisoformat(match.group(2)), IST)


def _phase(value: datetime | None, *, preopen: bool = False, structural: bool = False) -> str:
    if structural: return "CLOSING_AUCTION"
    if preopen: return "PREOPEN"
    if value is None: return "UNKNOWN"
    local = value.astimezone(IST).time()
    if local < time(9, 15): return "PREOPEN"
    if local < time(10, 30): return "OPENING_HOUR"
    if local < time(12): return "SCHEDULED_POLICY_WINDOW"
    if local < time(14, 30): return "MIDDAY_EARLY_AFTERNOON"
    if local < time(15, 15): return "FINAL_HOUR"
    if local < time(15, 40): return "CLOSING_AUCTION"
    return "POST_CLOSE"


def _direction(text: str, fallback: str) -> str:
    lowered = text.lower()
    bullish = any(word in lowered for word in ("recovery", "rebound", "relief", "rose", "gained", "buying", "easing"))
    bearish = any(word in lowered for word in ("fall", "fell", "weakness", "shock", "selling", "risk", "lower", "surge"))
    if bullish and bearish: return "MIXED"
    if bullish: return "BULLISH"
    if bearish: return "BEARISH"
    return "BULLISH" if fallback == "UP" else "BEARISH" if fallback == "DOWN" else "UNKNOWN"


def _summary(day: DailyMarketMemory) -> str:
    return next((line.strip() for line in day.what_happened.splitlines() if line.strip() and not line.startswith("**")), day.pattern_lesson)


def _event(day: DailyMarketMemory, summary: str, known_at: datetime | None,
           published_at: datetime | None, *, preopen: bool = False,
           structural: bool = False, ordinal: int = 0) -> MarketNewsEvent:
    values = classify(summary + " " + " ".join(day.tags))
    category = "NSE_MARKET_STRUCTURE" if structural else values["category"]
    confidence='UNKNOWN'
    if preopen and day.timing_precision in {'EXACT_OR_NARROW_WINDOW','SESSION_PHASE'}:
        confidence='PREOPEN_KNOWN'
    elif known_at and day.timing_precision=='EXACT_OR_NARROW_WINDOW':
        if re.search(r'live update|live blog',summary,re.I):confidence='CONFIRMED_LIVE_UPDATE'
        elif published_at and published_at<=known_at and re.search(r'RBI.*decision|decision.*published|official.*release',summary,re.I):confidence='CONFIRMED_OFFICIAL_RELEASE'
        elif published_at and published_at<=known_at and re.search(r'published|reported at',summary,re.I):confidence='CONFIRMED_FIRST_SEEN'
        else:confidence='RETROSPECTIVE_ONLY' if published_at and published_at>known_at else 'SESSION_APPROXIMATION'
    predictive = known_at is not None and not structural and confidence in {'PREOPEN_KNOWN','CONFIRMED_LIVE_UPDATE','CONFIRMED_OFFICIAL_RELEASE','CONFIRMED_FIRST_SEEN'}
    context_only = not predictive
    source = day.source_metadata[0] if day.source_metadata else {}
    first_seen = known_at if predictive else published_at
    event_id = sha256(f"historical|{day.trading_date}|{ordinal}|{summary}|{known_at}".encode()).hexdigest()[:24]
    return MarketNewsEvent(event_id, day.trading_date, category, day.tags, summary,
        known_at, published_at, first_seen, _phase(known_at, preopen=preopen, structural=structural),
        day.timing_precision, day.causal_evidence_grade, day.source_quality,
        predictive, context_only, "STRUCTURAL" if structural else _direction(summary, day.direction),
        f"Close {day.close_change_percent:+.3f}%; intraday range {day.range_percent:.3f}%.",
        source.get("title"), source.get("url"), day.special_flags,
        values["relevance"], {}, {"daily_checksum": day.checksum, "sources": list(day.source_metadata)},
        "scheduled-event" in day.tags, timing_confidence=confidence)


def build_historical_events(days: Iterable[DailyMarketMemory],
                            intraday: dict[str, list[dict[str, Any]]]) -> list[MarketNewsEvent]:
    reaction_service = NewsReactionService()
    output: list[MarketNewsEvent] = []
    for day in sorted(days, key=lambda item: item.trading_date):
        narrative = day.what_happened
        publication = _published_at(day.trading_date, day.event_timeline)
        structural = bool(set(day.tags) & STRUCTURAL_TAGS)
        timed_parts = re.findall(r"(?:^|[.;])\s*([^.;\n]*?\b\d{1,2}:\d{2}\s+IST[^.;\n]*)", narrative, re.IGNORECASE)
        created: list[MarketNewsEvent] = []
        for ordinal, part in enumerate(timed_parts):
            clock = re.search(r"(\d{1,2}:\d{2})\s+IST", part, re.IGNORECASE)
            if not clock:
                continue
            known = datetime.combine(day.trading_date, time.fromisoformat(clock.group(1)), IST)
            evidence_word = any(word in part.lower() for word in ("published", "live update", "article", "report", "decision", "documented"))
            item = _event(day, part.strip(" :"), known if evidence_word else None, publication,
                          structural=structural, ordinal=ordinal)
            created.append(item)
        if not created:
            is_preopen = bool(re.search(r"pre-open|overnight|before the open|known before", narrative, re.IGNORECASE))
            known = datetime.combine(day.trading_date, time(9, 15), IST) if is_preopen else None
            created.append(_event(day, _summary(day), known, publication, preopen=is_preopen,
                                  structural=structural, ordinal=0))
        rows = intraday.get(day.trading_date.isoformat(), [])
        for item in created:
            reaction = reaction_service.measure(item.known_at, rows) if rows and item.predictive_eligible else {
                "status": "REACTION_NOT_MEASURABLE", "reason": "MINUTE_DATA_UNAVAILABLE"}
            output.append(replace(item, reaction=reaction))
    return output


def event_from_article(article: NewsArticle, session_date: date) -> MarketNewsEvent:
    first_known = article.first_seen_at
    phase = _phase(first_known)
    identifier = sha256(f"live|{article.content_hash}".encode()).hexdigest()[:24]
    impact=EventImpactClassifier().classify(article.headline,article.category)
    direction=impact['directional_context']
    return MarketNewsEvent(identifier, session_date, article.category, article.tags, article.headline,
        first_known, article.published_at, article.first_seen_at, phase, "EXACT_OR_NARROW_WINDOW",
        "LIVE_UNVERIFIED_CAUSALITY", "LIVE_SOURCE", True, False, direction, "PENDING_REACTION",
        article.source, article.url, (), article.relevance, {'status':'PENDING','availableHorizons':[],'pendingHorizons':[5,10,20,30]},
        {"article_ids": [article.article_id],'sources':[{'name':article.source,'url':article.url,'articleId':article.article_id}],
         'constituentContext':{key:article.raw_metadata[key] for key in ('constituentUniverseMember','constituentSnapshotId','instrumentKeys','verifiedWeights') if key in article.raw_metadata}}, False,
        stance=impact['stance'],scope=article.raw_metadata.get('scope','UNKNOWN'),
        secondary_categories=tuple(article.raw_metadata.get('secondaryCategories',[])),affected_symbols=article.affected_symbols,
        timing_confidence='CONFIRMED_FIRST_SEEN')


class EventMemoryEngine:
    def search(self, current: MarketNewsEvent, historical: Iterable[MarketNewsEvent], limit: int = 12) -> dict[str, Any]:
        scored: list[tuple[float, MarketNewsEvent]] = []
        from .impact_study import comparable_similarity, similarity_dimensions, event_known_at
        reasons_by_id = {}
        cutoff = event_known_at(current)
        for event in historical:
            if not event.predictive_eligible or event.reaction.get("status") not in {"REACTION_AVAILABLE","COMPLETE_30M"}:
                continue
            measured = event.reaction.get('measuredAt') or event.source_metadata.get('reactionMeasuredAt')
            from ..research_context import _stamp
            try:
                if cutoff is None or not measured or _stamp(measured)>cutoff: continue
            except (ValueError,TypeError): continue
            known = event_known_at(event)
            if known is None or known>=cutoff: continue
            score, reasons = comparable_similarity(similarity_dimensions(current), similarity_dimensions(event))
            reasons_by_id[event.event_id] = reasons
            if score >= .2:
                scored.append((score, event))
        selected = sorted(scored, key=lambda item: (-item[0],item[1].event_id))[:limit]
        if not selected:
            return {"available": False, "matchedEvents": 0, "eventSimilarityStrength": "UNAVAILABLE",
                    "reason": "NO_TIMED_HISTORICAL_MATCHES", "historicalReaction": {}, "topMatches": []}
        reaction: dict[str, Any] = {}
        for horizon in (5, 10, 20, 30):
            returns = [(item.reaction.get("returns") or {}).get(str(horizon)) for _, item in selected]
            returns = [value for value in returns if isinstance(value, (int, float))]
            directional_labels = [((item.reaction.get("big_move") or {}).get(str(horizon)) or {}).get("label") for _, item in selected] if horizon != 5 else []
            valid_labels = [label for label in directional_labels if label and label != "UNKNOWN"]
            reaction[str(horizon)] = {
                "sampleSize": len(returns), "averageReturn": round(mean(returns), 6) if returns else None,
                "bigMoveRate": round(sum(label in {"BIG_UP", "BIG_DOWN", "AMBIGUOUS"} for label in valid_labels) / len(valid_labels), 4) if valid_labels else None,
                "upRate": round(sum(value > 0 for value in returns) / len(returns), 4) if returns else None,
                "downRate": round(sum(value < 0 for value in returns) / len(returns), 4) if returns else None,
            }
        average_score = mean(score for score, _ in selected)
        return {"available": True, "matchedEvents": len(selected),
                "eventSimilarityStrength": "HIGH" if average_score >= .7 else "MEDIUM" if average_score >= .45 else "LOW",
                "historicalReaction": reaction,
                "maxUpExcursion": _average(selected, "max_up_excursion"),
                "maxDownExcursion": _average(selected, "max_down_excursion"),
                "topMatches": [{"eventId": item.event_id, "date": item.session_date.isoformat(),
                    "category": item.event_category, "summary": item.headline_or_summary,
                    "similarity": round(score, 3), "similarityReasons": reasons_by_id[item.event_id],
                    "reaction": item.reaction} for score, item in selected[:5]]}


def _average(selected: list[tuple[float, MarketNewsEvent]], key: str) -> float | None:
    values = [item.reaction.get(key) for _, item in selected if isinstance(item.reaction.get(key), (int, float))]
    return round(mean(values), 6) if values else None


def write_structured_events(path: str | Path, events: Iterable[MarketNewsEvent]) -> None:
    Path(path).write_text(json.dumps([item.to_dict() for item in events], indent=2, ensure_ascii=False),
                          encoding="utf-8", newline="\n")
