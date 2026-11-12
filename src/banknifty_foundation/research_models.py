from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class FeatureSetDefinition:
    version: str
    created_at: datetime
    definition: dict[str, Any]
    checksum: str


@dataclass(frozen=True)
class FeatureRow:
    dataset_revision_id: str
    instrument_id: int
    as_of: datetime
    feature_set_version: str
    source_max_timestamp: datetime
    values: dict[str, Any]
    quality_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_revision_id": self.dataset_revision_id,
            "instrument_id": self.instrument_id,
            "as_of": self.as_of.isoformat(),
            "feature_set_version": self.feature_set_version,
            "source_max_timestamp": self.source_max_timestamp.isoformat(),
            "feature_values": {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in deepcopy(self.values).items()
            },
            "quality_status": self.quality_status,
        }


@dataclass(frozen=True)
class BaselineDefinition:
    version: str
    kind: str
    horizon_minutes: int
    config: dict[str, Any]
    checksum: str


@dataclass(frozen=True)
class ReplayManifest:
    dataset_revision_id: str
    instrument_id: int
    feature_set_version: str
    test_date: date
    training_mode: str
    rolling_days: int | None
    purge_minutes: int
    baseline_versions: tuple[str, ...]
    persistence_return_minutes: int
    persistence_threshold: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_revision_id": self.dataset_revision_id,
            "instrument_id": self.instrument_id,
            "feature_set_version": self.feature_set_version,
            "test_date": self.test_date.isoformat(),
            "training_mode": self.training_mode,
            "rolling_days": self.rolling_days,
            "purge_minutes": self.purge_minutes,
            "baseline_versions": list(self.baseline_versions),
            "persistence_return_minutes": self.persistence_return_minutes,
            "persistence_threshold": str(self.persistence_threshold),
        }


@dataclass(frozen=True)
class ResearchPrediction:
    id: str
    run_id: str
    fold_id: str
    baseline_version: str
    feature_set_version: str
    dataset_revision_id: str
    instrument_id: int
    horizon_minutes: int
    as_of: datetime
    expires_at: datetime
    predicted_class: str
    confidence: Decimal | None
    prediction_reason: str
    created_replay_clock: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "prediction_id": self.id,
            "run_id": self.run_id,
            "fold_id": self.fold_id,
            "baseline_version": self.baseline_version,
            "feature_set_version": self.feature_set_version,
            "dataset_revision": self.dataset_revision_id,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon_minutes,
            "as_of": self.as_of.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "predicted_class": self.predicted_class,
            "confidence": str(self.confidence) if self.confidence is not None else None,
            "prediction_reason": self.prediction_reason,
            "created_replay_clock": self.created_replay_clock.isoformat(),
        }


@dataclass(frozen=True)
class PredictionOutcome:
    prediction_id: str
    attached_replay_clock: datetime
    actual_class: str
    quality_status: str
