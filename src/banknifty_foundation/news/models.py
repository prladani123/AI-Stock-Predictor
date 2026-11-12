from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from hashlib import sha256
import re
from typing import Any


def normalized_headline(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


@dataclass(frozen=True)
class NewsArticle:
    headline: str
    source: str
    url: str
    published_at: datetime | None
    first_seen_at: datetime
    ingested_at: datetime
    affected_symbols: tuple[str, ...] = ()
    category: str = "OTHER_RELEVANT"
    tags: tuple[str, ...] = ()
    relevance: str = "LOW"
    relevance_score: float = 0.0
    banknifty_relevance: str = "LOW"
    relevance_reason: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        basis = f"{normalized_headline(self.headline)}|{self.source}|{self.published_at.isoformat() if self.published_at else ''}"
        return sha256(basis.encode()).hexdigest()

    @property
    def article_id(self) -> str:
        return self.content_hash[:24]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("published_at", "first_seen_at", "ingested_at"):
            result[key] = result[key].isoformat() if result[key] else None
        result["affected_symbols"] = list(self.affected_symbols)
        result["tags"] = list(self.tags)
        result["content_hash"] = self.content_hash
        result["article_id"] = self.article_id
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NewsArticle":
        return cls(value["headline"], value["source"], value["url"],
            datetime.fromisoformat(value["published_at"]) if value.get("published_at") else None,
            datetime.fromisoformat(value["first_seen_at"]), datetime.fromisoformat(value["ingested_at"]),
            tuple(value.get("affected_symbols", [])), value.get("category", "OTHER_RELEVANT"),
            tuple(value.get("tags", [])), value.get("relevance", "LOW"),
            float(value.get("relevance_score", 0)), value.get("banknifty_relevance", "LOW"),
            value.get("relevance_reason", ""), value.get("raw_metadata", {}))


@dataclass(frozen=True)
class MarketNewsEvent:
    event_id: str
    session_date: date
    event_category: str
    event_tags: tuple[str, ...]
    headline_or_summary: str
    known_at: datetime | None
    published_at: datetime | None
    first_seen_at: datetime | None
    knowledge_phase: str
    timing_precision: str
    causal_evidence_grade: str
    source_quality: str
    predictive_eligible: bool
    context_only: bool
    directional_context: str
    observed_banknifty_response: str
    source_name: str | None
    source_url: str | None
    special_flags: tuple[str, ...]
    relevance: str
    reaction: dict[str, Any] = field(default_factory=dict)
    source_metadata: dict[str, Any] = field(default_factory=dict)
    scheduled: bool = False
    stance: str = "UNKNOWN"
    scope: str = "UNKNOWN"
    secondary_categories: tuple[str, ...] = ()
    affected_symbols: tuple[str, ...] = ()
    timing_confidence: str = "UNKNOWN"

    @property
    def content_hash(self) -> str:
        return sha256(json_basis(self).encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["session_date"] = self.session_date.isoformat()
        for key in ("known_at", "published_at", "first_seen_at"):
            value = getattr(self, key)
            result[key] = value.isoformat() if value else None
        result["event_tags"] = list(self.event_tags)
        result["special_flags"] = list(self.special_flags)
        result["content_hash"] = self.content_hash
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MarketNewsEvent":
        fields = dict(value)
        fields.pop("content_hash", None)
        fields["session_date"] = date.fromisoformat(fields["session_date"])
        for key in ("known_at", "published_at", "first_seen_at"):
            fields[key] = datetime.fromisoformat(fields[key]) if fields.get(key) else None
        fields["event_tags"] = tuple(fields.get("event_tags", []))
        fields["special_flags"] = tuple(fields.get("special_flags", []))
        return cls(**fields)


def json_basis(event: MarketNewsEvent) -> str:
    return "|".join((event.session_date.isoformat(), event.event_category,
                     normalized_headline(event.headline_or_summary),
                     event.known_at.isoformat() if event.known_at else ""))


@dataclass(frozen=True)
class SourceHealth:
    source: str
    state: str
    checked_at: datetime
    article_count: int
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["checked_at"] = self.checked_at.isoformat()
        return result
