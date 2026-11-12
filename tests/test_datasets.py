from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from banknifty_foundation.calendar import IST, TradingCalendar
from banknifty_foundation.datasets import DatasetValidationService
from banknifty_foundation.models import Candle, TradingSession
from banknifty_foundation.repository import MemoryRepository


def _session(day: date, status: str = "OPEN", minutes: int = 3) -> TradingSession:
    opened = datetime.combine(day, time(9, 15), IST).astimezone(timezone.utc)
    return TradingSession(day, opened, opened + timedelta(minutes=minutes), status)


def _load(repository: MemoryRepository, instrument_id: int, session: TradingSession) -> str:
    job = repository.get_or_create_job(
        instrument_id, session.open_at, session.close_at - timedelta(minutes=1)
    )
    candles = [
        Candle(
            session.open_at + timedelta(minutes=minute),
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100"),
            10,
            20,
        )
        for minute in range(int((session.close_at - session.open_at).total_seconds() / 60))
    ]
    repository.insert_candles(instrument_id, job.id, candles)
    return job.id


def test_calendar_requires_explicit_open_closed_and_special_records():
    day = date(2024, 7, 24)
    calendar = TradingCalendar([_session(day), _session(day + timedelta(days=1), "SPECIAL")])
    assert len(calendar.sessions_between(day, day + timedelta(days=1))) == 2
    with pytest.raises(ValueError, match="Unknown trading calendar"):
        calendar.sessions_between(day, day + timedelta(days=2))


def test_unknown_calendar_is_quarantined_and_cannot_be_accepted():
    repository = MemoryRepository()
    revision, report = DatasetValidationService(repository).create_candidate(
        [1], date(2024, 7, 24), date(2024, 7, 24)
    )
    assert revision.status == "CANDIDATE"
    assert not report["quality"]["acceptance_eligible"]
    assert report["quality"]["issue_counts"]["UNKNOWN_TRADING_SESSION"] == 1
    with pytest.raises(ValueError, match="blocking quality"):
        DatasetValidationService(repository).decide(revision.id, "ACCEPTED", "not allowed")


def test_candidate_is_deterministic_complete_and_decided_revision_is_immutable():
    repository = MemoryRepository()
    day = date(2024, 7, 24)
    session = _session(day)
    repository.store_session(session)
    _load(repository, 1, session)
    service = DatasetValidationService(repository)
    first, report = service.create_candidate([1], day, day)
    second, _ = service.create_candidate([1], day, day)
    assert first.id != second.id
    assert first.checksum == second.checksum
    assert first.row_count == 3
    assert report["quality"]["coverage"][0]["coverage_percent"] == 100.0
    assert report["quality"]["no_forward_fill"] is True
    accepted = service.decide(first.id, "ACCEPTED", "quality reviewed")
    assert accepted.status == "ACCEPTED"
    with pytest.raises(ValueError, match="only a CANDIDATE"):
        service.decide(first.id, "REJECTED", "cannot revise history")
    with pytest.raises(FrozenInstanceError):
        accepted.status = "REJECTED"  # type: ignore[misc]


def test_outside_session_and_incomplete_coverage_are_blocking():
    repository = MemoryRepository()
    day = date(2024, 7, 24)
    session = _session(day)
    repository.store_session(session)
    job = repository.get_or_create_job(1, session.open_at, session.close_at)
    repository.insert_candles(
        1,
        job.id,
        [
            Candle(
                session.open_at - timedelta(minutes=1),
                Decimal("100"),
                Decimal("100"),
                Decimal("100"),
                Decimal("100"),
                1,
                1,
            )
        ],
    )
    _, report = DatasetValidationService(repository).create_candidate([1], day, day)
    codes = report["quality"]["blocking_issue_counts"]
    assert "OUTSIDE_TRADING_SESSION" in codes
    assert "MISSING_MINUTE" in codes
    assert "INCOMPLETE_SESSION_COVERAGE" in codes


def test_revision_content_checksum_does_not_depend_on_random_source_job_identity():
    day = date(2024, 7, 24)
    session = _session(day)
    first = MemoryRepository()
    second = MemoryRepository()
    first.store_session(session)
    second.store_session(session)
    # Consume one deterministic job identity only in the second repository.
    second.get_or_create_job(99, session.open_at, session.close_at)
    _load(first, 1, session)
    _load(second, 1, session)
    first_revision, _ = DatasetValidationService(first).create_candidate([1], day, day)
    second_revision, _ = DatasetValidationService(second).create_candidate([1], day, day)
    assert first_revision.source_job_ids != second_revision.source_job_ids
    assert first_revision.checksum == second_revision.checksum
