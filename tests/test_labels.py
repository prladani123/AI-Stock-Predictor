from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json

import pytest

from banknifty_foundation.calendar import IST
from banknifty_foundation.config import Settings
from banknifty_foundation.datasets import DatasetValidationService
from banknifty_foundation.labels import LabelGenerationService, build_label_input
from banknifty_foundation.models import Candle, DatasetRevision, Instrument, RevisionCandle, TradingSession
from banknifty_foundation.repository import MemoryRepository


def _accepted(fixture_dir):
    repository = MemoryRepository()
    repository.instruments[1] = Instrument(
        260105, "NSE", "NIFTY BANK", "NIFTY BANK", "INDICES", "EQ",
        None, Decimal("0.05"), 1
    )
    day = date(2024, 7, 25)
    opened = datetime.combine(day, time(9, 15), IST).astimezone(timezone.utc)
    session = TradingSession(day, opened, opened + timedelta(minutes=21), "SPECIAL")
    repository.store_session(session)
    job = repository.get_or_create_job(1, opened, session.close_at - timedelta(minutes=1))
    prices = json.loads((fixture_dir / "label_prices.json").read_text(encoding="utf-8"))
    candles = [
        Candle(
            opened + timedelta(minutes=item["minute"]),
            Decimal(item["close"]),
            Decimal(item["close"]),
            Decimal(item["close"]),
            Decimal(item["close"]),
            100,
            200,
        )
        for item in prices
    ]
    repository.insert_candles(1, job.id, candles)
    service = DatasetValidationService(repository)
    candidate, _ = service.create_candidate([1], day, day)
    accepted = service.decide(candidate.id, "ACCEPTED", "test fixture")
    return repository, accepted, session, candles


def test_hand_calculated_labels_cover_all_four_horizons(fixture_dir):
    repository, revision, session, _ = _accepted(fixture_dir)
    settings = Settings(database_url="")
    labels, _ = LabelGenerationService(
        repository, settings.threshold_policy_version, settings.label_thresholds()
    ).generate(revision.id, 1)
    sample = {item.horizon_minutes: item for item in labels if item.as_of == session.open_at}
    assert sample[5].label == "UP"
    assert sample[5].realised_return == Decimal("0.002")
    assert sample[10].label == "DOWN"
    assert sample[10].realised_return == Decimal("-0.002")
    assert sample[15].label == "NO_TRADE"
    assert sample[15].realised_return == Decimal("0.001")
    assert sample[20].label == "UP"
    assert sample[20].realised_return == Decimal("0.004")


def test_label_input_cannot_be_affected_by_future_price():
    day = date(2024, 7, 25)
    opened = datetime.combine(day, time(9, 15), IST).astimezone(timezone.utc)
    session = TradingSession(day, opened, opened + timedelta(minutes=21), "SPECIAL")
    reference = Candle(opened, Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), 1, 1)
    first = build_label_input(1, reference, 5, session, "fixed-v1", Decimal("0.001"))
    changed_future = Candle(
        opened + timedelta(minutes=5), Decimal("999"), Decimal("999"), Decimal("999"),
        Decimal("999"), 1, 1
    )
    second = build_label_input(1, reference, 5, session, "fixed-v1", Decimal("0.001"))
    assert changed_future.close == Decimal("999")
    assert first == second


def test_labels_do_not_cross_market_close(fixture_dir):
    repository, revision, session, _ = _accepted(fixture_dir)
    settings = Settings(database_url="")
    labels, _ = LabelGenerationService(
        repository, settings.threshold_policy_version, settings.label_thresholds()
    ).generate(revision.id, 1)
    assert all(item.expiry_at < session.close_at for item in labels)
    assert not any(
        item.as_of == session.close_at - timedelta(minutes=5) and item.horizon_minutes == 5
        for item in labels
    )


def test_candidate_and_rejected_revisions_cannot_generate_final_labels(fixture_dir):
    repository, accepted, _, _ = _accepted(fixture_dir)
    settings = Settings(database_url="")
    generator = LabelGenerationService(
        repository, settings.threshold_policy_version, settings.label_thresholds()
    )
    candidate, _ = DatasetValidationService(repository).create_candidate(
        [1], accepted.session_start, accepted.session_end
    )
    with pytest.raises(ValueError, match="ACCEPTED"):
        generator.generate(candidate.id, 1)
    rejected = DatasetValidationService(repository).decide(candidate.id, "REJECTED", "reviewed")
    with pytest.raises(ValueError, match="ACCEPTED"):
        generator.generate(rejected.id, 1)


def test_missing_expiry_is_unknown_and_generation_is_idempotent(fixture_dir):
    repository, revision, session, candles = _accepted(fixture_dir)
    # Simulate a defensively handled corrupted/missing expiry in a read-only revision source.
    repository.revision_rows[revision.id] = tuple(
        RevisionCandle(1, item, next(iter(repository.jobs)))
        for item in candles
        if item.timestamp != session.open_at + timedelta(minutes=5)
    )
    settings = Settings(database_url="")
    generator = LabelGenerationService(
        repository, settings.threshold_policy_version, settings.label_thresholds()
    )
    first, inserted_first = generator.generate(revision.id, 1)
    second, inserted_second = generator.generate(revision.id, 1)
    missing = next(
        item for item in first if item.as_of == session.open_at and item.horizon_minutes == 5
    )
    assert missing.label == "UNKNOWN"
    assert missing.future_price is None
    assert inserted_first > 0
    assert inserted_second == 0
    assert len(first) == len(second)
