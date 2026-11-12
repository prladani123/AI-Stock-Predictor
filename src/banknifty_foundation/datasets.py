from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4

from .calendar import IST, TradingCalendar
from .models import DatasetRevision, QualityIssue, RevisionCandle, TradingSession
from .repository import Repository, canonical_json, digest


BLOCKING_CODES = {
    "UNKNOWN_TRADING_SESSION",
    "MISSING_MINUTE",
    "DUPLICATE_TIMESTAMP",
    "CONFLICTING_DUPLICATE_CANDLE",
    "OUTSIDE_TRADING_SESSION",
    "OUT_OF_ORDER",
    "INVALID_OHLC",
    "INVALID_VOLUME_OI",
    "UNEXPECTED_TIMEZONE",
    "INCOMPLETE_SESSION_COVERAGE",
    "INVALID_ROW",
}


def _utc_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    first = datetime.combine(start, time.min, IST).astimezone(timezone.utc)
    after_last = datetime.combine(end + timedelta(days=1), time.min, IST).astimezone(timezone.utc)
    return first, after_last


def _session_for(timestamp: datetime, sessions: dict[date, TradingSession]) -> TradingSession | None:
    local_date = timestamp.astimezone(IST).date()
    return sessions.get(local_date)


class DatasetValidationService:
    """Build immutable candle snapshots and quarantine candidates until a human decision."""

    def __init__(
        self,
        repository: Repository,
        *,
        clock=None,
        revision_id_factory=None,
    ) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.revision_id_factory = revision_id_factory or (lambda: str(uuid4()))

    def create_candidate(
        self, instrument_ids: list[int] | tuple[int, ...], start: date, end: date
    ) -> tuple[DatasetRevision, dict[str, Any]]:
        instruments = tuple(sorted(set(instrument_ids)))
        if not instruments:
            raise ValueError("at least one instrument is required")
        if end < start:
            raise ValueError("session end must not precede session start")

        stored_sessions = self.repository.sessions_between(start, end)
        sessions = {item.session_date: item for item in stored_sessions}
        issues: list[QualityIssue] = []
        current = start
        while current <= end:
            if current not in sessions:
                issues.append(
                    QualityIssue(
                        "UNKNOWN_TRADING_SESSION",
                        "ERROR",
                        "No explicit OPEN, CLOSED or SPECIAL calendar record exists",
                        details={"session_date": current.isoformat()},
                    )
                )
            current += timedelta(days=1)

        range_start, range_end = _utc_bounds(start, end)
        rows = self.repository.revision_source_rows(instruments, range_start, range_end)
        issues.extend(self.repository.issues_between(instruments, range_start, range_end))

        rows_by_instrument: dict[int, list[RevisionCandle]] = {item: [] for item in instruments}
        for row in rows:
            rows_by_instrument[row.instrument_id].append(row)
            candle = row.candle
            if candle.timestamp.tzinfo is None or candle.timestamp.utcoffset() is None:
                issues.append(
                    QualityIssue("UNEXPECTED_TIMEZONE", "ERROR", "Stored candle has a naive timestamp")
                )
                continue
            if candle.timestamp.utcoffset() != timedelta(0):
                issues.append(
                    QualityIssue(
                        "UNEXPECTED_TIMEZONE",
                        "ERROR",
                        "Canonical candle timestamps must be UTC",
                        candle.timestamp,
                    )
                )
            session = _session_for(candle.timestamp, sessions)
            if (
                session is None
                or session.status == "CLOSED"
                or not (session.open_at <= candle.timestamp < session.close_at)
            ):
                issues.append(
                    QualityIssue(
                        "OUTSIDE_TRADING_SESSION",
                        "ERROR",
                        "Candle is outside its explicitly stored continuous session",
                        candle.timestamp,
                    )
                )
            if min(candle.open, candle.high, candle.low, candle.close) <= 0 or (
                candle.high < max(candle.open, candle.low, candle.close)
                or candle.low > min(candle.open, candle.high, candle.close)
            ):
                issues.append(QualityIssue("INVALID_OHLC", "ERROR", "Stored OHLC is invalid", candle.timestamp))
            if candle.volume < 0 or (candle.oi is not None and candle.oi < 0):
                issues.append(
                    QualityIssue("INVALID_VOLUME_OI", "ERROR", "Stored volume/OI is negative", candle.timestamp)
                )

        coverage: list[dict[str, Any]] = []
        for instrument_id in instruments:
            actual = {item.candle.timestamp for item in rows_by_instrument[instrument_id]}
            for session in stored_sessions:
                if session.status not in {"OPEN", "SPECIAL"}:
                    continue
                expected = TradingCalendar.expected_minutes(session)
                missing = [timestamp for timestamp in expected if timestamp not in actual]
                if missing:
                    issues.extend(
                        QualityIssue("MISSING_MINUTE", "ERROR", "Expected candle is absent", item)
                        for item in missing
                    )
                    issues.append(
                        QualityIssue(
                            "INCOMPLETE_SESSION_COVERAGE",
                            "ERROR",
                            f"Instrument {instrument_id} is missing {len(missing)} minute(s)",
                            session.open_at,
                            {"instrument_id": instrument_id, "missing_minutes": len(missing)},
                        )
                    )
                coverage.append(
                    {
                        "instrument_id": instrument_id,
                        "session_date": session.session_date.isoformat(),
                        "session_status": session.status,
                        "expected_minutes": len(expected),
                        "stored_minutes": len(expected) - len(missing),
                        "missing_minutes": [item.isoformat() for item in missing],
                        "coverage_percent": round(
                            100.0 * (len(expected) - len(missing)) / len(expected), 2
                        )
                        if expected
                        else 100.0,
                    }
                )

        counts = dict(sorted(Counter(item.code for item in issues).items()))
        blocking = {code: count for code, count in counts.items() if code in BLOCKING_CODES}
        quality_summary = {
            "acceptance_eligible": not blocking,
            "issue_counts": counts,
            "blocking_issue_counts": blocking,
            "coverage": coverage,
            "no_forward_fill": True,
        }
        source_jobs = tuple(sorted({item.source_job_id for item in rows}))
        checksum_payload = {
            "instruments": instruments,
            "session_start": start,
            "session_end": end,
            "rows": [
                {
                    "instrument_id": item.instrument_id,
                    "timestamp": item.candle.timestamp,
                    "open": item.candle.open,
                    "high": item.candle.high,
                    "low": item.candle.low,
                    "close": item.candle.close,
                    "volume": item.candle.volume,
                    "oi": item.candle.oi,
                }
                for item in rows
            ],
            "quality_summary": quality_summary,
        }
        revision = DatasetRevision(
            id=self.revision_id_factory(),
            created_at=self.clock(),
            instrument_ids=instruments,
            session_start=start,
            session_end=end,
            row_count=len(rows),
            quality_summary=quality_summary,
            source_job_ids=source_jobs,
            checksum=digest(checksum_payload),
        )
        self.repository.create_revision(revision, rows)
        report = {
            "schema_version": "dataset-quality-v1",
            "revision": revision.to_dict(),
            "quality": quality_summary,
        }
        return revision, report

    def decide(self, revision_id: str, decision: str, reason: str) -> DatasetRevision:
        normalized = decision.upper()
        revision = self.repository.get_revision(revision_id)
        if normalized == "ACCEPTED" and not revision.quality_summary["acceptance_eligible"]:
            raise ValueError("revision has blocking quality issues and cannot be accepted")
        return self.repository.decide_revision(revision_id, normalized, reason)


def report_json(report: dict[str, Any]) -> str:
    return canonical_json(report)
