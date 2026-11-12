from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from banknifty_foundation.calibration import (
    AbstentionPolicy,
    apply_abstention_policy,
    calibration_metrics,
    fit_calibrator,
    matching_reliability_bucket,
    top_confidence_reliability_buckets,
)
from banknifty_foundation.ml_records import MLPrediction, MLPredictionOutcome
from banknifty_foundation.repository import MemoryRepository


def _data(samples: int = 180):
    actual = ["DOWN", "NO_TRADE", "UP"] * (samples // 3)
    raw = []
    for index, label in enumerate(actual):
        base = np.asarray([0.15, 0.15, 0.15])
        base[("DOWN", "NO_TRADE", "UP").index(label)] = 0.50 + (index % 12) / 100
        base[(index + 1) % 3] += (index % 8) / 100
        raw.append(base / base.sum())
    return np.asarray(raw), actual


def _fit_selection():
    raw, actual = _data()
    fit_time = [
        datetime(2024, 7, 19, tzinfo=timezone.utc) + timedelta(minutes=index)
        for index in range(90)
    ]
    selection_time = [
        datetime(2024, 7, 22, tzinfo=timezone.utc) + timedelta(minutes=index)
        for index in range(90)
    ]
    return raw[:90], actual[:90], raw[90:], actual[90:], fit_time, selection_time


def test_calibration_metrics_and_top_bucket_evidence_are_complete():
    raw, actual = _data()
    metrics = calibration_metrics(actual, raw)
    assert {
        "multiclass_brier", "log_loss", "ece", "mce", "per_class",
        "class_reliability_buckets",
    } <= set(metrics)
    buckets = top_confidence_reliability_buckets(actual, raw)
    assert len(buckets) == 10
    assert sum(item["count"] for item in buckets) == len(actual)
    assert all("observed_accuracy" in item for item in buckets)
    assert all("observed_directional_precision" in item for item in buckets)


def test_calibrator_fit_and_selection_are_strictly_separate_and_deterministic():
    fit_x, fit_y, select_x, select_y, fit_t, select_t = _fit_selection()
    first_calibrator, first = fit_calibrator(
        fit_x, fit_y, select_x, select_y,
        fit_timestamps=fit_t, selection_timestamps=select_t,
        model_checksum="model", horizon_minutes=5,
    )
    second_calibrator, second = fit_calibrator(
        fit_x, fit_y, select_x, select_y,
        fit_timestamps=fit_t, selection_timestamps=select_t,
        model_checksum="model", horizon_minutes=5,
    )
    assert first.checksum == second.checksum
    assert first.fit_range["to"] < first.selection_range["from"]
    assert first.fit_count == first.selection_count == 90
    assert first.raw_selection_metrics
    assert first.calibrated_selection_metrics
    if first_calibrator:
        assert np.allclose(first_calibrator.transform(select_x).sum(axis=1), 1)


def test_calibration_rejects_nonchronological_and_insufficient_intervals():
    fit_x, fit_y, select_x, select_y, fit_t, select_t = _fit_selection()
    with pytest.raises(ValueError, match="strictly earlier"):
        fit_calibrator(
            fit_x, fit_y, select_x, select_y,
            fit_timestamps=select_t, selection_timestamps=fit_t,
            model_checksum="model", horizon_minutes=10,
        )
    calibrator, result = fit_calibrator(
        fit_x[:6], fit_y[:6], select_x[:6], select_y[:6],
        fit_timestamps=fit_t[:6], selection_timestamps=select_t[:6],
        model_checksum="model", horizon_minutes=10,
    )
    assert calibrator is None
    assert "INSUFFICIENT_CALIBRATION_FIT_SAMPLES" in result.rejection_reasons
    assert "INSUFFICIENT_CALIBRATION_SELECTION_SAMPLES" in result.rejection_reasons


def _buckets(target_count: int = 20, precision: float = 0.8):
    result = []
    for index in range(10):
        result.append(
            {
                "bucket_index": index,
                "lower": index / 10,
                "upper": (index + 1) / 10,
                "count": target_count if index == 8 else 100,
                "mean_confidence": 0.85 if index == 8 else 0.5,
                "observed_accuracy": precision,
                "directional_count": target_count if index == 8 else 100,
                "observed_directional_precision": precision,
            }
        )
    return result


def test_policy_uses_exact_matching_bucket_not_total_calibration_count():
    sparse = _buckets(target_count=1)
    decision = apply_abstention_policy(
        raw_probabilities=[0.05, 0.10, 0.85],
        calibrated_probabilities=[0.05, 0.10, 0.85],
        reliability_buckets=sparse,
        policy=AbstentionPolicy(minimum_reliability_bucket_samples=5),
    )
    assert decision["final_class"] == "NO_TRADE"
    assert decision["reliability_bucket"]["bucket_index"] == 8
    assert decision["reliability_bucket"]["count"] == 1
    assert "INSUFFICIENT_CALIBRATION_SAMPLES" in decision["abstention_reasons"]
    assert sum(item["count"] for item in sparse) > 5


def test_policy_preserves_raw_calibrated_final_and_system_abstention_separately():
    accepted = apply_abstention_policy(
        raw_probabilities=[0.1, 0.2, 0.7],
        calibrated_probabilities=[0.05, 0.10, 0.85],
        reliability_buckets=_buckets(),
        policy=AbstentionPolicy(),
    )
    assert accepted["raw_class"] == accepted["calibrated_class"] == accepted["final_class"] == "UP"
    assert accepted["system_abstained"] is False
    assert accepted["abstention_reasons"] == ()


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"model_ready": False}, "MODEL_NOT_READY"),
        ({"calibrator_ready": False, "calibrated_probabilities": None}, "CALIBRATOR_NOT_READY"),
        ({"partial_lookback": True}, "PARTIAL_LOOKBACK"),
        ({"data_quality_failure": True}, "DATA_QUALITY_FAILURE"),
        ({"schema_matches": False}, "FEATURE_SCHEMA_MISMATCH"),
        ({"artifact_checksum_valid": False}, "ARTEFACT_CHECKSUM_FAILURE"),
        ({"values_in_range": False}, "UNSEEN_FEATURE_RANGE"),
        ({"session_known": False}, "UNKNOWN_SESSION"),
        ({"unresolved_gap": True}, "UNRESOLVED_GAP"),
    ],
)
def test_policy_required_reason_codes(kwargs, reason):
    parameters = {
        "raw_probabilities": [0.05, 0.10, 0.85],
        "calibrated_probabilities": [0.05, 0.10, 0.85],
        "reliability_buckets": _buckets(),
        "policy": AbstentionPolicy(),
        **kwargs,
    }
    assert reason in apply_abstention_policy(**parameters)["abstention_reasons"]


def _prediction() -> MLPrediction:
    as_of = datetime(2024, 7, 24, 4, 0, tzinfo=timezone.utc)
    return MLPrediction(
        "prediction-1", "revision", "features-v1", "model", "calibration",
        "policy", "training", "fold-1", 1, 5, as_of,
        as_of + timedelta(minutes=5),
        {"DOWN": 0.1, "NO_TRADE": 0.2, "UP": 0.7},
        {"DOWN": 0.1, "NO_TRADE": 0.2, "UP": 0.7},
        "UP", "UP", "UP", False, (), _buckets()[8], as_of,
    )


def test_predictions_are_idempotent_immutable_and_outcomes_append_after_expiry():
    repository = MemoryRepository()
    prediction = _prediction()
    assert repository.save_ml_predictions([prediction]) == 1
    assert repository.save_ml_predictions([prediction]) == 0
    with pytest.raises(ValueError, match="semantic identity"):
        repository.save_ml_predictions([
            MLPrediction(**{**prediction.__dict__, "id": "different-id"})
        ])
    with pytest.raises(ValueError, match="immutable"):
        repository.save_ml_predictions([
            MLPrediction(**{**prediction.__dict__, "final_class": "DOWN"})
        ])
    with pytest.raises(ValueError, match="before expiry"):
        repository.append_ml_prediction_outcome(
            MLPredictionOutcome(prediction.id, "UP", "VALID", prediction.as_of)
        )
    outcome = MLPredictionOutcome(
        prediction.id, "UP", "VALID", prediction.expires_at
    )
    assert repository.append_ml_prediction_outcome(outcome)
    assert not repository.append_ml_prediction_outcome(outcome)
