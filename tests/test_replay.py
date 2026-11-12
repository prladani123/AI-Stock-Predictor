from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from banknifty_foundation.cli import build_phase45_fixture_repository
from banknifty_foundation.features import FEATURE_SET_VERSION
from banknifty_foundation.models import RevisionCandle
from banknifty_foundation.replay import AsOfDataView, WalkForwardReplay
from banknifty_foundation.research_models import ReplayManifest


def _manifest(revision_id: str, instrument_id: int) -> ReplayManifest:
    return ReplayManifest(
        revision_id,
        instrument_id,
        FEATURE_SET_VERSION,
        date(2024, 7, 24),
        "EXPANDING",
        None,
        20,
        ("majority-v1", "persistence-v1"),
        5,
        Decimal("0.001"),
    )


def test_23_to_24_walk_forward_hides_test_day_and_is_deterministic(fixture_dir):
    repository, revision_id, instrument_id = build_phase45_fixture_repository(fixture_dir)
    manifest = _manifest(revision_id, instrument_id)
    run_id, first = WalkForwardReplay(repository).run(manifest)
    second_run_id, second = WalkForwardReplay(repository).run(manifest)
    assert run_id == second_run_id
    assert first == second
    assert "2024-07-23" in first["fold"]["training_dates"]
    assert "2024-07-24" not in first["fold"]["training_dates"]
    assert set(first["by_horizon"]) == {"5", "10", "15", "20"}
    assert {"majority", "persistence"} == set(first["baseline_comparison"])


def test_outcomes_wait_for_expiry_and_predictions_are_immutable(fixture_dir):
    repository, revision_id, instrument_id = build_phase45_fixture_repository(fixture_dir)
    run_id, _ = WalkForwardReplay(repository).run(_manifest(revision_id, instrument_id))
    predictions = repository.predictions_for_run(run_id)
    outcomes = {item.prediction_id: item for item in repository.outcomes_for_run(run_id)}
    assert outcomes
    for prediction_id, outcome in outcomes.items():
        prediction = next(item for item in predictions if item.id == prediction_id)
        assert outcome.attached_replay_clock >= prediction.expires_at
    original = predictions[0]
    with pytest.raises(ValueError, match="immutable"):
        repository.save_predictions([replace(original, predicted_class="ERROR")])


def test_distinctive_future_value_cannot_change_earlier_prediction(fixture_dir):
    repository, revision_id, instrument_id = build_phase45_fixture_repository(fixture_dir)
    manifest = _manifest(revision_id, instrument_id)
    run_id, before_report = WalkForwardReplay(repository).run(manifest)
    before = list(repository.predictions_for_run(run_id))
    rows = list(repository.revision_rows[revision_id])
    last = rows[-1]
    rows[-1] = RevisionCandle(
        last.instrument_id,
        replace(
            last.candle,
            open=Decimal("999"),
            high=Decimal("999"),
            low=Decimal("999"),
            close=Decimal("999"),
        ),
        last.source_job_id,
    )
    repository.revision_rows[revision_id] = tuple(rows)
    _, after_report = WalkForwardReplay(repository).run(manifest)
    after = repository.predictions_for_run(run_id)
    assert before[0] == after[0]
    assert before_report == after_report


def test_rolling_window_and_minimum_purge_are_enforced(fixture_dir):
    repository, revision_id, instrument_id = build_phase45_fixture_repository(fixture_dir)
    rolling = replace(_manifest(revision_id, instrument_id), training_mode="ROLLING", rolling_days=1)
    _, report = WalkForwardReplay(repository).run(rolling)
    assert report["fold"]["training_dates"] == ["2024-07-23"]
    with pytest.raises(ValueError, match="at least 20"):
        WalkForwardReplay(repository).run(replace(rolling, purge_minutes=19))


def test_as_of_view_hides_current_day_labels_and_rejects_future_queries(fixture_dir):
    repository, revision_id, instrument_id = build_phase45_fixture_repository(fixture_dir)
    features = repository.feature_rows(revision_id, instrument_id, FEATURE_SET_VERSION)
    labels = repository.target_labels(revision_id, instrument_id)
    test_features = [
        item for item in features if item.as_of.date() == date(2024, 7, 24)
    ]
    training = [
        item for item in labels if item.as_of.date() < date(2024, 7, 24)
    ]
    current = {
        (item.as_of, item.horizon_minutes): item
        for item in labels
        if item.as_of.date() == date(2024, 7, 24)
    }
    clock = test_features[0].as_of
    view = AsOfDataView(clock, test_features, training, current)
    assert all(item.as_of.date() < date(2024, 7, 24) for item in view.training_labels())
    with pytest.raises(ValueError, match="after the replay clock"):
        view.feature_at(clock + __import__("datetime").timedelta(minutes=1))
    with pytest.raises(ValueError, match="before expiry"):
        view.outcome(clock, 5)
