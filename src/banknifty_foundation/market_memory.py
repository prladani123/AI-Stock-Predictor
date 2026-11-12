from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from statistics import mean, pstdev
from typing import Any, Iterable
from .paths import memory_root


NUMERIC_FEATURES = (
    "return_1m", "return_5m", "return_10m", "return_20m",
    "realized_volatility_5m", "realized_volatility_10m", "realized_volatility_20m",
    "range_expansion_ratio", "session_progress", "futures_premium_pct",
    "futures_oi_change_5m", "pcr_oi", "oi_change_imbalance",
    "atm_straddle_change", "ce_oi_concentration", "pe_oi_concentration",
)
CATEGORICAL_FEATURES = ("regime", "futures_buildup", "expiry_context")
V2_SECTION_PREFIXES = (
    "1. Verified BANKNIFTY price record", "2. What happened", "3. Why it moved",
    "4. News / event timeline", "5. Pattern lesson", "6. Option / futures context",
    "7. Machine-readable memory block", "8. Sources",
)


@dataclass(frozen=True)
class DailyMarketMemory:
    trading_date: date
    session_type: str
    expiry_context: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    close_change_percent: float | None
    gap_percent: float | None
    range_percent: float | None
    open_to_close_percent: float | None
    direction: str
    causal_evidence_grade: str
    timing_precision: str
    source_quality: str
    data_quality: str
    tags: tuple[str, ...]
    special_flags: tuple[str, ...]
    what_happened: str
    causal_explanation: str
    event_timeline: str
    pattern_lesson: str
    option_futures_context: str
    source_block: str
    source_metadata: tuple[dict[str, Any], ...]
    markdown_path: str
    checksum: str


@dataclass(frozen=True)
class MarketEpisode:
    episode_id: str
    session_date: date
    as_of: datetime
    state: dict[str, Any]
    outcome: dict[str, Any]
    regime: str
    expiry_context: str
    source_quality: str


def _front_matter(text: str, source: Path) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError(f"{source}: missing front matter")
    raw, body = text[4:].split("\n---\n", 1)
    metadata: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            raise ValueError(f"{source}: malformed front matter line {line!r}")
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    required = {"date", "market", "instrument", "session_type", "expiry_context",
                "data_quality", "causal_evidence_grade", "timing_precision",
                "source_quality", "generated_at"}
    if required - metadata.keys():
        raise ValueError(f"{source}: missing fields {sorted(required - metadata.keys())}")
    if metadata["market"] != "NSE" or metadata["instrument"] != "BANKNIFTY":
        raise ValueError(f"{source}: expected NSE BANKNIFTY")
    datetime.fromisoformat(metadata["generated_at"])
    return metadata, body


def _section(body: str, prefix: str) -> str:
    match = re.search(rf"^## {re.escape(prefix)}[^\n]*\n", body, re.MULTILINE)
    if not match:
        raise ValueError(f"missing section: {prefix}")
    end = body.find("\n## ", match.end())
    return body[match.end():] if end < 0 else body[match.end():end]


def _price_table(section: str) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("|---") or "Metric" in line:
            continue
        cells = [cell.strip().replace("**", "") for cell in line.strip("|").split("|")]
        if len(cells) != 2:
            continue
        match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", cells[1])
        values[cells[0]] = float(match.group().replace(",", "")) if match else None
    return values


def _machine_payload(section: str, source: Path) -> dict[str, Any]:
    match = re.search(r"```json\s*(\{.*?\})\s*```", section, re.DOTALL)
    if not match:
        raise ValueError(f"{source}: missing machine-readable JSON block")
    payload = json.loads(match.group(1))
    required = {"date", "banknifty_close", "close_change_pct", "gap_pct",
                "intraday_range_pct", "open_to_close_pct", "direction",
                "expiry_context", "evidence_grade", "timing_precision", "tags", "special_flags"}
    if required - payload.keys():
        raise ValueError(f"{source}: incomplete machine-readable block")
    return payload


def _sources(section: str) -> tuple[dict[str, Any], ...]:
    items: list[dict[str, Any]] = []
    lines = section.splitlines()
    for index, line in enumerate(lines):
        url_match = re.search(r"https?://\S+", line)
        if not url_match:
            continue
        previous = lines[index - 1].strip() if index else ""
        title = re.sub(r"^\d+\.\s*|^-\s*|\*\*", "", previous).strip()
        published = None
        match = re.search(r"Published:\s*([^.;]+)", previous, re.IGNORECASE)
        if match:
            published = match.group(1).strip()
        items.append({"title": title or None, "url": url_match.group().rstrip(").,"),
                      "publication_text": published})
    return tuple(items)


def parse_daily_memory(path: str | Path) -> DailyMarketMemory:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    metadata, body = _front_matter(text, source)
    positions = [body.find(f"## {prefix}") for prefix in V2_SECTION_PREFIXES]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        raise ValueError(f"{source}: V2 sections missing or out of order")
    price = _price_table(_section(body, V2_SECTION_PREFIXES[0]))
    payload = _machine_payload(_section(body, V2_SECTION_PREFIXES[6]), source)
    if payload["date"] != metadata["date"] or payload["expiry_context"] != metadata["expiry_context"]:
        raise ValueError(f"{source}: front matter and machine block disagree")
    if payload["evidence_grade"] != metadata["causal_evidence_grade"]:
        raise ValueError(f"{source}: evidence grade mismatch")
    source_block = _section(body, V2_SECTION_PREFIXES[7]).strip()
    return DailyMarketMemory(
        trading_date=date.fromisoformat(metadata["date"]), session_type=metadata["session_type"],
        expiry_context=payload["expiry_context"], open=price.get("Open"), high=price.get("High"),
        low=price.get("Low"), close=float(payload["banknifty_close"]),
        close_change_percent=float(payload["close_change_pct"]) if payload["close_change_pct"] is not None else None,
        gap_percent=float(payload["gap_pct"]) if payload["gap_pct"] is not None else None,
        range_percent=float(payload["intraday_range_pct"]),
        open_to_close_percent=float(payload["open_to_close_pct"]), direction=payload["direction"],
        causal_evidence_grade=payload["evidence_grade"], timing_precision=payload["timing_precision"],
        source_quality=metadata["source_quality"], data_quality=metadata["data_quality"],
        tags=tuple(payload["tags"]), special_flags=tuple(payload["special_flags"]),
        what_happened=_section(body, V2_SECTION_PREFIXES[1]).strip(),
        causal_explanation=_section(body, V2_SECTION_PREFIXES[2]).strip(),
        event_timeline=_section(body, V2_SECTION_PREFIXES[3]).strip(),
        pattern_lesson=_section(body, V2_SECTION_PREFIXES[4]).strip(),
        option_futures_context=_section(body, V2_SECTION_PREFIXES[5]).strip(),
        source_block=source_block, source_metadata=_sources(source_block),
        markdown_path=str(source), checksum=sha256(text.encode("utf-8")).hexdigest(),
    )


def load_episodes(path: str | Path) -> list[MarketEpisode]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [MarketEpisode(item["episode_id"], date.fromisoformat(item["session_date"]),
        datetime.fromisoformat(item["as_of"]), item["state"], item["outcome"], item["regime"],
        item["expiry_context"], item["source_quality"]) for item in payload]


class FileMarketMemoryRepository:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else memory_root()

    def days(self) -> list[DailyMarketMemory]:
        return sorted((parse_daily_memory(path) for path in (self.root / "daily").glob("*.md")),
                      key=lambda item: item.trading_date, reverse=True)

    def day(self, value: date) -> DailyMarketMemory | None:
        path = self.root / "daily" / f"{value.isoformat()}.md"
        return parse_daily_memory(path) if path.exists() else None

    def day_markdown(self, value: date) -> str | None:
        path = self.root / "daily" / f"{value.isoformat()}.md"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def episodes(self) -> list[MarketEpisode]:
        path = self.root / "derived" / "episodes.json"
        return load_episodes(path) if path.exists() else []

    def events(self) -> list[Any]:
        from .news.models import MarketNewsEvent
        path = self.root / "derived" / "structured_events.json"
        if not path.exists():
            return []
        return [MarketNewsEvent.from_dict(item) for item in json.loads(path.read_text(encoding="utf-8"))]


class PostgresMarketMemoryRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def ingest(self, days: Iterable[DailyMarketMemory], events: Iterable[Any], episodes: Iterable[MarketEpisode]) -> dict[str, int]:
        import psycopg
        from psycopg.types.json import Jsonb
        counts = {"days": 0, "events": 0, "episodes": 0}
        with psycopg.connect(self.database_url) as connection:
            for item in days:
                connection.execute("""
                    INSERT INTO market_memory_days
                    (trading_date, session_type, expiry_context, open_price, high_price, low_price,
                     close_price, gap_percent, range_percent, primary_regime, session_bias,
                     futures_buildup, tags, markdown_path,
                     source_quality, sources_count, checksum, data_quality, causal_evidence_grade,
                     timing_precision, close_change_percent, open_to_close_percent, special_flags,
                     causal_summary, pattern_lesson, memory_payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (trading_date) DO UPDATE SET expiry_context=EXCLUDED.expiry_context,
                      open_price=EXCLUDED.open_price, high_price=EXCLUDED.high_price, low_price=EXCLUDED.low_price,
                      close_price=EXCLUDED.close_price, gap_percent=EXCLUDED.gap_percent,
                      range_percent=EXCLUDED.range_percent, session_bias=EXCLUDED.session_bias,
                      tags=EXCLUDED.tags, markdown_path=EXCLUDED.markdown_path,
                      source_quality=EXCLUDED.source_quality, sources_count=EXCLUDED.sources_count,
                      checksum=EXCLUDED.checksum, data_quality=EXCLUDED.data_quality,
                      causal_evidence_grade=EXCLUDED.causal_evidence_grade,
                      timing_precision=EXCLUDED.timing_precision,
                      close_change_percent=EXCLUDED.close_change_percent,
                      open_to_close_percent=EXCLUDED.open_to_close_percent,
                      special_flags=EXCLUDED.special_flags, causal_summary=EXCLUDED.causal_summary,
                      pattern_lesson=EXCLUDED.pattern_lesson, memory_payload=EXCLUDED.memory_payload,
                      updated_at=now() WHERE market_memory_days.checksum <> EXCLUDED.checksum
                """, (item.trading_date, item.session_type, item.expiry_context, item.open, item.high,
                      item.low, item.close, item.gap_percent, item.range_percent, "UNKNOWN",
                      item.direction, "UNKNOWN", Jsonb(list(item.tags)), item.markdown_path, item.source_quality,
                      len(item.source_metadata), item.checksum, item.data_quality,
                      item.causal_evidence_grade, item.timing_precision, item.close_change_percent,
                      item.open_to_close_percent, Jsonb(list(item.special_flags)), item.what_happened,
                      item.pattern_lesson, Jsonb(public_day(item))))
                counts["days"] += 1
            for event in events:
                connection.execute("""
                    INSERT INTO market_memory_events
                    (event_id, session_date, known_at, category, summary, relevance, source,
                     published_at, first_seen_at, knowledge_phase, event_tags, evidence_grade,
                     timing_precision, predictive_eligible, context_only, directional_context,
                     reaction, special_flags, content_hash)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (event_id) DO UPDATE SET reaction=EXCLUDED.reaction,
                      first_seen_at=LEAST(market_memory_events.first_seen_at, EXCLUDED.first_seen_at),
                      updated_at=now()
                """, (event.event_id, event.session_date, event.known_at, event.event_category,
                      event.headline_or_summary, event.relevance, Jsonb(event.source_metadata),
                      event.published_at, event.first_seen_at, event.knowledge_phase,
                      Jsonb(list(event.event_tags)), event.causal_evidence_grade,
                      event.timing_precision, event.predictive_eligible, event.context_only,
                      event.directional_context, Jsonb(event.reaction),
                      Jsonb(list(event.special_flags)), event.content_hash))
                counts["events"] += 1
            for item in episodes:
                connection.execute("""
                    INSERT INTO market_memory_episodes
                    (episode_id, session_date, as_of, state, outcome, regime, expiry_context, source_quality)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (episode_id) DO UPDATE SET state=EXCLUDED.state, outcome=EXCLUDED.outcome,
                      regime=EXCLUDED.regime, expiry_context=EXCLUDED.expiry_context,
                      source_quality=EXCLUDED.source_quality, updated_at=now()
                """, (item.episode_id, item.session_date, item.as_of, Jsonb(item.state),
                      Jsonb(item.outcome), item.regime, item.expiry_context, item.source_quality))
                counts["episodes"] += 1
        return counts


class SimilarityEngine:
    def search(self, current: dict[str, Any], episodes: Iterable[MarketEpisode], limit: int = 14) -> dict[str, Any]:
        candidates = list(episodes)
        if current.get("_as_of"):
            cutoff = current["_as_of"] if isinstance(current["_as_of"], datetime) else datetime.fromisoformat(str(current["_as_of"]))
            candidates = [item for item in candidates if item.as_of < cutoff]
        scored: list[tuple[float, MarketEpisode, list[str]]] = []
        available = sum(1 for name in NUMERIC_FEATURES if _finite(current.get(name)))
        for episode in candidates:
            shared = [name for name in NUMERIC_FEATURES if _finite(current.get(name)) and _finite(episode.state.get(name))]
            categorical = [name for name in CATEGORICAL_FEATURES if current.get(name) not in {None, "UNKNOWN"} and episode.state.get(name) not in {None, "UNKNOWN"}]
            if len(shared) < 4:
                continue
            distances = []
            for name in shared:
                population = [float(item.state[name]) for item in candidates if _finite(item.state.get(name))]
                scale = pstdev(population) if len(population) > 1 else 0.0
                distances.append(abs(float(current[name]) - float(episode.state[name])) / (scale or 1.0))
            distances.extend(0.0 if current[name] == episode.state[name] else 1.0 for name in categorical)
            distance = math.sqrt(sum(value * value for value in distances) / len(distances))
            similarity = (1 / (1 + distance)) * (.7 + .3 * len(shared) / max(1, available))
            scored.append((similarity, episode, shared + categorical))
        selected = sorted(scored, key=lambda row: row[0], reverse=True)[:limit]
        if not selected:
            return {"available": False, "reason": "NO_COMPARABLE_EPISODES", "matchedEpisodes": 0,
                    "similarityStrength": "UNAVAILABLE", "historicalOutcome": {}, "topMatches": []}
        average = mean(row[0] for row in selected)
        return {"available": True, "matchedEpisodes": len(selected),
                "similarityStrength": "HIGH" if average >= .7 else "MEDIUM" if average >= .5 else "LOW",
                "historicalOutcome": _aggregate_outcomes([row[1] for row in selected]),
                "topMatches": [{"date": item.session_date.isoformat(), "asOf": item.as_of.isoformat(),
                    "similarity": round(score, 4), "regime": item.regime,
                    "expiryContext": item.expiry_context, "matchedFeatures": fields,
                    **{f"outcome{h}m": (item.outcome.get(str(h)) or {}).get("label") for h in (10, 20, 30)}}
                    for score, item, fields in selected[:5]],
                "notice": "Historical evidence only; Big Move V2 probabilities are unchanged."}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _aggregate_outcomes(episodes: list[MarketEpisode]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in (10, 20, 30):
        labels = [(item.outcome.get(str(horizon)) or {}).get("label") for item in episodes]
        valid = [label for label in labels if label and label != "UNKNOWN"]
        directional = [label for label in valid if label in {"BIG_UP", "BIG_DOWN"}]
        result[f"bigMove{horizon}Rate"] = round(sum(label in {"BIG_UP", "BIG_DOWN", "AMBIGUOUS"} for label in valid) / len(valid), 4) if valid else None
        result[f"up{horizon}Rate"] = round(directional.count("BIG_UP") / len(directional), 4) if directional else None
        result[f"down{horizon}Rate"] = round(directional.count("BIG_DOWN") / len(directional), 4) if directional else None
    return result


def public_day(item: DailyMarketMemory) -> dict[str, Any]:
    result = asdict(item)
    result["date"] = result.pop("trading_date").isoformat()
    for key in ("tags", "special_flags", "source_metadata"):
        result[key] = list(result[key])
    result["markdownPath"] = result.pop("markdown_path")
    result["sourceQuality"] = result.pop("source_quality")
    result["dataQuality"] = result.pop("data_quality")
    result["causalEvidenceGrade"] = result.pop("causal_evidence_grade")
    result["timingPrecision"] = result.pop("timing_precision")
    result["whatHappened"] = result.pop("what_happened")
    result["whyItMoved"] = result.pop("causal_explanation")
    result["eventTimeline"] = result.pop("event_timeline")
    result["patternLesson"] = result.pop("pattern_lesson")
    result["sourceMetadata"] = result.pop("source_metadata")
    return result
