from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MLPrediction:
    id: str
    dataset_revision_id: str
    feature_set_version: str
    model_checksum: str
    calibration_checksum: str | None
    policy_checksum: str
    training_manifest_checksum: str
    fold_id: str
    instrument_id: int
    horizon_minutes: int
    as_of: datetime
    expires_at: datetime
    raw_probabilities: dict[str, float]
    calibrated_probabilities: dict[str, float] | None
    raw_class: str
    calibrated_class: str | None
    final_class: str
    system_abstained: bool
    abstention_reasons: tuple[str, ...]
    reliability_bucket: dict[str, Any] | None
    created_replay_clock: datetime


@dataclass(frozen=True)
class MLPredictionOutcome:
    prediction_id: str
    actual_class: str
    quality_status: str
    attached_replay_clock: datetime


def validate_prediction(record: MLPrediction) -> None:
    if record.horizon_minutes not in {5, 10, 15, 20}:
        raise ValueError("unsupported prediction horizon")
    if record.created_replay_clock < record.as_of:
        raise ValueError("prediction cannot exist before its as-of timestamp")
    if record.expires_at <= record.as_of:
        raise ValueError("prediction expiry must follow as-of")
    if (
        record.raw_class not in {"UP", "DOWN", "NO_TRADE"}
        or record.calibrated_class not in {"UP", "DOWN", "NO_TRADE", None}
        or record.final_class not in {"UP", "DOWN", "NO_TRADE"}
    ):
        raise ValueError("invalid prediction class")
