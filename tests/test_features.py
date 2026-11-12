from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from banknifty_foundation.calendar import IST
from banknifty_foundation.cli import build_phase45_fixture_repository
from banknifty_foundation.features import (
    FEATURE_NAMES,
    FEATURE_SCHEMA,
    FEATURE_SET_VERSION,
    FeatureGenerationService,
    feature_definition,
)
from banknifty_foundation.models import Candle, RevisionCandle


def test_future_candle_cannot_change_earlier_feature_and_generation_is_idempotent(fixture_dir):
    repository, revision_id, instrument_id = build_phase45_fixture_repository(fixture_dir)
    rows = repository.feature_rows(revision_id, instrument_id, FEATURE_SET_VERSION)
    early_time = datetime.combine(date(2024, 7, 24), time(9, 40), IST).astimezone(timezone.utc)
    before = next(item for item in rows if item.as_of == early_time)
    future_time = early_time + timedelta(minutes=20)
    changed = []
    for item in repository.revision_rows[revision_id]:
        if item.candle.timestamp == future_time:
            future = replace(
                item.candle,
                open=Decimal("999"),
                high=Decimal("999"),
                low=Decimal("999"),
                close=Decimal("999"),
            )
            changed.append(RevisionCandle(item.instrument_id, future, item.source_job_id))
        else:
            changed.append(item)
    repository.revision_rows[revision_id] = tuple(changed)
    regenerated, inserted = FeatureGenerationService(repository).generate(
        revision_id, instrument_id
    )
    after = next(item for item in regenerated if item.as_of == early_time)
    assert before == after
    assert inserted == 0


def test_feature_watermarks_and_incomplete_lookback(fixture_dir):
    repository, revision_id, instrument_id = build_phase45_fixture_repository(fixture_dir)
    rows = repository.feature_rows(revision_id, instrument_id, FEATURE_SET_VERSION)
    assert all(item.source_max_timestamp <= item.as_of for item in rows)
    first = rows[0]
    assert first.quality_status == "PARTIAL_LOOKBACK"
    assert first.values["return_20m"] is None
    complete = next(
        item for item in rows
        if item.as_of.astimezone(IST).date() == date(2024, 7, 24)
        and item.values["return_20m"] is not None
    )
    assert complete.quality_status == "AVAILABLE"


def test_feature_definition_records_order_sources_lookbacks_and_null_policy():
    definition = feature_definition()
    assert tuple(definition["feature_order"]) == FEATURE_NAMES
    assert tuple(item["name"] for item in FEATURE_SCHEMA) == FEATURE_NAMES
    assert all(item["source_columns"] for item in FEATURE_SCHEMA)
    assert all("lookback_minutes" in item for item in FEATURE_SCHEMA)
    assert all(item["availability_rule"] for item in FEATURE_SCHEMA)
    assert all(item["null_policy"] for item in FEATURE_SCHEMA)
